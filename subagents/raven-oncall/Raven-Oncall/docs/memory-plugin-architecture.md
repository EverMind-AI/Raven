# Memory Plugin Architecture & EverOS as a Bundled Backend

Status legend: **[DONE]** implemented on `feature/integrate-everos` · **[PLAN]** proposed.

This document consolidates the memory subsystem design: the refactored
`MemoryBackend` contract, the plugin discovery model, EverOS shipped as
a built-in (bundled) plugin, how third-party backends integrate, and
the EverOS version-pinning / upgrade procedure.

---

## 1. Goals

1. EverOS works out of the box with no extra install step.
2. Adding a memory backend (first- or third-party) requires **no host
   code change** — drop a manifest + factory, or `uv add` a package.
3. Heavy backend dependencies never slow or break startup: discovery
   reads manifests only and never imports backend code until selected.
4. Behaviour is typed and predictable, with graceful degradation.

Key judgement: the plugin framework under `raven/plugin/` already
provided the machinery (four-source discovery + manifest contract +
typed Protocol). The work was to (a) wire the dormant directory sources
into the live boot and (b) move EverOS in-tree under
`raven/plugin/memory/everos/` — no new mechanism.

---

## 2. The `MemoryBackend` contract **[DONE]**

`raven/memory_engine/backend.py` defines the single Protocol every
memory plugin implements. The recall surface was refactored from a
single prefixed opaque `owner_id` to explicit XOR track ids:

```python
class MemoryBackend(Protocol):
    async def recall(self, query: str, *,
                     user_id: str | None = None,
                     agent_id: str | None = None,
                     top_k: int) -> list[Memory]: ...
    async def store(self, session_id: str, messages: list[dict]) -> None: ...
    async def feedback(self, signals: dict) -> None: ...   # may be no-op
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

Contract rules:

- `recall` takes **exactly one** of `user_id` / `agent_id` (XOR). The
  caller knows the track statically: the `# Memory` segment always
  passes `user_id`; `EverosSkillSource` always passes `agent_id`.
  Neither/both set → return `[]`.
- Ids are **bare, backend-native strings** — no `user:` / `agent:`
  prefix parsing. Dual-track backends (EverOS) route the set field to
  the matching store; flat backends (mem0, MemOS) use `user_id` and
  return `[]` for the `agent_id` call.
- Rationale for naming the track explicitly: the prefix convention
  smuggled the host's dual-track concept through an intentionally
  generic field. Each caller already knows its track at construction,
  so the prefix was redundant indirection.

### Identity consistency

`recall` is the read side; `store` is the write side. They must agree
on identity for stored memory to be retrievable:

| Host (read) | == | Plugin (write) |
|---|:--:|---|
| `memory.userId` | == | `plugins.config[<id>].user_id` |
| `memory.agentId` | == | `plugins.config[<id>].agent_id` |

Because ids are now bare strings, both sides hold the **identical
literal** (e.g. `"user-raven"`) — no prefix stripping to reconcile.

---

## 3. Plugin discovery model **[DONE]**

`raven/plugin/discover.py` scans four sources and deduplicates by
plugin id. Discovery **reads manifests only — it never imports backend
code.** `build_plugin_registry` (`raven/cli/_plugin_stack.py`) wires
all four via the shared `plugin_discovery_sources()` helper, which the
`raven plugins` CLI command reuses so both see the same set.

### Sources & priority

`Source` doubles as conflict priority (higher wins):

| Priority | Source | Location | Audience |
|---:|---|---|---|
| 4 | `BUNDLED` | `raven/plugin/memory/<id>/` | first-party, ships with raven |
| 3 | `USER` | `~/.raven/plugins/<id>/` | local drop-in |
| 2 | `PROJECT` | `./.raven/plugins/<id>/` | per-project |
| 1 | `ENTRY_POINTS` | pip pkg, group `raven.plugins` | third-party distribution |

`bundled > user > project > entry_points` enforces the "builtin shadow
rule": a bundled backend can never be silently shadowed by a same-id
local or pip copy. Different ids never conflict — they coexist as
available contributions; the active one is chosen by `memory.backend`.

### Manifest

Each plugin dir/package ships `raven-plugin.toml`:

```toml
[plugin]
id           = "everos-memory"
version      = "1.0.0"
bundled      = true

[[plugin.contributes.memory_backends]]
name    = "everos"
factory = "raven.plugin.memory.everos.backend:make_backend"
```

### Why manifest-only discovery matters

Discovery parses TOML; it does **not** import the backend. A backend
with a missing heavy dependency (lancedb, mem0ai) therefore cannot
break discovery for every other backend. The factory module is imported
only when `memory.backend` selects that backend — so "ships by default"
never means "pays the import/startup cost by default".

> Caveat: `_scan_entry_points` resolves a package's manifest via
> `importlib.resources.files(pkg)`, which executes that package's
> `__init__.py`. Plugin packages (and the bundled `everos/__init__.py`)
> must stay empty/cheap and must not import their heavy substrate there.

---

## 4. EverOS as a bundled plugin **[DONE]**

The EverOS adapter lives in-tree at `raven/plugin/memory/everos/`,
discovered via the bundled source — no separate package to install.

### 4.1 Layout

```
raven/
  plugin/                         # plugin framework + bundled implementations
    discover.py registry.py …     # framework (unchanged)
    memory/
      __init__.py                 # cheap
      everos/
        __init__.py               # cheap (touched by resource resolution; no heavy import)
        backend.py                # EverosBackend + make_backend
        tools.py                  # understand_media tool
        multimodal.py
        raven-plugin.toml      # manifest (ships as package-data)
```

The bundled memory implementations live **under the existing
`raven/plugin/` package** (not a separate top-level `plugins/`), so
there is a single, unambiguous home for both the framework and the
backends it loads.

### 4.2 What moved, what stays

| Item | Disposition | Why |
|---|---|---|
| `raven_everos` adapter code | **moved** into `raven/plugin/memory/everos/` | thin adapter; no heavy logic enters the host |
| `everos[multimodal]` substrate | **stays** a direct raven dependency | heavy deps stay isolated in the pip package; adapter only delegates |
| `httpx` | stays (already a dep) | http-mode client reuses it |
| `raven_everos` package / uv workspace member / its entry_points | **removed** | code is now built-in; no separate distribution |

Manifest factory path changed from `raven_everos.backend:make_backend`
to `raven.plugin.memory.everos.backend:make_backend`.

### 4.3 Dependency & packaging changes (`pyproject.toml`)

Removed: `raven-everos>=1.0.0,<2.0.0`; `[tool.uv.workspace]`;
`[tool.uv.sources] raven-everos`.

Added to the hatchling wheel include allowlist so bundled manifests
ship in the wheel (hatchling `include` is an explicit allowlist; the
`.toml` is not matched by `raven/**/*.py`):

```toml
[tool.hatch.build]
include = [
    "raven/**/*.py",
    "raven/plugin/**/raven-plugin.toml",
    ...
]
```

Kept: `everos[multimodal]==1.0.0`, `httpx` — the relocated adapter
reuses them, so **no new runtime dependency** was introduced.

### 4.4 Host wiring

`plugin_discovery_sources()` resolves the four source locations; both
`build_plugin_registry` and the `raven plugins` CLI use it:

```python
import raven
return {
    "bundled_dir": Path(raven.__path__[0]) / "plugin" / "memory",
    "user_dir": Path.home() / ".raven" / "plugins",
    "project_dir": Path.cwd() / ".raven" / "plugins",
    "entry_points_group": "raven.plugins",
}
```

Wheel resource path: a normal pip/uv install extracts raven to real
directories, so `raven.__path__[0]` is a usable `Path`. Only
zipimport (running from a `.pyz`) would need
`importlib.resources.as_file` materialization.

### 4.5 User-facing impact

None. Selection stays `memory.backend = "everos"`; config stays under
`plugins.config["everos-memory"]`. EverOS moving from "external plugin"
to "built-in plugin" is transparent.

---

## 5. Adding a new backend (drop-in contract)

A backend is integrated through any of three channels; the contract is
identical: ship `raven-plugin.toml` declaring a `memory_backends`
contribution (name + factory) + a factory returning a `MemoryBackend`.

| Channel | Where | "Install" | Dependencies |
|---|---|---|---|
| pip + entry_points (recommended) | standalone package | `uv add <pkg>` | package declares its own (e.g. `mem0ai`) |
| user drop-in | `~/.raven/plugins/<id>/` | copy the dir | user must provide deps |
| project drop-in | `./.raven/plugins/<id>/` | checked into project | user must provide deps |

### Example: a mem0 backend via pip + entry_points

```
raven-mem0/
  pyproject.toml          # [project.entry-points."raven.plugins"] mem0 = "raven_mem0"
  src/raven_mem0/
    __init__.py           # empty/cheap
    backend.py            # make_backend + Mem0Backend(MemoryBackend)
    raven-plugin.toml  # id="mem0-memory", contributes memory_backends name="mem0"
```

```python
class Mem0Backend:  # structurally a MemoryBackend
    async def recall(self, query, *, user_id=None, agent_id=None, top_k):
        if user_id is None:        # flat backend: no agent track
            return []
        hits = self._m.search(query, user_id=user_id, limit=top_k)
        return [Memory(text=h["memory"], score=h.get("score", 0.0),
                       metadata={"id": h.get("id")}) for h in hits["results"]]
    # store / feedback / start / stop ...

def make_backend(ctx) -> MemoryBackend:
    return Mem0Backend(ctx)
```

Install + activate:

```bash
uv add raven-mem0          # mem0ai pulled transitively; entry_points auto-discovered
```
```json
"memory":  { "backend": "mem0", "userId": "user-raven", "memoryTopK": 5 },
"plugins": { "config": { "mem0-memory": { "mem0_config": { "...": "..." } } } }
```

Multiple backends coexist; `memory.backend` picks one; `plugins.disabled`
turns one off. An installed-but-unselected backend's code is never
imported.

---

## 6. Lifecycle & failure semantics

- **Discovery**: cheap, manifest-only.
- **Construction**: lazy — `make_backend(ctx)` runs only for the
  selected backend. `ctx` carries `config` (the `plugins.config` slice),
  `services`, `logger`.
- **Lifecycle**: host awaits `start()` once at boot, `stop()` at
  shutdown.
- **Substrate missing / import fails**: the EverOS adapter degrades to a
  no-op adapter and logs once; the host still boots.
- **Factory raises**: `maybe_build_memory_backend` catches and falls
  back to no backend (core `MemoryStore` still works).
- **Drop-in dep missing**: that backend's construction fails and is
  skipped; other backends are unaffected.

---

## 7. EverOS version pinning & upgrade SOP

### 7.1 Exact pin is mandatory **[DONE: `everos[multimodal]==1.0.0`]**

The adapter is written against EverOS **internal** APIs, not a stable
public surface:

- `everos.service.search.search`, `everos.service.memorize.memorize`
- `everos.memory.search.dto.SearchRequest`
- `everos.entrypoints.api.app.create_app` (embedded lifespan)
- `everos.memory.extract.parser`, `everos.component.llm.client`
- `everos.config.load_settings`; and for drain in tests/scripts:
  `everos.service.memorize._get_engine`,
  `everos.infra.persistence.sqlite.md_change_state_repo`

Any release — even a patch — can move these symbols. Therefore EverOS
is pinned to an exact version (`==X.Y.Z`), not a range: upgrades are
deliberate, re-validated events, never something `uv lock --upgrade`
can do silently.

Single pin: with `raven_everos` removed, EverOS is pinned in **one**
place (raven's `pyproject.toml`). The upgrade surface is one line.

### 7.2 Upgrade procedure

0. **Assess**: read the EverOS changelog; check whether the internal
   symbols above moved, and whether the on-disk schema
   (`~/.everos/.index/` sqlite + lancedb) changed.
1. **Bump the pin (uv only — never hand-edit pyproject/lock)**:
   ```bash
   uv add 'everos[multimodal]==1.2.0' && uv sync
   ```
   Always keep the `[multimodal]` extra.
2. **Adapt the adapter** if symbols/signatures changed — only
   `raven/plugin/memory/everos/`. Re-check version assumptions
   written in adapter comments.
3. **Test (all three layers)**:
   ```bash
   uv run pytest tests/test_em1_skeleton.py tests/test_em2_backend.py \
     tests/test_em3_http.py tests/test_memory_backend_protocol.py \
     tests/test_memory_backend_contract.py -q          # unit (mock adapter)
   uv run pytest tests/integration/test_everos_backend_e2e.py -m real_llm  # real
   python scripts/everos_memory_roundtrip.py            # native shell smoke
   ```
4. **Data migration (major bumps only)**: if the schema changed, real
   `~/.everos/.index/` may need rebuild/migration per the changelog.
   Tests use per-test tmp roots and are unaffected.
5. **Finalize**: bump the manifest `version`; commit `pyproject.toml` +
   `uv.lock` + adapter changes. Rollback = `git revert` (plus data
   rollback if the schema changed).

---

## 8. Validation (this change)

| Layer | Result |
|---|---|
| Unit (em1/em2/em3, protocol, contract, plugin discovery/command/tools, cl1, context, config, ag1/fb1, agent-loop pipeline) | 240 passed |
| `raven plugins` | everos-memory · Source=`bundled` · Status=`activated` |
| `real_llm` e2e (`test_everos_backend_e2e.py`) | 2 passed, 1 xfailed (best-effort skill-cluster check) — store→extract→recall + dual-track isolation |
| roundtrip script (new import path) | OK; `users/user-raven/user.md` generated |

Known unrelated: the full unit suite cannot run to completion in-process
because `agent -m` calls an unconditional `os._exit(0)`
(`agent_commands.py`, commit 33b4b0d9, to dodge a torch teardown
segfault), which kills the in-process pytest CliRunner. Pre-existing,
independent of this refactor.

---

## 9. Design decisions & trade-offs

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Recall track | explicit `user_id` XOR `agent_id` | prefixed opaque `owner_id` | caller knows track statically; prefix was redundant indirection |
| EverOS code home | bundled in-tree (`raven/plugin/memory/everos/`) | external `raven_everos` package | default-available, single upgrade surface; substrate stays an isolated pip dep |
| Bundled dir location | under existing `raven/plugin/` | separate top-level `raven/plugins/` | one home for framework + backends; avoids singular/plural ambiguity |
| Discovery | manifest-only (TOML) | import-on-scan (Hermes-style) | missing deps can't break discovery; no startup cost for unselected backends |
| EverOS pin | exact `==X.Y.Z` | range `>=,<` | adapter binds internal (non-public) APIs; upgrades must be deliberate |
| Conflict priority | `bundled > user > project > entry_points` | user-overrides-bundled | builtin can't be silently shadowed |
