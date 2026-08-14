# TUI `/subagents` Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/subagents` command to `raven tui` that configures third-party sub-agents from the built-in presets - list with install/test status, add from preset (name + description, plus a masked api key for the openai kind), enable/disable, delete, probe, and test.

**Architecture:** Eight new `subagents.*` RPC methods whose handlers are thin adapters over the config/preset/probe/verdict core the web RPC already uses, plus a native Ink overlay (`subagentsHub`) modelled on `skillsHub.tsx`'s structure. Every mutation writes through `update_subagents` (validated, atomic, read-modify-write) and then hot-applies to the live loop, so the roster inside the `spawn` and `run_subagent_dag` tool descriptions changes without restarting the TUI.

**Tech Stack:** Python 3.12+ / pydantic v2 / `raven.rpc` dispatcher; TypeScript / React / Ink (`@hermes/ink`) / nanostores; contract-first OpenRPC codegen.

**Spec:** `docs/specs/2026-08-06-tui-subagents-command-design.md`

## Global Constraints

- Branch: `feat/subagents_preset_config`. Never commit to `main`, never `git push`, never `git commit --amend`, never create a git worktree.
- Python deps via `uv` only, never `pip`. JS deps in `ui-tui/` via `npm` (that package uses npm, not pnpm - `package-lock.json`).
- Run tests as `uv run pytest ...`, never bare `pytest`.
- Commit messages: Conventional Commits, all-English, ASCII-only (no em-dash, curly quotes, ellipsis, `§`). Trailer: `Co-authored-by: Claude (<actual-session-model-id>) <noreply@anthropic.com>`.
- Do not commit unprompted beyond the per-task commits this plan specifies.
- `ui-tui/src/rpc/generated.ts` is generated. Never hand-edit it; run `npm run gen:rpc`.
- `rpc-schema/openrpc.json` is the single source of truth for the RPC contract (REQ-5).
- Editing `ui-webui` i18n JSON is out of scope; this feature adds no web UI strings.
- The api key is never returned by any RPC, not even masked. Only the boolean `has_api_key`.
- Comments: English only, and only where the logic is non-obvious (repo `CLAUDE.md` section 1).
- Prettier in `ui-tui`: run `npm run fmt` before committing TS changes.

## Known-broken precedent (read before Task 5)

`ui-tui/src/components/skillsHub.tsx` calls `skills.manage`, which **exists only in `gatewayClientStub.ts`** - it is absent from `openrpc.json` and from every Python register call, so `/skills` returns method-not-found in a real `raven tui`. Its tests pass because they assert only that the client was *called* with certain params (`src/__tests__/createSlashHandler.test.ts:141-176`), never that the method exists.

Consequences for this plan:

1. Copy `skillsHub.tsx` for **UI structure only** (staged views, `useInput`, error line, loading state). Do not copy its RPC wiring.
2. Model the RPC wiring on a **registered** handler: `raven/rpc/methods/config.py`.
3. Task 1 adds a registration-coverage test so this class of drift cannot recur.
4. A TS test that mocks `rpc` proves nothing about the backend. Every method needs a Python registration test too.

## File Structure

| File | Responsibility |
|---|---|
| `raven/rpc/models.py` (modify) | `SubagentRow` schema + 8 `Params`/`Result` pairs + 8 `METHOD_MODELS` entries |
| `raven/rpc/errors.py` (modify) | `SubagentNotFoundError` (code -32017, next free) |
| `raven/rpc/methods/subagents.py` (create) | the 8 handlers + `register_subagents_methods`; the only new backend logic |
| `raven/rpc/methods/__init__.py` (modify) | wire the helper into the umbrella |
| `rpc-schema/openrpc.json` (modify) | 8 method entries, `SubagentRow` schema, `SubagentNotFound` error |
| `ui-tui/src/rpc/generated.ts` (regenerate) | `npm run gen:rpc` output |
| `ui-tui/src/app/overlayStore.ts` (modify) | `subagentsHub` key, preserved across turn resets |
| `ui-tui/src/components/subagentsHub.tsx` (create) | the overlay: list, form, delete confirm |
| `ui-tui/src/components/appOverlays.tsx` (modify) | mount the overlay |
| `ui-tui/src/app/slash/commands/ops.ts` (modify) | `/subagents` + subcommands |
| `tests/test_rpc_subagents.py` (create) | handler tests |
| `tests/test_rpc_registration.py` (create) | every `METHODS` name is registered |
| `ui-tui/src/__tests__/subagentsHub.test.tsx` (create) | overlay render + keyboard |
| `ui-tui/src/__tests__/createSlashHandler.test.ts` (modify) | `/subagents` parse |

---

### Task 1: RPC contract + registration-coverage guard

**Files:**
- Modify: `raven/rpc/errors.py`
- Modify: `raven/rpc/models.py`
- Modify: `rpc-schema/openrpc.json`
- Regenerate: `ui-tui/src/rpc/generated.ts`
- Test: `tests/test_rpc_registration.py` (create)

**Interfaces:**
- Produces: `SubagentRow`, `SubagentsListParams/Result`, `SubagentsAddParams/Result`, `SubagentsUpdateParams/Result`, `SubagentsRemoveParams/Result`, `SubagentsToggleParams/Result`, `SubagentsProbeParams/Result`, `SubagentsTestParams/Result`, `SubagentsTestCancelParams/Result`, `SubagentNotFoundError`.

- [ ] **Step 1: Write the failing registration-coverage test**

Create `tests/test_rpc_registration.py`:

```python
"""Every method in the RPC contract must have a handler.

`skill.*` and `mcp.*` are declared in `models.py` METHODS and in
`openrpc-schema/openrpc.json` but no register call backs them, so calling them
in a real `raven tui` returns -32601. That gap predates this test and is
allowlisted below rather than silently tolerated: the point of the test is that
no NEW method joins it. `ui-tui/src/components/skillsHub.tsx` is the visible
cost of the gap going unnoticed.
"""

from __future__ import annotations

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import register_aligned_methods
from raven.rpc.models import METHODS

# Declared in the contract, deliberately unimplemented in v0.1. Shrinking this
# set is progress; growing it needs a reason in the PR description.
KNOWN_UNREGISTERED = {
    "mcp.list",
    "mcp.test",
    "mcp.tools",
    "skill.list",
    "skill.pin",
    "skill.unpin",
}


def test_every_contract_method_is_registered() -> None:
    dispatcher = Dispatcher()
    register_aligned_methods(dispatcher)
    registered = set(dispatcher.methods())
    missing = set(METHODS) - registered - KNOWN_UNREGISTERED
    assert missing == set(), f"declared in METHODS but never registered: {sorted(missing)}"


def test_the_allowlist_does_not_name_a_method_that_is_registered() -> None:
    # A stale allowlist entry would hide a future regression on that name.
    dispatcher = Dispatcher()
    register_aligned_methods(dispatcher)
    registered = set(dispatcher.methods())
    assert KNOWN_UNREGISTERED & registered == set()


```

The `subagents.*` names are **not** asserted here: Task 4 adds that test, once all
eight are registered. Committing a knowingly-red test would leave the branch's
suite failing between commits and reads as a broken commit to a reviewer.

- [ ] **Step 2: Run it to see how it fails**

Run: `uv run pytest tests/test_rpc_registration.py -v`

Expected: errors, not failures. `Dispatcher` may expose its registry under a different name than `methods()`, and `register_aligned_methods` requires no args but check its signature at `raven/rpc/methods/__init__.py:59`. Read `raven/rpc/dispatcher.py` and use the real accessor (if the registry is a private dict, use it and note why in a comment rather than adding a public accessor for a test). Once the accessor is right, both tests must PASS - they describe the tree as it already is, and they are the guard that Tasks 2-4 do not widen the gap.

- [ ] **Step 3: Add the error class**

In `raven/rpc/errors.py`, after `NotDispatchCompatibleError` (the current highest, -32016):

```python
class SubagentNotFoundError(RpcError):
    CODE = -32017
```

Match the surrounding classes exactly - they are bare `CODE` assignments with no body beyond a docstring if the neighbours have one. Check whether `errors.py` keeps a name->code map or an `__all__` and update it if so.

- [ ] **Step 4: Add the models**

In `raven/rpc/models.py`, add a `SubagentRow` schema next to the other shared schemas (beside `SkillInfo`, around line 84), then the eight pairs in their own `# subagents.* methods` section, then the `METHODS` entries after the `config.*` block:

```python
class SubagentRow(_Strict):
    """One row of the /subagents overlay.

    `api_key` is deliberately absent: `has_api_key` is the only thing the UI
    needs, and returning the value - even masked - would put a secret on the
    wire for a screen that never displays it.
    """

    name: str
    preset: str | None
    kind: Literal["cli", "openai"]
    description: str
    enabled: bool
    configured: bool
    group: Literal["installed", "uninstalled"]
    probe_status: Literal["ready", "attention", "missing", "unknown"]
    probe_detail: str
    has_api_key: bool
    last_test_ok: bool | None = None
    last_test_detail: str | None = None
    last_test_at_ms: int | None = None


class SubagentsListParams(_Strict):
    pass


class SubagentsListResult(_Strict):
    rows: list[SubagentRow]


class SubagentsAddParams(_Strict):
    preset: str
    name: str | None = None
    description: str | None = None
    api_key: str | None = None


class SubagentsAddResult(_Strict):
    added: bool
    name: str


class SubagentsUpdateParams(_Strict):
    name: str
    new_name: str | None = None
    description: str | None = None
    api_key: str | None = None


class SubagentsUpdateResult(_Strict):
    updated: bool
    name: str


class SubagentsRemoveParams(_Strict):
    name: str


class SubagentsRemoveResult(_Strict):
    removed: bool


class SubagentsToggleParams(_Strict):
    name: str
    enabled: bool


class SubagentsToggleResult(_Strict):
    enabled: bool


class SubagentsProbeParams(_Strict):
    pass


class SubagentsProbeResult(_Strict):
    rows: list[SubagentRow]


class SubagentsTestParams(_Strict):
    name: str
    source: Literal["config", "preset"] = "config"


class SubagentsTestResult(_Strict):
    ok: bool
    detail: str
    elapsed_ms: int
    reply: str | None = None
    cancelled: bool = False


class SubagentsTestCancelParams(_Strict):
    name: str


class SubagentsTestCancelResult(_Strict):
    cancelled: bool
```

`METHOD_MODELS` entries (mirror the `# config.*` comment style):

```python
    # subagents.*
    "subagents.list": (SubagentsListParams, SubagentsListResult),
    "subagents.add": (SubagentsAddParams, SubagentsAddResult),
    "subagents.update": (SubagentsUpdateParams, SubagentsUpdateResult),
    "subagents.remove": (SubagentsRemoveParams, SubagentsRemoveResult),
    "subagents.toggle": (SubagentsToggleParams, SubagentsToggleResult),
    "subagents.probe": (SubagentsProbeParams, SubagentsProbeResult),
    "subagents.test": (SubagentsTestParams, SubagentsTestResult),
    "subagents.test_cancel": (SubagentsTestCancelParams, SubagentsTestCancelResult),
```

`last_test` is flattened into three optional scalars rather than a nested object because the codegen emits one interface per schema and a nullable nested object is the case the OpenRPC generator handles worst (see the `discriminator` caveat in `ui-tui/scripts/gen-rpc-types.mjs`). Three optional scalars generate cleanly.

- [ ] **Step 5: Add the schema entries**

In `rpc-schema/openrpc.json`: add `SubagentRow` under `components/schemas` (properties mirroring the pydantic model exactly, `additionalProperties: false`, `required` listing every non-defaulted field), add `SubagentNotFound` under `components/errors` as `{"code": -32017, "message": "subagent_not_found"}`, and add the eight methods to `methods` following the `skill.pin` entry's shape (`name`, `summary`, `params` with `required` flags, `result` with a named schema, `errors` as `$ref`s).

- [ ] **Step 6: Regenerate the TS types and verify they are clean**

```bash
cd ui-tui && npm run gen:rpc && npm run lint:rpc && npm run type-check
```

Expected: `gen:rpc` writes `src/rpc/generated.ts` including `SubagentRow`, `SubagentsListResult`, etc.; `lint:rpc` reports no drift; `type-check` passes. Confirm by grep that `SubagentRow` is present with `has_api_key` and **no** `api_key` field.

- [ ] **Step 7: Run the contract tests**

Run: `uv run pytest tests/test_rpc_registration.py -v`

Expected: both tests PASS. Nothing in this task is left red.

- [ ] **Step 8: Commit**

```bash
git add raven/rpc/errors.py raven/rpc/models.py \
        rpc-schema/openrpc.json ui-tui/src/rpc/generated.ts \
        tests/test_rpc_registration.py
git commit -m "feat(tui): declare the subagents RPC contract"
```

---

### Task 2: read handlers - `subagents.list` and `subagents.probe`

**Files:**
- Create: `raven/rpc/methods/subagents.py`
- Modify: `raven/rpc/methods/__init__.py`
- Test: `tests/test_rpc_subagents.py` (create)

**Interfaces:**
- Consumes: the models from Task 1.
- Produces: `subagents_list(params) -> dict`, `subagents_probe(params) -> dict`, `register_subagents_methods(dispatcher, *, agent_loop_factory=None)`, and the module-private `_rows()` that Tasks 3-4 reuse.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rpc_subagents.py`:

```python
"""Tests for the ``subagents.*`` RPC handlers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.rpc.methods.subagents import (
    register_subagents_methods,
    subagents_list,
    subagents_probe,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config file the handlers write to instead of the real ~/.raven."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "subagents": {
                    "thirdParty": [
                        {
                            "name": "Coder",
                            "preset": "claude_code",
                            "kind": "cli",
                            "command": "claude -p {prompt}",
                            "description": "coding",
                            "enabled": True,
                        },
                        {
                            "name": "Researcher",
                            "preset": "mirothinker",
                            "kind": "openai",
                            "baseUrl": "https://api.miromind.ai/v1",
                            "model": "m",
                            "apiKey": "sk-secret-value",
                            "enabled": False,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr("raven.rpc.methods.subagents.get_config_path", lambda: path)
    return path


async def test_list_returns_configured_entries_and_unconfigured_presets(config_path: Path) -> None:
    result = await subagents_list({})
    by_name = {row["name"]: row for row in result["rows"]}

    assert by_name["Coder"]["configured"] is True
    assert by_name["Coder"]["preset"] == "claude_code"
    assert by_name["Coder"]["enabled"] is True
    assert by_name["Researcher"]["enabled"] is False
    # A preset with no configured entry still appears, so the overlay can offer it.
    assert by_name["opencode"]["configured"] is False


async def test_list_never_returns_an_api_key(config_path: Path) -> None:
    result = await subagents_list({})
    blob = json.dumps(result)
    assert "sk-secret-value" not in blob
    by_name = {row["name"]: row for row in result["rows"]}
    assert by_name["Researcher"]["has_api_key"] is True
    assert "api_key" not in by_name["Researcher"]
    assert "apiKey" not in by_name["Researcher"]


async def test_list_groups_an_openai_entry_by_whether_a_key_is_set(config_path: Path) -> None:
    result = await subagents_list({})
    by_name = {row["name"]: row for row in result["rows"]}
    # The key is present, so the entry is usable regardless of any network probe.
    assert by_name["Researcher"]["group"] == "installed"


async def test_list_groups_a_cli_entry_by_the_probe(config_path: Path, monkeypatch) -> None:
    # `claude` is not on PATH in CI, so the row must land in uninstalled rather
    # than defaulting to installed and offering an agent that cannot run.
    monkeypatch.setattr("raven.agent.subagent.probe._login_path", lambda: "")
    result = await subagents_list({})
    by_name = {row["name"]: row for row in result["rows"]}
    assert by_name["Coder"]["group"] == "uninstalled"
    assert by_name["Coder"]["probe_status"] in {"missing", "unknown"}


async def test_list_surfaces_a_malformed_config_section_instead_of_an_empty_list(
    config_path: Path,
) -> None:
    # An empty overlay reads as "no sub-agents configured", which would send the
    # user off to add ones they already have. The validation error has to reach
    # them instead.
    config_path.write_text(
        json.dumps({"subagents": {"thirdParty": [{"name": "Broken", "kind": "cli"}]}}),
        encoding="utf-8",
    )
    with pytest.raises(Exception) as excinfo:
        await subagents_list({})
    assert "command" in str(excinfo.value).lower()


async def test_probe_returns_the_same_row_shape_as_list(config_path: Path) -> None:
    listed = await subagents_list({})
    probed = await subagents_probe({})
    assert {r["name"] for r in listed["rows"]} == {r["name"] for r in probed["rows"]}
    assert set(listed["rows"][0]) == set(probed["rows"][0])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rpc_subagents.py -v`
Expected: FAIL with `ModuleNotFoundError: raven.rpc.methods.subagents`.

Check first how the other `test_rpc_*.py` files mark async tests (`pytestmark = pytest.mark.anyio`, an `anyio_backend` fixture, or `asyncio_mode = auto` in `pyproject.toml`) and match it. If the suite runs `asyncio_mode = auto`, delete the `pytestmark` line.

- [ ] **Step 3: Write the handlers**

Create `raven/rpc/methods/subagents.py`:

```python
"""``subagents.*`` RPC handlers: configure third-party sub-agents from the TUI.

Thin adapters only. The config write path, the preset templates, the probe and
the persisted test verdicts all already exist and are shared with the web RPC
(`raven/web_rpc/methods_config.py`); duplicating any of that logic here would
let the two surfaces disagree about what "installed" means or which fields a
write is allowed to touch.

The install group is computed here rather than in the client because the web UI
computes it client-side in `ui-webui/frontend/src/pages/subagent/catalog.ts`; a
third copy in the TUI is how the rule drifts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.agent.subagent.presets import third_party_subagent_presets
from raven.agent.subagent.probe import probe_all
from raven.agent.subagent.test_state import TestStateStore
from raven.config.loader import get_config_path
from raven.config.schema import SubagentsConfig
from raven.config.update_subagents import get_third_party_subagents

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods import AgentLoopFactory


def _as_configs(entries: list[dict]) -> list[Any]:
    return list(SubagentsConfig(third_party=entries).third_party)


def _group(cfg: Any, probe_status: str) -> str:
    """Which install group a row belongs to.

    An openai entry is keyed off its api key, not the probe: the probe reports
    "api key not set or rejected" for both a missing key and a rejected one, and
    those are different groups needing different user action.
    """
    if getattr(cfg, "kind", None) == "openai":
        return "installed" if (getattr(cfg, "api_key", "") or "").strip() else "uninstalled"
    return "installed" if probe_status == "ready" else "uninstalled"


async def _rows() -> list[dict]:
    """Every row the overlay shows: configured entries first, then presets that
    have no configured entry of their own."""
    configured_raw = get_third_party_subagents(config_path=get_config_path())
    configured = _as_configs(configured_raw)
    claimed = {getattr(c, "preset", None) for c in configured}
    presets = [p for p in third_party_subagent_presets() if p.get("preset") not in claimed]
    preset_cfgs = _as_configs(presets)

    entries: list[tuple[Any, str]] = [(c, "config") for c in configured]
    entries += [(c, "preset") for c in preset_cfgs]

    verdicts = TestStateStore().load(entries)
    results = await probe_all(entries, verdicts=verdicts)

    rows: list[dict] = []
    for (cfg, source), probe in zip(entries, results, strict=True):
        last = probe.last_test
        rows.append(
            {
                "name": cfg.name,
                "preset": getattr(cfg, "preset", None),
                "kind": cfg.kind,
                "description": getattr(cfg, "description", "") or "",
                "enabled": bool(getattr(cfg, "enabled", True)) if source == "config" else False,
                "configured": source == "config",
                "group": _group(cfg, probe.status),
                "probe_status": probe.status,
                "probe_detail": probe.detail,
                "has_api_key": bool((getattr(cfg, "api_key", "") or "").strip()),
                "last_test_ok": None if last is None else last.ok,
                "last_test_detail": None if last is None else last.detail,
                "last_test_at_ms": None if last is None else last.tested_at_ms,
            }
        )
    return rows


async def subagents_list(params: dict) -> dict:
    """Every configured sub-agent plus every unconfigured preset, with status."""
    return {"rows": await _rows()}


async def subagents_probe(params: dict) -> dict:
    """Re-run the free availability probe. Same shape as ``subagents.list``."""
    return {"rows": await _rows()}


def register_subagents_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register the ``subagents.*`` methods on a dispatcher instance."""
    dispatcher.register("subagents.list", subagents_list)
    dispatcher.register("subagents.probe", subagents_probe)


__all__ = [
    "subagents_list",
    "subagents_probe",
    "register_subagents_methods",
]
```

Check `probe.ProbeResult` field names against `raven/agent/subagent/probe.py:50` before trusting `probe.status` / `probe.detail` / `probe.last_test`, and `LastTest` at `test_state.py:53` for `.ok` / `.detail` / `.tested_at_ms`.

- [ ] **Step 4: Wire it into the umbrella**

In `raven/rpc/methods/__init__.py`, import `register_subagents_methods` and call it inside `register_aligned_methods` beside `register_config_methods(dispatcher, agent_loop_factory=agent_loop_factory)` (line 126), passing `agent_loop_factory` the same way. Add it to the module docstring's method inventory if that list enumerates domains.

- [ ] **Step 5: Shrink the in-progress allowlist**

`tests/test_rpc_registration.py` carries an `IN_PROGRESS` set holding the
`subagents.*` names that are declared but not yet registered. Delete the two this
task registers - `"subagents.list"` and `"subagents.probe"` - from it. Leaving them
would let `test_neither_allowlist_names_a_method_that_is_registered` fail, which is
the guard telling you the set is stale.

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/test_rpc_subagents.py tests/test_rpc_registration.py -v
```

Expected: every Task 2 test passes, and all three registration-guard tests pass.

- [ ] **Step 7: Commit**

```bash
git add raven/rpc/methods/subagents.py raven/rpc/methods/__init__.py \
        tests/test_rpc_subagents.py tests/test_rpc_registration.py
git commit -m "feat(tui): serve the subagent roster over RPC"
```

---

### Task 3: write handlers - add / update / remove / toggle

**Files:**
- Modify: `raven/rpc/methods/subagents.py`
- Test: `tests/test_rpc_subagents.py`

**Interfaces:**
- Consumes: `_rows()`, `_as_configs()` from Task 2.
- Produces: `subagents_add`, `subagents_update`, `subagents_remove`, `subagents_toggle`, each `async def (params: dict, *, agent_loop_factory=None) -> dict`, and `_hot_apply(agent_loop_factory)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rpc_subagents.py`:

```python
from raven.rpc.errors import SubagentNotFoundError
from raven.rpc.methods.subagents import (
    subagents_add,
    subagents_remove,
    subagents_toggle,
    subagents_update,
)


def _stored(path: Path) -> list[dict]:
    return json.loads(path.read_text())["subagents"]["thirdParty"]


async def test_add_writes_the_preset_template_under_a_chosen_name(config_path: Path) -> None:
    out = await subagents_add({"preset": "opencode", "name": "Builder", "description": "builds"})
    assert out == {"added": True, "name": "Builder"}

    entry = next(e for e in _stored(config_path) if e["name"] == "Builder")
    assert entry["preset"] == "opencode"
    # The template's execution fields come from the preset, not the caller.
    assert entry["command"] == "opencode run --format json --auto {prompt}"
    assert entry["idSource"] == "derived"
    assert entry["description"] == "builds"


async def test_add_defaults_name_and_description_to_the_preset(config_path: Path) -> None:
    await subagents_add({"preset": "opencode"})
    entry = next(e for e in _stored(config_path) if e["name"] == "opencode")
    assert entry["description"]  # the preset's shipped text, not blank


async def test_add_disables_an_openai_preset_that_has_no_key(config_path: Path) -> None:
    # mirothinker ships an empty apiKey. Added enabled, it would be advertised to
    # the model and fail on first dispatch.
    await subagents_add({"preset": "mirothinker", "name": "Deep"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    assert entry["enabled"] is False


async def test_add_keeps_an_openai_preset_enabled_when_a_key_is_supplied(config_path: Path) -> None:
    await subagents_add({"preset": "mirothinker", "name": "Deep", "api_key": "sk-live"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    assert entry["enabled"] is True


async def test_add_keeps_a_cli_preset_enabled(config_path: Path) -> None:
    await subagents_add({"preset": "opencode"})
    entry = next(e for e in _stored(config_path) if e["name"] == "opencode")
    assert entry["enabled"] is True


async def test_add_rejects_a_duplicate_name_without_writing(config_path: Path) -> None:
    before = _stored(config_path)
    # ValueError specifically, and the message must name the duplicate: a bare
    # `Exception` would also pass if the handler crashed on a typo before ever
    # reaching the write, so it could not tell rejection from an explosion.
    with pytest.raises(ValueError, match="Coder"):
        await subagents_add({"preset": "opencode", "name": "Coder"})
    assert _stored(config_path) == before


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_update_keeps_the_stored_key_for_any_blank_value(config_path: Path, blank: str) -> None:
    # "   " is a truthy string: gating on bare truthiness would overwrite a real
    # key with whitespace, and no caller can see the key to notice or undo it.
    await subagents_update({"name": "Researcher", "api_key": blank})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-secret-value"


async def test_add_rejects_an_unknown_preset(config_path: Path) -> None:
    with pytest.raises(SubagentNotFoundError):
        await subagents_add({"preset": "no_such_preset"})


async def test_update_renames_and_keeps_the_stored_key_when_blank(config_path: Path) -> None:
    out = await subagents_update({"name": "Researcher", "new_name": "Deep", "api_key": None})
    assert out == {"updated": True, "name": "Deep"}
    entry = next(e for e in _stored(config_path) if e["name"] == "Deep")
    # Blank means keep: the caller never sees the stored key, so it cannot resend it.
    assert entry["apiKey"] == "sk-secret-value"


async def test_update_replaces_the_key_when_one_is_given(config_path: Path) -> None:
    await subagents_update({"name": "Researcher", "api_key": "sk-new"})
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["apiKey"] == "sk-new"


async def test_update_leaves_execution_fields_alone(config_path: Path) -> None:
    before = next(e for e in _stored(config_path) if e["name"] == "Coder")
    await subagents_update({"name": "Coder", "description": "new text"})
    after = next(e for e in _stored(config_path) if e["name"] == "Coder")
    assert after["command"] == before["command"]
    assert after["description"] == "new text"


async def test_update_rejects_an_unknown_name(config_path: Path) -> None:
    with pytest.raises(SubagentNotFoundError):
        await subagents_update({"name": "nope", "description": "x"})


async def test_toggle_flips_enabled(config_path: Path) -> None:
    assert await subagents_toggle({"name": "Researcher", "enabled": True}) == {"enabled": True}
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["enabled"] is True


async def test_remove_deletes_the_entry(config_path: Path) -> None:
    assert await subagents_remove({"name": "Coder"}) == {"removed": True}
    assert all(e["name"] != "Coder" for e in _stored(config_path))


async def test_remove_reports_false_for_an_unknown_name(config_path: Path) -> None:
    assert await subagents_remove({"name": "nope"}) == {"removed": False}


async def test_a_mutation_hot_applies_to_the_live_loop(config_path: Path) -> None:
    applied: list[list] = []

    class _Loop:
        def apply_third_party_subagents(self, configs: list) -> None:
            applied.append(configs)

    await subagents_toggle(
        {"name": "Researcher", "enabled": True}, agent_loop_factory=lambda: _Loop()
    )
    assert len(applied) == 1
    assert [c.name for c in applied[0]] == ["Coder", "Researcher"]


async def test_a_mutation_without_a_live_loop_still_writes(config_path: Path) -> None:
    # The demo runner has no loop; a missing loop is not an error.
    await subagents_toggle({"name": "Researcher", "enabled": True}, agent_loop_factory=lambda: None)
    entry = next(e for e in _stored(config_path) if e["name"] == "Researcher")
    assert entry["enabled"] is True


async def test_a_write_re_reads_the_file_first(config_path: Path) -> None:
    # The gateway may have written between the overlay's list call and this
    # mutation; the mutation must not resurrect the stale list it was rendered
    # from. Simulate a concurrent add, then toggle an unrelated agent.
    raw = json.loads(config_path.read_text())
    raw["subagents"]["thirdParty"].append(
        {"name": "Sneaky", "kind": "cli", "command": "cat", "enabled": True}
    )
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    await subagents_toggle({"name": "Coder", "enabled": False})
    names = [e["name"] for e in _stored(config_path)]
    assert "Sneaky" in names, "a concurrent write was clobbered"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_rpc_subagents.py -v -k "add or update or toggle or remove or hot_apply or re_read"`
Expected: FAIL on the imports (`subagents_add` etc. do not exist).

- [ ] **Step 3: Implement the write handlers**

Add to `raven/rpc/methods/subagents.py`:

```python
from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS, third_party_subagent_preset
from raven.config.update_subagents import (
    get_third_party_subagents,
    remove_third_party_subagent,
    set_third_party_subagents,
)
from raven.rpc.errors import SubagentNotFoundError


def _hot_apply(agent_loop_factory: "AgentLoopFactory | None") -> None:
    """Push the new roster into the live runtime.

    Skipped silently when there is no loop (the demo runner): the config write
    is the durable part, and refusing the whole call would make the TUI's own
    demo mode unable to configure anything.
    """
    if agent_loop_factory is None:
        return
    loop = agent_loop_factory()
    if loop is None or not hasattr(loop, "apply_third_party_subagents"):
        return
    entries = get_third_party_subagents(config_path=get_config_path())
    loop.apply_third_party_subagents(_as_configs(entries))


async def subagents_add(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Add a configured entry from a preset template.

    Only `name`, `description` and `api_key` come from the caller; every
    execution field (command, resumeCommand, idSource, transcriptFormat, ...)
    comes from the preset, which is already correct and version-verified.
    """
    preset_name = params.get("preset")
    if preset_name not in THIRD_PARTY_SUBAGENT_PRESETS:
        raise SubagentNotFoundError(
            f"unknown preset: {preset_name!r}",
            data={"preset": preset_name, "known": sorted(THIRD_PARTY_SUBAGENT_PRESETS)},
        )
    entry = third_party_subagent_preset(preset_name)
    if params.get("name"):
        entry["name"] = params["name"]
    if params.get("description"):
        entry["description"] = params["description"]
    if params.get("api_key") is not None:
        entry["apiKey"] = params["api_key"]
    # Every preset ships `enabled: true`, but an openai entry with no key cannot
    # answer: advertising it to the model would produce a sub-agent that fails on
    # first dispatch. Added disabled instead, so the user enables it once the key
    # is in. A cli preset is added enabled - the roster is how it becomes usable,
    # and its probe status is already shown in the row.
    if entry.get("kind") == "openai" and not (entry.get("apiKey") or "").strip():
        entry["enabled"] = False
    kept = list(get_third_party_subagents(config_path=get_config_path()))
    set_third_party_subagents([*kept, entry], config_path=get_config_path())
    _hot_apply(agent_loop_factory)
    return {"added": True, "name": entry["name"]}


async def subagents_update(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Change only name / description / api key on an existing entry."""
    name = params.get("name")
    entries = get_third_party_subagents(config_path=get_config_path())
    target = next((e for e in entries if e.get("name") == name), None)
    if target is None:
        raise SubagentNotFoundError(f"no configured sub-agent named {name!r}", data={"name": name})
    if params.get("new_name"):
        target["name"] = params["new_name"]
    if params.get("description") is not None:
        target["description"] = params["description"]
    # Blank/absent means keep the stored key: the caller is never shown it, so
    # an empty field is "unchanged", never "clear it". Stripped, not bare
    # truthiness - "   " is a truthy string, and letting it through would
    # overwrite a real key with whitespace that no caller can see or recover.
    if (params.get("api_key") or "").strip():
        target["apiKey"] = params["api_key"]
    set_third_party_subagents(entries, config_path=get_config_path())
    _hot_apply(agent_loop_factory)
    return {"updated": True, "name": target["name"]}


async def subagents_toggle(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Set `enabled` on one entry - the flag the roster filter reads."""
    name = params.get("name")
    enabled = bool(params.get("enabled"))
    entries = get_third_party_subagents(config_path=get_config_path())
    target = next((e for e in entries if e.get("name") == name), None)
    if target is None:
        raise SubagentNotFoundError(f"no configured sub-agent named {name!r}", data={"name": name})
    target["enabled"] = enabled
    set_third_party_subagents(entries, config_path=get_config_path())
    _hot_apply(agent_loop_factory)
    return {"enabled": enabled}


async def subagents_remove(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Delete one entry. Reports `removed: false` for a name that was not there."""
    removed = remove_third_party_subagent(params.get("name", ""), config_path=get_config_path())
    if removed:
        _hot_apply(agent_loop_factory)
    return {"removed": removed}
```

Every mutation calls `get_third_party_subagents` itself instead of taking a list from the caller - that is the read-modify-write the concurrency test pins down.

Then extend `register_subagents_methods` with closures that bind `agent_loop_factory`, exactly as `config.py:404` does for `config.set`:

```python
    async def _add(params: dict) -> dict:
        return await subagents_add(params, agent_loop_factory=agent_loop_factory)

    async def _update(params: dict) -> dict:
        return await subagents_update(params, agent_loop_factory=agent_loop_factory)

    async def _toggle(params: dict) -> dict:
        return await subagents_toggle(params, agent_loop_factory=agent_loop_factory)

    async def _remove(params: dict) -> dict:
        return await subagents_remove(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("subagents.add", _add)
    dispatcher.register("subagents.update", _update)
    dispatcher.register("subagents.toggle", _toggle)
    dispatcher.register("subagents.remove", _remove)
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_rpc_subagents.py -v
```

Expected: all pass. If `test_add_rejects_a_duplicate_name_without_writing` fails, check that `set_third_party_subagents` raises before writing (it validates and checks duplicates first - `update_subagents.py:57-66`); do not add a second duplicate check here.

- [ ] **Step 5: Shrink the in-progress allowlist**

Delete the four names this task registers - `"subagents.add"`, `"subagents.update"`,
`"subagents.toggle"`, `"subagents.remove"` - from `IN_PROGRESS` in
`tests/test_rpc_registration.py`, then re-run
`uv run pytest tests/test_rpc_registration.py -v` and confirm all three pass.

- [ ] **Step 6: Commit**

```bash
git add raven/rpc/methods/subagents.py tests/test_rpc_subagents.py \
        tests/test_rpc_registration.py
git commit -m "feat(tui): add, update, toggle and remove subagents over RPC"
```

---

### Task 4: test handlers - `subagents.test` and `subagents.test_cancel`

**Files:**
- Modify: `raven/rpc/methods/subagents.py`
- Test: `tests/test_rpc_subagents.py`

**Interfaces:**
- Produces: `subagents_test`, `subagents_test_cancel`, and the module-level `_RUNNING: dict[str, asyncio.Task]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rpc_subagents.py`:

```python
import asyncio

from raven.rpc.methods.subagents import subagents_test, subagents_test_cancel


async def test_test_records_a_verdict_for_a_configured_agent(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "cli", True, "the agent ran and replied", "PONG", 42)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    out = await subagents_test({"name": "Coder", "source": "config"})
    assert out["ok"] is True
    assert out["reply"] == "PONG"
    assert out["elapsed_ms"] == 42
    assert out["cancelled"] is False

    # The verdict is persisted, so it survives closing the overlay.
    rows = (await subagents_list({}))["rows"]
    coder = next(r for r in rows if r["name"] == "Coder")
    assert coder["last_test_ok"] is True


async def test_test_rejects_an_unknown_name(config_path: Path) -> None:
    with pytest.raises(SubagentNotFoundError):
        await subagents_test({"name": "nope", "source": "config"})


async def test_test_can_target_an_unconfigured_preset(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    out = await subagents_test({"name": "opencode", "source": "preset"})
    assert out["ok"] is True


async def test_cancel_stops_a_running_test(config_path: Path, monkeypatch) -> None:
    started = asyncio.Event()

    async def slow_run_test(cfg, *, source):
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", slow_run_test)
    task = asyncio.create_task(subagents_test({"name": "Coder", "source": "config"}))
    await asyncio.wait_for(started.wait(), timeout=5)

    assert await subagents_test_cancel({"name": "Coder"}) == {"cancelled": True}
    out = await asyncio.wait_for(task, timeout=5)
    assert out["cancelled"] is True
    assert out["ok"] is False


async def test_cancel_reports_false_when_nothing_is_running(config_path: Path) -> None:
    assert await subagents_test_cancel({"name": "Coder"}) == {"cancelled": False}


async def test_a_finished_test_is_not_left_in_the_running_map(config_path: Path, monkeypatch) -> None:
    from raven.agent.subagent.probe import TestResult
    from raven.rpc.methods.subagents import _RUNNING

    async def fake_run_test(cfg, *, source):
        return TestResult(cfg.name, source, "cli", True, "ok", "PONG", 1)

    monkeypatch.setattr("raven.rpc.methods.subagents.run_test", fake_run_test)
    await subagents_test({"name": "Coder", "source": "config"})
    assert "Coder" not in _RUNNING
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_rpc_subagents.py -v -k "test_test or cancel or running_map"`
Expected: FAIL on the imports.

- [ ] **Step 3: Implement**

Add to `raven/rpc/methods/subagents.py`:

```python
import asyncio
import time

from raven.agent.subagent.probe import run_test

# name -> the in-flight test task, so `subagents.test_cancel` can reach it.
# Cancelling the task is what kills the subprocess: `CliAgentBackend` catches
# `asyncio.CancelledError` and killpg's the whole process group
# (`backends/cli_agent.py:170`), and `run_test` catches only `Exception`, so the
# cancellation is not swallowed on the way out.
_RUNNING: dict[str, asyncio.Task] = {}


def _find(name: str, source: str) -> Any:
    """The config object a test should run against, by name and source."""
    pool = (
        third_party_subagent_presets()
        if source == "preset"
        else get_third_party_subagents(config_path=get_config_path())
    )
    entry = next((e for e in pool if e.get("name") == name), None)
    if entry is None:
        raise SubagentNotFoundError(f"no {source} sub-agent named {name!r}", data={"name": name})
    return _as_configs([entry])[0]


async def subagents_test(params: dict) -> dict:
    """Dispatch the real agent once and report the verdict.

    This spends the agent's own quota, so it is only ever reached by an explicit
    request. Looked up by name against config or the presets - never by running a
    command supplied by the caller.
    """
    name = params.get("name", "")
    source = params.get("source", "config")
    cfg = _find(name, source)

    task = asyncio.ensure_future(run_test(cfg, source=source))
    _RUNNING[name] = task
    try:
        result = await task
    except asyncio.CancelledError:
        # Cancelled through `subagents.test_cancel`: report it rather than
        # propagating, so the overlay gets a normal result to render. No verdict
        # is recorded - a cancelled run proves nothing either way.
        return {"ok": False, "detail": "test cancelled", "elapsed_ms": 0, "reply": None, "cancelled": True}
    finally:
        _RUNNING.pop(name, None)

    TestStateStore().record(
        cfg,
        source,
        ok=result.ok,
        detail=result.detail,
        tested_at_ms=int(time.time() * 1000),
    )
    return {
        "ok": result.ok,
        "detail": result.detail,
        "elapsed_ms": result.elapsed_ms,
        "reply": result.reply,
        "cancelled": False,
    }


async def subagents_test_cancel(params: dict) -> dict:
    """Cancel an in-flight test, killing the agent's process group."""
    task = _RUNNING.get(params.get("name", ""))
    if task is None or task.done():
        return {"cancelled": False}
    task.cancel()
    return {"cancelled": True}
```

Register both (no `agent_loop_factory` needed - a test changes no config):

```python
    dispatcher.register("subagents.test", subagents_test)
    dispatcher.register("subagents.test_cancel", subagents_test_cancel)
```

Confirm `TestResult`'s field names at `probe.py:94` before using `result.elapsed_ms` / `result.reply`.

- [ ] **Step 4: Add the registration assertion now that all eight exist**

Append to `tests/test_rpc_registration.py`:

```python
def test_subagents_methods_are_registered() -> None:
    dispatcher = Dispatcher()
    register_aligned_methods(dispatcher)
    registered = set(dispatcher.methods())
    for name in (
        "subagents.list",
        "subagents.add",
        "subagents.update",
        "subagents.remove",
        "subagents.toggle",
        "subagents.probe",
        "subagents.test",
        "subagents.test_cancel",
    ):
        assert name in registered, name
```

Also delete the last two names - `"subagents.test"` and `"subagents.test_cancel"` -
from `IN_PROGRESS`, then assert the set is now empty so it cannot quietly linger:

```python
def test_no_subagents_method_is_left_in_progress() -> None:
    from tests.test_rpc_registration import IN_PROGRESS  # noqa: PLC0415 - self-reference

    assert IN_PROGRESS == set()
```

Write that assertion against the module-level `IN_PROGRESS` directly rather than
re-importing if the file's own conventions make the import awkward. Use whatever
accessor the registry exposes (`dispatcher.methods()` was confirmed to exist).

- [ ] **Step 5: Run the full backend suite**

```bash
uv run pytest tests/test_rpc_subagents.py tests/test_rpc_registration.py -v
```

Expected: all pass, including the new `test_subagents_methods_are_registered` - all eight names are now registered.

- [ ] **Step 6: Commit**

```bash
git add raven/rpc/methods/subagents.py tests/test_rpc_subagents.py \
        tests/test_rpc_registration.py
git commit -m "feat(tui): run and cancel a subagent test over RPC"
```

---

### Task 5: overlay store key + `/subagents` slash command

**Files:**
- Modify: `ui-tui/src/app/overlayStore.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Test: `ui-tui/src/__tests__/createSlashHandler.test.ts`

**Interfaces:**
- Produces: overlay key `subagentsHub: boolean`; the `/subagents` command with four forms.

- [ ] **Step 1: Write the failing tests**

Add to `ui-tui/src/__tests__/createSlashHandler.test.ts`, following the existing `/skills` cases at lines 141-176:

```ts
  it('opens the subagents overlay with no argument', () => {
    const ctx = buildCtx({ gateway: buildGateway() })
    expect(createSlashHandler(ctx)('/subagents')).toBe(true)
    expect($overlayState.get().subagentsHub).toBe(true)
  })

  it('routes /subagents add <preset> [name] to subagents.add', () => {
    const ctx = buildCtx({ gateway: { ...buildGateway(), rpc: vi.fn(() => Promise.resolve({ added: true, name: 'Builder' })) } })
    expect(createSlashHandler(ctx)('/subagents add opencode Builder')).toBe(true)
    expect(ctx.gateway.rpc).toHaveBeenCalledWith('subagents.add', { preset: 'opencode', name: 'Builder' })
    expect($overlayState.get().subagentsHub).toBe(false)
  })

  it('routes /subagents on <name> to subagents.toggle', () => {
    const ctx = buildCtx({ gateway: { ...buildGateway(), rpc: vi.fn(() => Promise.resolve({ enabled: true })) } })
    expect(createSlashHandler(ctx)('/subagents on Coder')).toBe(true)
    expect(ctx.gateway.rpc).toHaveBeenCalledWith('subagents.toggle', { enabled: true, name: 'Coder' })
  })

  it('routes /subagents off <name> to subagents.toggle with enabled false', () => {
    const ctx = buildCtx({ gateway: { ...buildGateway(), rpc: vi.fn(() => Promise.resolve({ enabled: false })) } })
    expect(createSlashHandler(ctx)('/subagents off Coder')).toBe(true)
    expect(ctx.gateway.rpc).toHaveBeenCalledWith('subagents.toggle', { enabled: false, name: 'Coder' })
  })

  it('routes /subagents test <name> to subagents.test', () => {
    const ctx = buildCtx({ gateway: { ...buildGateway(), rpc: vi.fn(() => Promise.resolve({ cancelled: false, detail: 'ok', elapsed_ms: 1, ok: true })) } })
    expect(createSlashHandler(ctx)('/subagents test Coder')).toBe(true)
    expect(ctx.gateway.rpc).toHaveBeenCalledWith('subagents.test', { name: 'Coder', source: 'config' })
  })

  it('supports a multi-word agent name after on/off/test', () => {
    // Agent names may contain spaces ("General Agent"); only the first token is
    // the subcommand, the rest is the name.
    const ctx = buildCtx({ gateway: { ...buildGateway(), rpc: vi.fn(() => Promise.resolve({ enabled: false })) } })
    expect(createSlashHandler(ctx)('/subagents off General Agent')).toBe(true)
    expect(ctx.gateway.rpc).toHaveBeenCalledWith('subagents.toggle', { enabled: false, name: 'General Agent' })
  })
```

Import `$overlayState` the way the surrounding tests do (check the file's existing imports and its `patchOverlayState` usage; reset overlay state in `beforeEach` if the file already does).

- [ ] **Step 2: Run them to verify they fail**

```bash
cd ui-tui && npx vitest run src/__tests__/createSlashHandler.test.ts
```
Expected: FAIL - `subagentsHub` is not a property of the overlay state and `/subagents` is an unknown command.

- [ ] **Step 3: Add the overlay key**

In `ui-tui/src/app/overlayStore.ts`: add `subagentsHub: false` to the initial state beside `skillsHub` (line 20), add it to the `Boolean(...)` any-overlay-open expression (line 29), and add it to the **preserve** list in the turn-boundary reset (line 51-55) with the others. It is user-toggled: a delegation finishing must not silently close a config screen the user opened.

- [ ] **Step 4: Add the slash command**

In `ui-tui/src/app/slash/commands/ops.ts`, beside the `skills` command (line 515):

```ts
  {
    help: 'configure third-party sub-agents (presets, enable/disable, test)',
    name: 'subagents',
    run: (arg, ctx) => {
      const text = arg.trim()

      if (!text) {
        return patchOverlayState({ subagentsHub: true })
      }

      // Only the first token is the subcommand: an agent name may contain
      // spaces, so the remainder is taken verbatim as the name.
      const [sub, ...rest] = text.split(/\s+/)
      const name = rest.join(' ').trim()

      switch (sub) {
        case 'add': {
          const [preset, ...nameParts] = rest
          const chosen = nameParts.join(' ').trim()
          void ctx.gateway.rpc('subagents.add', chosen ? { name: chosen, preset } : { preset })
          return true
        }
        case 'off':
        case 'on':
          void ctx.gateway.rpc('subagents.toggle', { enabled: sub === 'on', name })
          return true
        case 'test':
          void ctx.gateway.rpc('subagents.test', { name, source: 'config' })
          return true
        default:
          return patchOverlayState({ subagentsHub: true })
      }
    }
  }
```

Match the file's real conventions: check whether sibling commands return `patchOverlayState(...)` directly or `void` it, whether they use `ctx.gateway.rpc` or `ctx.gateway.request`, and how they surface an RPC rejection (a `.catch` that pushes a toast/system line). An unhandled promise rejection is a defect - mirror whatever `/skills install` does. Add `supported: false` only if the sibling commands use it to mean "not a Hermes-backed name".

- [ ] **Step 5: Run the tests and the gates**

```bash
cd ui-tui && npx vitest run src/__tests__/createSlashHandler.test.ts && npm run type-check && npm run lint && npm run fmt
```
Expected: the new cases pass; type-check and lint clean.

- [ ] **Step 6: Verify the command is actually reachable**

`/subagents` must appear in the completion list and in `/help`. Check whether `SLASH_COMMANDS` feeds both automatically (`registry.ts` is the single source) - if `commands.catalog` on the Python side keeps its own list, update it too, otherwise the command works when typed but is invisible.

```bash
cd ui-tui && npx vitest run src/__tests__/slashParity.test.ts
```
Expected: PASS. That test exists to catch exactly this kind of registry drift; if it fails, it is telling you about a second list that needs the new name.

- [ ] **Step 7: Commit**

```bash
git add ui-tui/src/app/overlayStore.ts ui-tui/src/app/slash/commands/ops.ts \
        ui-tui/src/__tests__/createSlashHandler.test.ts
git commit -m "feat(tui): add the /subagents command and overlay slot"
```

---

### Task 6: the overlay - list stage

**Files:**
- Create: `ui-tui/src/components/subagentsHub.tsx`
- Modify: `ui-tui/src/components/appOverlays.tsx`
- Test: `ui-tui/src/__tests__/subagentsHub.test.tsx` (create)

**Interfaces:**
- Consumes: `SubagentRow` from `src/rpc/generated.ts`; `subagents.list` / `subagents.probe` / `subagents.toggle` / `subagents.test` / `subagents.test_cancel` / `subagents.remove`.
- Produces: `export function SubagentsHub({ gw, onClose, t }: SubagentsHubProps)` - the same prop shape as `SkillsHub` (`ui-tui/src/components/skillsHub.tsx:19`).

**Read first:** `ui-tui/src/components/skillsHub.tsx` in full. Copy its **structure** - `useEffect` load, `useState` for stage/index/error/loading, one `useInput` handler, `Box`/`Text` from `@hermes/ink`, theme tokens via the `t` prop. Do **not** copy its RPC calls: `skills.manage` does not exist on the backend (see "Known-broken precedent" above).

This task is specified as a behavioural contract plus tests rather than finished JSX: hand-written Ink markup in a plan tends to be subtly wrong about theme tokens and layout props, and the tests below are what actually pin the behaviour down.

**Contract:**

| Requirement | Detail |
|---|---|
| Load | on mount, call `subagents.list`; show a loading line until it resolves; on rejection show the error text and no rows |
| Groups | three sections in this order: `INSTALLED` (configured, `group === 'installed'`), `NOT INSTALLED` (configured, `group === 'uninstalled'`), `AVAILABLE PRESETS` (`configured === false`); omit an empty section entirely |
| Row | status glyph, name, preset name, `[on ]`/`[off]` for configured rows, and a status cell (see below) |
| Status cell | a running test shows a spinner + elapsed seconds; else `last_test_ok === true` -> `ok <age>`; `=== false` -> `failed <age>`; `null` -> `probe_detail` shortened |
| Selection | `up`/`down` move across all rows in all sections; the selected row is marked and never scrolls out of view |
| `space` | `subagents.toggle` on a configured row with `enabled` flipped; refuse with a message on an unconfigured preset row (adding is `enter`) |
| `t` | `subagents.test` for the selected row, `source` = `'config'` when configured else `'preset'`; the overlay stays interactive while it runs |
| `esc` | cancels a running test via `subagents.test_cancel` if one is running for the selected row, otherwise closes the overlay |
| `r` | `subagents.probe` and replace the rows |
| `d` | on a configured row, go to the delete-confirm stage (Task 7) |
| `q` | `onClose()` |
| Refresh | after any mutating RPC resolves, re-run `subagents.list` so the rendered state is the server's, not a local guess |
| Footer | the key hints, then `custom agents: web UI /subagents or ~/.raven/config.json` |

An unconfigured preset row has `enabled: false` from the server and cannot be toggled on directly - `enter` adds it (Task 7), and adding is what makes it toggleable. This mirrors the web UI, where the switch is the configuration act.

- [ ] **Step 1: Write the failing tests**

Create `ui-tui/src/__tests__/subagentsHub.test.tsx`, modelled on `src/__tests__/modelPicker.test.tsx` (read it for the render helper, the fake gateway, and how keystrokes are delivered):

```tsx
// Rows the handlers would return; two configured, one bare preset.
const ROWS = [
  {
    configured: true, description: 'coding', enabled: true, group: 'installed',
    has_api_key: false, kind: 'cli', last_test_at_ms: null, last_test_detail: null,
    last_test_ok: null, name: 'Coder', preset: 'claude_code',
    probe_detail: 'installed at /usr/bin/claude', probe_status: 'ready'
  },
  {
    configured: true, description: 'research', enabled: false, group: 'uninstalled',
    has_api_key: false, kind: 'cli', last_test_at_ms: null, last_test_detail: null,
    last_test_ok: false, name: 'Guard', preset: 'openclaw',
    probe_detail: 'not found on PATH', probe_status: 'missing'
  },
  {
    configured: false, description: 'opencode cli', enabled: false, group: 'installed',
    has_api_key: false, kind: 'cli', last_test_at_ms: null, last_test_detail: null,
    last_test_ok: null, name: 'opencode', preset: 'opencode',
    probe_detail: 'installed at /root/.opencode/bin/opencode', probe_status: 'ready'
  }
]
```

Cases to assert:

1. calls `subagents.list` once on mount
2. renders all three section headers, and each row's name
3. omits a section header when it has no rows (pass rows with no `configured === false` entry)
4. `space` on `Coder` calls `subagents.toggle` with `{ enabled: false, name: 'Coder' }`, then re-calls `subagents.list`
5. `space` on the `opencode` preset row does **not** call `subagents.toggle`
6. `t` on `Guard` calls `subagents.test` with `{ name: 'Guard', source: 'config' }`
7. `t` on `opencode` calls `subagents.test` with `source: 'preset'`
8. `esc` while a test is running calls `subagents.test_cancel` and does not call `onClose`
9. `esc` with no test running calls `onClose`
10. `q` calls `onClose`
11. a rejected `subagents.list` renders the error text and no row names
12. the footer names both custom-agent escape hatches

- [ ] **Step 2: Run them to verify they fail**

```bash
cd ui-tui && npx vitest run src/__tests__/subagentsHub.test.tsx
```
Expected: FAIL - the module does not exist.

- [ ] **Step 3: Implement the component**

Create `ui-tui/src/components/subagentsHub.tsx` satisfying the contract. The state
shape and exported signature are fixed, so Task 7 extends rather than rewrites:

```tsx
import type { SubagentRow } from '../rpc/generated.js'

interface SubagentsHubProps {
  gw: GatewayClient // same type SkillsHub's `gw` prop uses
  onClose: () => void
  t: Theme // same theme type SkillsHub takes
}

type Stage = 'confirm-delete' | 'form' | 'list'

export function SubagentsHub({ gw, onClose, t }: SubagentsHubProps) {
  const [rows, setRows] = useState<SubagentRow[]>([])
  const [idx, setIdx] = useState(0)
  const [stage, setStage] = useState<Stage>('list')
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  // name -> the ms timestamp the test started, so the elapsed counter is derived
  // rather than stored, and a finished test is removed instead of flagged.
  const [testing, setTesting] = useState<Map<string, number>>(new Map())
  // Task 7 adds: formMode, nameInput, descInput, keyInput, field, saving.
}
```

Notes that will otherwise be got wrong:

- One `useInput` handler for the whole overlay, branching on stage - two handlers race for the same keystroke.
- Keep the running-test state as `Map<string, number>` (name -> started-at ms) so the spinner's elapsed seconds come from a leaf-rendered clock, and per repo convention (`ui-webui/CLAUDE.md`, React Flow note) never rebuild derived arrays fresh each render where a signature will do.
- `probe_detail` can be long; truncate with `wrap="truncate-end"` rather than slicing the string, matching `skillsHub`.
- Age formatting (`ok 2h ago`) already exists in the web UI as `ageText` in `ui-webui/frontend/src/components/SubagentStatus.tsx`. Do not import across apps - reimplement the same thresholds locally and keep it a pure function so a test can cover it.

- [ ] **Step 4: Mount it**

In `ui-tui/src/components/appOverlays.tsx`: import `SubagentsHub` beside `SkillsHub` (line 20), add `overlay.subagentsHub` to the `hasAny` expression (line 135), and render it exactly as `SkillsHub` is rendered (line 175-177):

```tsx
      {overlay.subagentsHub && (
        <SubagentsHub gw={gw} onClose={() => patchOverlayState({ subagentsHub: false })} t={theme} />
      )}
```

- [ ] **Step 5: Run the tests and gates**

```bash
cd ui-tui && npx vitest run src/__tests__/subagentsHub.test.tsx && npm run type-check && npm run lint && npm run fmt
```
Expected: all pass, 0 lint errors.

- [ ] **Step 6: Commit**

```bash
git add ui-tui/src/components/subagentsHub.tsx ui-tui/src/components/appOverlays.tsx \
        ui-tui/src/__tests__/subagentsHub.test.tsx
git commit -m "feat(tui): render the subagents overlay"
```

---

### Task 7: the form stage - add / rename with a masked key, and delete confirm

**Files:**
- Modify: `ui-tui/src/components/subagentsHub.tsx`
- Test: `ui-tui/src/__tests__/subagentsHub.test.tsx`

**Interfaces:**
- Consumes: `subagents.add`, `subagents.update`, `subagents.remove`.

**Read first:** `ui-tui/src/components/modelPicker.tsx:540-600` - the key-entry stage. The masking must match it exactly, not approximately:

| Behaviour | Line |
|---|---|
| `const masked = keyInput ? '•'.repeat(Math.min(keyInput.length, 40)) : ''` | 547 |
| `{masked || '(empty)'}` | 578 |
| `const caret = keySaving ? '' : '▎'` | 553 |
| `Tab` cycles fields | 176 |
| focused `t.color.accent` / unfocused `t.color.muted`, `▸ ` vs `  ` marker | 571-572 |
| a "Saved to <path>" line under the title | 562 |
| the key field is omitted for a kind that has no key | 545 (`auth_type === 'local'`); here: `kind !== 'openai'` |

**Contract:**

| Requirement | Detail |
|---|---|
| Enter the stage | `enter` on any row: an unconfigured preset row opens it in `add` mode, a configured row in `edit` mode |
| Fields | `Name`, `Description`, and `API key` only when `kind === 'openai'`; `Tab` cycles, `shift+Tab` reverses |
| Defaults, add mode | name = the preset name, description = the preset's description, key = blank |
| Defaults, edit mode | name and description = the current values; key = **blank even when one is stored** |
| Key hint | when `has_api_key` is true, label the field so blank reads as "keep": e.g. `API key (stored - blank keeps it)` |
| Submit | `enter` on the last field, or `ctrl+s`: `subagents.add` in add mode, `subagents.update` in edit mode; omit `api_key` entirely when the field is blank |
| Cancel | `esc` returns to the list, discarding input |
| After submit | re-run `subagents.list` and return to the list stage; on rejection stay on the form and show the error |
| Delete confirm | `d` on a configured row asks `delete <name>? y/n`; `y` calls `subagents.remove` then refreshes; anything else returns to the list |

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/subagentsHub.test.tsx`:

1. `enter` on the `opencode` preset row shows a form titled for adding, with the name pre-filled `opencode`
2. submitting that form calls `subagents.add` with `{ preset: 'opencode', name: 'opencode', description: <preset text> }`
3. `enter` on `Coder` opens edit mode pre-filled with `Coder` / `coding`
4. submitting after editing calls `subagents.update` with `{ name: 'Coder', new_name: ..., description: ... }`
5. no `API key` field renders for a `kind: 'cli'` row
6. an `API key` field renders for a `kind: 'openai'` row, and typing shows only `•` characters - assert the typed text does **not** appear in the frame
7. with `has_api_key: true`, the key field renders empty and the label says the stored key is kept
8. submitting with the key field blank calls `subagents.update` **without** an `api_key` property
9. submitting with a typed key includes `api_key`
10. `esc` on the form returns to the list without calling any mutating RPC
11. a rejected `subagents.add` keeps the form open and shows the error
12. `d` then `y` calls `subagents.remove` with the selected name; `d` then `n` calls nothing

Case 6 is the one that matters most: it is the regression test for ever printing a secret into the terminal.

- [ ] **Step 2: Run them to verify they fail**

```bash
cd ui-tui && npx vitest run src/__tests__/subagentsHub.test.tsx
```
Expected: the new cases FAIL (no form stage yet); Task 6's cases still pass.

- [ ] **Step 3: Implement the form and confirm stages**

Extend the component. `keyInput` lives in component state and is passed to the RPC only on submit; it is never written into any rendered string except through `masked`.

- [ ] **Step 4: Run the tests and gates**

```bash
cd ui-tui && npx vitest run src/__tests__/subagentsHub.test.tsx && npm run type-check && npm run lint && npm run fmt
```

- [ ] **Step 5: Commit**

```bash
git add ui-tui/src/components/subagentsHub.tsx ui-tui/src/__tests__/subagentsHub.test.tsx
git commit -m "feat(tui): add and rename subagents from the overlay"
```

---

### Task 8: full gates, manual verification, and docs

**Files:**
- Modify: `ui-tui/CONTEXT.md`
- Modify: `docs/specs/2026-08-06-tui-subagents-command-design.md` (status line only)

- [ ] **Step 1: Run every gate**

```bash
uv run pytest -q --ignore=tests/integration
uv run --extra dev ruff check raven tests scripts
uv run --extra dev ruff format --check raven tests scripts
cd ui-tui && npm run type-check && npm run lint && npm test && npm run lint:rpc
```

Expected: `tests/test_cli_theme.py::test_bold_accent_renders_styled_not_bare` and `tests/test_skill_ops.py::test_read_local_body_by_name` fail - both pre-date this branch. Everything else passes. Report the counts; do not "fix" those two.

- [ ] **Step 2: Manually verify in a real TUI**

The overlay's whole point is a live surface, and no test here exercises the real dispatcher end to end.

```bash
uv run raven tui
```

Then: `/subagents` opens the overlay; the roster matches `~/.raven/config.json`; `space` on a row survives closing and reopening the overlay; `r` re-probes; `t` on an installed cli agent reports a verdict; `esc` mid-test cancels it and leaves no orphan process (`pgrep -f claude`); `enter` on `opencode` adds it and it then appears in `spawn`'s tool description **without restarting the TUI** (ask the agent "which sub-agents can you dispatch to?" before and after).

Record what you actually observed for each. If hot-apply does not work, that is a Task 3 defect, not a Task 8 note.

- [ ] **Step 3: Update the TUI context doc**

`ui-tui/CONTEXT.md` defines the overlay vocabulary and lists the user-toggled overlays. Add a **Subagents Overlay** entry beside **Agents Overlay** in the same voice, one short paragraph, naming `/subagents` and that it edits config rather than showing live state - the two are easy to confuse with the Agents Overlay.

- [ ] **Step 4: Flip the spec's status line**

Change `**Status:** approved design, not yet implemented` to `**Status:** implemented`.

- [ ] **Step 5: Commit**

```bash
git add ui-tui/CONTEXT.md docs/specs/2026-08-06-tui-subagents-command-design.md
git commit -m "docs(tui): document the subagents overlay"
```

---

## Deliberately not in this plan

- **`raven subagents` CLI command.** A separate deliverable; the overlay does not need it.
- **Custom agent creation/editing.** The overlay points at the web UI and `config.json`.
- **Fixing `/skills`.** `skills.manage` has no backend, but repairing it is unrelated work; Task 1's registration test stops the same gap widening, and the finding belongs in the PR description so it is not lost.
- **Migrating the web UI to the server-computed group.** `catalog.ts`'s `installGroupOf` stays a second copy for now; noted as a follow-up in the spec.
