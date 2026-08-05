# SkillForge Configuration Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/raven-skills` web page that configures Raven's SkillForge (`skillForge` block of `~/.raven/config.json`) through the existing gateway-proxied config data plane, plus a read-only view of resolved skills.

**Architecture:** Four layers, identical to the shipped `channels`/`cron` pages — `raven/config/update_skills.py` (validate + atomic whitelist-patch) -> `raven/web_rpc/methods_config.py` (`raven.skills.{get,set,list}` RPC) -> `ui-webui/service/raven_config_routes.py` (REST proxy over the gateway WS) -> `ui-webui/frontend/src/pages/raven-skills/` (React page). Writes are `restart_required` (SkillForge is read once at `AgentLoop` construction; no hot-apply hook).

**Tech Stack:** Python 3.13 / pydantic v2 / pytest (raven core, run via `uv`); React 19 + Vite + Tailwind v4 + shadcn (ui-webui frontend, `pnpm`); FastAPI (ui-webui service).

**Spec:** `ui-webui/docs/specs/2026-07-27-skillforge-config-page-design.md`

## Global Constraints

- Raven core tests run through `uv` only: `uv run pytest ...`, never bare `pytest` (AGENTS.md 4, 5.4). If `uv` is not on PATH, `./.venv/bin/python -m pytest ...` is the fallback.
- `ui-webui/` is a pnpm workspace; uv/pytest rules do not apply there.
- **Do not create new test files** beyond `tests/test_update_skills.py`. RPC tests extend the existing `tests/test_web_rpc_config.py` (AGENTS.md 5.4).
- Python lint gate for every task touching `.py`: `uv run ruff format --check <files>` and `uv run ruff check <files>` must both be clean.
- Config wire format is **camelCase** (`Base` uses `to_camel`, `populate_by_name=True`); reads return camelCase, writes accept either spelling.
- **Whitelist only** these `skillForge` keys (D2). Every other key in the block is preserved verbatim on write — never emit `SkillForgeConfig.model_dump()` as the write payload (it would reset advanced fields to defaults): `enabled`, `router.weights.{local,everos,hub}`, `router.hub.{endpoint,apiKey,minSafety,timeoutS}`, `everos.enabled`, `localDirs[]` (each `{path, enabled, name?, alwaysEnabled}`).
- `set` is **restart_required** (D1): it writes and returns `restart_required: true`; it does NOT hot-apply.
- Frontend Prettier: tabs, width 4, single quotes, semicolons, print width 100. Do not reformat untouched lines.
- i18n edits are **targeted text edits** to `src/i18n/locales/{en,zh}.json`. Never rewrite the file with a full `json.dump`.
- Source comments English-only, only where non-obvious (AGENTS.md 1).
- Commit messages: Conventional Commits, all-ASCII, header <= 100 chars, `Co-authored-by: Claude (claude-opus-4-8) <noreply@anthropic.com>` trailer (AGENTS.md 3).
- **Do not run `git commit` until the user explicitly asks** (AGENTS.md 3.4). The commit steps below are written out ready, but stay unchecked until the user says so.

## File Structure

- Create `raven/config/update_skills.py` — read/validate/whitelist-write of the `skillForge` block; read-only skill registry list. Mirrors `raven/config/update_subagents.py`.
- Create `tests/test_update_skills.py` — unit tests for the above.
- Modify `raven/web_rpc/methods_config.py` — register `raven.skills.{get,set,list}`.
- Modify `tests/test_web_rpc_config.py` — cover the three new RPC methods.
- Modify `ui-webui/service/raven_config_routes.py` — `GET/PUT /raven/skills`, `GET /raven/skills/available`.
- Modify `ui-webui/frontend/src/api/ravenConfig.ts` — types + `ravenConfigApi.skills.*`.
- Create `ui-webui/frontend/src/pages/raven-skills/index.tsx` — the page.
- Modify `ui-webui/frontend/src/App.tsx` — route `{ path: '/raven-skills', element: <RavenSkillsPage /> }`.
- Modify `ui-webui/frontend/src/components/layout/AppSidebar.tsx` — nav entry.
- Modify `ui-webui/frontend/src/i18n/locales/{en,zh}.json` — page strings.

---

### Task 1: `raven/config/update_skills.py` + tests

**Files:**
- Create: `raven/config/update_skills.py`
- Test: `tests/test_update_skills.py`

**Interfaces:**
- Consumes: `raven.config.loader.get_config_path`, `read_raw_or_raise`, `load_config`; `raven.config.raven.SkillForgeConfig`; `raven.memory_engine.skill_forge.LocalSkillCatalog`.
- Produces:
  - `get_skillforge(*, config_path: Path | None = None) -> dict` — `{enabled, router:{weights:{local,everos,hub}, hub:{endpoint,apiKey,minSafety,timeoutS}}, everos:{enabled}, localDirs:[...]}` (camelCase).
  - `set_skillforge(fields: dict, *, config_path: Path | None = None) -> None` — validate + whitelist-patch + atomic write. Raises `ValueError` on bad weight/url/path; `ValidationError` if the merged block is schema-illegal. Writes nothing on failure.
  - `list_skills() -> list[dict]` — `[{name, source, description}]` from the SkillForge registry.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_update_skills.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config.update_skills import get_skillforge, set_skillforge


def _write(tmp: Path, cfg: dict) -> Path:
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_get_returns_whitelist_camelcase(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {"enabled": True, "router": {"weights": {"local": 1.0, "everos": 0.9, "hub": 0.85}}}})
    out = get_skillforge(config_path=p)
    assert out["enabled"] is True
    assert out["router"]["weights"] == {"local": 1.0, "everos": 0.9, "hub": 0.85}
    assert "hub" in out["router"] and "localDirs" in out


def test_set_patches_only_whitelist_and_preserves_other_keys(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {"enabled": True, "embeddingModel": "keepme", "router": {"topK": 7}}})
    set_skillforge({"enabled": False, "router": {"weights": {"hub": 0.5}}}, config_path=p)
    raw = json.loads(p.read_text(encoding="utf-8"))["skillForge"]
    assert raw["enabled"] is False
    assert raw["embeddingModel"] == "keepme"       # advanced key preserved
    assert raw["router"]["topK"] == 7               # advanced router key preserved
    assert raw["router"]["weights"]["hub"] == 0.5


def test_set_rejects_out_of_range_weight(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {}})
    with pytest.raises(ValueError):
        set_skillforge({"router": {"weights": {"hub": 5.0}}}, config_path=p)
    assert json.loads(p.read_text(encoding="utf-8")) == {"skillForge": {}}  # nothing written


def test_set_rejects_bad_hub_url_and_empty_localdir_path(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {}})
    with pytest.raises(ValueError):
        set_skillforge({"router": {"hub": {"endpoint": "not-a-url"}}}, config_path=p)
    with pytest.raises(ValueError):
        set_skillforge({"localDirs": [{"path": "  "}]}, config_path=p)


def test_set_localdirs_normalizes_entry(tmp_path: Path) -> None:
    p = _write(tmp_path, {"skillForge": {}})
    set_skillforge({"localDirs": [{"path": "~/skills"}]}, config_path=p)
    raw = json.loads(p.read_text(encoding="utf-8"))["skillForge"]["localDirs"]
    assert raw == [{"path": "~/skills", "enabled": True, "name": None, "alwaysEnabled": True}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_update_skills.py -q`
Expected: FAIL (`ModuleNotFoundError: raven.config.update_skills`).

- [ ] **Step 3: Implement `update_skills.py`**

```python
"""Atomic write path for the ``skillForge`` config block (P4 skills page).

Whitelist-patch only: reads the raw block, patches the exposed keys, validates
the merged block against ``SkillForgeConfig``, and writes the RAW patched dict
back (never ``model_dump`` — that would reset advanced keys to defaults). Every
non-whitelisted key is preserved verbatim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from raven.config.loader import get_config_path, load_config, read_raw_or_raise
from raven.config.raven import SkillForgeConfig

_WEIGHTS = ("local", "everos", "hub")


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _raw_config(path: Path) -> dict[str, Any]:
    return read_raw_or_raise(path) if path.exists() else {}


def _raw_block(cfg: dict) -> dict:
    return dict(cfg.get("skillForge") or cfg.get("skill_forge") or {})


def get_skillforge(*, config_path: Path | None = None) -> dict:
    path = config_path or get_config_path()
    sf = _raw_block(_raw_config(path))
    d = SkillForgeConfig.model_validate(sf).model_dump(by_alias=True)  # raises if malformed
    router = d.get("router") or {}
    return {
        "enabled": d.get("enabled", True),
        "router": {"weights": router.get("weights") or {}, "hub": router.get("hub") or {}},
        "everos": {"enabled": (d.get("everos") or {}).get("enabled", True)},
        "localDirs": d.get("localDirs") or [],
    }


def set_skillforge(fields: dict, *, config_path: Path | None = None) -> None:
    path = config_path or get_config_path()
    data = _raw_config(path)
    sf = _raw_block(data)

    if "enabled" in fields:
        sf["enabled"] = bool(fields["enabled"])

    rin = fields.get("router") or {}
    if rin:
        router = dict(sf.get("router") or {})
        if "weights" in rin:
            w = dict(router.get("weights") or {})
            for k in _WEIGHTS:
                if k in (rin["weights"] or {}):
                    v = float(rin["weights"][k])
                    if not 0.0 <= v <= 1.0:
                        raise ValueError(f"weight {k}={v} out of range [0, 1]")
                    w[k] = v
            router["weights"] = w
        if "hub" in rin:
            h = dict(router.get("hub") or {})
            hin = rin["hub"] or {}
            if "endpoint" in hin:
                ep = hin["endpoint"]
                if ep and not str(ep).startswith(("http://", "https://")):
                    raise ValueError(f"hub endpoint must be an http(s) URL: {ep!r}")
                h["endpoint"] = ep
            for k in ("apiKey", "minSafety", "timeoutS"):
                if k in hin:
                    h[k] = hin[k]
            router["hub"] = h
        sf["router"] = router

    if "everos" in fields and "enabled" in (fields["everos"] or {}):
        ev = dict(sf.get("everos") or {})
        ev["enabled"] = bool(fields["everos"]["enabled"])
        sf["everos"] = ev

    if "localDirs" in fields:
        out = []
        for d in fields["localDirs"] or []:
            p = str((d or {}).get("path") or "").strip()
            if not p:
                raise ValueError("localDirs entry needs a non-empty path")
            out.append({
                "path": p,
                "enabled": bool(d.get("enabled", True)),
                "name": d.get("name"),
                "alwaysEnabled": bool(d.get("alwaysEnabled", True)),
            })
        sf["localDirs"] = out

    SkillForgeConfig.model_validate(sf)  # raises ValidationError if illegal; nothing written yet

    data.pop("skill_forge", None)
    data["skillForge"] = sf
    _write_atomic(path, data)
    logger.info("update_skills: wrote skillForge block")


def list_skills() -> list[dict]:
    from raven.memory_engine.skill_forge import LocalSkillCatalog

    config = load_config()
    svc = LocalSkillCatalog(config.workspace_path, config=getattr(config, "skill_forge", None), start_watcher=False)
    return [{"name": m.name, "source": m.source, "description": m.description or ""} for m in svc.gather_all_skills()]


__all__ = ["get_skillforge", "set_skillforge", "list_skills"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_update_skills.py -q`
Expected: PASS (5 tests). If `everos`/`localDirs` alias differs from assumptions, adjust `get_skillforge` mapping to match `SkillForgeConfig.model_dump(by_alias=True)` output (inspect once with a scratch `python -c`).

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check raven/config/update_skills.py tests/test_update_skills.py && uv run ruff check raven/config/update_skills.py tests/test_update_skills.py`
Expected: clean.

- [ ] **Step 6: Commit (only after user asks)**

```bash
git add raven/config/update_skills.py tests/test_update_skills.py
git commit -m "feat(config): atomic whitelist write path for skillForge config (P4)"
```

---

### Task 2: `raven.skills.{get,set,list}` RPC + tests

**Files:**
- Modify: `raven/web_rpc/methods_config.py` (inside `register_config_methods`)
- Test: `tests/test_web_rpc_config.py` (extend)

**Interfaces:**
- Consumes: `get_skillforge`, `set_skillforge`, `list_skills` from Task 1; the existing `Dispatcher.register(name, async_handler)`.
- Produces RPC methods: `raven.skills.get` -> `{skillforge: {...}}`; `raven.skills.set` -> `{ok: true, restart_required: true}`; `raven.skills.list` -> `{skills: [...]}`.

- [ ] **Step 1: Write the failing test** (extend `tests/test_web_rpc_config.py`, mirroring its existing channels/cron dispatcher tests)

```python
async def test_skills_get_set_list(tmp_path, monkeypatch):
    import raven.config.update_skills as us
    p = tmp_path / "config.json"
    p.write_text('{"skillForge": {"enabled": true}}', encoding="utf-8")
    monkeypatch.setattr(us, "get_config_path", lambda: p)

    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.web_rpc.methods_config import register_config_methods
    d = Dispatcher()
    register_config_methods(d)

    got = await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "raven.skills.get", "params": {}})
    assert got["result"]["skillforge"]["enabled"] is True

    res = await d.dispatch({"jsonrpc": "2.0", "id": 2, "method": "raven.skills.set",
                            "params": {"fields": {"enabled": False}}})
    assert res["result"] == {"ok": True, "restart_required": True}
    assert '"enabled": false' in p.read_text(encoding="utf-8")
```

(Follow the exact monkeypatch/import style the file already uses for `raven.channels.*`; if it patches `update_channels.get_config_path`, patch `update_skills.get_config_path` the same way.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_web_rpc_config.py -k skills -q`
Expected: FAIL (`method_not_found` / KeyError).

- [ ] **Step 3: Register the methods** — add inside `register_config_methods(...)` in `methods_config.py`, next to the channels block:

```python
    from raven.config.update_skills import get_skillforge, list_skills, set_skillforge

    async def _skills_get(params: dict) -> dict:
        return {"skillforge": get_skillforge()}

    async def _skills_set(params: dict) -> dict:
        set_skillforge(params.get("fields") or {})
        return {"ok": True, "restart_required": True}

    async def _skills_list(params: dict) -> dict:
        return {"skills": list_skills()}

    dispatcher.register("raven.skills.get", _skills_get)
    dispatcher.register("raven.skills.set", _skills_set)
    dispatcher.register("raven.skills.list", _skills_list)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_web_rpc_config.py -k skills -q`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `uv run ruff format --check raven/web_rpc/methods_config.py && uv run ruff check raven/web_rpc/methods_config.py`

- [ ] **Step 6: Commit (only after user asks)**

```bash
git add raven/web_rpc/methods_config.py tests/test_web_rpc_config.py
git commit -m "feat(config): gateway RPC to get/set skillForge config + list skills (P4)"
```

---

### Task 3: service REST routes

**Files:**
- Modify: `ui-webui/service/raven_config_routes.py` (inside `build_raven_config_router`)

**Interfaces:**
- Consumes: `GatewayClient.shared().call(method, params)` (already used by the channels/cron routes in this file).
- Produces: `GET /raven/skills`, `PUT /raven/skills`, `GET /raven/skills/available`.

- [ ] **Step 1: Add the routes** — mirror the existing `/raven/channels` handlers:

```python
    @router.get("/skills")
    async def get_skills() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.skills.get", {})

    @router.put("/skills")
    async def set_skills(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.skills.set", {"fields": body.get("fields") or {}})
        except Exception as exc:  # validation / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/skills/available")
    async def list_skills() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.skills.list", {})
```

- [ ] **Step 2: Manual verification** (no pytest for the service layer; verify against a running stack)

Run the stack (`../start_webapp.sh` from repo root, gateway mode), then:
```bash
curl -s -H "X-User-ID: demo" http://localhost:8000/raven/skills | head -c 200
curl -s -XPUT -H "X-User-ID: demo" -H 'Content-Type: application/json' \
  -d '{"fields":{"enabled":true}}' http://localhost:8000/raven/skills
curl -s -H "X-User-ID: demo" http://localhost:8000/raven/skills/available | head -c 200
```
Expected: first returns the skillforge JSON; PUT returns `{"ok":true,"restart_required":true}`; available returns `{"skills":[...]}`.

- [ ] **Step 3: Commit (only after user asks)**

```bash
git add ui-webui/service/raven_config_routes.py
git commit -m "feat(ui-webui/service): REST proxy for skillForge config + skill list (P4)"
```

---

### Task 4: frontend API client

**Files:**
- Modify: `ui-webui/frontend/src/api/ravenConfig.ts`

**Interfaces:**
- Consumes: `client.get/put` (already imported at top of file).
- Produces: `ravenConfigApi.skills.get()`, `.set(fields)`, `.listAvailable()`, plus the `RavenSkillForge` / `RavenSkillEntry` / `RavenLocalDir` types.

- [ ] **Step 1: Add types + client methods** — append near the other `ravenConfigApi` sections:

```ts
export interface RavenLocalDir {
	path: string;
	enabled: boolean;
	name?: string | null;
	alwaysEnabled: boolean;
}

export interface RavenSkillForge {
	enabled: boolean;
	router: {
		weights: { local?: number; everos?: number; hub?: number };
		hub: { endpoint?: string; apiKey?: string | null; minSafety?: number; timeoutS?: number };
	};
	everos: { enabled: boolean };
	localDirs: RavenLocalDir[];
}

export interface RavenSkillEntry {
	name: string;
	source: string;
	description: string;
}

// inside the existing `export const ravenConfigApi = { ... }` object, add:
	skills: {
		get: () => client.get<{ skillforge: RavenSkillForge }>('/raven/skills'),
		set: (fields: Partial<RavenSkillForge>) =>
			client.put<{ ok: boolean; restart_required: boolean }>('/raven/skills', { fields }),
		listAvailable: () => client.get<{ skills: RavenSkillEntry[] }>('/raven/skills/available'),
	},
```

(Match the exact `client.get/put` generic + return shape the sibling `channels`/`cron` methods in this file already use.)

- [ ] **Step 2: Type-check gate**

Run: `pnpm -C ui-webui/frontend build`
Expected: `tsc -b` passes (no type errors from the new declarations).

- [ ] **Step 3: Commit (only after user asks)**

```bash
git add ui-webui/frontend/src/api/ravenConfig.ts
git commit -m "feat(ui-webui): raven skills config api client (P4)"
```

---

### Task 5: frontend `/raven-skills` page + route + nav + i18n

**Files:**
- Create: `ui-webui/frontend/src/pages/raven-skills/index.tsx`
- Modify: `ui-webui/frontend/src/App.tsx`, `src/components/layout/AppSidebar.tsx`, `src/i18n/locales/{en,zh}.json`

**Interfaces:**
- Consumes: `ravenConfigApi.skills` + types from Task 4.
- Produces: `export function RavenSkillsPage()`.

- [ ] **Step 1: Build the page** — model it on `src/pages/raven-cron/index.tsx` (same imports: `useEffect/useState`, `toast`, `ravenConfigApi`, `@/components/ui/*`, solar icons). Structure top-to-bottom:

```tsx
// Load on mount:
//   const { skillforge } = await ravenConfigApi.skills.get();
//   const { skills } = await ravenConfigApi.skills.listAvailable();
// Local form state seeded from `skillforge`. On Save:
//   await ravenConfigApi.skills.set(form);  // -> setRestartBanner(true); toast.success(...)
//
// Sections (each a shadcn card / labelled group):
//   1. Restart banner (render when `restartRequired`) — "Restart the gateway to apply."
//   2. Master switch: <Switch checked={form.enabled} .../>  (Label = t('ravenSkills.enabled'))
//   3. Retrieval weights: three number Inputs 0..1 for local / everos / hub
//   4. Hub card: Inputs for endpoint / apiKey(type=password) / minSafety / timeoutS
//   5. everos.enabled switch
//   6. localDirs editable list: rows of { path Input, enabled Switch, name Input, remove btn }
//      + "Add directory" button pushing { path:'', enabled:true, name:null, alwaysEnabled:true }
//   7. Save button (calls set)
//   8. Read-only skills table: skills.map(s => row(name, Badge(source), description))
//
// Validation UX: on 400 from set(), show toast.error(err.message); do not clear the form.
```

Keep Prettier tabs/width-4/single-quotes. Reuse the exact `<Input>/<Label>/<Button>/<Badge>` usage from `raven-cron`; add `<Switch>` from `@/components/ui/switch` if the project has it (grep first; fall back to a checkbox Input if not).

- [ ] **Step 2: Register the route** — `src/App.tsx`:

```tsx
import { RavenSkillsPage } from '@/pages/raven-skills';
// ...in the same children array as raven-cron/raven-channels:
{ path: '/raven-skills', element: <RavenSkillsPage /> },
```

- [ ] **Step 3: Add the sidebar entry** — `src/components/layout/AppSidebar.tsx`, next to the `raven-cron`/`raven-channels` items, mirroring their `isActive={location.pathname === '/raven-skills'}` + `onClick={() => navigate('/raven-skills')}` pattern, with a solar skill/book icon and label `t('nav.ravenSkills')`.

- [ ] **Step 4: Add i18n strings** — targeted edits (NOT a full re-dump) to `src/i18n/locales/en.json` and `zh.json`: a `ravenSkills` object (title, section labels, hub field labels, localDirs add/remove, restart banner, save) and a `nav.ravenSkills` label. Keep both locales in sync.

- [ ] **Step 5: Frontend gate**

Run: `pnpm -C ui-webui/frontend lint` (0 errors) and `pnpm -C ui-webui/frontend build`
Expected: both clean.

- [ ] **Step 6: End-to-end smoke** (against the running stack)

Open `http://localhost:5173/raven-skills`: page loads current config; toggle `enabled`, change a weight, add a localDir, Save -> restart banner appears, `~/.raven/config.json` `skillForge` block updated (advanced keys intact); read-only table lists skills.

- [ ] **Step 7: Commit (only after user asks)**

```bash
git add ui-webui/frontend/src/pages/raven-skills ui-webui/frontend/src/App.tsx ui-webui/frontend/src/components/layout/AppSidebar.tsx ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "feat(ui-webui): web page to configure Raven SkillForge (P4)"
```

---

## Self-Review

- **Spec coverage:** enabled/router.weights/hub/everos/localDirs write (Tasks 1-5), restart_required (Tasks 2,5), whitelist-preserve (Task 1 test), read-only list (Tasks 1,2,3,4,5), validation (Task 1), four layers (Tasks 1-5), tests (Tasks 1-2), out-of-scope untouched (Task 1 preserves advanced keys). All covered.
- **Type consistency:** `get_skillforge`/`set_skillforge`/`list_skills` names match across Tasks 1-4; RPC names `raven.skills.{get,set,list}` match Tasks 2-3; REST `/raven/skills{,/available}` match Tasks 3-4; `ravenConfigApi.skills.{get,set,listAvailable}` match Tasks 4-5; `RavenSkillForge` shape identical in Tasks 4-5.
- **Open assumption flagged for the implementer:** the exact alias of `everos`/`localDirs` in `SkillForgeConfig.model_dump(by_alias=True)` — verify once at Task 1 Step 4 and adjust `get_skillforge` mapping if needed (the write path already patches raw keys, so only the read mapping is at risk).
