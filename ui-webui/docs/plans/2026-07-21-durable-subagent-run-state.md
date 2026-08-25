# Durable & live sub-agent run state — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DAG runs and stateful sub-agent instance statuses survive page reload / SSE reconnect (durable Redis projection replayed on connect), and show a live, animated per-instance status + transport badge in the chat "Instances" monitor.

**Architecture:** Mirror the shipped DAG live-viz. An out-of-band `CustomEvent` drives the live overlay (as today); a matching write to the durable `SessionProjection` hash (the same store that makes HITL cards survive reload) makes it durable; an SSE-connect replay block re-emits the stored state as the same `CustomEvent`s so the frontend rebuilds with no new plumbing. Because out-of-band events never reach `EventProjector`, the durable write happens **inside the publishing closure**, calling `SessionProjection.upsert` + `.publish` directly.

**Tech Stack:** Python 3.11 (AgentScope backend), FastAPI SSE, Redis message bus / `SessionProjection`; React 19 + TypeScript + Vite + Tailwind (`examples/web_ui/frontend`).

**Spec:** `docs/superpowers/specs/2026-07-21-durable-subagent-run-state-design.md`

## Global Constraints

- **Encapsulation:** internal files/classes/functions are `_`-prefixed; exposed only through `__init__.py`.
- **Lazy imports:** the `agentscope.subagent` package must NOT import `agentscope.app` at module top. Reach `SessionProjection`/`publish_session_event` via a **function-body** import (precedent: `_agent_tools.py:103` `from ..app._tool import DeliverFiles`, and the existing `_publish_dag_progress` lazy import).
- **Event names / projection kinds are a cross-layer contract — use these exact strings on both ends:** events `dag_run_completed`, `subagent_instance_updated` (plus existing `dag_run_started`, `dag_node_updated`); projection kinds `"dag_run"`, `"subagent_instance"`; instance transports `"cli" | "codex" | "openai"`; instance statuses `"running" | "completed" | "failed"`.
- **Progress publishing must never fail a node/invocation:** every publish is guarded (swallow + `logger.warning(..., exc_info=True)`), matching the DAG runner's `_emit`.
- **Tests (backend):** `python -m pytest tests/<file> -v`. Whole-structure assertions (compare the entire dict/list, not field-by-field); use `AnyString`/`AnyValue` from `tests/utils.py` ONLY for nondeterministic fields (`run_id`, `agent_id`, `created_at`). Reuse the existing fakes in `tests/subagent_tool_test.py` (`_CannedBackend`, `_MemRegistry`, `_collect`) and `tests/subagent_dag_progress_test.py` (`_Recorder`, `_FakeSubAgent`).
- **E501 gotcha:** a stray `/Evermind/sh_evermind/xuedizhan/.flake8` (OUTSIDE the repo) sets `ignore=E501`, so local flake8 SILENTLY SKIPS line-length. Verify with `python -m flake8 --isolated --max-line-length=79 --select=E501 <files>` (black at 79 catches most, but not multi-context `with` lines).
- **Frontend:** files use **TABS**. No JS unit-test runner exists → per-task gate is `cd examples/web_ui/frontend && pnpm build` (tsc -b + vite) and `pnpm lint`. Never stage `dist/`, `*.tsbuildinfo`, or `.pid` files; the pnpm lockfile is at `examples/web_ui/pnpm-lock.yaml` (workspace root).
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

**Backend — new:**
- `src/agentscope/subagent/_dag/_projection.py` — pure `fold_dag_run_entry(prev, name, value, created_at)`; no app import.
- `src/agentscope/subagent/_instance_progress.py` — `emit_instance_status(publisher, ...)` guarded helper (shared by both tools).
- `src/agentscope/app/_service/_run_state_replay.py` — kind constants + pure `dag_run_replay_events(entries)` / `subagent_instance_replay_events(entries)` returning `list[CustomEvent]`.
- `tests/subagent_dag_projection_test.py`, `tests/subagent_instance_progress_test.py`, `tests/service_run_state_replay_test.py`.

**Backend — edit:**
- `src/agentscope/subagent/_dag/_tool.py` — emit `dag_run_completed` with the manifest.
- `src/agentscope/subagent/_agent_tools.py` — `build_dag_progress_publisher` + `build_instance_progress_publisher`; wire both in `_factory`; thread instance publisher into both tool constructors.
- `src/agentscope/subagent/_tool.py`, `_openai_tool.py` — accept `progress_publisher`; compute `_transport`; emit instance status on every path.
- `src/agentscope/app/_router/_session.py` — SSE §1c/§1d replay blocks.
- `src/agentscope/app/_service/_chat.py` (or the `SessionService.delete_session` it hosts) — purge `dag_run` + `subagent_instance` on session delete.

**Frontend — new:**
- `src/components/chat/SubagentInstancesContext.tsx`.

**Frontend — edit:**
- `src/components/chat/DagRunsContext.tsx`, `src/components/dag/deriveDag.ts`, `src/hooks/useMessages.ts`, `src/pages/chat/ChatViewport.tsx`, `src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx`, `src/components/subagent/SubagentInstanceMonitor.tsx`, `src/components/subagent/deriveInstances.ts`, `src/i18n/locales/en.json`, `src/i18n/locales/zh.json`.

---

## Task 1: DAG tool emits `dag_run_completed` with the manifest

**Files:**
- Modify: `src/agentscope/subagent/_dag/_tool.py` (the `call` method, ~165-189; imports).
- Test: `tests/subagent_dag_tool_test.py` (add a test).

**Interfaces:**
- Consumes: `self._progress_publisher: Callable[[str, dict], Awaitable[None]] | None` (already an init param).
- Produces: a `dag_run_completed` event with value `{"run_id": str, "manifest": {run_id, dir, terminal_outputs, files, summary}}`, emitted once, just before the terminal `SUCCESS` chunk.

- [ ] **Step 1: Write the failing test** in `tests/subagent_dag_tool_test.py`.

```python
async def test_emits_dag_run_completed_with_manifest(self) -> None:
    """The tool emits dag_run_completed carrying the terminal manifest."""
    events: list[tuple[str, dict]] = []

    async def _pub(name: str, value: dict) -> None:
        events.append((name, value))

    with tempfile.TemporaryDirectory() as workdir:
        tool = SubAgentDagTool(
            subagents={"s": _FakeSubAgent("s")},
            backend=LocalBackend(),
            workdir=workdir,
            progress_publisher=_pub,
        )
        chunks = [c async for c in tool.call(
            nodes=[{"id": "A", "subagent": "s", "prompt_template": "go"}],
        )]

    completed = [v for (n, v) in events if n == "dag_run_completed"]
    self.assertEqual(len(completed), 1)
    self.assertEqual(completed[0], {
        "run_id": AnyString(),
        "manifest": chunks[-1].metadata,
    })
```
(Reuse/import `_FakeSubAgent` from `tests.subagent_dag_progress_test`, `LocalBackend`, `SubAgentDagTool`, `AnyString`, `tempfile`.)

- [ ] **Step 2: Run it — expect FAIL** (`dag_run_completed` never emitted).

Run: `python -m pytest tests/subagent_dag_tool_test.py -v`

- [ ] **Step 3: Implement.** In `_dag/_tool.py`, add the logger import at top:

```python
from ..._logging import logger
```

Replace the terminal-chunk block (currently `yield ToolChunk(... metadata={...})` at ~178-189) with:

```python
        metadata = {
            "run_id": result.run_id,
            "dir": result.dir,
            "terminal_outputs": result.terminal_outputs,
            "files": result.files,
            "summary": result.summary,
        }
        if self._progress_publisher is not None:
            try:
                await self._progress_publisher(
                    "dag_run_completed",
                    {"run_id": result.run_id, "manifest": metadata},
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "dag_run_completed publish failed",
                    exc_info=True,
                )
        yield ToolChunk(
            content=[TextBlock(text="\n".join(lines))],
            state=ToolResultState.SUCCESS,
            is_last=True,
            metadata=metadata,
        )
```

- [ ] **Step 4: Run — expect PASS.** Also re-run `python -m pytest tests/subagent_dag_tool_test.py tests/subagent_dag_progress_test.py -v` (no regressions).

- [ ] **Step 5: E501 check + commit.**

Run: `python -m flake8 --isolated --max-line-length=79 --select=E501 src/agentscope/subagent/_dag/_tool.py`

```bash
git add src/agentscope/subagent/_dag/_tool.py tests/subagent_dag_tool_test.py
git commit -m "feat(subagent): emit dag_run_completed with the run manifest"
```

---

## Task 2: Durable `dag_run` projection (pure fold + publisher builder + factory wiring)

**Files:**
- Create: `src/agentscope/subagent/_dag/_projection.py`
- Modify: `src/agentscope/subagent/_agent_tools.py` (add `build_dag_progress_publisher`; replace the inline `_publish_dag_progress` closure with it).
- Test: `tests/subagent_dag_projection_test.py` (new).

**Interfaces:**
- Produces: `fold_dag_run_entry(prev: dict | None, name: str, value: dict, created_at: str) -> dict | None`; `build_dag_progress_publisher(message_bus, session_id) -> Callable[[str, dict], Awaitable[None]]`.
- The durable entry shape (projection kind `"dag_run"`, entry_id `run_id`): `{run_id, created_at, nodes:[{id,subagent,depends_on}], byNode:{id:status}, manifest?}`.
- Live `dag_run_started` events are **augmented with `created_at`** before publish (the frontend orders runs by it — §Task 8).

- [ ] **Step 1: Write the failing tests** in `tests/subagent_dag_projection_test.py`.

```python
# -*- coding: utf-8 -*-
"""Tests for durable DAG-run projection folding + publisher."""
import unittest

from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app._service._session_projection import SessionProjection
from agentscope.subagent._agent_tools import build_dag_progress_publisher
from agentscope.subagent._dag._projection import fold_dag_run_entry
from tests.utils import AnyString


class FoldDagRunEntryTest(unittest.TestCase):
    """The pure fold accumulates a self-contained per-run entry."""

    def test_fold_sequence(self) -> None:
        e = fold_dag_run_entry(
            None,
            "dag_run_started",
            {"run_id": "r1", "nodes": [
                {"id": "A", "subagent": "s", "depends_on": []},
                {"id": "B", "subagent": "s", "depends_on": ["A"]},
            ]},
            "T0",
        )
        e = fold_dag_run_entry(
            e, "dag_node_updated",
            {"run_id": "r1", "node": "A", "status": "completed"}, "T1")
        e = fold_dag_run_entry(
            e, "dag_run_completed",
            {"run_id": "r1", "manifest": {"run_id": "r1", "files": []}}, "T2")
        self.assertEqual(e, {
            "run_id": "r1",
            "created_at": "T0",
            "nodes": [
                {"id": "A", "subagent": "s", "depends_on": []},
                {"id": "B", "subagent": "s", "depends_on": ["A"]},
            ],
            "byNode": {"A": "completed", "B": "pending"},
            "manifest": {"run_id": "r1", "files": []},
        })

    def test_update_before_start_is_dropped(self) -> None:
        self.assertIsNone(fold_dag_run_entry(
            None, "dag_node_updated",
            {"run_id": "r1", "node": "A", "status": "running"}, "T0"))


class DagProgressPublisherTest(unittest.IsolatedAsyncioTestCase):
    """The publisher upserts a durable entry and publishes live events."""

    async def test_upserts_entry_and_augments_started(self) -> None:
        bus = InMemoryMessageBus()
        pub = build_dag_progress_publisher(bus, "sid")
        await pub("dag_run_started", {"run_id": "r1", "nodes": [
            {"id": "A", "subagent": "s", "depends_on": []}]})
        await pub("dag_node_updated",
                  {"run_id": "r1", "node": "A", "status": "completed"})
        await pub("dag_run_completed",
                  {"run_id": "r1", "manifest": {"run_id": "r1", "files": []}})

        entries = await SessionProjection(bus).list("sid", "dag_run")
        self.assertEqual(entries, [{
            "run_id": "r1",
            "created_at": AnyString(),
            "nodes": [{"id": "A", "subagent": "s", "depends_on": []}],
            "byNode": {"A": "completed"},
            "manifest": {"run_id": "r1", "files": []},
        }])
```

- [ ] **Step 2: Run — expect FAIL** (imports missing).

Run: `python -m pytest tests/subagent_dag_projection_test.py -v`

- [ ] **Step 3a: Create `src/agentscope/subagent/_dag/_projection.py`.**

```python
# -*- coding: utf-8 -*-
"""Pure folding of DAG progress events into a durable projection entry.

Kept free of any ``agentscope.app`` import so it stays in the
``subagent`` layer; the app-facing write (upsert/publish) lives in
``_agent_tools.build_dag_progress_publisher``.
"""


def fold_dag_run_entry(
    prev: dict | None,
    name: str,
    value: dict,
    created_at: str,
) -> dict | None:
    """Fold one progress event into a self-contained per-run entry.

    Args:
        prev (`dict | None`):
            The accumulated entry so far, or ``None`` before the run
            started.
        name (`str`):
            The event name (``dag_run_started`` / ``dag_node_updated`` /
            ``dag_run_completed``).
        value (`dict`):
            The event payload.
        created_at (`str`):
            Timestamp stamped onto a new entry (used only for
            ``dag_run_started``).

    Returns:
        `dict | None`:
            The updated entry, or ``None`` when an update arrives before
            the run started (dropped).
    """
    if name == "dag_run_started":
        nodes = value.get("nodes", [])
        return {
            "run_id": value.get("run_id"),
            "created_at": created_at,
            "nodes": nodes,
            "byNode": {n["id"]: "pending" for n in nodes},
        }
    if prev is None:
        return None
    entry = {**prev, "byNode": dict(prev["byNode"])}
    if name == "dag_node_updated":
        entry["byNode"][value["node"]] = value["status"]
    elif name == "dag_run_completed":
        entry["manifest"] = value["manifest"]
    return entry
```

- [ ] **Step 3b: Add the builder to `_agent_tools.py`.** Add `from datetime import datetime` to the top imports, and `from ._dag._projection import fold_dag_run_entry`. Add this module-level function (above `make_subagent_tool_factory`):

```python
def build_dag_progress_publisher(
    message_bus: Any,
    session_id: str,
) -> Callable[[str, dict], Awaitable[None]]:
    """Build a DAG progress publisher: durable upsert + live publish.

    Accumulates a self-contained per-run entry in-closure and writes it
    to the durable ``SessionProjection`` (kind ``"dag_run"``) on every
    event, then publishes the live ``CustomEvent``. ``dag_run_started``
    is augmented with ``created_at`` so the frontend can order runs.

    Args:
        message_bus (`MessageBus`):
            The app message bus (durable hash + live pub/sub).
        session_id (`str`):
            The session the events belong to.

    Returns:
        `Callable[[str, dict], Awaitable[None]]`:
            An async ``(name, value) -> None`` publisher.
    """
    runs: dict[str, dict] = {}

    async def _publish(name: str, value: dict) -> None:
        # Lazy import: subagent must not import app at module top.
        from ..app._service._session_projection import SessionProjection

        projection = SessionProjection(message_bus)
        run_id = value.get("run_id")
        published = value
        if run_id is not None:
            folded = fold_dag_run_entry(
                runs.get(run_id),
                name,
                value,
                datetime.now().isoformat(),
            )
            if folded is not None:
                runs[run_id] = folded
                await projection.upsert(
                    session_id,
                    "dag_run",
                    run_id,
                    folded,
                )
                if name == "dag_run_started":
                    published = {**value, "created_at": folded["created_at"]}
        await projection.publish(session_id, name, published)

    return _publish
```

- [ ] **Step 3c: Replace the inline closure in `_factory`.** In `make_subagent_tool_factory._factory`, delete the whole `if message_bus is not None: async def _publish_dag_progress(...)` block (lines ~202-240) and its `progress_publisher = None` scaffold, and set:

```python
        subagent_tools = {t.name: t for t in tools}
        if subagent_tools and backend is not None and session_workdir:
            dag_publisher = (
                build_dag_progress_publisher(message_bus, session_id)
                if message_bus is not None
                else None
            )
            tools.append(
                SubAgentDagTool(
                    subagents=subagent_tools,
                    backend=backend,
                    workdir=session_workdir,
                    progress_publisher=dag_publisher,
                ),
            )
```

- [ ] **Step 4: Run — expect PASS.** Then regression: `python -m pytest tests/subagent_dag_projection_test.py tests/subagent_agent_tools_test.py tests/subagent_dag_progress_test.py -v`.

- [ ] **Step 5: E501 + commit.**

Run: `python -m flake8 --isolated --max-line-length=79 --select=E501 src/agentscope/subagent/_dag/_projection.py src/agentscope/subagent/_agent_tools.py`

```bash
git add src/agentscope/subagent/_dag/_projection.py src/agentscope/subagent/_agent_tools.py tests/subagent_dag_projection_test.py
git commit -m "feat(subagent): persist DAG runs to a durable session projection"
```

---

## Task 3: SSE replay of `dag_run` projection on connect (§1c)

**Files:**
- Create: `src/agentscope/app/_service/_run_state_replay.py`
- Modify: `src/agentscope/app/_router/_session.py` (`_sse_generator`, after the §1b HITL block).
- Test: `tests/service_run_state_replay_test.py` (new).

**Interfaces:**
- Produces: `DAG_RUN_KIND = "dag_run"`; `dag_run_replay_events(entries: list[dict]) -> list[CustomEvent]` yielding, per entry, one `dag_run_started` (with `created_at`), then a `dag_node_updated` per non-`pending` node, then a `dag_run_completed` when a manifest exists.

- [ ] **Step 1: Write the failing test** in `tests/service_run_state_replay_test.py`.

```python
# -*- coding: utf-8 -*-
"""Tests for durable run-state SSE replay helpers."""
import unittest

from agentscope.app._service._run_state_replay import (
    dag_run_replay_events,
)


class DagRunReplayTest(unittest.TestCase):
    """Replay yields started -> non-pending updates -> completed."""

    def test_replay_events(self) -> None:
        entries = [{
            "run_id": "r1",
            "created_at": "T0",
            "nodes": [
                {"id": "A", "subagent": "s", "depends_on": []},
                {"id": "B", "subagent": "s", "depends_on": ["A"]},
            ],
            "byNode": {"A": "completed", "B": "pending"},
            "manifest": {"run_id": "r1", "files": []},
        }]
        events = dag_run_replay_events(entries)
        self.assertEqual(
            [(e.name, e.value) for e in events],
            [
                ("dag_run_started", {
                    "run_id": "r1",
                    "created_at": "T0",
                    "nodes": [
                        {"id": "A", "subagent": "s", "depends_on": []},
                        {"id": "B", "subagent": "s", "depends_on": ["A"]},
                    ],
                }),
                ("dag_node_updated",
                 {"run_id": "r1", "node": "A", "status": "completed"}),
                ("dag_run_completed",
                 {"run_id": "r1", "manifest": {"run_id": "r1", "files": []}}),
            ],
        )
```

- [ ] **Step 2: Run — expect FAIL.** `python -m pytest tests/service_run_state_replay_test.py -v`

- [ ] **Step 3a: Create `src/agentscope/app/_service/_run_state_replay.py`.**

```python
# -*- coding: utf-8 -*-
"""Rebuild durable run-state projections as replayable CustomEvents.

Pure helpers used by the SSE generator to re-inject stored DAG-run and
sub-agent-instance state on (re)connect, in a deterministic order so a
client that reloaded after the run ended sees the same picture the live
overlay held.
"""
from ...event import CustomEvent

DAG_RUN_KIND = "dag_run"
SUBAGENT_INSTANCE_KIND = "subagent_instance"


def dag_run_replay_events(entries: list[dict]) -> list[CustomEvent]:
    """Rebuild DAG-run projection entries as ordered CustomEvents.

    Args:
        entries (`list[dict]`):
            Stored ``dag_run`` projection payloads.

    Returns:
        `list[CustomEvent]`:
            For each entry: one ``dag_run_started`` (carrying
            ``created_at``), a ``dag_node_updated`` per non-``pending``
            node, then ``dag_run_completed`` when a manifest is present.
    """
    events: list[CustomEvent] = []
    for entry in entries:
        run_id = entry.get("run_id")
        events.append(
            CustomEvent(
                name="dag_run_started",
                value={
                    "run_id": run_id,
                    "created_at": entry.get("created_at"),
                    "nodes": entry.get("nodes", []),
                },
            ),
        )
        for node_id, st in entry.get("byNode", {}).items():
            if st != "pending":
                events.append(
                    CustomEvent(
                        name="dag_node_updated",
                        value={
                            "run_id": run_id,
                            "node": node_id,
                            "status": st,
                        },
                    ),
                )
        if entry.get("manifest") is not None:
            events.append(
                CustomEvent(
                    name="dag_run_completed",
                    value={"run_id": run_id, "manifest": entry["manifest"]},
                ),
            )
    return events
```

- [ ] **Step 3b: Wire §1c into `_sse_generator`** (`_router/_session.py`), right after the §1b HITL loop (after line ~790). Add the import at top:
`from .._service._run_state_replay import dag_run_replay_events, DAG_RUN_KIND`

```python
        # 1c. Replay durable DAG-run projections (design §4.4). These
        #     survive the run-end log trim and page reloads.
        for evt in dag_run_replay_events(
            await projection.list(session_id, DAG_RUN_KIND),
        ):
            yield (
                "data: "
                + json.dumps(evt.model_dump(mode="json"), ensure_ascii=False)
                + "\n\n"
            )
```
(`projection` is already in scope from §1b: `projection = SessionProjection(message_bus)`.)

- [ ] **Step 4: Run — expect PASS.** Regression: `python -m pytest tests/service_run_state_replay_test.py tests/service_subagent_hitl_projector_test.py -v`.

- [ ] **Step 5: E501 + commit.**

Run: `python -m flake8 --isolated --max-line-length=79 --select=E501 src/agentscope/app/_service/_run_state_replay.py src/agentscope/app/_router/_session.py`

```bash
git add src/agentscope/app/_service/_run_state_replay.py src/agentscope/app/_router/_session.py tests/service_run_state_replay_test.py
git commit -m "feat(app): replay durable DAG-run projection on SSE connect"
```

---

## Task 4: CLI sub-agent tool emits instance status

**Files:**
- Create: `src/agentscope/subagent/_instance_progress.py`
- Modify: `src/agentscope/subagent/_tool.py` (constructor + `call`).
- Test: `tests/subagent_instance_progress_test.py` (new; CLI cases).

**Interfaces:**
- Produces: `emit_instance_status(publisher, *, handle, agent_id, prototype, transport, status, action) -> None`; event `subagent_instance_updated` value `{handle, agent_id, prototype, transport, status, action}`.
- `CliSubAgentTool.__init__` gains `progress_publisher: Callable[[str, dict], Awaitable[None]] | None = None`; computes `self._transport = "codex" if transcript_format == "codex_jsonl" else "cli"`.
- Emits: `running` after create/resume is decided (before dispatch); `completed` on success; `failed` on every ERROR early-return. No-op when stateless (no `handle`) or `publisher is None`.

- [ ] **Step 1: Write the failing test** in `tests/subagent_instance_progress_test.py`.

```python
# -*- coding: utf-8 -*-
"""Tests for live sub-agent instance status events (CLI + OpenAI)."""
import unittest

from agentscope.subagent import CliSubAgentTool
from agentscope.tool import ExecResult
from tests.subagent_tool_test import _CannedBackend, _MemRegistry, _collect
from tests.utils import AnyString


class _Rec:
    """Capturing publisher recording (name, value) tuples."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, name: str, value: dict) -> None:
        self.events.append((name, value))


class CliInstanceProgressTest(unittest.IsolatedAsyncioTestCase):
    """CLI tool emits running -> completed / failed with transport."""

    async def test_create_success(self) -> None:
        rec = _Rec()
        tool = CliSubAgentTool(
            name="cc", description="d",
            command="claude -p {prompt} --id {agent_id}",
            resume_command="claude -r {agent_id} -p {prompt}",
            registry=_MemRegistry(),
            backend=_CannedBackend(stdout=b"hello"),
            progress_publisher=rec,
        )
        await _collect(tool.call(prompt="hi", instance="researcher"))
        self.assertEqual(rec.events, [
            ("subagent_instance_updated", {
                "handle": "researcher", "agent_id": AnyString(),
                "prototype": "cc", "transport": "cli",
                "status": "running", "action": "create"}),
            ("subagent_instance_updated", {
                "handle": "researcher", "agent_id": AnyString(),
                "prototype": "cc", "transport": "cli",
                "status": "completed", "action": "create"}),
        ])

    async def test_exec_failure_emits_failed(self) -> None:
        rec = _Rec()
        tool = CliSubAgentTool(
            name="cc", description="d",
            command="claude -p {prompt} --id {agent_id}",
            resume_command="claude -r {agent_id} -p {prompt}",
            registry=_MemRegistry(),
            backend=_CannedBackend(stdout=b"", stderr=b"bad", exit_code=2),
            progress_publisher=rec,
        )
        await _collect(tool.call(prompt="hi", instance="researcher"))
        self.assertEqual(
            [(n, v["status"]) for (n, v) in rec.events],
            [("subagent_instance_updated", "running"),
             ("subagent_instance_updated", "failed")],
        )

    async def test_codex_transport_and_stateless_noop(self) -> None:
        rec = _Rec()
        codex = CliSubAgentTool(
            name="cx", description="d",
            command="codex exec {prompt}",
            resume_command="codex resume {agent_id} {prompt}",
            id_source="derived", transcript_format="codex_jsonl",
            registry=_MemRegistry(),
            backend=_CannedBackend(stdout=b'{"type":"item.completed",'
                                          b'"item":{"type":"agent_message",'
                                          b'"text":"hi"}}'),
            progress_publisher=rec,
        )
        await _collect(codex.call(prompt="hi", instance="r"))
        self.assertEqual({v["transport"] for (_, v) in rec.events}, {"codex"})

        rec2 = _Rec()
        stateless = CliSubAgentTool(
            name="cc", description="d", command="claude -p {prompt}",
            backend=_CannedBackend(stdout=b"ok"), progress_publisher=rec2)
        await _collect(stateless.call(prompt="hi"))
        self.assertEqual(rec2.events, [])
```

- [ ] **Step 2: Run — expect FAIL.** `python -m pytest tests/subagent_instance_progress_test.py -v`

- [ ] **Step 3a: Create `src/agentscope/subagent/_instance_progress.py`.**

```python
# -*- coding: utf-8 -*-
"""Shared helper: emit a sub-agent instance status event.

Used by both the CLI and OpenAI sub-agent tools to push a live +
durable ``subagent_instance_updated`` event. No-op when there is no
publisher (standalone/tests) or no ``handle`` (stateless calls).
"""
from collections.abc import Awaitable, Callable

from .._logging import logger


async def emit_instance_status(
    publisher: Callable[[str, dict], Awaitable[None]] | None,
    *,
    handle: str | None,
    agent_id: str | None,
    prototype: str,
    transport: str,
    status: str,
    action: str | None,
) -> None:
    """Publish one instance status transition, swallowing failures.

    Args:
        publisher (`Callable[[str, dict], Awaitable[None]] | None`):
            The progress publisher, or ``None`` to no-op.
        handle (`str | None`):
            The instance handle; ``None`` (stateless) no-ops.
        agent_id (`str | None`):
            The CLI/HTTP session id (may be ``None`` before creation).
        prototype (`str`):
            The sub-agent prototype (tool) name.
        transport (`str`):
            One of ``"cli"`` / ``"codex"`` / ``"openai"``.
        status (`str`):
            One of ``"running"`` / ``"completed"`` / ``"failed"``.
        action (`str | None`):
            ``"create"`` / ``"resume"`` when known, else ``None``.
    """
    if publisher is None or handle is None:
        return
    try:
        await publisher(
            "subagent_instance_updated",
            {
                "handle": handle,
                "agent_id": agent_id,
                "prototype": prototype,
                "transport": transport,
                "status": status,
                "action": action,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("subagent_instance publish failed", exc_info=True)
```

- [ ] **Step 3b: Edit `_tool.py`.** Add imports:
`from collections.abc import Awaitable, Callable` and `from ._instance_progress import emit_instance_status`.

In `__init__`, add the param (after `middlewares`): `progress_publisher: Callable[[str, dict], Awaitable[None]] | None = None,` and store `self._progress_publisher = progress_publisher` and `self._transport = "codex" if transcript_format == "codex_jsonl" else "cli"`.

Add a private helper method on the class:

```python
    async def _emit(self, status: str, handle, agent_id, action) -> None:
        """Emit an instance status transition (no-op when stateless)."""
        await emit_instance_status(
            self._progress_publisher,
            handle=handle,
            agent_id=agent_id,
            prototype=self.name,
            transport=self._transport,
            status=status,
            action=action,
        )
```

In `call`, add emits:
- Inside the lookup `except` block (before the ERROR yield): `await self._emit("failed", handle, None, None)`.
- At the end of the `if self.is_stateful:` branch (after the `else:` that sets create, i.e. right before the closing of the stateful block / before `else: agent_id = str(uuid.uuid4())`): `await self._emit("running", handle, agent_id, action)`.
- Before **each** subsequent `yield ToolChunk(... state=ToolResultState.ERROR ...); return` (prompt-file error, exec failure, timeout, non-zero exit): `await self._emit("failed", handle, agent_id, action)`.
- Immediately before the terminal success `yield` (after building `metadata`, ~426): `await self._emit("completed", handle, agent_id, action)`.

- [ ] **Step 4: Run — expect PASS.** Regression: `python -m pytest tests/subagent_instance_progress_test.py tests/subagent_tool_test.py -v`.

- [ ] **Step 5: E501 + commit.**

Run: `python -m flake8 --isolated --max-line-length=79 --select=E501 src/agentscope/subagent/_instance_progress.py src/agentscope/subagent/_tool.py`

```bash
git add src/agentscope/subagent/_instance_progress.py src/agentscope/subagent/_tool.py tests/subagent_instance_progress_test.py
git commit -m "feat(subagent): emit live instance status from the CLI sub-agent tool"
```

---

## Task 5: OpenAI sub-agent tool emits instance status

**Files:**
- Modify: `src/agentscope/subagent/_openai_tool.py` (constructor + `call`).
- Test: `tests/subagent_instance_progress_test.py` (add OpenAI cases).

**Interfaces:**
- Consumes: `emit_instance_status` (Task 4). `transport = "openai"`.
- `OpenAISubAgentTool.__init__` gains `progress_publisher: Callable[[str, dict], Awaitable[None]] | None = None`.

- [ ] **Step 1: Add the failing test** to `tests/subagent_instance_progress_test.py`.

```python
class OpenAiInstanceProgressTest(unittest.IsolatedAsyncioTestCase):
    """OpenAI tool emits running -> completed / failed, transport=openai."""

    async def test_create_success(self) -> None:
        rec = _Rec()

        async def _post(url, headers, body, timeout):
            return 200, {"choices": [
                {"message": {"content": "hi"}, "finish_reason": "stop"}]}

        tool = OpenAISubAgentTool(
            name="mm", description="d", model="m",
            base_url="http://x/v1", api_key="k",
            registry=_MemRegistry(), backend=_CannedBackend(),
            cwd=".", post=_post, progress_publisher=rec)
        await _collect(tool.call(prompt="hi", instance="r"))
        self.assertEqual(
            [(v["transport"], v["status"]) for (_, v) in rec.events],
            [("openai", "running"), ("openai", "completed")])

    async def test_http_error_emits_failed(self) -> None:
        rec = _Rec()

        async def _post(url, headers, body, timeout):
            return 500, {"error": {"message": "boom"}}

        tool = OpenAISubAgentTool(
            name="mm", description="d", model="m",
            base_url="http://x/v1", api_key="k",
            registry=_MemRegistry(), backend=_CannedBackend(),
            cwd=".", post=_post, progress_publisher=rec)
        await _collect(tool.call(prompt="hi", instance="r"))
        self.assertEqual(
            [v["status"] for (_, v) in rec.events], ["running", "failed"])
```
(Add `from agentscope.subagent import OpenAISubAgentTool` to the imports.)

- [ ] **Step 2: Run — expect FAIL.** `python -m pytest tests/subagent_instance_progress_test.py -v`

- [ ] **Step 3: Edit `_openai_tool.py`.** Add `from ._instance_progress import emit_instance_status`. Add the `progress_publisher` param to `__init__` (after `middlewares`), store it, set `self._transport = "openai"`, and add the same `_emit` helper method as in Task 4.

In `call`, add emits at the mirror points:
- lookup `except` block → `await self._emit("failed", handle, None, None)` before the ERROR yield.
- after the `if self.is_stateful:` block (before `else: agent_id = str(uuid.uuid4())`) → `await self._emit("running", handle, agent_id, action)`.
- before each ERROR `yield ... return` (prompt-file error, HTTP error, non-2xx status, malformed response, `finish_reason`/empty) → `await self._emit("failed", handle, agent_id, action)`.
- before the terminal success `yield` (~486) → `await self._emit("completed", handle, agent_id, action)`.

- [ ] **Step 4: Run — expect PASS.** Regression: `python -m pytest tests/subagent_instance_progress_test.py tests/subagent_openai_tool_test.py -v`.

- [ ] **Step 5: E501 + commit.**

Run: `python -m flake8 --isolated --max-line-length=79 --select=E501 src/agentscope/subagent/_openai_tool.py`

```bash
git add src/agentscope/subagent/_openai_tool.py tests/subagent_instance_progress_test.py
git commit -m "feat(subagent): emit live instance status from the OpenAI sub-agent tool"
```

---

## Task 6: Wire the instance-progress publisher into the factory

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py` (`build_instance_progress_publisher`; thread into both tool constructors in `_factory`).
- Test: `tests/subagent_dag_projection_test.py` (add a builder test) or `tests/subagent_agent_tools_test.py`.

**Interfaces:**
- Produces: `build_instance_progress_publisher(message_bus, session_id) -> Callable[[str, dict], Awaitable[None]]` — upserts kind `"subagent_instance"` (entry_id = `value["handle"]`) + publishes.

- [ ] **Step 1: Write the failing test** (append to `tests/subagent_dag_projection_test.py`).

```python
class InstanceProgressPublisherTest(unittest.IsolatedAsyncioTestCase):
    """The instance publisher upserts the latest status by handle."""

    async def test_upsert_by_handle(self) -> None:
        from agentscope.subagent._agent_tools import (
            build_instance_progress_publisher,
        )
        bus = InMemoryMessageBus()
        pub = build_instance_progress_publisher(bus, "sid")
        base = {"handle": "r", "agent_id": "a", "prototype": "cc",
                "transport": "cli", "action": "create"}
        await pub("subagent_instance_updated", {**base, "status": "running"})
        await pub("subagent_instance_updated", {**base, "status": "completed"})
        entries = await SessionProjection(bus).list("sid", "subagent_instance")
        self.assertEqual(entries, [{**base, "status": "completed"}])
```

- [ ] **Step 2: Run — expect FAIL.** `python -m pytest tests/subagent_dag_projection_test.py::InstanceProgressPublisherTest -v`

- [ ] **Step 3a: Add the builder** to `_agent_tools.py` (next to `build_dag_progress_publisher`):

```python
def build_instance_progress_publisher(
    message_bus: Any,
    session_id: str,
) -> Callable[[str, dict], Awaitable[None]]:
    """Build a sub-agent-instance status publisher.

    Upserts the latest payload for the handle into the durable
    ``SessionProjection`` (kind ``"subagent_instance"``, entry_id =
    ``handle``) and publishes the live ``CustomEvent``.

    Args:
        message_bus (`MessageBus`):
            The app message bus.
        session_id (`str`):
            The session the events belong to.

    Returns:
        `Callable[[str, dict], Awaitable[None]]`:
            An async ``(name, value) -> None`` publisher.
    """

    async def _publish(name: str, value: dict) -> None:
        from ..app._service._session_projection import SessionProjection

        projection = SessionProjection(message_bus)
        handle = value.get("handle")
        if handle:
            await projection.upsert(
                session_id,
                "subagent_instance",
                handle,
                value,
            )
        await projection.publish(session_id, name, value)

    return _publish
```

- [ ] **Step 3b: Thread it into the tools in `_factory`.** At the top of `_factory` (after `session_id` is available, before the `for record in records:` loop), build:

```python
        instance_publisher = (
            build_instance_progress_publisher(message_bus, session_id)
            if message_bus is not None
            else None
        )
```

Add `progress_publisher=instance_publisher,` to BOTH the `CliSubAgentTool(...)` constructor (after `registry=...`) and the `OpenAISubAgentTool(...)` constructor (after `registry=...`).

- [ ] **Step 4: Run — expect PASS.** Regression: `python -m pytest tests/subagent_dag_projection_test.py tests/subagent_agent_tools_test.py -v`.

- [ ] **Step 5: E501 + commit.**

Run: `python -m flake8 --isolated --max-line-length=79 --select=E501 src/agentscope/subagent/_agent_tools.py`

```bash
git add src/agentscope/subagent/_agent_tools.py tests/subagent_dag_projection_test.py
git commit -m "feat(subagent): wire instance-progress publisher into the tool factory"
```

---

## Task 7: SSE replay of instances (§1d) + purge both kinds on session delete

**Files:**
- Modify: `src/agentscope/app/_service/_run_state_replay.py` (add `subagent_instance_replay_events`).
- Modify: `src/agentscope/app/_router/_session.py` (§1d block).
- Modify: `src/agentscope/app/_service/_session.py` — `SessionService.delete_session` (`_session.py:319`, the coroutine the route at `_router/_session.py:373` awaits). It holds `self._projection` (`_session.py:133`) and already calls `SubagentHitlProjector.purge(self._projection, ...)` in its team-cascade paths, so add our purge there too.
- Test: `tests/service_run_state_replay_test.py` (add instance-replay + purge cases).

**Interfaces:**
- Produces: `subagent_instance_replay_events(entries) -> list[CustomEvent]` (one `subagent_instance_updated` per entry); purge of kinds `"dag_run"` + `"subagent_instance"` on session delete.

- [ ] **Step 1: Write the failing test** (append to `tests/service_run_state_replay_test.py`).

```python
from agentscope.app._service._run_state_replay import (
    subagent_instance_replay_events,
)


class InstanceReplayTest(unittest.TestCase):
    """Replay yields one subagent_instance_updated per entry."""

    def test_replay_events(self) -> None:
        entries = [{"handle": "r", "agent_id": "a", "prototype": "cc",
                    "transport": "cli", "status": "completed",
                    "action": "create"}]
        events = subagent_instance_replay_events(entries)
        self.assertEqual(
            [(e.name, e.value) for e in events],
            [("subagent_instance_updated", entries[0])],
        )
```

Add a purge test in `tests/service_run_state_replay_test.py` (or extend the session-service test if one exists) using an `InMemoryMessageBus`: upsert one `dag_run` and one `subagent_instance` entry via `SessionProjection`, run the delete path (or call the purge directly), then assert both `list(...)` return `[]`. If a `SessionService` unit is awkward to construct, assert against a small helper `purge_run_state(projection, sid)` you add to `_run_state_replay.py` and call from `delete_session`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3a: Add to `_run_state_replay.py`:**

```python
def subagent_instance_replay_events(
    entries: list[dict],
) -> list[CustomEvent]:
    """Rebuild sub-agent-instance projection entries as CustomEvents.

    Args:
        entries (`list[dict]`):
            Stored ``subagent_instance`` projection payloads.

    Returns:
        `list[CustomEvent]`:
            One ``subagent_instance_updated`` event per entry.
    """
    return [
        CustomEvent(name="subagent_instance_updated", value=entry)
        for entry in entries
    ]


async def purge_run_state(projection, session_id: str) -> None:
    """Drop this session's DAG-run and instance projection feeds.

    Args:
        projection (`SessionProjection`):
            The shared projection store.
        session_id (`str`):
            The session being deleted.
    """
    await projection.purge(session_id, DAG_RUN_KIND)
    await projection.purge(session_id, SUBAGENT_INSTANCE_KIND)
```

- [ ] **Step 3b: Wire §1d** into `_sse_generator` right after §1c (update the import to include `subagent_instance_replay_events, SUBAGENT_INSTANCE_KIND`):

```python
        # 1d. Replay durable sub-agent instance statuses (design §5.4).
        for evt in subagent_instance_replay_events(
            await projection.list(session_id, SUBAGENT_INSTANCE_KIND),
        ):
            yield (
                "data: "
                + json.dumps(evt.model_dump(mode="json"), ensure_ascii=False)
                + "\n\n"
            )
```

- [ ] **Step 3c: Purge on delete.** In `SessionService.delete_session` (where `self._projection` / the message bus is in scope; if it holds a `SessionProjection` as `self._projection`, reuse it, else construct `SessionProjection(self._message_bus)`), after the existing storage delete succeeds, add:
`from ._run_state_replay import purge_run_state` (top of that module) and `await purge_run_state(self._projection, session_id)`.

- [ ] **Step 4: Run — expect PASS.** Regression: `python -m pytest tests/service_run_state_replay_test.py tests/service_subagent_hitl_projector_test.py -v`.

- [ ] **Step 5: E501 + commit.**

Run: `python -m flake8 --isolated --max-line-length=79 --select=E501 src/agentscope/app/_service/_run_state_replay.py src/agentscope/app/_router/_session.py <session-service file>`

```bash
git add src/agentscope/app/_service/_run_state_replay.py src/agentscope/app/_router/_session.py <session-service file> tests/service_run_state_replay_test.py
git commit -m "feat(app): replay instance statuses on connect and purge run-state on delete"
```

---

## Task 8: Frontend — DAG overlay durability (manifest, created_at, binding map)

**Files:**
- Modify: `src/components/chat/DagRunsContext.tsx`, `src/components/dag/deriveDag.ts`, `src/hooks/useMessages.ts`, `src/pages/chat/ChatViewport.tsx`.

**Interfaces:**
- `DagRunLive` gains `created_at?: string` and `manifest?: Record<string, unknown>` (matches `readDagManifest`'s param type, so it can be passed directly).
- `DagRunsState` gains `runIdByToolCallId: Record<string, string>`.
- `deriveDag.ts` gains `buildRunIdByToolCallId(msgs, dagRuns): Record<string,string>`.
- `useMessages` options gain `onDagRunCompleted?: (value) => void`.

- [ ] **Step 1: Extend `DagRunsContext.tsx`.**

```tsx
export interface DagRunLive {
	nodes: DagRunLiveNode[];
	byNode: Record<string, string>;
	created_at?: string;
	manifest?: Record<string, unknown>;
}

export interface DagRunsState {
	dagRuns: Record<string, DagRunLive>;
	latestRunId: string | null;
	runIdByToolCallId: Record<string, string>;
}

export const DagRunsContext = createContext<DagRunsState>({
	dagRuns: {},
	latestRunId: null,
	runIdByToolCallId: {},
});
```

- [ ] **Step 2: Add `buildRunIdByToolCallId` to `deriveDag.ts`.** Bind the k-th non-error `run_subagent_dag` tool-call (transcript order) to the k-th run ordered by `created_at`.

```tsx
import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';
// ... existing imports; RUN_SUBAGENT_DAG_TOOL already exported.

interface DagRunLiveLike {
	created_at?: string;
}

/**
 * Bind each `run_subagent_dag` tool-call to its own `run_id`.
 *
 * The k-th run-PRODUCING DAG call in transcript order (i.e. excluding
 * calls whose result errored — a validation error produced no run) maps
 * to the k-th run ordered by `created_at`. Correct under the
 * single-active-run invariant (the leader runs DAGs sequentially), and
 * reload-safe because both inputs rebuild from durable state.
 */
export function buildRunIdByToolCallId(
	msgs: Msg[],
	dagRuns: Record<string, DagRunLiveLike>,
): Record<string, string> {
	const callIds: string[] = [];
	const errored = new Set<string>();
	const resultState = new Map<string, string | undefined>();
	// First pass: collect tool_result states by id.
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const b of blocks) {
			if (b.type === 'tool_result') resultState.set(b.id, b.state);
		}
	}
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const b of blocks) {
			if (b.type === 'tool_call' && b.name === RUN_SUBAGENT_DAG_TOOL) {
				if (resultState.get(b.id) === 'error') errored.add(b.id);
				callIds.push(b.id);
			}
		}
	}
	const producing = callIds.filter((id) => !errored.has(id));
	const orderedRunIds = Object.keys(dagRuns).sort((a, b) =>
		(dagRuns[a].created_at ?? '').localeCompare(dagRuns[b].created_at ?? ''),
	);
	const map: Record<string, string> = {};
	producing.forEach((callId, i) => {
		if (i < orderedRunIds.length) map[callId] = orderedRunIds[i];
	});
	return map;
}
```

- [ ] **Step 3: `useMessages.ts` — add `onDagRunCompleted`.** Add to the options type (beside `onDagNodeUpdated`):

```tsx
			/**
			 * Called on a ``CUSTOM`` event ``name="dag_run_completed"`` — a
			 * DAG run finished. ``value`` is ``{ run_id, manifest }``.
			 */
			onDagRunCompleted?: (value: Record<string, unknown>) => void;
```

Add the branch in the `CUSTOM` block (after the `dag_node_updated` branch):

```tsx
				} else if (custom.name === 'dag_run_completed' && custom.value) {
					optionsRef.current?.onDagRunCompleted?.(
						custom.value as Record<string, unknown>,
					);
```

- [ ] **Step 4: `ChatViewport.tsx`.** In `handleDagRunStarted`, store `created_at`:

```tsx
			[runId]: {
				nodes: rawNodes,
				byNode: Object.fromEntries(rawNodes.map((n) => [n.id, 'pending'])),
				created_at: (value.created_at as string) ?? '',
			},
```

Add a completed handler and wire it:

```tsx
	const handleDagRunCompleted = useCallback((value: Record<string, unknown>) => {
		const runId = value.run_id as string | undefined;
		if (!runId) return;
		setDagRuns((prev) => {
			const existing = prev[runId] ?? { nodes: [], byNode: {} };
			return {
				...prev,
				[runId]: {
					...existing,
					manifest: value.manifest as Record<string, unknown> | undefined,
				},
			};
		});
	}, []);
```
Add `onDagRunCompleted: handleDagRunCompleted,` to the `useMessages(...)` options. Extend `dagRunsState`:

```tsx
	const dagRunsState = useMemo(
		() => ({
			dagRuns,
			latestRunId: latestDagRunId,
			runIdByToolCallId: buildRunIdByToolCallId(msgs, dagRuns),
		}),
		[dagRuns, latestDagRunId, msgs],
	);
```
(Import `buildRunIdByToolCallId` from `@/components/dag/deriveDag`.) The reset effect (already resetting `dagRuns`) needs no change.

- [ ] **Step 5: Gate + commit.**

Run: `cd examples/web_ui/frontend && pnpm build && pnpm lint`

```bash
git add examples/web_ui/frontend/src/components/chat/DagRunsContext.tsx examples/web_ui/frontend/src/components/dag/deriveDag.ts examples/web_ui/frontend/src/hooks/useMessages.ts examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx
git commit -m "feat(web-ui): carry DAG manifest + run binding in the live overlay"
```

---

## Task 9: Frontend — DAG renderer bound-run reconciliation

**Files:**
- Modify: `src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx`.

**Interfaces:**
- Consumes: `useDagRuns()` → `{ dagRuns, latestRunId, runIdByToolCallId }`.

- [ ] **Step 1: Rewrite `DagBody`** to bind to its own run and prefer an overlay manifest. Replace the `useDagRuns`/`manifest`/`live` lines (top of `DagBody`) with:

```tsx
	const { dagRuns, latestRunId, runIdByToolCallId } = useDagRuns();
	const runId = runIdByToolCallId[pair.call.id] ?? latestRunId ?? undefined;
	const live = runId ? dagRuns[runId] : undefined;
	const manifest =
		readDagManifest(pair.result?.metadata) ?? readDagManifest(live?.manifest);
	const isError = pair.result?.state === 'error';
```
Keep the existing branch bodies (`if (manifest) { ... } else if (live && live.nodes.length > 0) { ... } else if (!isError) { ... } else { vizNodes = []; }`) unchanged — they already read `manifest` and `live`. `readDagManifest(metadata: Record<string, unknown> | undefined)` matches `live?.manifest`'s type (Task 8), so pass it directly — no cast.

- [ ] **Step 2: Update `nodeCountOf`** (header) to also consider the bound overlay so a reloaded offloaded run shows the right count — optional but keeps the header honest: it already falls back to `call.input`, which is correct for structure, so no change is required. Leave as-is.

- [ ] **Step 3: Gate + commit.**

Run: `cd examples/web_ui/frontend && pnpm build && pnpm lint`

```bash
git add examples/web_ui/frontend/src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx
git commit -m "feat(web-ui): bind each DAG box to its own run and restore manifest on reload"
```

---

## Task 10: Frontend — instances overlay context + wiring

**Files:**
- Create: `src/components/chat/SubagentInstancesContext.tsx`.
- Modify: `src/hooks/useMessages.ts`, `src/pages/chat/ChatViewport.tsx`.

**Interfaces:**
- Produces: `SubagentInstancesContext`, `useSubagentInstances()`, `InstanceStatus`.
- `useMessages` options gain `onSubagentInstanceUpdated?: (value) => void`.

- [ ] **Step 1: Create `SubagentInstancesContext.tsx`.**

```tsx
import { createContext, useContext } from 'react';

/** Live status of one stateful sub-agent instance (keyed by handle). */
export interface InstanceStatus {
	status: string;
	transport: string;
	agentId?: string;
	prototype: string;
	action?: string;
}

/** Shared live instance-status map exposed to the instance monitor. */
export interface SubagentInstancesState {
	instances: Record<string, InstanceStatus>;
}

/**
 * Live per-instance status keyed by handle. Populated by `ChatViewport`
 * from `subagent_instance_updated` CustomEvents (live + durable replay).
 */
export const SubagentInstancesContext = createContext<SubagentInstancesState>({
	instances: {},
});

/** Read the current live instance-status map. */
export function useSubagentInstances(): SubagentInstancesState {
	return useContext(SubagentInstancesContext);
}
```

- [ ] **Step 2: `useMessages.ts` — add option + branch.** Option:

```tsx
			/**
			 * Called on a ``CUSTOM`` event ``name="subagent_instance_updated"``
			 * — a stateful sub-agent invocation changed state. ``value`` is
			 * ``{ handle, agent_id, prototype, transport, status, action }``.
			 */
			onSubagentInstanceUpdated?: (value: Record<string, unknown>) => void;
```
Branch (after `dag_run_completed`):

```tsx
				} else if (custom.name === 'subagent_instance_updated' && custom.value) {
					optionsRef.current?.onSubagentInstanceUpdated?.(
						custom.value as Record<string, unknown>,
					);
```

- [ ] **Step 3: `ChatViewport.tsx`.** Add state + handler + reset + memo + Provider. Imports:

```tsx
import { SubagentInstancesContext } from '@/components/chat/SubagentInstancesContext';
import type { InstanceStatus } from '@/components/chat/SubagentInstancesContext';
```
State (near `dagRuns`):

```tsx
	const [subagentInstances, setSubagentInstances] = useState<
		Record<string, InstanceStatus>
	>({});
```
Handler:

```tsx
	const handleSubagentInstanceUpdated = useCallback(
		(value: Record<string, unknown>) => {
			const handle = value.handle as string | undefined;
			if (!handle) return;
			setSubagentInstances((prev) => ({
				...prev,
				[handle]: {
					status: (value.status as string) ?? 'running',
					transport: (value.transport as string) ?? 'cli',
					agentId: value.agent_id as string | undefined,
					prototype: (value.prototype as string) ?? '',
					action: value.action as string | undefined,
				},
			}));
		},
		[],
	);
```
Wire into `useMessages(...)`: add `onSubagentInstanceUpdated: handleSubagentInstanceUpdated,`. Add to the reset-on-`sessionId` effect: `setSubagentInstances({});`. Memo + Provider:

```tsx
	const subagentInstancesState = useMemo(
		() => ({ instances: subagentInstances }),
		[subagentInstances],
	);
```
Wrap the existing `<DagRunsContext.Provider>` subtree with `<SubagentInstancesContext.Provider value={subagentInstancesState}>...</SubagentInstancesContext.Provider>` (open right inside `SubagentNamesContext.Provider`, close alongside it).

- [ ] **Step 4: Gate + commit.**

Run: `cd examples/web_ui/frontend && pnpm build && pnpm lint`

```bash
git add examples/web_ui/frontend/src/components/chat/SubagentInstancesContext.tsx examples/web_ui/frontend/src/hooks/useMessages.ts examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx
git commit -m "feat(web-ui): hold live sub-agent instance status in a context overlay"
```

---

## Task 11: Frontend — instance monitor status + transport badges + i18n

**Files:**
- Modify: `src/components/subagent/SubagentInstanceMonitor.tsx`, `src/i18n/locales/en.json`, `src/i18n/locales/zh.json`.

**Interfaces:**
- Consumes: `useSubagentInstances()` → `{ instances }` keyed by handle.

- [ ] **Step 1: Add i18n keys** under the existing `subagent-monitor` group in both `en.json` and `zh.json`:

`en.json`:
```json
"status": { "running": "Running", "completed": "Done", "failed": "Failed" },
"transport": { "cli": "Claude Code", "codex": "Codex", "openai": "MiroMind" }
```
`zh.json`:
```json
"status": { "running": "运行中", "completed": "已完成", "failed": "失败" },
"transport": { "cli": "Claude Code", "codex": "Codex", "openai": "MiroMind" }
```
(Validate JSON with `python -c "import json;json.load(open('src/i18n/locales/en.json'));json.load(open('src/i18n/locales/zh.json'))"`.)

- [ ] **Step 2: Merge overlay status into the monitor.** In `SubagentInstanceMonitor.tsx`, call `const { instances: liveInstances } = useSubagentInstances();`. For each row, look up `liveInstances[inst.handle]` for `status`/`transport` (fallback: derive transport from the matching `subagents` config — `openai_subagent` → `openai`; `cli` + `transcript_format==='codex_jsonl'` → `codex`; else `cli`). Add two badges next to the prototype name:

```tsx
{(() => {
	const live = liveInstances[inst.handle];
	const transport = live?.transport ?? transportOf(inst.prototype, subagents);
	const status = live?.status;
	return (
		<>
			<span className="rounded-sm border px-1 text-[10px] text-muted-foreground">
				{t(`subagent-monitor.transport.${transport}`)}
			</span>
			{status && (
				<span
					className={
						status === 'completed'
							? 'text-green-600'
							: status === 'failed'
								? 'text-red-600'
								: 'text-blue-600 animate-pulse'
					}
					title={t(`subagent-monitor.status.${status}`)}
				>
					{status === 'completed' ? '✓' : status === 'failed' ? '✕' : '⟳'}
				</span>
			)}
		</>
	);
})()}
```
Add a small `transportOf(prototype, subagents)` helper in the file (or in `deriveInstances.ts`) implementing the fallback mapping above.

- [ ] **Step 3: Gate + commit.**

Run: `cd examples/web_ui/frontend && pnpm build && pnpm lint`

```bash
git add examples/web_ui/frontend/src/components/subagent/SubagentInstanceMonitor.tsx examples/web_ui/frontend/src/components/subagent/deriveInstances.ts examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "feat(web-ui): show live status + transport badges in the instance monitor"
```

---

## Task 12: End-to-end browser smoke (deferred to a live stack)

**Files:** none (verification only).

Not automatable here (needs the live stack: ravenx env + redis + service on :8001 + `pnpm dev` + registered CLI/HTTP sub-agents + LLM creds). Use the `superpowers:verification-before-completion` / `verify` skill in the user's environment.

- [ ] **Step 1:** Boot the stack (`./start_webapp.sh` or the RavenX web-UI setup). Run a multi-node DAG with a dependency and a failure→skip; watch it animate; **reload** and confirm the finished graph is fully restored (colors + clickable per-node output/error), not all-pending.
- [ ] **Step 2:** Run a **second** DAG in the same session; reload; confirm **each** box shows its own graph (no cross-render).
- [ ] **Step 3:** Invoke stateful sub-agents of all three transports; confirm the "Instances" dock shows each with a transport badge and an animated running→done/failed status; **reload** and confirm statuses persist.
- [ ] **Step 4:** Report results; file any follow-ups.

---

## Notes for the executor

- **Task order matters for a couple of pairs:** Task 2 introduces `build_dag_progress_publisher` and removes the inline DAG closure; Task 6 adds `build_instance_progress_publisher` and threads it into the tool constructors (Tasks 4/5 added the `progress_publisher` param those constructors accept). Frontend Tasks 8→9 and 10→11 are ordered (context/overlay before the consumer).
- **Cross-layer contract:** the event names, projection kinds, and transport/status enums in Global Constraints must match byte-for-byte between the backend emitters (Tasks 1–7) and the frontend consumers (Tasks 8–11).
- **Single active DAG run** remains the correctness assumption behind the Task 8 binding; it holds because the leader executes tool calls sequentially and blocks on each.
