# Subagent enable switch and install grouping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every subagent an enable switch that decides whether the model is offered it, split the presets by whether they can work at all, and remember a test verdict without letting a stale one lie.

**Architecture:** Backend first. Task 1 adds `enabled` and filters the roster at the two consumers that build it. Task 2 adds a self-invalidating store for test verdicts. Task 3 merges verdicts into the probe payload and has the RPC record them. Tasks 4-6 then rebuild the page: status precedence and verdict age, the Installed/Uninstalled/Custom groups, and the switch itself.

**Tech Stack:** Python 3.13 + pydantic v2 + pytest (`uv run`); React 19 + Vite + Tailwind v4 + shadcn/Radix + i18next (`pnpm`, from `ui-webui/`).

**Spec:** `docs/specs/2026-08-06-subagent-enable-and-install-groups-design.md`. Read "Enabled is intent, not readiness" before Task 1 and "Invalidation by fingerprint" before Task 2 - both record why the obvious alternative is wrong.

## Global Constraints

- Branch is `feat/subagents_preset_config`, already checked out. Never commit on `main`. Never `git push`. Never `git commit --amend` - a fix is a new commit (`AGENTS.md` section 3.4).
- **Never create a git worktree.** Never touch any branch other than `feat/subagents_preset_config`.
- Commit messages: Conventional Commits, all-English, **ASCII-only**, header <= 100 chars, and a `Co-authored-by: Claude (<model-id>) <noreply@anthropic.com>` trailer. Per `AGENTS.md` section 3.3 `<model-id>` is the **actual model writing the commit**; this branch already carries a mix. The sample messages below spell `claude-opus-5` - substitute your own.
- Python package manager is `uv` only. Never `pip`. Run tests as `uv run pytest`, never bare `pytest`.
- `pytest` runs with `asyncio_mode = "auto"`, so an `async def test_*` needs no decorator.
- JS package manager is **pnpm only**, run from `ui-webui/`.
- **There is no JS unit-test runner in this repo.** For frontend tasks the gate is `pnpm -C frontend lint` (0 errors; 17 pre-existing warnings are expected) plus `pnpm -C frontend build`. Do not invent a test framework.
- Prettier for the frontend is tabs, width 4, single quotes, semicolons, print width 100. `cn()` is `twMerge(clsx(...))`, so a later class wins a Tailwind conflict.
- i18n: edit `src/i18n/locales/{en,zh}.json` with **targeted** edits only. Never rewrite either file with `json.dump`. Every new key must land in **both** locales at matching indentation, and both files must still parse.
- Code comments: English, only where the logic is non-obvious or a constraint is hidden. Do not annotate edits.
- Two tests fail at this branch's base and are **not yours to fix**: `tests/test_cli_theme.py::test_bold_accent_renders_styled_not_bare` and `tests/test_skill_ops.py::test_read_local_body_by_name`. The established full-suite state is `2 failed, 5701 passed, 30 skipped, 13 deselected`.
- `enabled` defaults to `True` everywhere. That default is what keeps every existing `config.json` working untouched, so no task may make it required or default it to `False`.
- **The roster must never consult a probe.** `enabled` is the user's intent; deriving it from a PATH lookup or a network call would let an agent silently vanish from the model's options mid-session.
- Two files are intentionally uncommitted from separate in-flight work: `ui-webui/frontend/src/components/SubagentIcon.tsx` and `MiroMindMark.tsx`. Leave them unstaged.

---

### Task 1: `enabled` on the config, filtered at the two roster consumers

**Files:**
- Modify: `raven/config/schema.py` (both third-party models)
- Modify: `raven/agent/subagent/backends/__init__.py` (new helper + `__all__`)
- Modify: `raven/agent/subagent/manager.py:128-142` (`add_third_party_subagent`)
- Modify: `raven/agent/subagent_dag/tool.py:120-145` (`add_third_party_subagent`)
- Modify: `ui-webui/frontend/src/api/ravenConfig.ts` (mirror the field in both interfaces)
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `enabled: bool = True` on `ThirdPartyCliSubagentConfig` and `ThirdPartyOpenAISubagentConfig`, wire alias `enabled`; `enabled_third_party(configs: Sequence[Any]) -> list[Any]` exported from `raven.agent.subagent.backends`; `enabled?: boolean` on `RavenCliSubagent` and `RavenOpenAISubagent`. Task 6 writes the field; Tasks 4-5 read it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_subagent_third_party.py`:

```python
def test_enabled_defaults_to_true_on_both_kinds() -> None:
    # The default is what keeps an existing config.json working untouched: every
    # entry already on disk predates this field.
    cli = ThirdPartyCliSubagentConfig(name="c", command="echo {prompt}")
    api = ThirdPartyOpenAISubagentConfig(name="a", base_url="http://x/v1", model="m")
    assert cli.enabled is True
    assert api.enabled is True


def test_enabled_third_party_filters_only_disabled() -> None:
    from raven.agent.subagent.backends import enabled_third_party

    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    assert [c.name for c in enabled_third_party([on, off])] == ["on"]
    assert enabled_third_party([]) == []

    # An object with no `enabled` attribute counts as enabled, so a caller passing
    # something other than a validated config cannot silently lose agents.
    class _Bare:
        name = "bare"

    bare = _Bare()
    assert enabled_third_party([bare]) == [bare]


def test_manager_roster_omits_a_disabled_agent(tmp_path: Path) -> None:
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    # `_mgr` is this file's existing helper: SubagentManager needs `provider` and
    # `model` too, and every manager test here goes through it.
    mgr = _mgr(tmp_path, [on, off])
    assert [m.name for m in mgr.list_third_party_agents()] == ["on"]


def test_spawn_tool_listing_omits_a_disabled_agent(tmp_path: Path) -> None:
    # This is the assertion that actually protects the tool description the model
    # reads: a disabled agent must not appear in the roster it chooses from.
    on = ThirdPartyCliSubagentConfig(name="on", description="stays", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", description="goes", command="echo {prompt}", enabled=False)
    # `_mgr` is this file's existing helper: SubagentManager needs `provider` and
    # `model` too, and every manager test here goes through it.
    mgr = _mgr(tmp_path, [on, off])
    listing = format_agent_listing(mgr.list_third_party_agents())
    assert "on" in listing
    assert "off" not in listing


def test_dag_tool_roster_omits_a_disabled_agent(tmp_path: Path) -> None:
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[on, off])
    assert [m.name for m in tool._subagent_meta] == ["on"]
    assert set(tool._subagents) == {"on"}


def test_dag_tool_with_everything_disabled_has_an_empty_roster(tmp_path: Path) -> None:
    # Reachable by switch now, not only by deleting entries, so it must degrade to
    # "no agents" rather than raise.
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[off])
    assert tool._subagent_meta == []
    assert tool._subagents == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "enabled or disabled" -v`
Expected: FAIL - `ValidationError` on the unknown `enabled` field, and `ImportError` for `enabled_third_party`.

- [ ] **Step 3: Add the field to both config models**

In `raven/config/schema.py`, add to **both** `ThirdPartyCliSubagentConfig` and `ThirdPartyOpenAISubagentConfig`, directly after `preset`:

```python
    enabled: bool = True
    """Whether the dispatching model is offered this agent at all.

    Read only where the roster is built (see ``enabled_third_party``), never
    derived from a probe: this records what the user wants, not whether the agent
    currently works. Defaults to ``true`` so an entry written before this field
    existed keeps being advertised exactly as it was.
    """
```

- [ ] **Step 4: Add the shared filter**

In `raven/agent/subagent/backends/__init__.py`, add after `third_party_agent_meta` and extend `__all__` with `"enabled_third_party"`:

```python
def enabled_third_party(configs: Sequence[Any]) -> list[Any]:
    """The subset of third-party configs the model may dispatch to.

    Lives here beside ``third_party_agent_meta`` and ``format_agent_listing``
    because this module owns how a config is presented to the model, and it is
    applied inside the two consumers rather than at their call sites: five paths
    hand a config list to those setters (three CLI entry points, the AgentLoop's
    construction, and its hot-apply), so filtering at the boundary would be five
    places to keep in step and the sixth would be written without it.

    ``enabled`` is the user's intent and is deliberately not derived from a probe.
    A roster that depended on a PATH lookup or a network call would let an agent
    vanish from the model's options mid-session, and the model would then plan
    around a roster that shrank underneath it -- worse than a spawn that fails
    with a clear error. Missing attribute counts as enabled, so a duck-typed
    caller cannot silently lose agents.
    """
    return [cfg for cfg in configs or [] if getattr(cfg, "enabled", True)]
```

`Sequence` is already imported in that module (`from collections.abc import Sequence`).

- [ ] **Step 5: Apply the filter in both consumers**

In `raven/agent/subagent/manager.py`, import `enabled_third_party` alongside the existing `build_third_party_backend` / `third_party_agent_meta` imports, and change the loop header in `set_third_party_subagents`:

```python
        for cfg in enabled_third_party(configs):
```

Do the same in `raven/agent/subagent_dag/tool.py`'s `add_third_party_subagent`, adding `enabled_third_party` to its existing import from `raven.agent.subagent.backends`.

Both loops keep their `try` / `except` and their `logger.warning` untouched.

- [ ] **Step 6: Mirror the field in the TS types**

In `ui-webui/frontend/src/api/ravenConfig.ts`, add to **both** `RavenCliSubagent` and `RavenOpenAISubagent`, beside `preset`:

```ts
	/** Whether the dispatching model is offered this agent. Absent counts as
	 *  enabled, matching the backend default. */
	enabled?: boolean;
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -q`
Expected: PASS.

Run: `uv run pytest tests/test_subagent_probe.py tests/test_web_rpc_config.py tests/test_update_subagents.py tests/test_subagent_dag_runner.py -q`
Expected: PASS - nothing sets `enabled`, so every existing entry defaults to enabled and the rosters are unchanged.

Run: `uv run ruff check raven/ tests/` and `uv run ruff format --check raven/ tests/`
Expected: both clean.

From `ui-webui/`: `pnpm -C frontend lint` (0 errors) and `pnpm -C frontend build`.

- [ ] **Step 8: Commit**

```bash
git add raven/config/schema.py raven/agent/subagent/backends/__init__.py \
        raven/agent/subagent/manager.py raven/agent/subagent_dag/tool.py \
        ui-webui/frontend/src/api/ravenConfig.ts tests/test_subagent_third_party.py
git commit -m "$(cat <<'EOF'
feat(agent): let a third-party subagent be disabled without deleting it

Every configured entry was advertised to the dispatching model, so the only way
to take one off the roster was to delete its configuration and lose the name, the
key and any command edits with it. Add an enabled flag, defaulting to true so an
existing config is untouched, and filter inside the two consumers that build the
roster rather than at the five call sites that feed them. The flag records intent
and is never derived from a probe: a roster that depended on a PATH lookup would
let an agent vanish from the model's options mid-session.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `test_state.py` - remembered verdicts that invalidate themselves

A cli agent's free probe can only answer "installed". The failures that matter -
a missing provider credential, a hard runtime-version gate - are only observable
by running it, and that verdict is currently thrown away. Keeping it is only safe
if a stale one cannot masquerade as current, which is what the fingerprint is for.

**Files:**
- Create: `raven/agent/subagent/test_state.py`
- Test: `tests/test_subagent_test_state.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces, for Task 3:
  - `LastTest` (frozen dataclass): `ok: bool`, `detail: str`, `tested_at_ms: int`
  - `fingerprint(cfg: Any) -> str`
  - `TestStateStore(path: Path | None = None)` with `record(cfg, source, *, ok, detail, tested_at_ms) -> None` and `load(entries: Sequence[tuple[Any, str]]) -> dict[str, LastTest]` keyed `f"{source}:{name}"`
  - `default_state_path() -> Path`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_test_state.py`:

```python
"""Remembered subagent test verdicts and their fingerprint-based invalidation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.subagent.test_state import LastTest, TestStateStore, fingerprint
from raven.config.schema import ThirdPartyCliSubagentConfig, ThirdPartyOpenAISubagentConfig


def _cli(**over) -> ThirdPartyCliSubagentConfig:
    base = {"name": "Coder", "command": "claude -p {prompt}"}
    return ThirdPartyCliSubagentConfig(**{**base, **over})


def _openai(**over) -> ThirdPartyOpenAISubagentConfig:
    base = {"name": "DeepResearcher", "base_url": "https://x/v1", "model": "m1", "api_key": "k1"}
    return ThirdPartyOpenAISubagentConfig(**{**base, **over})


def test_record_then_load_round_trips(tmp_path: Path) -> None:
    store = TestStateStore(path=tmp_path / "state.json")
    cfg = _cli()
    store.record(cfg, "config", ok=False, detail="exited 1: ProviderAuthError", tested_at_ms=1700)
    got = store.load([(cfg, "config")])
    assert got == {"config:Coder": LastTest(ok=False, detail="exited 1: ProviderAuthError", tested_at_ms=1700)}


def test_changing_the_command_discards_the_verdict(tmp_path: Path) -> None:
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_cli(), "config", ok=False, detail="bad", tested_at_ms=1700)
    assert store.load([(_cli(command="claude -p {prompt} --verbose"), "config")]) == {}


def test_changing_the_api_key_discards_the_verdict(tmp_path: Path) -> None:
    # Fixing a rejected key must clear the old failure, or the page keeps showing a
    # verdict the user has already acted on.
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_openai(), "config", ok=False, detail="401", tested_at_ms=1700)
    assert store.load([(_openai(api_key="k2"), "config")]) == {}


def test_redescribing_or_toggling_preserves_the_verdict(tmp_path: Path) -> None:
    # Neither changes whether the agent runs, so a still-true verdict must survive.
    # This is what the fingerprint's field list buys, so it is worth pinning.
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_cli(), "config", ok=True, detail="ran", tested_at_ms=1700)
    assert "config:Coder" in store.load([(_cli(description="new words", enabled=False), "config")])


def test_a_rename_does_not_find_the_verdict(tmp_path: Path) -> None:
    # Intended, and pinned so nobody later "fixes" it into a stale-verdict bug: the
    # record is keyed source:name because that is how the page looks one up, so a
    # renamed agent finds nothing. Re-testing is one click.
    store = TestStateStore(path=tmp_path / "state.json")
    store.record(_cli(), "config", ok=True, detail="ran", tested_at_ms=1700)
    assert store.load([(_cli(name="Coder2"), "config")]) == {}


def test_source_is_part_of_the_key(tmp_path: Path) -> None:
    store = TestStateStore(path=tmp_path / "state.json")
    cfg = _cli(name="codex", command="codex exec {prompt}")
    store.record(cfg, "preset", ok=True, detail="ran", tested_at_ms=1700)
    assert store.load([(cfg, "config")]) == {}
    assert "preset:codex" in store.load([(cfg, "preset")])


def test_record_replaces_rather_than_appends(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = TestStateStore(path=path)
    cfg = _cli()
    store.record(cfg, "config", ok=False, detail="first", tested_at_ms=1700)
    store.record(cfg, "config", ok=True, detail="second", tested_at_ms=1800)
    assert len(json.loads(path.read_text())["verdicts"]) == 1
    assert store.load([(cfg, "config")])["config:Coder"].detail == "second"


def test_stored_payload_carries_no_api_key(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    TestStateStore(path=path).record(_openai(api_key="sk-super-secret"), "config", ok=True, detail="ok", tested_at_ms=1)
    assert "sk-super-secret" not in path.read_text()


def test_missing_file_yields_no_verdicts(tmp_path: Path) -> None:
    assert TestStateStore(path=tmp_path / "absent.json").load([(_cli(), "config")]) == {}


def test_malformed_file_yields_no_verdicts(tmp_path: Path) -> None:
    # The page must not break because this convenience file is corrupt.
    path = tmp_path / "state.json"
    path.write_text("{not json at all")
    assert TestStateStore(path=path).load([(_cli(), "config")]) == {}


def test_unwritable_directory_does_not_raise(tmp_path: Path) -> None:
    doomed = tmp_path / "nope"
    doomed.write_text("i am a file, not a directory")
    TestStateStore(path=doomed / "state.json").record(_cli(), "config", ok=True, detail="ok", tested_at_ms=1)


def test_fingerprint_ignores_identity_fields_and_tracks_execution_fields() -> None:
    assert fingerprint(_cli()) == fingerprint(_cli(name="other", description="d", enabled=False))
    assert fingerprint(_cli()) != fingerprint(_cli(cwd="/tmp"))
    assert fingerprint(_cli()) != fingerprint(_cli(env={"A": "b"}))
    assert fingerprint(_openai()) != fingerprint(_openai(base_url="https://y/v1"))
    assert fingerprint(_openai()) != fingerprint(_openai(model="m2"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_test_state.py -q`
Expected: collection error - `ModuleNotFoundError: No module named 'raven.agent.subagent.test_state'`.

- [ ] **Step 3: Write the implementation**

Create `raven/agent/subagent/test_state.py`:

```python
"""Remembered outcomes of explicit subagent availability tests.

Worth keeping, because for a cli agent the free probe can only answer
"installed" while the failures that matter -- a missing provider credential, a
hard runtime-version gate -- are observable only by running the thing.

Worth invalidating, because a remembered verdict becomes a lie the moment the
configuration it measured changes. Every record therefore carries a digest of the
fields that decide how the agent runs, and a verdict whose digest no longer
matches is treated as absent rather than shown as current. Doing it by digest
rather than by hooking the write path is what also covers a hand-edited
``config.json``, an edit no UI hook would ever see.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

_FILENAME = "subagent_test_state.json"

# Only the fields that decide how the agent runs. `name`, `description`, `preset`
# and `enabled` are deliberately absent: renaming an agent or switching it off and
# on does not change whether it works, so neither may discard a verdict that holds.
_CLI_FIELDS = (
    "command",
    "resume_command",
    "id_source",
    "session_id_pattern",
    "output_pattern",
    "transcript_format",
    "cwd",
    "env",
    "timeout",
)
_OPENAI_FIELDS = ("base_url", "model", "api_key")


def default_state_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path().parent / _FILENAME


@dataclass(frozen=True)
class LastTest:
    """One remembered verdict, already validated against the current config."""

    ok: bool
    detail: str
    tested_at_ms: int


def fingerprint(cfg: Any) -> str:
    """A digest of the fields that decide how this agent runs.

    Truncated because this is a change detector, not a security boundary: a
    collision would at worst surface one stale verdict. ``api_key`` is included
    so that fixing a rejected key clears the old failure, and only the digest is
    ever written, never the key.
    """
    fields = _OPENAI_FIELDS if getattr(cfg, "kind", None) == "openai" else _CLI_FIELDS
    payload = {name: getattr(cfg, name, None) for name in fields}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class TestStateStore:
    """One JSON file of remembered verdicts, keyed by source and name.

    Stored as a list rather than a keyed object for the reason
    :mod:`raven.agent.subagent.instances` gives for the same choice: a subagent
    name is an arbitrary user string, and a list needs no key escaping.
    """

    # The name starts with pytest's default `python_classes = Test*` prefix, so
    # importing it into a test module makes the collector try to collect it and warn
    # about the constructor. The name is right for the domain -- it stores test
    # state -- so opt the class out of collection rather than rename it.
    __test__ = False

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_state_path()

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("verdicts") if isinstance(raw, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "verdicts": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def record(self, cfg: Any, source: str, *, ok: bool, detail: str, tested_at_ms: int) -> None:
        """Remember one verdict, replacing any previous one for the same agent.

        Takes the config object rather than a precomputed digest so ``record`` and
        ``load`` cannot drift onto different field sets -- the one failure that
        would make invalidation silently stop working. ``tested_at_ms`` is passed
        in rather than read from the clock here so a test can pin it.
        """
        name = getattr(cfg, "name", "") or ""
        rows = [r for r in self._read() if not (r.get("source") == source and r.get("name") == name)]
        rows.append(
            {
                "source": source,
                "name": name,
                "ok": bool(ok),
                "detail": detail,
                "fingerprint": fingerprint(cfg),
                "testedAtMs": int(tested_at_ms),
            }
        )
        try:
            self._write(rows)
        except OSError as e:  # noqa: BLE001 - a remembered verdict is a convenience, never a dependency
            logger.warning("subagent test state write failed (verdict not remembered): {}", e)

    def load(self, entries: Sequence[tuple[Any, str]]) -> dict[str, LastTest]:
        """Verdicts still valid for these ``(config, source)`` pairs, keyed ``"source:name"``.

        A verdict whose fingerprint no longer matches its config is skipped, which
        is the whole invalidation mechanism -- nothing has to delete it.
        """
        rows = {(row.get("source"), row.get("name")): row for row in self._read()}
        found: dict[str, LastTest] = {}
        for cfg, source in entries:
            name = getattr(cfg, "name", "") or ""
            row = rows.get((source, name))
            if row is None or row.get("fingerprint") != fingerprint(cfg):
                continue
            found[f"{source}:{name}"] = LastTest(
                ok=bool(row.get("ok")),
                detail=str(row.get("detail") or ""),
                tested_at_ms=int(row.get("testedAtMs") or 0),
            )
        return found


__all__ = ["LastTest", "TestStateStore", "default_state_path", "fingerprint"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_test_state.py -q`
Expected: PASS, all tests.

Run: `uv run ruff check raven/ tests/` and `uv run ruff format --check raven/ tests/`
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add raven/agent/subagent/test_state.py tests/test_subagent_test_state.py
git commit -m "$(cat <<'EOF'
feat(agent): remember subagent test verdicts, invalidated by fingerprint

A cli agent's free probe can only answer "installed", while the failures that
matter are observable only by running it - and that verdict was discarded the
moment the pane changed. Persist it, and guard against a stale one by digesting
the fields that decide how the agent runs: a verdict whose digest no longer
matches is simply absent, so nothing has to delete it and a hand-edited config
invalidates too. Renaming or toggling deliberately preserves a verdict, since
neither changes whether the agent works.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: merge verdicts into the probe payload, and record them on test

**Files:**
- Modify: `raven/agent/subagent/probe.py` (`ProbeResult`, `probe_all`, imports, `__all__` unchanged)
- Modify: `raven/web_rpc/methods_config.py` (`_probe`, `_test`, `import time`)
- Modify: `ui-webui/frontend/src/api/ravenConfig.ts` (`lastTest` on `RavenSubagentProbe`)
- Test: `tests/test_subagent_probe.py`, `tests/test_web_rpc_config.py`

**Interfaces:**
- Consumes: `LastTest`, `TestStateStore` from Task 2.
- Produces, for Tasks 4-5: `ProbeResult.last_test: LastTest | None`; `to_wire()["lastTest"]` as `null` or `{ok, detail, testedAtMs}`; `probe_all(entries, verdicts=None)`; `RavenSubagentProbe.lastTest`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_subagent_probe.py`:

```python
async def test_probe_all_attaches_a_verdict_to_the_matching_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent.test_state import LastTest

    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    a = _cli("a {prompt}", name="a")
    b = _cli("b {prompt}", name="b")
    verdicts = {"config:a": LastTest(ok=False, detail="auth failed", tested_at_ms=1700)}
    results = await probe_all([(a, "config"), (b, "config")], verdicts=verdicts)
    assert results[0].last_test == LastTest(ok=False, detail="auth failed", tested_at_ms=1700)
    assert results[1].last_test is None


async def test_probe_wire_shape_carries_last_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent.test_state import LastTest

    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    cfg = _cli("nope {prompt}", name="a")
    [bare] = await probe_all([(cfg, "config")])
    assert bare.to_wire()["lastTest"] is None
    [withv] = await probe_all([(cfg, "config")], verdicts={"config:a": LastTest(True, "ran", 42)})
    assert withv.to_wire()["lastTest"] == {"ok": True, "detail": "ran", "testedAtMs": 42}
```

Add to `tests/test_web_rpc_config.py`. **First an autouse fixture**, because
`_probe` and `_test` will now construct a `TestStateStore()` with no path, which
resolves to the real `~/.raven/subagent_test_state.json` -- every existing test in
this file that calls either RPC would otherwise read and write the user's own file
on every run. Same hazard, and same remedy, as the isolated-registry fixtures in
`tests/test_subagent_third_party.py` and `tests/test_subagent_dag_runner.py`:

```python
@pytest.fixture(autouse=True)
def _isolated_test_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may touch the real ~/.raven/subagent_test_state.json.

    `raven.subagents.probe` and `.test` build a store with no explicit path, so
    without this every run of this file would accumulate verdicts in the user's
    own state file.
    """
    import raven.agent.subagent.test_state as state_mod

    monkeypatch.setattr(state_mod, "default_state_path", lambda: tmp_path / "_autouse_state.json")
```

Then the test itself:

```python
async def test_subagents_test_records_a_verdict_the_probe_then_returns(
    cfg_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import raven.agent.subagent.backends.env as env_mod
    import raven.agent.subagent.probe as probe_mod

    exe = tmp_path / "verdict-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(
        env_mod,
        "_LOGIN_ENV",
        {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root")},
    )

    d = Dispatcher()
    register_config_methods(d, agent=_FakeAgent())
    await _dispatch(
        d,
        "raven.subagents.set",
        {"agents": [{"name": "verdicts", "kind": "cli", "command": "verdict-agent {prompt}"}]},
    )
    resp = await _dispatch(d, "raven.subagents.test", {"name": "verdicts", "source": "config"})
    assert resp["result"]["result"]["ok"] is True

    probe = await _dispatch(d, "raven.subagents.probe", {})
    row = next(r for r in probe["result"]["results"] if r["source"] == "config" and r["name"] == "verdicts")
    assert row["lastTest"] is not None
    assert row["lastTest"]["ok"] is True
    assert row["lastTest"]["testedAtMs"] > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_probe.py -k "last_test" tests/test_web_rpc_config.py -k "records_a_verdict" -v`
Expected: FAIL - `probe_all() got an unexpected keyword argument 'verdicts'`, and `KeyError: 'lastTest'`.

- [ ] **Step 3: Carry `last_test` on `ProbeResult`**

In `raven/agent/subagent/probe.py`, add to the imports:

```python
from dataclasses import dataclass, replace
```

(the module already imports `dataclass`; add `replace` to the same line) and:

```python
from raven.agent.subagent.test_state import LastTest
```

Add the field as the **last** member of `ProbeResult`, so existing positional
construction keeps working:

```python
    last_test: LastTest | None = None
    """The remembered outcome of an explicit test, when one is still valid for this
    exact configuration. Attached by ``probe_all``; ``probe.py`` never reads or
    writes the store itself, which keeps file I/O out of this module."""
```

and extend `to_wire()` with:

```python
            "lastTest": None
            if self.last_test is None
            else {
                "ok": self.last_test.ok,
                "detail": self.last_test.detail,
                "testedAtMs": self.last_test.tested_at_ms,
            },
```

- [ ] **Step 4: Let `probe_all` attach them**

Change `probe_all`'s signature and body:

```python
async def probe_all(
    entries: Sequence[tuple[Any, Source]],
    *,
    verdicts: Mapping[str, LastTest] | None = None,
) -> list[ProbeResult]:
    """Probe many configs concurrently, returning results in the order given.

    The login-shell PATH is captured once here rather than inside each cli
    probe: ``login_shell_env`` shells out to ``bash -lic`` and can block for
    real seconds on its first call, which would serialise the whole batch.

    ``verdicts`` is keyed ``"source:name"``; a missing entry simply leaves
    ``last_test`` as ``None``, so the caller needs no per-result branching.
    """
    path = await _captured_login_path()
    results = list(await asyncio.gather(*(probe_one(cfg, source=src, path=path) for cfg, src in entries)))
    if verdicts is None:
        return results
    return [replace(r, last_test=verdicts.get(f"{r.source}:{r.name}")) for r in results]
```

Add `Mapping` to the `collections.abc` import.

- [ ] **Step 5: Wire the store into the two RPC handlers**

In `raven/web_rpc/methods_config.py`, add `import time` at the top of the module
(it currently imports only `__future__` and `typing`), and extend the deferred
import block inside `register_config_methods`:

```python
    from raven.agent.subagent.test_state import TestStateStore
```

Change `_probe` to load verdicts and pass them:

```python
    async def _probe(params: dict) -> dict:
        # Read config from disk rather than from the live AgentLoop: a save lands
        # there, so the next probe reflects it with nothing to invalidate.
        entries = [(cfg, "config") for cfg in _as_configs(get_third_party_subagents())]
        entries += [(cfg, "preset") for cfg in _as_configs(third_party_subagent_presets())]
        verdicts = TestStateStore().load(entries)
        return {"results": [r.to_wire() for r in await probe_all(entries, verdicts=verdicts)]}
```

and have `_test` record its verdict, replacing the final `return` of the existing
handler:

```python
        cfg = _as_configs([match])[0]
        result = await run_test(cfg, source=source)
        # Recorded here rather than inside run_test so probe.py stays free of file
        # I/O and the store stays the single owner of persistence.
        TestStateStore().record(
            cfg, source, ok=result.ok, detail=result.detail, tested_at_ms=int(time.time() * 1000)
        )
        return {"result": result.to_wire()}
```

The unknown-name branch above it returns before this, so a name that matched
nothing records nothing.

- [ ] **Step 6: Mirror the field in the TS type**

In `ui-webui/frontend/src/api/ravenConfig.ts`, add to `RavenSubagentProbe`:

```ts
	/** The remembered outcome of an explicit test, when one is still valid for this
	 *  exact configuration. Null when never tested, or when the configuration has
	 *  changed since - a stale verdict is dropped rather than shown as current. */
	lastTest?: { ok: boolean; detail: string; testedAtMs: number } | null;
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_probe.py tests/test_web_rpc_config.py tests/test_subagent_test_state.py -q`
Expected: PASS.

Run: `uv run ruff check raven/ tests/` and `uv run ruff format --check raven/ tests/`
Expected: both clean.

From `ui-webui/`: `pnpm -C frontend lint` (0 errors) and `pnpm -C frontend build`.

- [ ] **Step 8: Commit**

```bash
git add raven/agent/subagent/probe.py raven/web_rpc/methods_config.py \
        ui-webui/frontend/src/api/ravenConfig.ts \
        tests/test_subagent_probe.py tests/test_web_rpc_config.py
git commit -m "$(cat <<'EOF'
feat(web_rpc): carry remembered test verdicts on the subagent probe

The probe answers "installed" for a cli agent and nothing more, so the page had
no way to show that a test had actually failed. Attach the remembered verdict to
each probe result and record a new one when a test runs. The merge happens in the
RPC layer so probe.py stays free of file I/O and the store keeps sole ownership of
persistence.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: status precedence and verdict age

**Files:**
- Modify: `ui-webui/frontend/src/components/SubagentStatus.tsx`
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`

**Interfaces:**
- Consumes: `RavenSubagentProbe.lastTest` from Task 3.
- Produces, for Task 5: `SubagentStatusDot` and `SubagentStatusLine` unchanged in signature; both now apply the precedence internally, so callers pass the probe and nothing else changes.

- [ ] **Step 1: Add the i18n keys**

In `en.json`, inside `subagent-sidebar`, after the existing `statusUnavailable`:

```json
		"statusTestFailed": "Test failed",
		"testedAgo": "tested {{age}} ago",
```

In `zh.json`, at the same place:

```json
		"statusTestFailed": "测试未通过",
		"testedAgo": "{{age}} 前测试过",
```

- [ ] **Step 2: Apply the precedence**

In `SubagentStatus.tsx`, add below the existing `LABEL_KEY` map:

```tsx
/** Colour and label for one probe, with a remembered test failure outranking a
 *  green probe: "which found it and it still cannot authenticate" is exactly the
 *  case the free probe gets wrong, and it is the whole reason verdicts persist.
 *  A `missing` or `unknown` probe still wins, because "not installed" is the more
 *  actionable fact and a verdict from before an uninstall says nothing useful. */
function effective(probe: RavenSubagentProbe): { dot: string; labelKey: string } {
	if (probe.status === 'missing' || probe.status === 'unknown') {
		return { dot: DOT[probe.status], labelKey: LABEL_KEY[probe.status] };
	}
	if (probe.lastTest && !probe.lastTest.ok) {
		return { dot: DOT.missing, labelKey: 'subagent-sidebar.statusTestFailed' };
	}
	return {
		dot: DOT[probe.status] ?? DOT.unknown,
		labelKey: LABEL_KEY[probe.status] ?? LABEL_KEY.unknown,
	};
}

/** Coarse age for a remembered verdict, so it is never read as fresh. The units
 *  are compact and language-neutral; the sentence around them is translated. */
function ageText(testedAtMs: number): string {
	const mins = Math.max(0, Math.round((Date.now() - testedAtMs) / 60000));
	if (mins < 1) return '<1m';
	if (mins < 60) return `${mins}m`;
	const hours = Math.round(mins / 60);
	if (hours < 24) return `${hours}h`;
	return `${Math.round(hours / 24)}d`;
}
```

Rewrite `SubagentStatusDot`'s body to use it:

```tsx
export function SubagentStatusDot({ probe }: { probe?: RavenSubagentProbe }) {
	const { t } = useTranslation();
	if (!probe) return null;
	const { dot, labelKey } = effective(probe);
	const label = t(labelKey);
	return (
		<span
			role="img"
			className={cn('size-1.5 shrink-0 rounded-full', dot)}
			title={`${label} - ${probe.detail}`}
			aria-label={label}
		/>
	);
}
```

In `SubagentStatusLine`, replace the dot's `className` and the label expression so
both come from `effective(probe)` when a probe exists, and add the verdict line
below the existing `detail` paragraph:

```tsx
			{probe?.lastTest && (
				<p className="text-muted-foreground mt-1 text-[11px]">
					{probe.lastTest.ok
						? t('subagent-sidebar.testPassed')
						: t('subagent-sidebar.testFailed')}
					{' - '}
					{t('subagent-sidebar.testedAgo', { age: ageText(probe.lastTest.testedAtMs) })}
					{!probe.lastTest.ok && `: ${probe.lastTest.detail}`}
				</p>
			)}
```

`testPassed` and `testFailed` already exist in both locales from the Test button
work - reuse them rather than adding near-duplicates.

- [ ] **Step 3: Run the gate**

From `ui-webui/`: `pnpm -C frontend lint` (0 errors; 17 pre-existing warnings) and
`pnpm -C frontend build`.

Run: `python3 -c "import json; [json.load(open(f'frontend/src/i18n/locales/{n}.json')) for n in ('en','zh')]; print('both parse')"`
Expected: `both parse`.

- [ ] **Step 4: Walk your own diff**

State in your report what the dot and the pane line render for each of: a cli
agent on PATH with no verdict; a cli agent on PATH whose last test failed; a cli
agent **not** on PATH whose last test failed (the precedence case); an openai
agent whose probe is `attention` with a passing verdict.

- [ ] **Step 5: Commit**

```bash
git add ui-webui/frontend/src/components/SubagentStatus.tsx \
        ui-webui/frontend/src/i18n/locales/en.json \
        ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "$(cat <<'EOF'
feat(ui-webui): let a remembered test failure outrank a green probe

An agent whose executable resolves but whose auth is broken looked identical to a
working one, because the free probe cannot see past which. Render a remembered
failure ahead of the probe's own verdict, and show its age so a stale result is
never read as fresh. A missing executable still wins: not-installed is the more
actionable fact.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Installed / Uninstalled / Custom groups

**Files:**
- Modify: `ui-webui/frontend/src/pages/subagent/catalog.ts` (new `installGroupOf`)
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx` (replace the two preset-facing groups)
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`

**Interfaces:**
- Consumes: `installGroupOf` (defined here), `probes` / `probesLoaded` from the hook, `RavenSubagentProbe.lastTest` from Task 3.
- Produces, for Task 6: a `presetRows` array of `{preset, configured, entry, source, probe, group}` that Task 6 attaches a switch to.

- [ ] **Step 1: Add the i18n keys**

In `en.json`, inside `subagent-sidebar`, after `groupPresets`:

```json
		"groupInstalled": "Installed",
		"groupUninstalled": "Uninstalled",
```

In `zh.json`, at the same place:

```json
		"groupInstalled": "已安装",
		"groupUninstalled": "未安装",
```

- [ ] **Step 2: Add the grouping rule**

In `catalog.ts`, add (and import the two types it needs from `@/api`):

```ts
/** Which install group a preset row belongs to, or `null` while unknown.
 *
 *  The cli rule reads the probe: `ready` means `argv[0]` resolved on the
 *  login-shell PATH. The openai rule reads `apiKey` off the entry instead, and
 *  deliberately not the probe's detail text: the probe only short-circuits on a
 *  blank key when the source is a preset, so a *configured* openai entry with no
 *  key is actually sent and comes back "api key not set or rejected" - a string
 *  that cannot tell "no key" from "key rejected", which are different groups and
 *  different user actions. */
export function installGroupOf(
	entry: RavenThirdPartySubagent,
	probe: RavenSubagentProbe | null,
): 'installed' | 'uninstalled' | null {
	if (entry.kind === 'openai') {
		return (entry.apiKey ?? '').trim() === '' ? 'uninstalled' : 'installed';
	}
	if (probe === null) return null;
	return probe.status === 'ready' ? 'installed' : 'uninstalled';
}
```

- [ ] **Step 3: Build the preset rows in `index.tsx`**

Take `probesLoaded` from the hook alongside the values already destructured, and
replace the `unconfiguredPresets` line with:

```tsx
	// A preset row stands for the user's configured entry when there is one, so its
	// group and its switch reflect the key they actually saved rather than the
	// template's blank. `source` follows, because the probe is keyed by it.
	const presetRows = presets.map((p) => {
		const configured = agents.find((a) => a.preset === p.name) ?? null;
		const entry = configured ?? p;
		const source: 'config' | 'preset' = configured ? 'config' : 'preset';
		const probe = probeOf(source, entry.name);
		return { preset: p, configured, entry, source, probe, group: installGroupOf(entry, probe) };
	});
	// Anything with no preset, plus anything whose preset names nothing we know
	// about. The second half is defence in depth: `_resolve_preset_provenance` in
	// raven/config/schema.py rejects an unknown preset value today, so the case
	// should be unreachable -- but that makes "every agent is visible somewhere"
	// depend on a rule enforced in a different layer, and the cost of being wrong
	// is a row the user can neither see nor delete.
	const customAgents = agents.filter(
		(a) => !a.preset || !presets.some((p) => p.name === a.preset),
	);
```

- [ ] **Step 4: Replace the two preset-facing groups**

Delete the whole `{agents.length > 0 && (<SidebarGroup> ... configured ... </SidebarGroup>)}`
block and the whole `{unconfiguredPresets.length > 0 && (...)}` block, and put in
their place a helper defined just above the `return`:

```tsx
	const presetGroup = (titleKey: string, rows: typeof presetRows) =>
		rows.length === 0 ? null : (
			<SidebarGroup key={titleKey}>
				<SidebarGroupLabel className="justify-between">
					<span>{t(titleKey)}</span>
					<span className="text-gold font-semibold tabular-nums">{rows.length}</span>
				</SidebarGroupLabel>
				<SidebarGroupContent>
					<SidebarMenu>
						{rows.map((row) => (
							<SidebarMenuItem key={row.preset.name}>
								<SidebarMenuButton
									isActive={editingName === row.entry.name}
									onClick={() =>
										row.configured
											? openEdit(row.configured)
											: addFromPreset(row.preset)
									}
								>
									<SubagentIcon type={row.preset.name} size={18} />
									<SubagentStatusDot probe={row.probe ?? undefined} />
									<span className="min-w-0 flex-1 truncate">
										{row.configured
											? rowLabel(row.configured)
											: presetLabel(row.preset.name, t)}
									</span>
								</SidebarMenuButton>
							</SidebarMenuItem>
						))}
					</SidebarMenu>
				</SidebarGroupContent>
			</SidebarGroup>
		);
```

and render, in place of the deleted blocks:

```tsx
							{/* Until probes land there is no cli install status, so every preset
							    stays in one group: splitting on partial information would make
							    rows jump between groups as results arrive. */}
							{!probesLoaded
								? presetGroup('subagent-sidebar.groupPresets', presetRows)
								: [
										presetGroup(
											'subagent-sidebar.groupInstalled',
											presetRows.filter((r) => r.group === 'installed'),
										),
										presetGroup(
											'subagent-sidebar.groupUninstalled',
											presetRows.filter((r) => r.group !== 'installed'),
										),
									]}
```

`r.group !== 'installed'` deliberately sweeps a still-`null` group into
Uninstalled once probes have loaded: after a successful probe fetch a cli preset
always has a result, so a `null` there means the row could not be verified, and
"not verified" belongs with "not usable".

- [ ] **Step 5: Add a Custom group listing hand-written agents**

The existing Custom group holds only the two "add" rows. Add the hand-written
agents above them, inside the same `SidebarMenu`:

```tsx
										{customAgents.map((sa) => (
											<SidebarMenuItem key={sa.name}>
												<SidebarMenuButton
													isActive={editingName === sa.name}
													onClick={() => openEdit(sa)}
												>
													<SubagentIcon type={sa.kind} size={18} />
													<SubagentStatusDot
														probe={probeOf('config', sa.name) ?? undefined}
													/>
													<span className="min-w-0 flex-1 truncate">
														{sa.name}
													</span>
												</SidebarMenuButton>
											</SidebarMenuItem>
										))}
```

Without this, deleting the Configured group would make hand-written agents
unreachable - the page would list presets and nothing else.

- [ ] **Step 6: Run the gate**

From `ui-webui/`: `pnpm -C frontend lint` (0 errors) and `pnpm -C frontend build`, plus
the JSON parse check on both locales.

- [ ] **Step 7: Walk your own diff**

State in your report which group each of these lands in, and why: `claude_code`
with its executable on PATH; `openclaw` not installed; `mirothinker` unconfigured
(blank key); `mirothinker` configured with a key; a hand-written `Coder`; and all
of them before the first probe returns.

- [ ] **Step 8: Commit**

```bash
git add ui-webui/frontend/src/pages/subagent/catalog.ts \
        ui-webui/frontend/src/pages/subagent/index.tsx \
        ui-webui/frontend/src/i18n/locales/en.json \
        ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "$(cat <<'EOF'
feat(ui-webui): group subagent presets by whether they can work

Configured and unconfigured was the wrong axis: what a user needs to know first
is whether an agent is usable at all. Split the presets into installed and
uninstalled, reading the probe for a cli agent and the stored key for an api one -
not the probe's detail text, which cannot tell a missing key from a rejected one.
Hand-written agents move into the Custom group, which now lists them rather than
only offering to add more.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: the enable switch

**Files:**
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx`
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`

**Interfaces:**
- Consumes: `presetRows` from Task 5, `enabled` from Task 1, `save` from the hook.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Add the i18n keys**

In `en.json`, inside `subagent-sidebar`, after `groupUninstalled`:

```json
		"enableLabel": "Offer this subagent to the model",
		"enableBlockedNotInstalled": "Not installed on this machine",
		"enableBlockedNoKey": "No API key configured",
```

In `zh.json`, at the same place:

```json
		"enableLabel": "把这个子智能体交给模型调用",
		"enableBlockedNotInstalled": "本机未安装",
		"enableBlockedNoKey": "未配置 API key",
```

- [ ] **Step 2: Add the toggle handler**

In `index.tsx`, import `Switch` from `@/components/ui/switch` and
`SidebarMenuAction` from `@/components/ui/sidebar`, then add beside the existing
state:

```tsx
	const [toggling, setToggling] = useState(false);
```

and the handler, after `remove`:

```tsx
	// Toggling writes the whole thirdParty list, so two overlapping writes would make
	// the later one carry a stale copy of the earlier. One at a time, with every
	// switch disabled while a write is in flight.
	const toggleEnabled = async (row: (typeof presetRows)[number], next: boolean) => {
		setToggling(true);
		try {
			const list = row.configured
				? agents.map((a) =>
						a.name === row.configured?.name ? { ...a, enabled: next } : a,
					)
				: // Switching on an unconfigured preset IS configuring it: the shipped
					// payload is already schema-shaped, so it needs no form round-trip.
					[...agents, { ...row.preset, preset: row.preset.preset ?? row.preset.name, enabled: true }];
			await save(list);
		} catch {
			// client.ts toasts; the switch snaps back because `agents` never changed
		} finally {
			setToggling(false);
		}
	};
```

Declaration order in the component body matters: `presetRows` (Task 5), then
`toggleEnabled` (so `typeof presetRows` resolves), then `presetGroup` (which calls
`toggleEnabled`), then the `return`.

- [ ] **Step 3: Render the switch in `presetGroup`**

Inside `presetGroup`'s `SidebarMenuItem`, as a **sibling** of `SidebarMenuButton`
and directly after it:

```tsx
								{/* A sibling, not a child: SidebarMenuButton is itself a <button>,
								    so nesting an interactive control is invalid HTML and swallows
								    the click. SidebarMenuAction is also what gives the row its
								    right-hand clearance, via
								    group-has-data-[sidebar=menu-action]/menu-item:pr-8. Its own
								    aspect-square w-5 would squash a switch, hence the overrides. */}
								<SidebarMenuAction
									asChild
									className="top-1/2 right-2 aspect-auto h-auto w-auto -translate-y-1/2 hover:bg-transparent"
								>
									<Switch
										size="sm"
										checked={row.entry.enabled ?? true}
										disabled={row.group !== 'installed' || toggling}
										onCheckedChange={(v) => void toggleEnabled(row, v)}
										aria-label={t('subagent-sidebar.enableLabel')}
										title={
											row.group === 'installed'
												? t('subagent-sidebar.enableLabel')
												: row.entry.kind === 'openai'
													? t('subagent-sidebar.enableBlockedNoKey')
													: t(
															'subagent-sidebar.enableBlockedNotInstalled',
														)
										}
									/>
								</SidebarMenuAction>
```

`row.entry.enabled ?? true` matches the backend default, so a configured entry
written before this field existed reads as on.

- [ ] **Step 4: Give the Custom rows the same switch**

Hand-written agents need it too - the gate is global, and a custom agent with no
switch would be permanently enabled while its neighbours can be turned off. In the
Custom group's mapped rows, add after the `SidebarMenuButton`:

```tsx
												<SidebarMenuAction
													asChild
													className="top-1/2 right-2 aspect-auto h-auto w-auto -translate-y-1/2 hover:bg-transparent"
												>
													<Switch
														size="sm"
														checked={sa.enabled ?? true}
														disabled={toggling}
														onCheckedChange={(v) => {
															setToggling(true);
															void save(
																agents.map((a) =>
																	a.name === sa.name
																		? { ...a, enabled: v }
																		: a,
																),
															).finally(() => setToggling(false));
														}}
														aria-label={t('subagent-sidebar.enableLabel')}
														title={t('subagent-sidebar.enableLabel')}
													/>
												</SidebarMenuAction>
```

A custom agent is never in Uninstalled, so its switch has no blocked state.

- [ ] **Step 5: Run the gate**

From `ui-webui/`: `pnpm -C frontend lint` (0 errors) and `pnpm -C frontend build`, plus
the JSON parse check on both locales.

- [ ] **Step 6: Walk your own diff**

State in your report: what switching on an unconfigured `codex` writes to config;
what switching off a configured `claude_code` writes; why `openclaw`'s switch is
disabled when it is not installed; and what stops two rapid toggles racing.

- [ ] **Step 7: Commit**

```bash
git add ui-webui/frontend/src/pages/subagent/index.tsx \
        ui-webui/frontend/src/i18n/locales/en.json \
        ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "$(cat <<'EOF'
feat(ui-webui): add an enable switch to every subagent row

Switching on an unconfigured preset now configures it: the shipped payload is
already schema-shaped, so the four agents that need no input need no form round
trip either. Switching off keeps the entry, so a name, a key and command edits all
survive. An uninstalled row's switch is disabled with the reason in its title,
because an agent that cannot work should not be offered to the model, and every
switch is disabled during a write since each one replaces the whole list.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Final gate (after Task 6)

- [ ] `uv run pytest -q` - expect only the two pre-existing failures named in Global Constraints.
- [ ] `uv run ruff check raven/ ui-webui/service/ tests/` and `uv run ruff format --check raven/ tests/`
- [ ] From `ui-webui/`: `pnpm -C frontend lint` and `pnpm -C frontend build`
- [ ] `grep -nP "[^\x00-\x7F]"` over each new commit message body - must be silent
- [ ] **Report that browser verification needs the gateway restarted.** The gateway holds the RPC table in memory, so the new `lastTest` field and the `enabled` filter only take effect after a restart. Do not restart the user's live stack without asking.
