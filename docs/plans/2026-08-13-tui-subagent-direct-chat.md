# TUI Sub-agent Direct Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user switch from the main Raven conversation into a direct chat with any sub-agent instance this session has used, and hand the main agent a pointer block - times and file paths - on the next turn.

**Architecture:** A direct-chat turn rides the existing turn pipeline through a new `TurnRequest.direct_target` field, so it inherits the one-turn-per-session slot, the subscription emitter, and the lane's write serialization; it dispatches to a new `SubagentManager.chat()` instead of the model, and writes a `SpawnRecord`-shaped audit directory instead of the main transcript. Resumption is per-kind: `cli` instances resume through `resume_command` plus the registry's `agentId`, while `openai` and `raven-loop` instances get a Raven-owned `messages.json` replay they do not have today. The handoff is held by the runtime, not the client.

**Tech Stack:** Python 3.12+ / pydantic v2 / `raven.rpc` dispatcher / loguru; TypeScript / React / Ink (`@hermes/ink`) / nanostores; contract-first OpenRPC codegen.

**Spec:** `docs/specs/2026-08-13-tui-subagent-direct-chat-design.md`

## Global Constraints

- Branch: `feat/tui_subagent_direct_chat`, based on `origin/main` at `84884632`. Never commit to `main`, never `git push` unprompted, never `git commit --amend`, never `git merge origin/main` (rebase only), never create a git worktree.
- Python deps via `uv` only, never `pip`. JS deps in `ui-tui/` via `npm` (that package uses npm - `package-lock.json`).
- Run tests as `uv run pytest ...`, never bare `pytest`.
- Commit messages: Conventional Commits, all-English, ASCII-only (no em-dash, curly quotes, ellipsis, section-sign numbering). Header <= 100 chars. Trailer: `Co-authored-by: Claude (<actual-session-model-id>) <noreply@anthropic.com>`.
- Do not commit beyond the per-task commits this plan specifies, and do not push.
- `ui-tui/src/rpc/generated.ts` is generated. Never hand-edit it; run `npm run gen:rpc`.
- `rpc-schema/openrpc.json` is the single source of truth for the RPC contract. `tests/test_rpc_schema_match.py` fails until `models.py`, `METHOD_MODELS` and the schema agree, so all three move in the same commit.
- Comments: English only, and only where the logic is non-obvious (repo `CLAUDE.md` section 1). Do not add comments that restate the code or mark edits.
- Prettier in `ui-tui`: run `npm run fmt` before committing TS changes.
- Test file naming follows `CLAUDE.md` section 5: extend the existing `tests/test_<module>.py` for a changed module; never add a phase or ticket suffix.
- New domain terms must be defined in the matching `CONTEXT.md` in the same change (`CLAUDE.md` section 6). This feature coins three: **Direct Chat**, **Instance Chip**, **Handoff Block**.
- No files over 1 MiB; run `make check-large-files` if any asset is touched.

## Base-line hazard (read before Task 1)

`hold_handle` and `CliAgentBackend._run_stateful` do **not** exist on `origin/main`. They exist only on an unpushed local integration branch (`19a575a5` and `854bebd9` respectively, reachable as `main` in this clone). Task 1 ports them verbatim.

Two consequences:

- **Port, do not improve.** When those local commits eventually reach `origin/main`, a verbatim copy makes the rebase a delete-one-copy conflict. A reimplementation makes it a semantic merge, which is how two subtly different per-handle locks end up coexisting.
- **The port is partial on purpose.** `854bebd9` also removes the `provider` and `model` kwargs from `CliAgentBackend.run`. `origin/main` still declares them on the `SubagentBackend` protocol (`raven/agent/subagent/backends/base.py:49-59`) and still passes them at `raven/agent/subagent/manager.py:363`. **Keep those kwargs.** Dropping them breaks the protocol and every other backend's call site.

## File Structure

New files:

| Path | Responsibility |
|---|---|
| `raven/agent/subagent/instance_state.py` | The Raven-owned `messages.json` for one instance: load, save, atomic replace. Nothing else knows the file layout. |
| `raven/agent/subagent/direct_chat.py` | `DirectChatRecord` (the `direct/<agent>/<handle>/<call_id>/` audit directory) and `DirectChatHandoff` (the per-session pending list plus its block renderer). |
| `raven/rpc/methods/instances.py` | The four `subagents.instance*` RPC handlers. Kept out of `methods/subagents.py`, which is about *configuring* sub-agents; this file is about *live instances*. |
| `ui-tui/src/app/directChatStore.ts` | The `$directChat` nanostore: active target, instance rows, per-instance transcripts and scroll offsets. |
| `ui-tui/src/components/instanceChips.tsx` | The chip strip above the composer. |

Modified files, and why each is touched:

| Path | Change |
|---|---|
| `raven/agent/subagent/instances.py` | port `hold_handle` |
| `raven/agent/subagent/backends/cli_agent.py` | port `_run_stateful` + `hold_handle` wrapper (keeping `provider`/`model`) |
| `raven/agent/subagent/backends/raven_loop.py` | lift `messages` out of `_run` so it can be injected and persisted |
| `raven/agent/subagent/backends/openai_api.py` | replay history instead of starting empty |
| `raven/agent/subagent/backends/__init__.py` | `third_party_agent_meta` becomes kind-aware |
| `raven/config/schema.py` | `_reject_declared_stateful` allows `stateful` on the `openai` kind |
| `raven/agent/subagent/manager.py` | registry rows for `raven-loop`; new `chat()`; persist `messages.json` on spawn completion |
| `raven/agent/tools/spawn.py` | `_reject_useless_instance` stops rejecting a handle on a default Raven sub-agent |
| `raven/spine/turn.py` | new `direct_target` field |
| `raven/agent/loop/main.py` | `direct_target` branch; handoff prepend on ordinary turns |
| `raven/rpc/models.py` | params/result models for the new methods; `target` on `TurnSendParams`; `METHOD_MODELS` entries |
| `rpc-schema/openrpc.json` | the same four methods and the `turn.send` change |
| `raven/rpc/methods/turn.py` | pass `target` through to `TurnRequest`; tag emitted events |
| `raven/rpc/server.py` | register the new method group |
| `ui-tui/src/app/turnController.ts` | route target-tagged events to the direct transcript |
| `ui-tui/src/components/appLayout.tsx` | mount `InstanceChips`; swap the ChatStream source in direct mode |
| `ui-tui/src/app/useInputHandlers.ts` | Esc returns to main; Ctrl+Left/Right cycle chips |
| `CONTEXT.md`, `ui-tui/CONTEXT.md` | the three new domain terms |

---

## Phase 1 - Prerequisite and persistence

Deliverable at end of phase: every instance kind has a durable, resumable session, and `raven-loop` instances appear in the registry. No UI, no RPC. Verifiable purely by `uv run pytest`.

### Task 1: Port `hold_handle` and `_run_stateful`

**Files:**
- Modify: `raven/agent/subagent/instances.py` (append before `__all__`)
- Modify: `raven/agent/subagent/backends/cli_agent.py:189-210`
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `hold_handle(session_key: str, agent: str, handle: str) -> AsyncIterator[None]` (an `@asynccontextmanager`), exported from `raven.agent.subagent.instances`; `CliAgentBackend._run_stateful(task, task_id, cwd, skey, handle, *, resumable: bool) -> str`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_subagent_third_party.py`:

```python
import asyncio

import pytest

from raven.agent.subagent.instances import hold_handle


@pytest.mark.asyncio
async def test_hold_handle_serializes_same_handle():
    order: list[str] = []

    async def worker(tag: str) -> None:
        async with hold_handle("s1", "Raven-Code", "refactor"):
            order.append(f"{tag}-in")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-out")

    await asyncio.gather(worker("a"), worker("b"))

    # Neither critical section may interleave with the other.
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )


@pytest.mark.asyncio
async def test_hold_handle_does_not_block_a_different_handle():
    entered = asyncio.Event()
    released = asyncio.Event()

    async def holder() -> None:
        async with hold_handle("s1", "Raven-Code", "one"):
            entered.set()
            await released.wait()

    async def other() -> None:
        await entered.wait()
        async with hold_handle("s1", "Raven-Code", "two"):
            released.set()

    await asyncio.wait_for(asyncio.gather(holder(), other()), timeout=1.0)


@pytest.mark.asyncio
async def test_hold_handle_frees_its_map_entry():
    from raven.agent.subagent.instances import _handle_locks

    async with hold_handle("s1", "A", "h"):
        assert ("s1", "A", "h") in _handle_locks
    assert ("s1", "A", "h") not in _handle_locks
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_third_party.py -k hold_handle -v`
Expected: FAIL - `ImportError: cannot import name 'hold_handle'`

- [ ] **Step 3: Port `hold_handle` verbatim**

In `raven/agent/subagent/instances.py`, extend the stdlib imports:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
```

Then append, immediately before the existing `__all__` line:

```python
@dataclass
class _HeldLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_handle_locks: dict[_Key, _HeldLock] = {}


@asynccontextmanager
async def hold_handle(session_key: str, agent: str, handle: str) -> AsyncIterator[None]:
    """Serialize everything that resumes one stateful instance handle.

    A handle maps to a single session inside the CLI's own store; two runs
    resuming it at once interleave that session's transcript or corrupt it.
    Process-wide and keyed by the handle rather than held on a backend object,
    because the DAG tool and the sub-agent manager build *separate* backends
    from the same config -- a per-backend lock would let a spawn and a DAG node
    resume the same session side by side.

    The refcount is taken before the lock is awaited, so a waiter keeps the
    entry alive and the map never strands a lock two callers disagree about.
    """
    key = (session_key, agent, handle)
    entry = _handle_locks.get(key)
    if entry is None:
        entry = _handle_locks[key] = _HeldLock()
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0:
            _handle_locks.pop(key, None)
```

And replace the `__all__` line with:

```python
__all__ = ["InstanceRegistry", "get_registry", "default_registry_path", "hold_handle"]
```

- [ ] **Step 4: Extract `_run_stateful` in `CliAgentBackend`**

In `raven/agent/subagent/backends/cli_agent.py`, add `hold_handle` to the existing instances import:

```python
from raven.agent.subagent.instances import InstanceRegistry, get_registry, hold_handle
```

Replace the `if self.is_stateful:` block inside `run` (currently lines 207-231, the inline lookup/resume/mint sequence) with:

```python
        if self.is_stateful:
            # Held across lookup, run and commit: the whole sequence is what
            # binds a handle to one CLI session, and a concurrent spawn or DAG
            # node on the same handle would otherwise resume that session
            # side by side. See ``hold_handle``.
            async with hold_handle(skey, self.name, handle):
                return await self._run_stateful(task, task_id, cwd, skey, handle, resumable=instance is not None)
```

Do **not** touch the `provider: LLMProvider | None = None` / `model: str | None = None` kwargs on `run`, nor the `TYPE_CHECKING` import that types them.

Add the extracted method immediately after `run`:

```python
    async def _run_stateful(self, task: str, task_id: str, cwd: str, skey: str, handle: str, *, resumable: bool) -> str:
        """Resume this handle's session, or mint one. Caller holds its lock.

        Only an explicitly named ``instance`` resumes. Without one the handle
        falls back to ``task_id``, which for a DAG node is its author-chosen id
        -- so a later graph with a node called ``research`` would otherwise pick
        up an earlier graph's session, having asked for nothing of the sort. (A
        spawn's ``task_id`` is a fresh uuid, so nothing there could ever match
        anyway.) The binding is still committed: nothing looks it up now, but it
        is what records which session a node actually ran in.
        """
        existing = await self._registry.lookup(skey, self.name, handle) if resumable else None
        if existing is not None:
            try:
                return await self._attempt(
                    task,
                    task_id,
                    cwd,
                    agent_id=existing,
                    template=self.resume_command or self.command,
                    created=False,
                    skey=skey,
                    handle=handle,
                )
            except (CliAgentTimeoutError, CliAgentReportedError):
                # Neither is evidence the CLI's session store pruned this id: a
                # timeout may just mean the run was slow, and a transcript-level
                # error can happen inside a perfectly valid session. Forgetting
                # the handle here would discard a valid binding for nothing.
                raise
            except Exception:  # noqa: BLE001 - a hard non-zero exit: the CLI's own session store may have pruned this id
                logger.warning(
                    "Subagent [{}] resume of {}/{!r} failed; forgetting the stale handle and "
                    "retrying once as a fresh create",
                    task_id,
                    self.name,
                    handle,
                )
                await self._registry.forget(skey, self.name, handle)
        # Minted independently of `handle`: a CLI constrains its session
        # id (claude rejects a non-UUID) while a handle is free-form.
        agent_id = None if self.id_source == "derived" else str(uuid.uuid4())
        return await self._attempt(
            task, task_id, cwd, agent_id=agent_id, template=self.command, created=True, skey=skey, handle=handle
        )
```

- [ ] **Step 5: Add the `resumable` gate test**

Append to `tests/test_subagent_third_party.py`:

```python
@pytest.mark.asyncio
async def test_unnamed_instance_does_not_resume(monkeypatch, tmp_path):
    """A spawn with no `instance` must mint a session, never reuse task_id's."""
    from raven.agent.subagent.backends.cli_agent import CliAgentBackend
    from raven.agent.subagent.instances import InstanceRegistry

    registry = InstanceRegistry(tmp_path / "reg.json")
    await registry.commit("s1", "fake", "task-1234", "stale-session-id")

    seen: list[str | None] = []

    backend = CliAgentBackend(
        name="fake",
        command="true --prompt {prompt}",
        resume_command="true --resume {agent_id} --prompt {prompt}",
        registry=registry,
    )

    async def fake_attempt(task, task_id, cwd, *, agent_id, template, created, skey, handle):
        seen.append(agent_id)
        return "ok"

    monkeypatch.setattr(backend, "_attempt", fake_attempt)

    await backend.run("t", task_id="task-1234", workspace=tmp_path, executor=None, session_key="s1")

    assert seen == [None] or seen[0] != "stale-session-id"
```

If `CliAgentBackend.__init__` on this branch does not accept exactly these kwargs, read its signature at `raven/agent/subagent/backends/cli_agent.py:61` and adjust the constructor call only - the assertion is the point.

- [ ] **Step 6: Run the full sub-agent suite**

Run: `uv run pytest tests/test_subagent_third_party.py tests/test_subagent_manager.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add raven/agent/subagent/instances.py raven/agent/subagent/backends/cli_agent.py tests/test_subagent_third_party.py
git commit -m "refactor(subagent): serialize stateful handle resumption behind one lock"
```

---

### Task 2: Registry rows for `raven-loop` instances

**Files:**
- Modify: `raven/agent/subagent/manager.py:238` (the `if agent:` gate) and the `_write_spawn_status` helper at `raven/agent/subagent/manager.py:41`
- Modify: `raven/agent/tools/spawn.py:127` (`_reject_useless_instance`)
- Test: `tests/test_subagent_manager.py`

**Interfaces:**
- Consumes: `hold_handle` from Task 1 (not called here, but the module now exports it).
- Produces: the module constant `RAVEN_LOOP_AGENT = "raven"` in `raven/agent/subagent/manager.py`, imported by Tasks 3, 6 and 10.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_subagent_manager.py`:

```python
RAVEN_ROW = ("s1", "raven", "notes")


@pytest.mark.asyncio
async def test_default_subagent_gets_a_registry_row(tmp_path, monkeypatch):
    from raven.agent.subagent import manager as manager_mod
    from raven.agent.subagent.instances import InstanceRegistry

    registry = InstanceRegistry(tmp_path / "reg.json")
    monkeypatch.setattr(manager_mod, "get_registry", lambda: registry)

    await manager_mod._write_spawn_status("s1", None, "notes", "running")

    rows = registry.list_instances("s1")
    assert [(r["sessionKey"], r["agent"], r["handle"]) for r in rows] == [RAVEN_ROW]
    # `upsert_spawn` hardcodes kind="cli" (instances.py:115). Semantically off for
    # the built-in sub-agent, but deliberately unchanged: the web RPC's
    # reconciliation branches on kind == "cli" to decide whether to consult
    # live_handles, and a new kind would silently stop reconciling these rows.
    assert rows[0]["kind"] == "cli"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_manager.py -k registry_row -v`
Expected: FAIL - `assert [] == [('s1', 'raven', 'notes')]` (the helper returns early when `agent` is falsy)

- [ ] **Step 3: Give the built-in sub-agent a name**

In `raven/agent/subagent/manager.py`, add near the existing module constants (beside `_SPAWN_WINDOW_SECONDS`):

```python
# The reserved instance-registry name for the built-in in-process sub-agent.
# A default spawn has no third-party agent name, but it still needs an identity
# for a direct chat to address it by.
RAVEN_LOOP_AGENT = "raven"
```

Rewrite `_write_spawn_status` so a missing `agent` resolves to that name instead of dropping the row:

```python
async def _write_spawn_status(session_key: str | None, agent: str | None, handle: str, status: str) -> None:
    """Best-effort registry write for one spawn's status, swallowing any failure."""
    if not session_key:
        return
    try:
        await asyncio.wait_for(
            get_registry().upsert_spawn(session_key, agent or RAVEN_LOOP_AGENT, handle, status),
            timeout=_REGISTRY_WRITE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - a status row must never fail or hang a spawn
        logger.opt(exception=True).warning(
            "Subagent instance registry write failed for {}/{!r} (status={})",
            agent or RAVEN_LOOP_AGENT,
            handle,
            status,
        )
```

Delete the docstring paragraph that says only third-party spawns get a row - it is now false, and a stale comment is worse than none.

At `raven/agent/subagent/manager.py:238`, replace the `if agent:` gate around the pre-dispatch row with an unconditional write:

```python
        await _write_spawn_status(session_key, agent, handle, "pending")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_subagent_manager.py -k registry_row -v`
Expected: PASS

- [ ] **Step 5: Let `spawn` accept a handle for the built-in sub-agent**

`_reject_useless_instance` (`raven/agent/tools/spawn.py:127`) currently rejects `instance` whenever `agent is None`, on the grounds that a default Raven sub-agent has no resumable session. After Task 3 it does. Replace the `if agent is None:` branch body with a pass-through:

```python
        if agent is None:
            # A default Raven sub-agent is resumable now: its transcript is
            # persisted per handle (raven/agent/subagent/instance_state.py).
            return None
```

Leave the stateless-third-party branch below it untouched: an `openai` entry without `stateful` still cannot resume.

- [ ] **Step 6: Update the spawn-tool test**

In `tests/test_subagent_manager.py` (or wherever `_reject_useless_instance` is currently asserted - find it with `uv run pytest --collect-only -q | grep -i useless`), change the "no agent named" case from expecting an error string to expecting `None`. Add:

```python
def test_instance_is_accepted_for_the_default_subagent():
    tool = SpawnTool(manager=_stub_manager())
    assert tool._reject_useless_instance(None, "refactor-auth") is None
```

- [ ] **Step 7: Run the suite**

Run: `uv run pytest tests/test_subagent_manager.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add raven/agent/subagent/manager.py raven/agent/tools/spawn.py tests/test_subagent_manager.py
git commit -m "feat(subagent): give the built-in sub-agent an addressable instance identity"
```

---

### Task 3: `messages.json` for Raven-owned instances

**Files:**
- Create: `raven/agent/subagent/instance_state.py`
- Modify: `raven/agent/subagent/backends/raven_loop.py:186` (lift `messages`)
- Test: `tests/test_subagent_direct_chat.py` (new file)

**Interfaces:**
- Consumes: `RAVEN_LOOP_AGENT` from Task 2.
- Produces:
  - `InstanceState(path: Path)` with `load() -> list[dict]`, `save(messages: list[dict]) -> None`
  - `instance_state_path(session_dir: Path, agent: str, handle: str) -> Path`
  - `RavenLoopBackend.run(..., history: list[dict] | None = None, on_messages: Callable[[list[dict]], None] | None = None)`

- [ ] **Step 1: Write the failing test**

Create `tests/test_subagent_direct_chat.py`:

```python
"""Direct-chat resumption: the Raven-owned message store and its replay."""

from __future__ import annotations

import json

import pytest

from raven.agent.subagent.instance_state import InstanceState, instance_state_path


def test_state_path_is_scoped_by_agent_and_handle(tmp_path):
    p = instance_state_path(tmp_path, "Raven-Code", "refactor-auth")
    assert p == tmp_path / "subagents" / "direct" / "Raven-Code" / "refactor-auth" / "messages.json"


def test_load_of_a_missing_file_is_empty(tmp_path):
    assert InstanceState(tmp_path / "nope.json").load() == []


def test_save_then_load_roundtrips(tmp_path):
    state = InstanceState(tmp_path / "messages.json")
    msgs = [{"role": "system", "content": "you are"}, {"role": "user", "content": "hi"}]
    state.save(msgs)
    assert state.load() == msgs


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    state = InstanceState(tmp_path / "messages.json")
    state.save([{"role": "user", "content": "a"}])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["messages.json"]


def test_load_of_corrupt_json_is_empty_not_an_error(tmp_path):
    p = tmp_path / "messages.json"
    p.write_text("{ truncated", encoding="utf-8")
    assert InstanceState(p).load() == []


def test_load_rejects_a_non_list_document(tmp_path):
    p = tmp_path / "messages.json"
    p.write_text(json.dumps({"role": "user"}), encoding="utf-8")
    assert InstanceState(p).load() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_direct_chat.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'raven.agent.subagent.instance_state'`

- [ ] **Step 3: Write the module**

Create `raven/agent/subagent/instance_state.py`:

```python
"""The Raven-owned conversation state for one sub-agent instance.

A ``cli`` instance's session lives inside the CLI's own store and the registry
keeps only its id. The built-in in-process sub-agent and an OpenAI-compatible
HTTP agent have no such store, so Raven keeps their message list here and
replays it on the next turn -- which is what makes those two kinds resumable.

Sits beside the audit record (``raven/agent/subagent_history.py``) but is a
different thing: this file is mutable resume state and gets overwritten, while
a record directory is append-only evidence. Keeping them in one file would let
a single interrupted write destroy both.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.helpers import safe_path_segment

_FILENAME = "messages.json"


def instance_state_path(session_dir: Path, agent: str, handle: str) -> Path:
    """Where one instance's message list lives. Not created here.

    Both segments go through ``safe_path_segment``: a handle is free-form text
    chosen by the model, so it is the one component here that could otherwise
    escape the directory.
    """
    return (
        Path(session_dir)
        / "subagents"
        / "direct"
        / safe_path_segment(agent)
        / safe_path_segment(handle)
        / _FILENAME
    )


class InstanceState:
    """One instance's message list, persisted as a single JSON array."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        """The stored messages, or an empty list.

        Every failure degrades to empty rather than raising: a missing file is
        the normal first-turn case, and a corrupt one must cost the user this
        instance's memory, not the turn they are trying to send.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            logger.warning("Instance state at {} is not a list; ignoring it", self._path)
            return []
        return [m for m in raw if isinstance(m, dict)]

    def save(self, messages: list[dict[str, Any]]) -> None:
        """Replace the stored messages atomically."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("Instance state at {} could not be written: {}", self._path, exc)


__all__ = ["InstanceState", "instance_state_path"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_subagent_direct_chat.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for `RavenLoopBackend` replay**

Append to `tests/test_subagent_direct_chat.py`:

```python
@pytest.mark.asyncio
async def test_raven_loop_replays_injected_history(tmp_path):
    """A second turn must see the first turn's messages, not a fresh prompt."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    seen: list[list[dict]] = []
    captured: list[list[dict]] = []

    class StubProvider:
        def get_default_model(self):
            return "stub"

        async def chat_with_retry(self, *, messages, tools, model):
            seen.append(list(messages))
            return _no_tool_response("done")

    backend = RavenLoopBackend(provider=StubProvider(), model="stub", agent_home=tmp_path)

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-answer"},
    ]

    await backend.run(
        "second",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        history=history,
        on_messages=captured.append,
    )

    # The provider saw the prior turns, and the system prompt was not rebuilt.
    assert seen[0][:3] == history
    assert seen[0][-1] == {"role": "user", "content": "second"}
    # The backend handed back the full list for persistence, ending in the reply.
    assert captured[-1][-1]["role"] == "assistant"
    assert captured[-1][-1]["content"] == "done"
```

Add this helper near the top of the file, matching whatever response object `provider.chat_with_retry` returns on this branch (read `raven/providers/base.py` for the type, and copy the construction that `tests/test_subagent_manager.py` already uses for its stub provider):

```python
def _no_tool_response(text: str):
    """A provider response with no tool calls, ending the sub-agent loop."""
    from types import SimpleNamespace

    return SimpleNamespace(
        content=text,
        has_tool_calls=False,
        tool_calls=[],
        reasoning_content=None,
        thinking_blocks=None,
    )
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_direct_chat.py -k raven_loop_replays -v`
Expected: FAIL - `TypeError: run() got an unexpected keyword argument 'history'`

- [ ] **Step 7: Lift `messages` out of `RavenLoopBackend._run`**

In `raven/agent/subagent/backends/raven_loop.py`, thread two new keyword-only parameters through `run` and `_run` (both currently take `task, *, task_id, workspace, executor, session_key=None, instance=None`):

```python
        history: list[dict[str, Any]] | None = None,
        on_messages: Callable[[list[dict[str, Any]]], None] | None = None,
```

Add `Callable` to the `collections.abc` import.

Then replace the message seeding at `raven/agent/subagent/backends/raven_loop.py:186`:

```python
        # A resumed instance brings its own history, system prompt included;
        # rebuilding the prompt here would append a second system turn.
        messages: list[dict[str, Any]] = list(history) if history else [
            {"role": "system", "content": build_subagent_prompt(self.agent_home, workspace, tools.tool_names)}
        ]
        messages.append({"role": "user", "content": task})
```

At the end of `_run`, immediately before `return final_result`, hand the list back:

```python
        if on_messages is not None:
            messages.append({"role": "assistant", "content": final_result})
            on_messages(messages)
```

`on_messages` rather than a direct write: the backend must not know where the state file lives, so the same backend still works for a plain spawn that persists nothing.

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_subagent_direct_chat.py -v`
Expected: PASS

- [ ] **Step 9: Confirm no regression in the plain spawn path**

Run: `uv run pytest tests/test_subagent_manager.py tests/test_subagent_workdir.py -v`
Expected: PASS - a spawn passing neither `history` nor `on_messages` behaves exactly as before.

- [ ] **Step 10: Commit**

```bash
git add raven/agent/subagent/instance_state.py raven/agent/subagent/backends/raven_loop.py tests/test_subagent_direct_chat.py
git commit -m "feat(subagent): persist and replay the built-in sub-agent's conversation"
```

---

### Task 4: OpenAI-kind replay and a kind-aware `stateful`

**Files:**
- Modify: `raven/agent/subagent/backends/openai_api.py:60`
- Modify: `raven/agent/subagent/backends/__init__.py:40` (`third_party_agent_meta`)
- Modify: `raven/config/schema.py:1040` (`_reject_declared_stateful`)
- Test: `tests/test_subagent_third_party.py`, `tests/test_config_raven_sections.py`

**Interfaces:**
- Consumes: `InstanceState` from Task 3 (not called here; the backend takes `history`/`on_messages` like `RavenLoopBackend`).
- Produces: `OpenAIApiBackend.run(..., history=None, on_messages=None)`; `third_party_agent_meta` returning `stateful=True` for an `openai` config that declares it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_subagent_third_party.py`:

```python
def test_openai_kind_is_stateful_when_declared():
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    cfg = SubagentsConfig(
        third_party=[
            {
                "kind": "openai",
                "name": "mirothinker",
                "baseUrl": "http://localhost:8000/v1",
                "model": "miro",
                "stateful": True,
            }
        ]
    ).third_party[0]

    assert third_party_agent_meta(cfg).stateful is True


def test_openai_kind_is_stateless_by_default():
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    cfg = SubagentsConfig(
        third_party=[
            {
                "kind": "openai",
                "name": "mirothinker",
                "baseUrl": "http://localhost:8000/v1",
                "model": "miro",
            }
        ]
    ).third_party[0]

    assert third_party_agent_meta(cfg).stateful is False


def test_cli_kind_still_derives_stateful_from_resume_command():
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    entries = SubagentsConfig(
        third_party=[
            {"kind": "cli", "name": "plain", "command": "true --prompt {prompt}"},
            {
                "kind": "cli",
                "name": "resumable",
                "command": "true --session {agent_id} --prompt {prompt}",
                "resumeCommand": "true --resume {agent_id} --prompt {prompt}",
                "idSource": "provisioned",
            },
        ]
    ).third_party

    assert third_party_agent_meta(entries[0]).stateful is False
    assert third_party_agent_meta(entries[1]).stateful is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k stateful -v`
Expected: FAIL - the `stateful=True` openai config raises `ValueError: stateful is not supported for kind 'openai'` during validation.

- [ ] **Step 3: Relax the schema validator**

In `raven/config/schema.py`, replace the body of `_reject_declared_stateful` (at line 1040) so it becomes a no-op returning `self`, and rename it to reflect what it now does. Keep the field itself:

```python
    @model_validator(mode="after")
    def _allow_replayed_state(self) -> "ThirdPartyOpenAISubagentConfig":
        """``stateful`` is honoured for this kind now.

        It used to be rejected on the grounds that an HTTP agent has no
        resumable session. It has one now, but Raven owns it: the message list
        is replayed from ``raven/agent/subagent/instance_state.py`` rather than
        resumed by the provider, so no ``resumeCommand`` is involved and there
        is nothing for the cli-kind cross-check to verify here.
        """
        return self
```

Update the neighbouring docstring that references `_reject_declared_stateful` by name (`raven/config/schema.py:1059`) to the new name, or the comment lies.

- [ ] **Step 4: Make `third_party_agent_meta` kind-aware**

In `raven/agent/subagent/backends/__init__.py`, replace the `stateful` derivation inside `third_party_agent_meta`:

```python
def third_party_agent_meta(cfg: Any) -> AgentMeta:
    """The advertised capabilities of one third-party subagent config.

    "Stateful" means reusing an instance handle continues that agent's
    conversation instead of starting a fresh one, and the two kinds get there
    differently: a cli agent resumes its own session, so the capability is
    exactly whether a ``resume_command`` exists; an openai agent has no session
    to resume, so Raven replays the message list itself and the capability is
    whatever the config opted into. Defined once here because three callers
    derive it -- the spawn manager, the DAG tool's roster, and the DAG
    capability pre-check -- and a split definition would let them disagree.
    """
    if getattr(cfg, "kind", None) == "openai":
        stateful = bool(getattr(cfg, "stateful", False))
    else:
        stateful = bool(getattr(cfg, "resume_command", None))
    return AgentMeta(
        getattr(cfg, "name", "") or "",
        getattr(cfg, "description", "") or "",
        stateful,
        bool(getattr(cfg, "reads_local_files", True)),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -k stateful -v`
Expected: PASS

- [ ] **Step 6: Write the failing test for OpenAI replay**

Append to `tests/test_subagent_direct_chat.py`:

```python
@pytest.mark.asyncio
async def test_openai_backend_replays_history(monkeypatch, tmp_path):
    from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

    sent: list[dict] = []

    async def fake_post(url, *, json, headers, timeout=None):
        sent.append(json)
        return {"choices": [{"message": {"content": "reply"}}]}

    backend = OpenAIApiBackend(
        name="mirothinker",
        base_url="http://localhost:8000/v1",
        model="miro",
        system_prompt="sys",
    )
    monkeypatch.setattr(backend, "_post_chat", fake_post)

    captured: list[list[dict]] = []
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first-answer"},
    ]

    out = await backend.run(
        "second",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        history=history,
        on_messages=captured.append,
    )

    assert out == "reply"
    assert sent[0]["messages"][:3] == history
    assert sent[0]["messages"][-1] == {"role": "user", "content": "second"}
    assert captured[-1][-1] == {"role": "assistant", "content": "reply"}
```

- [ ] **Step 7: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_direct_chat.py -k openai_backend_replays -v`
Expected: FAIL - `TypeError: run() got an unexpected keyword argument 'history'`

- [ ] **Step 8: Add replay to `OpenAIApiBackend`**

In `raven/agent/subagent/backends/openai_api.py`, extract the aiohttp call into a `_post_chat` seam so the test above can stub it without patching aiohttp, then thread `history` / `on_messages` through `run` with the same signature shape as `RavenLoopBackend`. Replace the seeding at line 60:

```python
        messages: list[dict[str, Any]] = list(history) if history else []
        if not messages and self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": task})
```

Note the `not messages` guard: a resumed history already carries the system turn, and appending a second one is how a replayed conversation slowly acquires one system message per turn.

After the reply is extracted, before returning:

```python
        reply = str(content).strip()[: self.max_output_chars]
        if on_messages is not None:
            on_messages([*messages, {"role": "assistant", "content": reply}])
        return reply
```

Also update the module docstring: the "v1 is stateless: one Chat Completions call per spawn (no client-side history replay yet)" line is now wrong.

- [ ] **Step 9: Run the suite**

Run: `uv run pytest tests/test_subagent_direct_chat.py tests/test_subagent_third_party.py tests/test_config_raven_sections.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add raven/agent/subagent/backends/openai_api.py raven/agent/subagent/backends/__init__.py raven/config/schema.py tests/test_subagent_direct_chat.py tests/test_subagent_third_party.py tests/test_config_raven_sections.py
git commit -m "feat(subagent): let an openai-kind sub-agent continue a conversation"
```

---

### Task 5: The `direct/` audit record

**Files:**
- Create: `raven/agent/subagent/direct_chat.py`
- Test: `tests/test_subagent_history.py`

**Interfaces:**
- Consumes: `instance_state_path` from Task 3 (shares the same `direct/<agent>/<handle>/` root).
- Produces:
  - `direct_root(session_dir: Path, agent: str, handle: str) -> Path`
  - `DirectChatRecord.open(session_dir, *, agent, handle, task_id, task) -> DirectChatRecord` with `.dir`, `.finish(status=..., output=..., error=...)`, `.started_at_ms`
  - `DirectTurnMeta` NamedTuple: `agent, handle, call_id, dir, started_at_ms, ended_at_ms, status`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_subagent_history.py`:

```python
def test_direct_record_lands_under_the_instance(tmp_path):
    from raven.agent.subagent.direct_chat import DirectChatRecord, direct_root

    record = DirectChatRecord.open(
        tmp_path, agent="Raven-Code", handle="refactor-auth", task_id="t1", task="do it"
    )

    root = direct_root(tmp_path, "Raven-Code", "refactor-auth")
    assert record.dir.parent == root
    assert (record.dir / "prompt.md").read_text(encoding="utf-8") == "do it"
    assert not (record.dir / "out.md").exists()


def test_direct_record_shares_the_state_directory(tmp_path):
    from raven.agent.subagent.direct_chat import direct_root
    from raven.agent.subagent.instance_state import instance_state_path

    root = direct_root(tmp_path, "A", "h")
    assert instance_state_path(tmp_path, "A", "h").parent == root


def test_direct_record_finish_writes_the_output(tmp_path):
    from raven.agent.subagent.direct_chat import DirectChatRecord

    record = DirectChatRecord.open(tmp_path, agent="A", handle="h", task_id="t1", task="q")
    record.finish(status="completed", output="a")

    import json

    meta = json.loads((record.dir / "meta.json").read_text(encoding="utf-8"))
    assert (record.dir / "out.md").read_text(encoding="utf-8") == "a"
    assert meta["status"] == "completed"
    assert meta["agent"] == "A"
    assert meta["handle"] == "h"
    assert meta["ended_at_ms"] >= meta["started_at_ms"]


def test_direct_record_survives_an_unwritable_root(tmp_path, monkeypatch):
    """History is an audit trail; losing it must never take down the turn."""
    from raven.agent.subagent.direct_chat import DirectChatRecord

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr("pathlib.Path.mkdir", boom)
    record = DirectChatRecord.open(tmp_path, agent="A", handle="h", task_id="t1", task="q")
    record.finish(status="completed", output="a")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_history.py -k direct_record -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'raven.agent.subagent.direct_chat'`

- [ ] **Step 3: Write the module**

Create `raven/agent/subagent/direct_chat.py`:

```python
"""One direct chat's on-disk record, and the handoff it owes the main agent.

A direct-chat turn is deliberately absent from the session transcript (the
whole point is to keep those exchanges out of the main agent's context), so
this directory is the only evidence it happened:

    <session_dir>/subagents/direct/<agent>/<handle>/
    |-- messages.json         resume state (raven/agent/subagent/instance_state.py)
    `-- <call_id>/            one turn: prompt.md, out.md, meta.json

Same shape as ``spawn/<call_id>/`` on purpose, so both delegation paths are
inspectable the same way and the handoff can name a turn's input and output
file without inventing a second convention.

A file name is never derived from a sub-agent's output, only from ids raven
mints itself -- the same invariant ``raven/agent/subagent_history.py`` states.
The handoff block relies on it: the block is prepended to user text, and it is
safe to leave unwrapped only because every byte in it is raven's own.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger

from raven.agent.subagent_history import make_call_id
from raven.utils.helpers import safe_path_segment

_DIRECT_DIRNAME = "direct"


def direct_root(session_dir: Path, agent: str, handle: str) -> Path:
    """Where one instance's direct chat lives. Not created here."""
    return (
        Path(session_dir)
        / "subagents"
        / _DIRECT_DIRNAME
        / safe_path_segment(agent)
        / safe_path_segment(handle)
    )


class DirectTurnMeta(NamedTuple):
    """One direct turn, as the handoff block needs to describe it."""

    agent: str
    handle: str
    call_id: str
    directory: Path
    started_at_ms: int
    ended_at_ms: int | None
    status: str


class DirectChatRecord:
    """One direct turn's record directory.

    ``open`` writes the prompt before the sub-agent is dispatched, so a turn the
    user abandons with Esc -- or one killed by a gateway restart -- still leaves
    what was asked. ``finish`` records the outcome, failures included.

    Every method swallows its own I/O errors, for the same reason
    ``SpawnRecord`` does: this is an audit trail, and losing it must never take
    down the turn it describes.
    """

    def __init__(self, directory: Path, *, agent: str, handle: str, started_at_ms: int) -> None:
        self.dir = directory
        self.agent = agent
        self.handle = handle
        self.started_at_ms = started_at_ms

    @classmethod
    def open(
        cls,
        session_dir: Path,
        *,
        agent: str,
        handle: str,
        task_id: str,
        task: str,
    ) -> "DirectChatRecord":
        started = int(time.time() * 1000)
        record = cls(
            direct_root(session_dir, agent, handle) / make_call_id(task_id),
            agent=agent,
            handle=handle,
            started_at_ms=started,
        )
        try:
            record.dir.mkdir(parents=True, exist_ok=True)
            (record.dir / "prompt.md").write_text(task, encoding="utf-8")
            record._write_meta(
                {
                    "call_id": record.dir.name,
                    "agent": agent,
                    "handle": handle,
                    "status": "running",
                    "started_at_ms": started,
                }
            )
        except OSError as exc:
            logger.warning("Direct chat [{}] history could not be opened at {}: {}", task_id, record.dir, exc)
        return record

    def finish(self, *, status: str, output: str | None = None, error: str | None = None) -> None:
        try:
            if not self.dir.is_dir():
                return
            if output is not None:
                (self.dir / "out.md").write_text(output, encoding="utf-8")
            if error is not None:
                (self.dir / "error.md").write_text(error, encoding="utf-8")
            meta = self._read_meta()
            meta.update(status=status, ended_at_ms=int(time.time() * 1000))
            self._write_meta(meta)
        except OSError as exc:
            logger.warning("Direct chat history at {} could not be finished: {}", self.dir, exc)

    def meta(self) -> DirectTurnMeta:
        """This turn as the handoff describes it, read back from disk."""
        raw = self._read_meta()
        return DirectTurnMeta(
            agent=self.agent,
            handle=self.handle,
            call_id=self.dir.name,
            directory=self.dir,
            started_at_ms=int(raw.get("started_at_ms") or self.started_at_ms),
            ended_at_ms=raw.get("ended_at_ms"),
            status=str(raw.get("status") or "running"),
        )

    def _read_meta(self) -> dict[str, Any]:
        try:
            return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = ["DirectChatRecord", "DirectTurnMeta", "direct_root"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_subagent_history.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add raven/agent/subagent/direct_chat.py tests/test_subagent_history.py
git commit -m "feat(subagent): record each direct-chat turn beside its instance state"
```

---

## Phase 2 - Execution and handoff

Deliverable at end of phase: a direct chat can be driven entirely from Python (no RPC, no UI) and the next ordinary turn carries the handoff block. Verifiable by `uv run pytest`.

### Task 6: `SubagentManager.chat()`

**Files:**
- Modify: `raven/agent/subagent/manager.py` (new method after `spawn`)
- Test: `tests/test_subagent_direct_chat.py`

**Interfaces:**
- Consumes: `hold_handle` (Task 1), `RAVEN_LOOP_AGENT` (Task 2), `InstanceState` / `instance_state_path` (Task 3), `DirectChatRecord` (Task 5).
- Produces: `SubagentManager.chat(*, session_key: str, agent: str, handle: str, text: str, workspace: Path | None = None) -> tuple[str, DirectTurnMeta]` - the reply text and this turn's record metadata. Both are needed by the caller: the reply is what goes to the client, the meta is what the handoff records. There is NO `on_token`: no sub-agent backend accepts a token callback, so nothing streams (spec D6). **Superseded 2026-08-16**: `chat` now takes an optional `on_delta`, offered to the backend only when it declares `streams`; see the spec's "Streaming".
- Produces: `SubagentManager._is_replayed(agent: str) -> bool`, and a `kind` class attribute on each backend (`"cli"`, `"openai"`, `"raven-loop"`) if one is not already present.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_direct_chat.py`:

```python
@pytest.mark.asyncio
async def test_chat_resumes_the_built_in_subagent(tmp_path, monkeypatch):
    """Two chat turns on one handle: the second must see the first."""
    manager = _direct_chat_manager(tmp_path, monkeypatch)

    reply1, meta1 = await manager.chat(
        session_key="s1", agent="raven", handle="notes", text="first"
    )
    reply2, meta2 = await manager.chat(
        session_key="s1", agent="raven", handle="notes", text="second"
    )

    assert reply1 and reply2
    assert meta1.call_id != meta2.call_id
    assert meta2.status == "completed"

    # Both turns left a record, and the state file grew rather than reset.
    from raven.agent.subagent.direct_chat import direct_root
    from raven.agent.subagent.instance_state import InstanceState, instance_state_path

    root = direct_root(manager._session_dir("s1"), "raven", "notes")
    assert len([p for p in root.iterdir() if p.is_dir()]) == 2

    stored = InstanceState(instance_state_path(manager._session_dir("s1"), "raven", "notes")).load()
    assert [m["content"] for m in stored if m["role"] == "user"] == ["first", "second"]


@pytest.mark.asyncio
async def test_chat_does_not_write_the_main_transcript(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch)
    await manager.chat(session_key="s1", agent="raven", handle="notes", text="hi")

    transcript = manager._session_dir("s1").parent / "s1.jsonl"
    assert not transcript.exists()


@pytest.mark.asyncio
async def test_chat_records_a_failure(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch, fail=True)

    with pytest.raises(RuntimeError):
        await manager.chat(session_key="s1", agent="raven", handle="notes", text="hi")

    from raven.agent.subagent.direct_chat import direct_root
    import json

    root = direct_root(manager._session_dir("s1"), "raven", "notes")
    call_dir = next(p for p in root.iterdir() if p.is_dir())
    assert json.loads((call_dir / "meta.json").read_text())["status"] == "failed"
    assert (call_dir / "error.md").exists()
```

Add the fixture helper near the top of the file. Build it by copying the `SubagentManager` construction that `tests/test_subagent_manager.py` already uses (read it first - the constructor takes `provider`, `workspace`, and optionally `session_dir`), and pointing `session_dir` at `tmp_path`:

```python
def _direct_chat_manager(tmp_path, monkeypatch, *, fail: bool = False):
    """A manager whose built-in backend answers without touching a provider."""
    from raven.agent.subagent.manager import SubagentManager

    class StubProvider:
        def get_default_model(self):
            return "stub"

    manager = SubagentManager(
        provider=StubProvider(),
        workspace=tmp_path / "home",
        session_dir=lambda key: tmp_path / "sessions" / key,
    )

    async def fake_run(task, *, task_id, workspace, executor, session_key=None, instance=None,
                       provider=None, model=None, history=None, on_messages=None):
        if fail:
            raise RuntimeError("backend exploded")
        msgs = list(history) if history else [{"role": "system", "content": "sys"}]
        msgs.append({"role": "user", "content": task})
        if on_messages is not None:
            on_messages([*msgs, {"role": "assistant", "content": f"re: {task}"}])
        return f"re: {task}"

    monkeypatch.setattr(manager._raven_backend, "run", fake_run)
    return manager
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_subagent_direct_chat.py -k chat_ -v`
Expected: FAIL - `AttributeError: 'SubagentManager' object has no attribute 'chat'`

- [ ] **Step 3: Implement `chat`**

In `raven/agent/subagent/manager.py`, add the imports:

```python
from raven.agent.subagent.direct_chat import DirectChatRecord, DirectTurnMeta
from raven.agent.subagent.instance_state import InstanceState, instance_state_path
from raven.agent.subagent.instances import get_registry, hold_handle
```

Add the method after `spawn`:

```python
    async def chat(
        self,
        *,
        session_key: str,
        agent: str,
        handle: str,
        text: str,
        workspace: Path | None = None,
    ) -> tuple[str, DirectTurnMeta]:
        """Run one direct-chat turn against an existing instance.

        Unlike ``spawn`` this awaits its result rather than scheduling a
        background task: the caller is a turn on the spine, and its whole job is
        to carry this reply back to one client.

        The handle lock is held for the entire turn, so a direct chat and a
        main-loop spawn addressing the same instance queue rather than
        interleave that instance's conversation. See ``hold_handle``.

        Nothing here touches the session transcript. A direct chat exists to
        keep these exchanges out of the main agent's context, so the record
        directory is the only place the turn is written.
        """
        session_dir = self._session_dir(session_key)
        effective_workspace = workspace or self.workspace
        task_id = str(uuid.uuid4())[:8]
        owns_state = agent == RAVEN_LOOP_AGENT or self._is_replayed(agent)
        state = InstanceState(instance_state_path(session_dir, agent, handle)) if owns_state else None

        record = DirectChatRecord.open(
            session_dir, agent=agent, handle=handle, task_id=task_id, task=text
        )
        async with hold_handle(session_key, agent, handle):
            await _write_spawn_status(session_key, agent, handle, "running")
            backend = self._resolve_backend(None if agent == RAVEN_LOOP_AGENT else agent)
            kwargs: dict[str, Any] = {}
            if state is not None:
                kwargs["history"] = state.load()
                kwargs["on_messages"] = state.save
            try:
                executor = build_executor(
                    self._sandbox_config,
                    effective_workspace,
                    self._owned_ids,
                    self._home_volume(effective_workspace),
                )
                async with executor:
                    reply = await backend.run(
                        text,
                        task_id=task_id,
                        workspace=effective_workspace,
                        executor=executor,
                        session_key=session_key,
                        instance=handle,
                        provider=self.provider,
                        model=self.model,
                        **kwargs,
                    )
            except asyncio.CancelledError:
                await _write_spawn_status(session_key, agent, handle, "cancelled")
                record.finish(status="cancelled")
                raise
            except Exception as exc:
                await _write_spawn_status(session_key, agent, handle, "failed")
                record.finish(status="failed", error=f"Error: {exc}")
                raise
            await _write_spawn_status(session_key, agent, handle, "completed")
            record.finish(status="completed", output=reply)
            return reply, record.meta()

    def _is_replayed(self, agent: str) -> bool:
        """Whether raven owns this agent's conversation state.

        True for the openai kind, whose backend has no session of its own and
        depends on raven replaying the message list; false for a cli agent,
        which resumes inside its own store.
        """
        backend = self._backends.get(agent)
        return backend is not None and getattr(backend, "kind", None) == "openai"
```

If `OpenAIApiBackend` has no `kind` attribute on this branch, add `kind = "openai"` as a class attribute to it (and `kind = "cli"` to `CliAgentBackend`) rather than type-sniffing with `isinstance` here - the manager should not import backend classes to ask what they are.

Add `Callable` to the `collections.abc` import if it is not already there (it is - `session_dir` is typed with it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_subagent_direct_chat.py -v`
Expected: PASS

- [ ] **Step 5: Add the serialization test**

Append to `tests/test_subagent_direct_chat.py`:

```python
@pytest.mark.asyncio
async def test_chat_and_spawn_on_one_handle_do_not_interleave(tmp_path, monkeypatch):
    manager = _direct_chat_manager(tmp_path, monkeypatch)
    inside: list[str] = []

    original = manager._raven_backend.run

    async def tracked(task, **kwargs):
        inside.append(f"in:{task}")
        await asyncio.sleep(0.01)
        inside.append(f"out:{task}")
        return await original(task, **kwargs)

    monkeypatch.setattr(manager._raven_backend, "run", tracked)

    await asyncio.gather(
        manager.chat(session_key="s1", agent="raven", handle="h", text="a"),
        manager.chat(session_key="s1", agent="raven", handle="h", text="b"),
    )

    assert inside in (
        ["in:a", "out:a", "in:b", "out:b"],
        ["in:b", "out:b", "in:a", "out:a"],
    )
```

- [ ] **Step 6: Run it**

Run: `uv run pytest tests/test_subagent_direct_chat.py -k interleave -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add raven/agent/subagent/manager.py raven/agent/subagent/backends/openai_api.py raven/agent/subagent/backends/cli_agent.py tests/test_subagent_direct_chat.py
git commit -m "feat(subagent): drive one direct-chat turn against a live instance"
```

---

### Task 7: `TurnRequest.direct_target` and the loop branch

**Files:**
- Modify: `raven/spine/turn.py:64` (add the field after `deliver_text`)
- Modify: `raven/agent/loop/main.py:3303` (add a branch beside the `deliver_text` one)
- Test: `tests/test_spine_turn.py`, `tests/test_subagent_direct_chat.py`

**Interfaces:**
- Consumes: `SubagentManager.chat` (Task 6).
- Produces: `TurnRequest.direct_target: tuple[str, str] | None`; `AgentLoop.run_turn` handling it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_spine_turn.py`:

```python
def test_direct_target_defaults_to_none():
    from raven.spine import Origin, Source, TurnRequest
    from raven.spine.turn import ChatType

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
    )
    assert req.direct_target is None


def test_direct_target_carries_agent_and_handle():
    from raven.spine import Origin, Source, TurnRequest
    from raven.spine.turn import ChatType

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
        direct_target=("Raven-Code", "refactor-auth"),
    )
    assert req.direct_target == ("Raven-Code", "refactor-auth")
```

Match the `Source` / `ChatType` construction the existing tests in that file already use; read the top of `tests/test_spine_turn.py` first.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spine_turn.py -k direct_target -v`
Expected: FAIL - `TypeError: TurnRequest.__init__() got an unexpected keyword argument 'direct_target'`

- [ ] **Step 3: Add the field**

In `raven/spine/turn.py`, immediately after `deliver_text`:

```python
    # Direct sub-agent delivery: when set to ``(agent, handle)`` the turn skips
    # the model and runs against that instance instead, streaming its reply back
    # to this conversation. Runs through the lane like any other turn so the
    # one-turn-per-session slot still holds, but unlike every other turn it is
    # NOT written to the session transcript -- a direct chat exists to keep
    # those exchanges out of the main agent's context. See AgentLoop.run_turn.
    direct_target: tuple[str, str] | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spine_turn.py -k direct_target -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the loop branch**

Append to `tests/test_subagent_direct_chat.py`:

```python
@pytest.mark.asyncio
async def test_run_turn_routes_a_direct_target_to_the_subagent(tmp_path, monkeypatch):
    """The model is never called, and the transcript is never written."""
    emitted: list[Any] = []
    called: dict[str, Any] = {}

    loop = _agent_loop_for_direct_chat(tmp_path, monkeypatch)

    async def fake_chat(*, session_key, agent, handle, text, workspace=None):
        called.update(session_key=session_key, agent=agent, handle=handle, text=text)
        from raven.agent.subagent.direct_chat import DirectTurnMeta

        return "sub reply", DirectTurnMeta(
            agent=agent, handle=handle, call_id="c1", directory=tmp_path,
            started_at_ms=1, ended_at_ms=2, status="completed",
        )

    monkeypatch.setattr(loop.subagents, "chat", fake_chat)

    async def emit(event):
        emitted.append(event)

    from raven.spine import Origin, Source, TurnRequest
    from raven.spine.turn import ChatType

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="fix it",
        conversation="s1",
        direct_target=("Raven-Code", "refactor-auth"),
    )

    await loop.run_turn(req, emit)

    assert called == {
        "session_key": "s1",
        "agent": "Raven-Code",
        "handle": "refactor-auth",
        "text": "fix it",
    }
    assert any(getattr(e, "content", None) == "sub reply" for e in emitted)
    assert loop.sessions.get_or_create("s1").messages == []
```

Build `_agent_loop_for_direct_chat` by copying the `AgentLoop` construction from `tests/test_message_tool_turn_local.py` (it already builds a loop with a stub provider and a tmp agent home). Adjust only what is needed; the point is a loop whose `subagents` attribute can be monkeypatched and whose `sessions` is inspectable.

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_direct_chat.py -k run_turn_routes -v`
Expected: FAIL - the model stub is called and `emitted` has no `"sub reply"`.

- [ ] **Step 7: Add the branch**

In `raven/agent/loop/main.py`, immediately before the existing `if req.deliver_text is not None:` block at line 3303:

```python
        # Direct sub-agent turn (direct_target -- see TurnRequest). Deliberately
        # asymmetric with the deliver_text branch below: that one persists to the
        # session before emitting, this one persists nothing. The whole purpose
        # of a direct chat is that the main agent's transcript does not carry it;
        # the turn's evidence is its record directory, and what the main agent
        # eventually learns is the handoff block, not these messages.
        if req.direct_target is not None:
            agent, handle = req.direct_target
            reply, meta = await self.subagents.chat(
                session_key=cid,
                agent=agent,
                handle=handle,
                text=req.text,
            )
            self._direct_handoff.record(cid, meta)
            await emit(Text(content=reply))
            return TurnOutcome(
                usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                explicit_reply=True,
            )
```

Nothing streams: no sub-agent backend accepts a token callback (spec D6), so the whole reply arrives as one `Text`. **Superseded 2026-08-16**: the branch emits `StreamDelta` and withholds the closing `Text` when anything streamed; a backend that cannot stream still takes this path unchanged.

Note the ordering constraint: `Text` and `Usage` are resolved *inside* `run_turn` by the local `from raven.spine.events import ...` at the top of the method. Place the direct branch **after** those imports, not before, or it will fail on a `NameError` at runtime while every unit test that stubs `emit` still passes.

`self._direct_handoff` arrives in Task 8. Until then, guard it:

```python
            if getattr(self, "_direct_handoff", None) is not None:
                self._direct_handoff.record(cid, meta)
```

and delete the guard in Task 8 once the attribute always exists.

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_subagent_direct_chat.py -k run_turn_routes -v`
Expected: PASS

- [ ] **Step 9: Run the loop suite for regressions**

Run: `uv run pytest tests/test_message_tool_turn_local.py tests/test_spine_turn.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add raven/spine/turn.py raven/agent/loop/main.py tests/test_spine_turn.py tests/test_subagent_direct_chat.py
git commit -m "feat(agent): run a direct sub-agent turn without touching the transcript"
```

---

### Task 8: The handoff block

**Files:**
- Modify: `raven/agent/subagent/direct_chat.py` (add `DirectChatHandoff`)
- Modify: `raven/agent/loop/main.py` (own a `DirectChatHandoff`; prepend on ordinary turns; drop Task 7's guard)
- Test: `tests/test_subagent_handoff.py` (new file)

**Interfaces:**
- Consumes: `DirectTurnMeta` (Task 5), the `record` call site (Task 7).
- Produces: `DirectChatHandoff` with `record(session_key: str, meta: DirectTurnMeta) -> None`, `pending_count(session_key: str) -> int`, `take(session_key: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_subagent_handoff.py`:

```python
"""The pointer block a direct chat owes the main agent on its next turn."""

from __future__ import annotations

from pathlib import Path

from raven.agent.subagent.direct_chat import DirectChatHandoff, DirectTurnMeta


def _meta(agent="Raven-Code", handle="refactor-auth", call_id="20260813T041207Z-a1b2c3d4",
          started=1786000000000, ended=1786000060000, status="completed"):
    return DirectTurnMeta(
        agent=agent,
        handle=handle,
        call_id=call_id,
        directory=Path("/root/.raven/workspace/sessions/g/c/subagents/direct")
        / agent / handle / call_id,
        started_at_ms=started,
        ended_at_ms=ended,
        status=status,
    )


def test_nothing_pending_takes_nothing():
    h = DirectChatHandoff()
    assert h.pending_count("s1") == 0
    assert h.take("s1") is None


def test_take_clears_the_pending_list():
    h = DirectChatHandoff()
    h.record("s1", _meta())
    assert h.pending_count("s1") == 1
    assert h.take("s1") is not None
    assert h.pending_count("s1") == 0
    assert h.take("s1") is None


def test_sessions_do_not_share_pending_state():
    h = DirectChatHandoff()
    h.record("s1", _meta())
    assert h.pending_count("s2") == 0
    assert h.take("s2") is None
    assert h.pending_count("s1") == 1


def test_block_uses_utc_iso_timestamps_not_epoch_ms():
    h = DirectChatHandoff()
    h.record("s1", _meta())
    block = h.take("s1")
    assert "1786000000000" not in block
    assert "2026-" in block and "Z" in block


def test_turns_are_grouped_by_instance():
    h = DirectChatHandoff()
    h.record("s1", _meta(call_id="c1"))
    h.record("s1", _meta(call_id="c2"))
    h.record("s1", _meta(agent="mirothinker", handle="scan", call_id="c3"))
    block = h.take("s1")

    # One header line per instance, not per turn.
    assert block.count("Raven-Code / refactor-auth") == 1
    assert block.count("mirothinker / scan") == 1
    assert "2 turns" in block
    assert "1 turn," in block


def test_an_unlanded_turn_is_marked_and_lists_only_its_prompt():
    h = DirectChatHandoff()
    h.record("s1", _meta(ended=None, status="running"))
    block = h.take("s1")

    assert "running at handoff time" in block
    assert "prompt.md" in block
    assert "out.md" not in block


def test_the_first_path_is_absolute_and_later_ones_are_abbreviated():
    h = DirectChatHandoff()
    h.record("s1", _meta(call_id="c1"))
    h.record("s1", _meta(call_id="c2"))
    block = h.take("s1")

    assert "/root/.raven/workspace/sessions/g/c/subagents/direct/Raven-Code/refactor-auth/" in block
    assert "c1/{prompt.md,out.md}" in block
    assert "c2/{prompt.md,out.md}" in block


def test_the_block_carries_no_subagent_authored_text():
    """Every byte is raven-minted, which is why the block needs no untrusted wrap."""
    h = DirectChatHandoff()
    h.record("s1", _meta())
    block = h.take("s1")
    assert "sub reply" not in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subagent_handoff.py -v`
Expected: FAIL - `ImportError: cannot import name 'DirectChatHandoff'`

- [ ] **Step 3: Implement `DirectChatHandoff`**

Append to `raven/agent/subagent/direct_chat.py`:

```python
_HANDOFF_HEADER = "[subagent direct chats since your last turn]"


def _utc(ms: int) -> str:
    """Epoch ms as UTC ISO-8601. The block is read by a model, which cannot map
    an epoch integer onto the user's sense of "just now"."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


class DirectChatHandoff:
    """The direct-chat turns a session owes its main agent, and their block.

    Held by the runtime rather than the client: the runtime is what wrote the
    records, so it is what knows which ones the main agent has not been told
    about, and the list survives a client restart. A client reads only the
    count, to draw a hint.

    An entry is appended as each direct turn lands, not on mode exit -- the user
    may enter and leave an instance repeatedly, or go straight back to the main
    conversation without leaving at all.
    """

    def __init__(self) -> None:
        self._pending: dict[str, list[DirectTurnMeta]] = {}

    def record(self, session_key: str, meta: DirectTurnMeta) -> None:
        self._pending.setdefault(session_key, []).append(meta)

    def pending_count(self, session_key: str) -> int:
        return len(self._pending.get(session_key, ()))

    def take(self, session_key: str) -> str | None:
        """The rendered block, clearing the list. ``None`` when nothing is pending.

        Take-and-clear is what stops one segment being reported twice. The empty
        case returns before touching anything: this runs on every ordinary user
        turn, so it has to be free when there is nothing to say.
        """
        metas = self._pending.pop(session_key, None)
        if not metas:
            return None
        return self._render(metas)

    def _render(self, metas: list[DirectTurnMeta]) -> str:
        grouped: dict[tuple[str, str], list[DirectTurnMeta]] = {}
        for meta in metas:
            grouped.setdefault((meta.agent, meta.handle), []).append(meta)

        lines = [_HANDOFF_HEADER]
        for (agent, handle), turns in grouped.items():
            lines.append(f"{agent} / {handle}")
            lines.append(f"  {self._span(turns)}")
            root = turns[0].directory.parent
            lines.append(f"  {root}{os.sep}")
            for turn in turns:
                files = "{prompt.md,out.md}" if turn.ended_at_ms is not None else "prompt.md"
                lines.append(f"    {turn.call_id}/{files}")
        return "\n".join(lines)

    @staticmethod
    def _span(turns: list[DirectTurnMeta]) -> str:
        count = f"{len(turns)} turn{'s' if len(turns) != 1 else ''},"
        start = _utc(turns[0].started_at_ms)
        unlanded = [t for t in turns if t.ended_at_ms is None]
        last_end = max((t.ended_at_ms for t in turns if t.ended_at_ms is not None), default=None)
        if last_end is None:
            return f"{count} {start} (running at handoff time)"
        span = f"{count} {start} -> {_utc(last_end)}"
        return f"{span} ({len(unlanded)} running at handoff time)" if unlanded else span
```

Add `import os` to the module's imports, and extend the module's `__all__` (written in Task 5) to include the new name:

```python
__all__ = ["DirectChatHandoff", "DirectChatRecord", "DirectTurnMeta", "direct_root"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_subagent_handoff.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the prepend**

Append to `tests/test_subagent_handoff.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_an_ordinary_turn_carries_the_pending_block(tmp_path, monkeypatch):
    """The block reaches the model, prepended to the user's own text."""
    loop = _loop_with_handoff(tmp_path, monkeypatch)
    loop._direct_handoff.record("s1", _meta())

    seen = await _run_ordinary_turn(loop, "what did you find?")

    user_text = seen[-1]["content"]
    assert user_text.startswith("[subagent direct chats since your last turn]")
    assert user_text.endswith("what did you find?")
    assert loop._direct_handoff.pending_count("s1") == 0


@pytest.mark.asyncio
async def test_a_turn_with_nothing_pending_is_untouched(tmp_path, monkeypatch):
    loop = _loop_with_handoff(tmp_path, monkeypatch)
    seen = await _run_ordinary_turn(loop, "plain question")
    assert seen[-1]["content"] == "plain question"


@pytest.mark.asyncio
async def test_a_direct_turn_does_not_consume_the_block(tmp_path, monkeypatch):
    """Only a turn addressed to the main agent takes the handoff."""
    loop = _loop_with_handoff(tmp_path, monkeypatch)
    loop._direct_handoff.record("s1", _meta())

    await _run_direct_turn(loop, "more work", target=("Raven-Code", "refactor-auth"))

    # Still pending: the main agent has not had a turn yet. Plus this turn's own.
    assert loop._direct_handoff.pending_count("s1") == 2
```

Reuse `_agent_loop_for_direct_chat` from `tests/test_subagent_direct_chat.py` for `_loop_with_handoff` - import it, or lift it into `tests/conftest.py` as a fixture if that file already hosts shared loop builders (check first). `_run_ordinary_turn` submits a `TurnRequest` with no `direct_target` and returns the message list the stub provider was handed.

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_subagent_handoff.py -k ordinary_turn -v`
Expected: FAIL - the user message is unmodified.

- [ ] **Step 7: Wire it into the loop**

In `raven/agent/loop/main.py`:

1. In `__init__`, alongside the other per-loop state:

```python
        self._direct_handoff = DirectChatHandoff()
```

2. Import it: `from raven.agent.subagent.direct_chat import DirectChatHandoff`.

3. Delete the `getattr(self, "_direct_handoff", None) is not None` guard Task 7 added; the attribute always exists now.

4. In `run_turn`, after the `direct_target` and `deliver_text` early-return branches (so a direct turn cannot consume its own block) and before the message list is assembled for the model:

```python
        # A direct chat is invisible to the main agent by design, so this is
        # where it finds out one happened: pointers to what was asked and
        # answered, never the text. Take-and-clear, so a segment is reported
        # once. Nothing pending returns immediately -- this runs on every turn.
        handoff = self._direct_handoff.take(cid)
        if handoff is not None:
            req = replace(req, text=f"{handoff}\n\n{req.text}")
```

`TurnRequest` is a dataclass, so `from dataclasses import replace` is the mutation-free way to do this; check whether `main.py` already imports `replace` before adding it. If `req` is used by reference further down in a way `replace` would break (grep for `req.` after this point), assign the composed text to a local and pass that to the message assembly instead - do not mutate the frozen request in place.

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_subagent_handoff.py -v`
Expected: PASS

- [ ] **Step 9: Full Python suite**

Run: `uv run pytest tests/ -x -q`
Expected: PASS. If an unrelated test fails, note it and check whether it also fails on `git stash` - do not "fix" a pre-existing failure inside this task.

- [ ] **Step 10: Commit**

```bash
git add raven/agent/subagent/direct_chat.py raven/agent/loop/main.py tests/test_subagent_handoff.py
git commit -m "feat(agent): tell the main agent where a direct chat left its record"
```

---

## Phase 3 - RPC surface

Deliverable at end of phase: a TUI-RPC client can list instances, read one's history, cancel or forget it, and send a targeted turn. Verifiable by `uv run pytest` plus `npm run gen:rpc` producing a clean diff.

### Task 9: The contract - schema, models, registry

> **Landed together with Task 10** (`feat(rpc): serve the session's sub-agent
> instances and route a turn to one`). Splitting them would have put a method in
> the contract with no handler, which `tests/test_rpc_registration.py` rejects on
> purpose. The paths below were rewritten for upstream's rename of `tui_rpc` to
> `rpc`; two shapes also changed against the draft: `InstanceRow` carries the
> registry's camelCase through pydantic aliases rather than as attribute names,
> and the reconciliation rule was extracted to `reconcile_instance_rows` and
> shared with the web RPC instead of being copied structurally.


**Files:**
- Modify: `rpc-schema/openrpc.json`
- Modify: `raven/rpc/models.py` (new models + `METHOD_MODELS` entries + `target` on `TurnSendParams`)
- Regenerate: `ui-tui/src/rpc/generated.ts`
- Test: `tests/test_rpc_schema_match.py`, `tests/test_rpc_registration.py`

**Interfaces:**
- Consumes: nothing at runtime; this task is contract-only.
- Produces the four method names and their param/result models, which Task 10 implements and Tasks 12-14 call:
  - `subagents.instances` - params `{session_key: str}`, result `{instances: InstanceRow[], pending_handoff_count: int}`
  - `subagents.instance.history` - params `{session_key: str, agent: str, handle: str}`, result `{turns: DirectTurn[]}`
  - `subagents.instance.forget` - params `{session_key: str, agent: str, handle: str}`, result `{removed: bool}`
  - `subagents.instance.cancel` - params `{session_key: str, agent: str, handle: str}`, result `{cancelled: bool}`
  - `turn.send` - params gain `target: {agent: str, handle: str} | None`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_rpc_registration.py`:

```python
DIRECT_CHAT_METHODS = {
    "subagents.instances",
    "subagents.instance.history",
    "subagents.instance.forget",
    "subagents.instance.cancel",
}


def test_direct_chat_methods_are_in_the_contract():
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "ui-tui" / "rpc-schema" / "openrpc.json").read_text()
    )
    assert DIRECT_CHAT_METHODS <= {m["name"] for m in schema["methods"]}


def test_turn_send_accepts_a_target():
    from raven.rpc.models import TurnSendParams

    parsed = TurnSendParams.model_validate(
        {
            "session_key": "s1",
            "content": "hi",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        }
    )
    assert parsed.target is not None
    assert (parsed.target.agent, parsed.target.handle) == ("Raven-Code", "refactor-auth")


def test_turn_send_target_is_optional():
    from raven.rpc.models import TurnSendParams

    assert TurnSendParams.model_validate({"session_key": "s1", "content": "hi"}).target is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rpc_registration.py -k direct_chat -v tests/test_rpc_registration.py -k turn_send_accepts`
Expected: FAIL - the methods are absent and `TurnSendParams` forbids the extra `target` key (`_Strict` sets `extra="forbid"`).

- [ ] **Step 3: Add the Pydantic models**

In `raven/rpc/models.py`, in the public-types section:

```python
class DirectTarget(_Strict):
    agent: str
    handle: str


class InstanceRow(_Strict):
    """One live sub-agent instance, mirroring the web RPC's row shape.

    Field names stay camelCase because they come straight off the instance
    registry's JSON records, which both surfaces read. Renaming them here would
    make the TUI and the web UI disagree about what an instance is.
    """

    sessionKey: str
    agent: str
    handle: str
    kind: str
    status: str | None = None
    agentId: str | None = None
    runId: str | None = None
    nodeId: str | None = None
    createdAtMs: int | None = None
    updatedAtMs: int | None = None


class DirectTurn(_Strict):
    call_id: str
    role: Literal["user", "assistant"]
    content: str
    at_ms: int
    prompt_path: str | None = None
    out_path: str | None = None
```

Then the params/results:

```python
class SubagentsInstancesParams(_Strict):
    session_key: str


class SubagentsInstancesResult(_Strict):
    instances: list[InstanceRow]
    pending_handoff_count: int


class SubagentsInstanceHistoryParams(_Strict):
    session_key: str
    agent: str
    handle: str


class SubagentsInstanceHistoryResult(_Strict):
    turns: list[DirectTurn]


class SubagentsInstanceForgetParams(_Strict):
    session_key: str
    agent: str
    handle: str


class SubagentsInstanceForgetResult(_Strict):
    removed: bool


class SubagentsInstanceCancelParams(_Strict):
    session_key: str
    agent: str
    handle: str


class SubagentsInstanceCancelResult(_Strict):
    cancelled: bool
```

Add `target` to `TurnSendParams` (currently at `raven/rpc/models.py:576`):

```python
    target: DirectTarget | None = None
```

Add all four entries to `METHOD_MODELS` (which starts at `raven/rpc/models.py:1136`), in a `# subagents.instance*` group beside the existing `subagents.*` entries, and add every new class name to `__all__`.

- [ ] **Step 4: Mirror it in `openrpc.json`**

Add the four methods to the `methods` array and the three object types to `components/schemas`, following the exact shape of the `subagents.probe` entry (`params` list, `result.name`/`result.schema`, `additionalProperties: false`, explicit `required`). Add `target` to `turn.send`'s params as an optional `$ref` to a new `DirectTarget` schema.

The schema-match test compares name, required, core JSON type, enum values and array item type field-by-field, so `Literal["user", "assistant"]` must appear as an `enum` on `DirectTurn.role`, and `list[InstanceRow]` as `{"type": "array", "items": {"$ref": ...}}`.

- [ ] **Step 5: Run the schema-match test**

Run: `uv run pytest tests/test_rpc_schema_match.py -v`
Expected: PASS. This test is the whole point of this task - it diffs the schema against `METHOD_MODELS` field by field, so iterate here until it is green before moving on.

- [ ] **Step 6: Regenerate the TypeScript types**

```bash
cd ui-tui && npm run gen:rpc && npx tsc -b --noEmit
```

Expected: `generated.ts` gains the four methods and the new types; `tsc` clean. Never hand-edit `generated.ts`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_rpc_registration.py tests/test_rpc_schema_match.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add rpc-schema/openrpc.json raven/rpc/models.py ui-tui/src/rpc/generated.ts tests/test_rpc_registration.py
git commit -m "feat(rpc): declare the instance and direct-target contract"
```

---

### Task 10: The instance handlers

**Files:**
- Create: `raven/rpc/methods/instances.py`
- Modify: `raven/rpc/server.py` (register the group)
- Test: `tests/test_rpc_subagents.py`

**Interfaces:**
- Consumes: the models from Task 9; `DirectChatHandoff.pending_count` (Task 8); `direct_root` (Task 5); `SubagentManager.cancel_by_instance` (`raven/agent/subagent/manager.py:466`) and `live_handles` (`:491`).
- Produces: `register_instance_methods(dispatcher, *, agent_loop_factory=None) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_rpc_subagents.py`:

```python
@pytest.mark.asyncio
async def test_instances_lists_only_this_session(tmp_path, monkeypatch):
    from raven.rpc.methods.instances import instances_list

    registry = _registry_with(tmp_path, monkeypatch, [
        ("s1", "Raven-Code", "a", "completed"),
        ("s2", "Raven-Code", "b", "completed"),
    ])

    out = await instances_list({"session_key": "s1"})
    assert [(r["agent"], r["handle"]) for r in out["instances"]] == [("Raven-Code", "a")]
    assert out["pending_handoff_count"] == 0


@pytest.mark.asyncio
async def test_instances_reports_a_dead_running_row_as_interrupted(tmp_path, monkeypatch):
    """Same reconciliation the web RPC does; a killed gateway leaves 'running'."""
    from raven.rpc.methods.instances import instances_list

    _registry_with(tmp_path, monkeypatch, [("s1", "Raven-Code", "a", "running")])

    out = await instances_list({"session_key": "s1"})
    assert out["instances"][0]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_history_reads_the_record_directories_in_order(tmp_path, monkeypatch):
    from raven.agent.subagent.direct_chat import DirectChatRecord
    from raven.rpc.methods.instances import instances_history

    session_dir = tmp_path / "sessions" / "s1"
    for text, answer in (("first", "a1"), ("second", "a2")):
        rec = DirectChatRecord.open(session_dir, agent="A", handle="h", task_id=text, task=text)
        rec.finish(status="completed", output=answer)

    monkeypatch.setattr(
        "raven.rpc.methods.instances._session_dir", lambda key: tmp_path / "sessions" / key
    )

    out = await instances_history({"session_key": "s1", "agent": "A", "handle": "h"})
    assert [t["content"] for t in out["turns"]] == ["first", "a1", "second", "a2"]
    assert [t["role"] for t in out["turns"]] == ["user", "assistant", "user", "assistant"]
    assert out["turns"][0]["prompt_path"].endswith("prompt.md")


@pytest.mark.asyncio
async def test_history_of_an_unknown_instance_is_empty_not_an_error(tmp_path, monkeypatch):
    from raven.rpc.methods.instances import instances_history

    monkeypatch.setattr(
        "raven.rpc.methods.instances._session_dir", lambda key: tmp_path / "nope" / key
    )
    assert await instances_history({"session_key": "s1", "agent": "A", "handle": "h"}) == {"turns": []}


@pytest.mark.asyncio
async def test_forget_drops_one_row_and_reports_it(tmp_path, monkeypatch):
    from raven.rpc.methods.instances import instances_forget

    _registry_with(tmp_path, monkeypatch, [("s1", "A", "h", "completed")])

    assert await instances_forget({"session_key": "s1", "agent": "A", "handle": "h"}) == {"removed": True}
    assert await instances_forget({"session_key": "s1", "agent": "A", "handle": "h"}) == {"removed": False}
```

Write `_registry_with` as a local helper that builds an `InstanceRegistry` at `tmp_path`, calls `upsert_spawn` for each tuple, and monkeypatches `raven.rpc.methods.instances.get_registry` to return it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rpc_subagents.py -k instances -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'raven.rpc.methods.instances'`

- [ ] **Step 3: Write the handlers**

Create `raven/rpc/methods/instances.py`:

```python
"""``subagents.instance*`` RPC handlers: the session's live sub-agent instances.

Thin adapters, like their neighbours in ``methods/subagents.py`` - but about a
different noun. That module configures *which* sub-agents exist; this one is
about the *instances* a session has actually talked to, which is what the
direct-chat surface addresses.

The row shape and the interrupted-status reconciliation are deliberately the
same as the web RPC's ``raven.subagents.instances``
(``raven/web_rpc/methods_config.py:181``). Two surfaces disagreeing about what
counts as a live instance is the hardest kind of bug to find later, so the rule
is copied structurally rather than re-derived.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent.subagent.direct_chat import direct_root
from raven.agent.subagent.instances import get_registry

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory

_loop_factory: "AgentLoopFactory | None" = None


def _loop() -> Any:
    return _loop_factory() if _loop_factory is not None else None


def _manager() -> Any:
    return getattr(_loop(), "subagents", None)


def _session_dir(session_key: str) -> Path:
    """The metadata directory of one chat session.

    Routed through the live loop's manager so this resolves a session exactly
    the way the code that wrote the records did. Without a loop (the demo
    runner) there is nothing to read, and the caller degrades to empty.
    """
    manager = _manager()
    if manager is None:
        raise FileNotFoundError("no live sub-agent manager")
    return manager._session_dir(session_key)


async def instances_list(params: dict) -> dict:
    """Every instance this session has used, most recently updated first."""
    session_key = params.get("session_key", "")
    rows = get_registry().list_instances(session_key)
    manager = _manager()
    live = manager.live_handles(session_key) if manager is not None else set()

    def reconciled(row: dict) -> dict:
        # Never mutate the registry's cached record -- a rewrite would be read
        # back on the next call as if it had come from disk.
        if row.get("status") in ("pending", "running") and (row.get("agent"), row.get("handle")) not in live:
            return {**row, "status": "interrupted"}
        return row

    handoff = getattr(_loop(), "_direct_handoff", None)
    return {
        "instances": [reconciled(row) for row in rows],
        "pending_handoff_count": handoff.pending_count(session_key) if handoff is not None else 0,
    }


async def instances_history(params: dict) -> dict:
    """One instance's direct chat, flattened to alternating user/assistant turns.

    Read from the record directories rather than from ``messages.json``: the
    records are the audit trail and exist for every kind, while the state file
    is absent for a cli instance and carries tool turns the user never typed.
    """
    try:
        root = direct_root(_session_dir(params.get("session_key", "")), params.get("agent", ""), params.get("handle", ""))
    except (FileNotFoundError, OSError):
        return {"turns": []}
    if not root.is_dir():
        return {"turns": []}

    turns: list[dict] = []
    # call_id is "<UTC timestamp>-<hex>", so lexical order is chronological.
    for call in sorted(p for p in root.iterdir() if p.is_dir()):
        meta = _read_json(call / "meta.json")
        prompt = _read_text(call / "prompt.md")
        out = _read_text(call / "out.md")
        if prompt is not None:
            turns.append(
                {
                    "call_id": call.name,
                    "role": "user",
                    "content": prompt,
                    "at_ms": int(meta.get("started_at_ms") or 0),
                    "prompt_path": str(call / "prompt.md"),
                    "out_path": None,
                }
            )
        if out is not None:
            turns.append(
                {
                    "call_id": call.name,
                    "role": "assistant",
                    "content": out,
                    "at_ms": int(meta.get("ended_at_ms") or meta.get("started_at_ms") or 0),
                    "prompt_path": None,
                    "out_path": str(call / "out.md"),
                }
            )
    return {"turns": turns}


async def instances_forget(params: dict) -> dict:
    """Drop one instance's registry row. The record directories stay: they are
    the audit trail, and their lifetime is the chat session's."""
    removed = await get_registry().forget(
        params.get("session_key", ""), params.get("agent", ""), params.get("handle", "")
    )
    return {"removed": bool(removed)}


async def instances_cancel(params: dict) -> dict:
    """Stop whatever is running on one instance, killing its process group."""
    manager = _manager()
    if manager is None:
        return {"cancelled": False}
    cancelled = await manager.cancel_by_instance(
        params.get("session_key", ""), params.get("agent", ""), params.get("handle", "")
    )
    return {"cancelled": bool(cancelled)}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def register_instance_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register the ``subagents.instance*`` methods on a dispatcher instance."""
    global _loop_factory
    _loop_factory = agent_loop_factory
    dispatcher.register("subagents.instances", instances_list)
    dispatcher.register("subagents.instance.history", instances_history)
    dispatcher.register("subagents.instance.forget", instances_forget)
    dispatcher.register("subagents.instance.cancel", instances_cancel)


__all__ = [
    "instances_list",
    "instances_history",
    "instances_forget",
    "instances_cancel",
    "register_instance_methods",
]
```

A module-level `_loop_factory` mirrors how `methods/subagents.py` keeps `_RUNNING` at module scope; if the dispatcher on this branch instead threads state through closures for every group, follow that pattern here rather than introducing module state.

- [ ] **Step 4: Register the group**

In `raven/rpc/server.py`, find the call to `register_subagents_methods` and add beside it:

```python
    register_instance_methods(dispatcher, agent_loop_factory=agent_loop_factory)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_rpc_subagents.py -v`
Expected: PASS

- [ ] **Step 6: Confirm the registration guard is satisfied**

Run: `uv run pytest tests/test_rpc_registration.py -v`
Expected: PASS - every schema method is registered, and nothing is registered that the schema does not declare.

- [ ] **Step 7: Commit**

```bash
git add raven/rpc/methods/instances.py raven/rpc/server.py tests/test_rpc_subagents.py
git commit -m "feat(rpc): serve the session's live sub-agent instances"
```

---

### Task 11: `turn.send` target plumbing and event tagging

> **Landed.** Two departures from the draft below: the tag is *absent* rather
> than null on a main-agent event (so existing payloads keep their shape), and
> the conversation-to-target map is an optional `build_rpc_spine` parameter owned
> by the caller -- the shape `readback_texts` already uses -- rather than a fifth
> return value. The draft also missed that `_merge_consecutive_token_deltas`
> rebuilds a merged payload and so dropped the tag; that is fixed and pinned.


**Files:**
- Modify: `raven/rpc/methods/turn.py:106-187` (`turn_send`)
- Test: `tests/test_rpc_session.py` or the file that already covers `turn_send` (find it with `grep -rln "turn_send" tests/`)

**Interfaces:**
- Consumes: `TurnSendParams.target` (Task 9), `TurnRequest.direct_target` (Task 7).
- Produces: every emitted turn event carries `payload["target"]` (`None` for a main-agent turn).

- [ ] **Step 1: Write the failing test**

Add to the file that covers `turn_send`:

```python
@pytest.mark.asyncio
async def test_target_becomes_direct_target_on_the_request():
    submitted: list[Any] = []

    class StubScheduler:
        def submit(self, req):
            submitted.append(req)
            return object()

    await turn_send(
        {
            "session_key": "s1",
            "content": "fix it",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        },
        emitter=None,
        scheduler=StubScheduler(),
    )

    assert submitted[0].direct_target == ("Raven-Code", "refactor-auth")


@pytest.mark.asyncio
async def test_no_target_leaves_direct_target_none():
    submitted: list[Any] = []

    class StubScheduler:
        def submit(self, req):
            submitted.append(req)
            return object()

    await turn_send({"session_key": "s1", "content": "hi"}, emitter=None, scheduler=StubScheduler())
    assert submitted[0].direct_target is None


@pytest.mark.asyncio
async def test_message_start_carries_the_target():
    events: list[dict] = []

    class StubEmitter:
        async def emit(self, session_key, event):
            events.append(event)

    class StubScheduler:
        def submit(self, req):
            return object()

    await turn_send(
        {
            "session_key": "s1",
            "content": "fix it",
            "target": {"agent": "Raven-Code", "handle": "refactor-auth"},
        },
        emitter=StubEmitter(),
        scheduler=StubScheduler(),
    )

    start = next(e for e in events if e["type"] == "message.start")
    assert start["payload"]["target"] == {"agent": "Raven-Code", "handle": "refactor-auth"}
```

Match the existing tests' `_resolve_model` patching in that file - `turn_send` calls it before anything else and it will raise without a routable model.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rpc_session.py -k target -v`
Expected: FAIL - `AttributeError: 'TurnRequest' object has no attribute 'direct_target'` is already fixed, so instead: `submitted[0].direct_target is None` for the targeted case.

- [ ] **Step 3: Thread the target through**

In `raven/rpc/methods/turn.py`, inside `turn_send`, build the tuple and pass it to `TurnRequest`:

```python
    target = (parsed.target.agent, parsed.target.handle) if parsed.target is not None else None
```

```python
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel=parsed.channel or default_channel,
            chat_id=parsed.chat_id or "default",
            sender_id=parsed.sender_id or "user",
            chat_type=ChatType.DM,
        ),
        text=parsed.content,
        conversation=parsed.session_key,
        direct_target=target,
    )
```

There is no payload helper on this branch: the `message.start` payload is written as an inline `{"turn_id": turn_id}` literal at two sites (`raven/rpc/methods/turn.py:142`, inside `_emit_start_then_error`, and `:229`, in `turn_send`). Introduce the helper rather than editing two literals that will drift:

```python
def _start_payload(turn_id: str, target: tuple[str, str] | None = None) -> dict[str, Any]:
    """The ``message.start`` payload, tagged with the turn's direct target.

    A client can switch instances mid-flight or reconnect, so it cannot infer
    which transcript a stream belongs to from its own state -- only from the
    event. An untagged event is the main conversation's.
    """
    return {
        "turn_id": turn_id,
        "target": None if target is None else {"agent": target[0], "handle": target[1]},
    }
```

Use it at both sites. `_emit_start_then_error` needs a `target` parameter threaded from its three call sites (`raven/rpc/methods/turn.py:185`, `:189`, `:217`); at the first two the target is `parsed.target`, and at the third likewise - a turn that failed to submit still belonged to whatever the client addressed.

The sink that emits `token.delta` and `message.complete` lives in `build_tui`, not here. Tag those too: find where the sink builds its payloads (`grep -rn "token.delta" raven/`) and carry the turn's target through the same per-session slot this module already uses for `turn_id` (the `turn_ids` dict threaded in as a parameter). A `direct_targets: dict[str, tuple[str, str] | None]` beside it, cleared by the same `clear_active` / `on_turn_end` path, is the shape that cannot drift out of sync with `turn_ids`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rpc_session.py -k target -v`
Expected: PASS

- [ ] **Step 5: Run the RPC suite**

Run: `uv run pytest tests/test_rpc_session.py tests/test_rpc_spine.py tests/test_rpc_registration.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add raven/rpc/methods/turn.py tests/
git commit -m "feat(rpc): route a turn to one sub-agent instance and tag its events"
```

---

## Phase 4 - TUI

Deliverable at end of phase: the feature is usable in `raven tui`. Verifiable by `npm test`, `npx tsc -b`, and the manual script in Task 15.

### Task 12: The `$directChat` store

**Files:**
- Create: `ui-tui/src/app/directChatStore.ts`
- Test: `ui-tui/src/__tests__/directChatMode.test.ts` (new)

**Interfaces:**
- Consumes: the generated `InstanceRow` / `DirectTurn` types from Task 9.
- Produces:
  - `$directChat` atom, `getDirectChat()`, `patchDirectChat(next)`, `resetDirectChat()`
  - `directKey(agent, handle) -> string`
  - `enterDirect(agent, handle)`, `leaveDirect()`
  - `setDirectTranscript(key, msgs)`, `appendDirectMessage(key, msg)`
  - `rememberScroll(key, offset)`, `recallScroll(key)`

- [ ] **Step 1: Write the failing test**

Create `ui-tui/src/__tests__/directChatMode.test.ts`:

```typescript
import { beforeEach, describe, expect, it } from 'vitest'

import {
  appendDirectMessage,
  directKey,
  enterDirect,
  getDirectChat,
  leaveDirect,
  recallScroll,
  rememberScroll,
  resetDirectChat,
  setDirectTranscript
} from '../app/directChatStore.js'

beforeEach(() => {
  resetDirectChat()
})

describe('directChatStore', () => {
  it('starts on the main agent', () => {
    expect(getDirectChat().active).toBeNull()
  })

  it('enters and leaves a direct target', () => {
    enterDirect('Raven-Code', 'refactor-auth')
    expect(getDirectChat().active).toEqual({ agent: 'Raven-Code', handle: 'refactor-auth' })
    leaveDirect()
    expect(getDirectChat().active).toBeNull()
  })

  it('keys a transcript by agent and handle', () => {
    // Length-prefixed: see the collision test below for why a bare join fails.
    expect(directKey('Raven-Code', 'refactor-auth')).toBe('10:Raven-Code/refactor-auth')
  })

  it('does not collide two handles that concatenate alike', () => {
    expect(directKey('a/b', 'c')).not.toBe(directKey('a', 'b/c'))
  })

  it('keeps each instance transcript separate', () => {
    const a = directKey('A', 'one')
    const b = directKey('B', 'two')
    appendDirectMessage(a, { role: 'user', text: 'to a' })
    appendDirectMessage(b, { role: 'user', text: 'to b' })
    expect(getDirectChat().transcripts.get(a)).toHaveLength(1)
    expect(getDirectChat().transcripts.get(b)).toHaveLength(1)
    expect(getDirectChat().transcripts.get(a)?.[0]?.text).toBe('to a')
  })

  it('remembers a scroll offset per instance', () => {
    rememberScroll(directKey('A', 'one'), 42)
    rememberScroll(directKey('B', 'two'), 7)
    expect(recallScroll(directKey('A', 'one'))).toBe(42)
    expect(recallScroll(directKey('B', 'two'))).toBe(7)
  })

  it('recalls zero for an instance never scrolled', () => {
    expect(recallScroll(directKey('never', 'seen'))).toBe(0)
  })

  it('replaces a transcript wholesale on a history load', () => {
    const k = directKey('A', 'one')
    appendDirectMessage(k, { role: 'user', text: 'stale' })
    setDirectTranscript(k, [{ role: 'user', text: 'fresh' }])
    expect(getDirectChat().transcripts.get(k)).toHaveLength(1)
    expect(getDirectChat().transcripts.get(k)?.[0]?.text).toBe('fresh')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui-tui && npx vitest run src/__tests__/directChatMode.test.ts`
Expected: FAIL - cannot resolve `../app/directChatStore.js`

- [ ] **Step 3: Write the store**

Create `ui-tui/src/app/directChatStore.ts`, following the header and `atom` idiom of `ui-tui/src/app/delegationStore.ts` (copy its SPDX header verbatim):

```typescript
import { atom } from 'nanostores'

import type { InstanceRow } from '../rpc/generated.js'
import type { Msg } from '../types.js'

export interface DirectTargetRef {
  agent: string
  handle: string
}

export interface DirectChatState {
  // null = the main Raven conversation.
  active: DirectTargetRef | null
  instances: InstanceRow[]
  pendingHandoffCount: number
  // Keyed by directKey(agent, handle).
  scrollPos: Map<string, number>
  transcripts: Map<string, Msg[]>
}

const buildState = (): DirectChatState => ({
  active: null,
  instances: [],
  pendingHandoffCount: 0,
  scrollPos: new Map(),
  transcripts: new Map()
})

export const $directChat = atom<DirectChatState>(buildState())

export const getDirectChat = () => $directChat.get()

export const patchDirectChat = (next: Partial<DirectChatState>) =>
  $directChat.set({ ...$directChat.get(), ...next })

export const resetDirectChat = () => $directChat.set(buildState())

// Length-prefixed rather than a bare join: a handle is free-form model-chosen
// text, so `a/b` + `c` and `a` + `b/c` would otherwise share one transcript.
export const directKey = (agent: string, handle: string) => `${agent.length}:${agent}/${handle}`

export const enterDirect = (agent: string, handle: string) => patchDirectChat({ active: { agent, handle } })

export const leaveDirect = () => patchDirectChat({ active: null })

export const setDirectTranscript = (key: string, msgs: Msg[]) => {
  const transcripts = new Map($directChat.get().transcripts)
  transcripts.set(key, msgs)
  patchDirectChat({ transcripts })
}

export const appendDirectMessage = (key: string, msg: Msg) => {
  const transcripts = new Map($directChat.get().transcripts)
  transcripts.set(key, [...(transcripts.get(key) ?? []), msg])
  patchDirectChat({ transcripts })
}

export const rememberScroll = (key: string, offset: number) => {
  const scrollPos = new Map($directChat.get().scrollPos)
  scrollPos.set(key, offset)
  patchDirectChat({ scrollPos })
}

export const recallScroll = (key: string) => $directChat.get().scrollPos.get(key) ?? 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui-tui && npx vitest run src/__tests__/directChatMode.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ui-tui && npm run fmt && cd ..
git add ui-tui/src/app/directChatStore.ts ui-tui/src/__tests__/directChatMode.test.ts
git commit -m "feat(ui-tui): hold the direct-chat target, transcripts and scroll offsets"
```

---

### Task 13: The instance chip strip

**Files:**
- Create: `ui-tui/src/components/instanceChips.tsx`
- Modify: `ui-tui/src/components/appLayout.tsx:235` (mount it above `QueuedMessages`)
- Test: `ui-tui/src/__tests__/instanceChips.test.tsx` (new)

**Interfaces:**
- Consumes: `$directChat`, `directKey` (Task 12).
- Produces: `<InstanceChips cols={number} t={Theme} />`, and the pure helper `chipsForWidth(rows, active, cols) -> {chips: Chip[], overflow: number}` that the tests drive directly.

- [ ] **Step 1: Write the failing test**

Create `ui-tui/src/__tests__/instanceChips.test.tsx`:

```typescript
import { describe, expect, it } from 'vitest'

import { chipsForWidth } from '../components/instanceChips.js'

const row = (agent: string, handle: string, status: string, updatedAtMs: number) => ({
  agent,
  createdAtMs: 0,
  handle,
  kind: 'cli',
  sessionKey: 's1',
  status,
  updatedAtMs
})

describe('chipsForWidth', () => {
  it('always leads with the main-agent chip', () => {
    const { chips } = chipsForWidth([], null, 80)
    expect(chips[0]?.label).toBe('Raven')
    expect(chips[0]?.target).toBeNull()
  })

  it('orders instances most recently updated first', () => {
    const { chips } = chipsForWidth(
      [row('A', 'old', 'completed', 1), row('B', 'new', 'completed', 9)],
      null,
      120
    )
    expect(chips.slice(1).map(c => c.target?.agent)).toEqual(['B', 'A'])
  })

  it('marks a running instance', () => {
    const { chips } = chipsForWidth([row('A', 'h', 'running', 1)], null, 120)
    expect(chips[1]?.running).toBe(true)
  })

  it('does not mark an interrupted instance as running', () => {
    const { chips } = chipsForWidth([row('A', 'h', 'interrupted', 1)], null, 120)
    expect(chips[1]?.running).toBe(false)
  })

  it('marks the active chip', () => {
    const { chips } = chipsForWidth([row('A', 'h', 'completed', 1)], { agent: 'A', handle: 'h' }, 120)
    expect(chips[1]?.active).toBe(true)
    expect(chips[0]?.active).toBe(false)
  })

  it('truncates from the right and reports the overflow', () => {
    const rows = Array.from({ length: 12 }, (_, i) => row(`agent${i}`, `handle${i}`, 'completed', i))
    const { chips, overflow } = chipsForWidth(rows, null, 40)
    expect(overflow).toBeGreaterThan(0)
    expect(chips.length).toBeLessThan(rows.length + 1)
  })

  it('never drops the main chip, however narrow', () => {
    const rows = Array.from({ length: 12 }, (_, i) => row(`agent${i}`, `handle${i}`, 'completed', i))
    const { chips } = chipsForWidth(rows, null, 8)
    expect(chips[0]?.label).toBe('Raven')
  })

  it('keeps the active chip visible even when it would overflow', () => {
    const rows = Array.from({ length: 12 }, (_, i) => row(`agent${i}`, `handle${i}`, 'completed', i))
    const { chips } = chipsForWidth(rows, { agent: 'agent0', handle: 'handle0' }, 40)
    expect(chips.some(c => c.target?.agent === 'agent0')).toBe(true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui-tui && npx vitest run src/__tests__/instanceChips.test.tsx`
Expected: FAIL - cannot resolve `../components/instanceChips.js`

- [ ] **Step 3: Write the component**

Create `ui-tui/src/components/instanceChips.tsx`. Copy the SPDX header from `ui-tui/src/components/queuedMessages.tsx`, and follow its `Box`/`Text` and theme-prop idiom.

```typescript
export interface Chip {
  active: boolean
  label: string
  running: boolean
  target: DirectTargetRef | null
}

const MAIN_CHIP: Chip = { active: true, label: 'Raven', running: false, target: null }

/**
 * Which chips fit in `cols`, and how many were dropped.
 *
 * Kept pure and exported so the fitting rule is testable without a renderer.
 * Two chips are never dropped: the main-agent chip (it is the way back) and the
 * active one (a strip that hides where you are is worse than a truncated one).
 */
export function chipsForWidth(
  rows: readonly InstanceRow[],
  active: DirectTargetRef | null,
  cols: number
): { chips: Chip[]; overflow: number } {
  const ordered = [...rows].sort((a, b) => (b.updatedAtMs ?? 0) - (a.updatedAtMs ?? 0))
  const all: Chip[] = [
    { ...MAIN_CHIP, active: active === null },
    ...ordered.map(r => ({
      active: active !== null && active.agent === r.agent && active.handle === r.handle,
      label: `${r.agent}/${r.handle}`,
      running: r.status === 'running' || r.status === 'pending',
      target: { agent: r.agent, handle: r.handle }
    }))
  ]

  const width = (c: Chip) => c.label.length + (c.running ? 4 : 3)
  const kept: Chip[] = [all[0]!]
  let used = width(all[0]!)

  for (const chip of all.slice(1)) {
    const next = used + width(chip)
    if (next <= cols || chip.active) {
      kept.push(chip)
      used = next
    }
  }

  return { chips: kept, overflow: all.length - kept.length }
}
```

Then the component itself: subscribe with `useStore($directChat)`, call `chipsForWidth`, render each chip as `[label]` with `theme.color.label` when active, `theme.color.muted` otherwise, and a bullet when running; append `+N` when `overflow > 0`. Give each chip an `onClick` that calls `enterDirect` (or `leaveDirect` for the main chip).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui-tui && npx vitest run src/__tests__/instanceChips.test.tsx`
Expected: PASS

- [ ] **Step 5: Mount it**

In `ui-tui/src/components/appLayout.tsx`, immediately above the `<QueuedMessages` element at line 235:

```tsx
      <InstanceChips cols={composer.cols} t={ui.theme} />
```

`ComposerPane` is `flexShrink={0}`, so the strip must stay one row - that is what `chipsForWidth` guarantees.

- [ ] **Step 6: Typecheck and test**

Run: `cd ui-tui && npx tsc -b --noEmit && npx vitest run`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd ui-tui && npm run fmt && cd ..
git add ui-tui/src/components/instanceChips.tsx ui-tui/src/components/appLayout.tsx ui-tui/src/__tests__/instanceChips.test.tsx
git commit -m "feat(ui-tui): list the session's sub-agent instances above the composer"
```

---

### Task 14: Mode takeover, greying, and keys

**Files:**
- Modify: `ui-tui/src/components/appLayout.tsx` (ChatStream source swap; hint line)
- Modify: `ui-tui/src/app/turnController.ts:1136` region (route target-tagged events)
- Modify: `ui-tui/src/app/useInputHandlers.ts:420-505` (Esc, Ctrl+X, Ctrl+Left/Right)
- Modify: `ui-tui/src/app/useSubmission.ts` (send `target` when in direct mode)
- Test: `ui-tui/src/__tests__/directChatMode.test.ts`, `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`

**Interfaces:**
- Consumes: everything from Tasks 12 and 13, plus the `target`-tagged events from Task 11.
- Produces: no new exported API; this task wires existing pieces.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/directChatMode.test.ts`:

```typescript
describe('direct mode routing and gating', () => {
  it('sends the active target with a submitted turn', async () => {
    const sent: unknown[] = []
    const gw = { request: async (m: string, p: unknown) => { sent.push([m, p]); return { accepted: true, turn_id: 't' } } }
    enterDirect('Raven-Code', 'refactor-auth')
    await submitForTest(gw, 'fix it')
    expect(sent[0]).toEqual([
      'turn.send',
      expect.objectContaining({ target: { agent: 'Raven-Code', handle: 'refactor-auth' } })
    ])
  })

  it('sends no target on the main conversation', async () => {
    const sent: unknown[] = []
    const gw = { request: async (m: string, p: unknown) => { sent.push([m, p]); return { accepted: true, turn_id: 't' } } }
    leaveDirect()
    await submitForTest(gw, 'hi')
    expect((sent[0] as [string, Record<string, unknown>])[1].target).toBeUndefined()
  })

  it('greys the composer while the main agent is replying', () => {
    leaveDirect()
    patchUiState({ busy: true })
    enterDirect('A', 'h')
    expect(composerDisabled()).toBe(true)
  })

  it('greys the composer on the main conversation while a direct turn is in flight', () => {
    enterDirect('A', 'h')
    patchUiState({ busy: true })
    leaveDirect()
    expect(composerDisabled()).toBe(true)
  })

  it('leaves the composer live when nothing is in flight', () => {
    patchUiState({ busy: false })
    enterDirect('A', 'h')
    expect(composerDisabled()).toBe(false)
  })
})
```

And to `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`:

```typescript
it('routes a target-tagged delta to the instance transcript, not the main one', () => {
  enterDirect('Raven-Code', 'refactor-auth')
  const handle = makeHandler()

  handle({
    payload: { target: { agent: 'Raven-Code', handle: 'refactor-auth' }, text: 'from the subagent' },
    type: 'token.delta'
  })

  expect(getDirectChat().transcripts.get(directKey('Raven-Code', 'refactor-auth'))).toHaveLength(1)
  expect(mainTranscriptRows()).toHaveLength(0)
})

it('routes an untagged delta to the main transcript even while in direct mode', () => {
  enterDirect('Raven-Code', 'refactor-auth')
  const handle = makeHandler()

  handle({ payload: { target: null, text: 'from raven' }, type: 'token.delta' })

  expect(getDirectChat().transcripts.get(directKey('Raven-Code', 'refactor-auth')) ?? []).toHaveLength(0)
  expect(mainTranscriptRows()).toHaveLength(1)
})

it('routes a delta for a non-active instance to that instance, not the visible one', () => {
  enterDirect('A', 'one')
  const handle = makeHandler()

  handle({ payload: { target: { agent: 'B', handle: 'two' }, text: 'late reply' }, type: 'token.delta' })

  expect(getDirectChat().transcripts.get(directKey('B', 'two'))).toHaveLength(1)
  expect(getDirectChat().transcripts.get(directKey('A', 'one')) ?? []).toHaveLength(0)
})
```

The last case is the one that matters most: after Esc, an in-flight direct turn keeps streaming while the user is back on the main conversation, so routing by "what is visible" rather than "what the event says" corrupts both transcripts.

Build `makeHandler`, `mainTranscriptRows` and `submitForTest` from the harness the existing tests in each file already use - read them first rather than inventing a second harness.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ui-tui && npx vitest run src/__tests__/directChatMode.test.ts src/__tests__/createGatewayEventHandler.test.ts`
Expected: FAIL

- [ ] **Step 3: Route events by their tag**

In `ui-tui/src/app/turnController.ts`, at the entry points that consume stream events (`recordMessageDelta`, `recordMessageComplete`, `startMessage`, `recordError`), branch on the payload's `target` **before** touching the main chat stream: a tagged event appends to `getDirectChat().transcripts` via `appendDirectMessage(directKey(agent, handle), ...)`; an untagged one behaves exactly as today.

Route on the event's own tag, never on `getDirectChat().active`.

- [ ] **Step 4: Send the target**

In `ui-tui/src/app/useSubmission.ts`, where it builds the `turn.send` params, add:

```typescript
  const active = getDirectChat().active
```

and spread `...(active ? { target: active } : {})` into the params. Omit the key entirely when on the main conversation - `TurnSendParams` is `extra="forbid"`, so a literal `target: undefined` serialised as `null` would still validate, but omitting it keeps the wire shape identical to today's for every existing client.

- [ ] **Step 5: Swap the ChatStream source and add the hint**

In `ui-tui/src/components/appLayout.tsx`, where the transcript rows are built (the `transcript.virtualHistory` block at lines 107-147), choose the source by `getDirectChat().active`: `null` keeps today's rows, otherwise render that instance's transcript. On every switch, `rememberScroll` the outgoing key and `recallScroll` the incoming one.

Under the composer, when the composer is disabled, print the reason rather than leaving a dead input:

- main agent busy, user in direct mode: `Raven is replying, sending is paused`
- direct turn in flight, user back on main: `<agent>/<handle> is still replying; you can continue once it lands`

- [ ] **Step 6: Bind the keys**

In `ui-tui/src/app/useInputHandlers.ts`:

- **Esc**: add a branch *after* the existing voice (line 420), queue-edit (427) and selection (431) branches, and *before* the cancel-turn path: when `getDirectChat().active !== null`, call `leaveDirect()` and return. Unconditional - in flight or not. This is D8: one meaning, no branch, so "I thought I was leaving and it cancelled the run" cannot happen.
- **Ctrl+X**: dropped. `turn.cancel` (Ctrl+C) already stops a direct chat, because it is the session's own turn. (Superseded by the concurrent-direct-chats design, D3: on its own lane a direct chat is not cancellable, so Ctrl+X stays dropped but Ctrl+C no longer reaches it either.)
- **Ctrl+Left / Ctrl+Right**: cycle through `chipsForWidth(...).chips`, calling `enterDirect` / `leaveDirect`.

- [ ] **Step 7: Refresh the instance list on sub-agent events**

Where `createGatewayEventHandler` handles `subagent.start` / `subagent.spawn_requested` / `subagent.complete` (lines 599-618 and nearby), call `subagents.instances` and `patchDirectChat({ instances, pendingHandoffCount })`. Also fetch once on startup and once per session switch (`useSessionLifecycle.ts`). Do not poll: the registry is a file with no change notification, and these events are the only signal that it moved.

- [ ] **Step 8: Run all TUI tests and typecheck**

Run: `cd ui-tui && npx tsc -b --noEmit && npx vitest run && npm run lint`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
cd ui-tui && npm run fmt && cd ..
git add ui-tui/src
git commit -m "feat(ui-tui): take over the chat view for a direct sub-agent conversation"
```

---

### Task 15: Gates, domain terms, and manual verification

**Files:**
- Modify: `CONTEXT.md` (runtime terms), `ui-tui/CONTEXT.md` (TUI terms)
- Modify: `docs/specs/2026-08-13-tui-subagent-direct-chat-design.md` (`Status:`)
- Test: the full suite

**Interfaces:**
- Consumes: everything.
- Produces: nothing in code.

- [ ] **Step 1: Define the new domain terms**

`CLAUDE.md` section 6 requires a new domain term to be defined in the matching `CONTEXT.md` in the same change, with a definition verifiable against the code.

In `ui-tui/CONTEXT.md`, in the Language section beside **Agents Overlay** and **Subagents Overlay**:

```markdown
**Direct Chat**:
The mode in which the chat view is taken over by one sub-agent instance's own
conversation: same composer, that instance's transcript, Esc to return. Tracked
in `directChatStore`'s `active` field; `null` means the main Raven conversation.
A direct-chat turn is `turn.send` with a `target`, and is never written to the
session transcript.
_Avoid_: "sub-agent session" - that is the CLI-side session a handle resumes,
not this view.

**Instance Chip**:
One entry in the strip above the composer, naming a sub-agent instance this
session has used and switching to its Direct Chat when selected. The first chip
is always the way back to the main agent.
_Avoid_: "agent chip" - a chip is one *instance* of an agent, and one agent can
have several.
```

In `CONTEXT.md`, beside the sub-agent runtime terms:

```markdown
**Handoff Block**:
The pointer block the runtime prepends to the user's next turn to the main agent
after one or more Direct Chats: per instance, a UTC time span and the absolute
paths of each turn's `prompt.md` and `out.md`. Carries no transcript text.
Accumulated per session by `DirectChatHandoff` and taken-and-cleared on the
next turn that has no `direct_target`.
_Avoid_: "handoff summary" - it is deliberately not a summary; nothing in it is
generated.
```

Check `CONTEXT-MAP.md` routes to both files and add an entry if the term list there is enumerated rather than pointed at.

- [ ] **Step 2: Run the full Python suite**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 3: Run lint**

Run: `make lint`
Expected: PASS. Only GitLab CI's Python unit suite runs on a merge request; lint failures otherwise surface much later, at the monthly GitHub PR.

- [ ] **Step 4: Run the TUI gates**

Run: `cd ui-tui && npx tsc -b --noEmit && npx vitest run && npm run lint && npm run build`
Expected: PASS

- [ ] **Step 5: Check the commit range**

Run: `make commitlint`
Expected: PASS - every commit ASCII-only, Conventional Commits, header <= 100 chars.

- [ ] **Step 6: Add the integration smoke test**

The unit tests all stub the backend. This one drives the whole path with a real
`AgentLoop`, a real registry file and real record directories, so the wiring
between the four phases is exercised once end to end.

Create `tests/integration/test_direct_chat_smoke.py` (`CLAUDE.md` section 5.2:
`test_<scope>_<kind>.py`, no version or ticket in the scope):

```python
"""Direct chat end to end: spawn, switch in, follow up, hand off.

Uses a real AgentLoop, registry file and record directories. The only stub is
the LLM provider - this is about the wiring between the sub-agent manager, the
turn pipeline and the handoff, not about model quality.
"""

from __future__ import annotations

import pytest

from raven.spine import Origin, Source, TurnRequest
from raven.spine.turn import ChatType


def _req(text: str, *, target=None):
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        conversation="s1",
        direct_target=target,
    )


@pytest.mark.asyncio
async def test_direct_chat_round_trip_leaves_records_and_a_handoff(tmp_path, stub_loop):
    emitted: list[str] = []

    async def emit(event):
        text = getattr(event, "content", None)
        if text:
            emitted.append(text)

    # Two direct turns on one instance.
    await stub_loop.run_turn(_req("first", target=("raven", "notes")), emit)
    await stub_loop.run_turn(_req("second", target=("raven", "notes")), emit)

    # Both are pending for the main agent, and neither reached the transcript.
    assert stub_loop._direct_handoff.pending_count("s1") == 2
    assert stub_loop.sessions.get_or_create("s1").messages == []

    # Two record directories, each with a prompt and an output.
    from raven.agent.subagent.direct_chat import direct_root

    root = direct_root(stub_loop.subagents._session_dir("s1"), "raven", "notes")
    calls = sorted(p for p in root.iterdir() if p.is_dir())
    assert len(calls) == 2
    for call in calls:
        assert (call / "prompt.md").exists()
        assert (call / "out.md").exists()

    # The instance is listed, and the state file carries both user turns.
    from raven.agent.subagent.instance_state import InstanceState, instance_state_path
    from raven.agent.subagent.instances import get_registry

    rows = get_registry().list_instances("s1")
    assert ("raven", "notes") in {(r["agent"], r["handle"]) for r in rows}

    stored = InstanceState(instance_state_path(stub_loop.subagents._session_dir("s1"), "raven", "notes")).load()
    assert [m["content"] for m in stored if m["role"] == "user"] == ["first", "second"]

    # The next main-agent turn carries the block and clears it.
    seen = await stub_loop.run_turn(_req("what happened?"), emit)  # noqa: F841
    assert stub_loop._direct_handoff.pending_count("s1") == 0
```

Write the `stub_loop` fixture in `tests/integration/conftest.py` if that file
already hosts loop fixtures, otherwise inline it in this module. It must point
the registry at `tmp_path` (monkeypatch `default_registry_path`) so the test
never touches `~/.raven/subagent_instances.json`.

Run: `uv run pytest tests/integration/test_direct_chat_smoke.py -v`
Expected: PASS

- [ ] **Step 7: Manual verification in a real TUI**

This is the part no unit test covers. Run `uv run raven tui` and walk it:

1. Ask Raven to spawn a default sub-agent with a named instance ("spawn a subagent with instance 'notes' to summarise README.md"). A chip `raven/notes` appears once the spawn registers.
2. Click it, or `Ctrl+Right` to it. The transcript switches to that instance and shows the spawn's prompt and reply.
3. Send a follow-up. Confirm the reply demonstrates it remembers the first exchange - this is the `messages.json` replay.
4. `Esc`. Confirm you are back on the main conversation and the chip strip still lists the instance.
5. Send a turn to Raven. Confirm its reply shows it knows a direct chat happened - and that it names the file paths rather than quoting the exchange.
6. `cat` one of the named `out.md` paths and confirm it holds what the sub-agent said.
7. With a `cli` instance (e.g. `Raven-Code`) that is mid-run, switch into it and confirm the composer is greyed with the reason printed, and that history still scrolls.
8. `Esc` out of an in-flight direct turn. Confirm the main composer is greyed with its own reason, and that when the turn lands the reply appears in the *instance* transcript, not the main one.

Record any deviation as a finding rather than fixing it inline - a fix at this point belongs in its own commit.

- [ ] **Step 8: Mark the spec implemented**

Change the spec's header line to `Status: implemented`.

- [ ] **Step 9: Commit**

```bash
git add CONTEXT.md ui-tui/CONTEXT.md CONTEXT-MAP.md docs/specs/2026-08-13-tui-subagent-direct-chat-design.md
git commit -m "docs(*): define the direct-chat domain terms and mark the spec implemented"
```

---

## Deliberately not in this plan

- **Streaming for `cli`-kind direct chat.** `_exec` buffers the whole subprocess (`raven/agent/subagent/backends/cli_agent.py:168`); making it incremental touches four transcript parsers with different terminal-event semantics. Spec D6.
- **Any modified-file report.** No trustworthy source exists. Spec D7.
- **A web UI surface.** The mechanism is in the runtime so a web view can be added later without reimplementing the protocol; the view itself is not here.
- **Direct chat with a DAG node.** Needs a resume story for nodes first.
- **Landing the 36 unpushed local commits.** Task 1 ports two helpers from them verbatim; deciding what happens to the rest is separate work.

## Risks

1. **`stateful` widening is user-visible.** It changes the roster text `spawn` and `run_subagent_dag` advertise to the model, and relaxes a config validator. Rollback: revert `third_party_agent_meta` and the schema validator; direct chat then degrades to unavailable for `openai` instances, leaving `cli` and `raven-loop` working.
2. **The handoff prepend runs on every ordinary turn.** `take()` must return before touching anything when the list is empty, or this adds work to the main path for nothing. Task 8's `test_a_turn_with_nothing_pending_is_untouched` is the guard.
3. **Event routing by tag, not by visible mode.** Task 14 Step 3. Routing on `active` looks equivalent and is not: after Esc, a still-streaming direct turn would land in the main transcript. The third test in that step exists solely to pin this.
4. **The ported helpers will conflict on a future rebase.** Expected and cheap, because they are verbatim copies. Resolution is deleting one copy - never a semantic merge.
