# Sub-agent DAG Live Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render every `run_subagent_dag` tool call as an interactive React Flow graph inside the tool-call box that animates per-node in real time and lets the user click a node for detail.

**Architecture:** The DAG runner emits two new out-of-band `CustomEvent`s (`dag_run_started`, `dag_node_updated`) on the session's message bus as each node transitions — mirroring `StateChangeMiddleware` — by receiving an optional `progress_publisher` threaded through `SubAgentDagTool → run_dag → _run_group → _run_node`, built from the app-global bus + per-turn `session_id` inside `make_subagent_tool_factory`. The web UI routes those events into a `ChatViewport` overlay shared via `DagRunsContext`, and a `run_subagent_dag` tool renderer draws the graph — from the live overlay while running, then reconciling to the authoritative (enriched) final `tool_result` metadata.

**Tech Stack:** Backend — Python 3.11, AgentScope, pytest. Frontend — React 19 + TypeScript, Vite 8, Tailwind CSS v4, shadcn/ui, lucide-react, `@xyflow/react` v12 (new), next-themes.

**Spec:** `docs/superpowers/specs/2026-07-21-subagent-dag-live-visualization-design.md`

## Global Constraints

- **Backend style:** black line length 79; flake8 / pylint / mypy clean; English docstrings with the `Args:`/`Returns:` template and backtick-typed params; third-party imports lazy (at point of use); internal files/classes `_`-prefixed and exposed only via `__init__.py`.
- **Backend tests:** assertions compare the **whole** data structure (not field-by-field); use `AnyString`/`AnyValue` from `tests/utils.py` only for nondeterministic fields (e.g. `run_id`, file paths). Test files are `tests/*_test.py`.
- **Run backend tests** in the project's configured env: `python -m pytest <path> -v`. Lint changed files with `pre-commit run --files <files>` before each commit.
- **Frontend has NO JS unit-test runner** (package.json scripts are only dev/build/lint/preview; no vitest/jest). The per-task gate for frontend tasks is therefore `pnpm build` (runs `tsc -b` typecheck + `vite build`) **and** `pnpm lint`, both from `examples/web_ui/frontend/`. Behavioral verification is the end-to-end browser smoke in Task 8.
- **Frontend indentation is TABS.** Match the surrounding files exactly. Path alias `@/` → `examples/web_ui/frontend/src/`.
- **Do not** skip pre-commit hooks or disable checks file-wide. **Commit frequently**, one commit per task minimum. Conventional Commit titles (`feat/fix/docs/... (scope): ...`).
- **Every commit message ends with:**
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```
- **Branch:** all work lands on `feat/subagent-dag-viz` (already created, holds the design spec).

---

### Task 1: Enrich DAG result metadata + fix terminal chunk state

Enrich each returned `files` entry with `subagent`/`depends_on`/capped `error`, and change the success-path terminal chunk from `state=RUNNING` to `SUCCESS` so the tool-call box chrome resolves. Update the two existing whole-structure tests.

**Files:**
- Modify: `src/agentscope/subagent/_dag/_runner.py` (add `_MAX_ERROR_CHARS`; enrich the `files.append({...})` in `_finalize`, currently lines 354-361)
- Modify: `src/agentscope/subagent/_dag/_tool.py` (terminal chunk `state`, line 170)
- Test: `tests/subagent_dag_runner_test.py` (update 2 whole-structure assertions), `tests/subagent_dag_tool_test.py` (add a state assertion)

**Interfaces:**
- Produces: enriched `DagRunResult.files` entries — each is
  `{"node": str, "subagent": str, "depends_on": list[str], "status": str, "prompt_file": str | None, "output_file": str | None, "error": str | None}`.
  This is the shape the frontend `readDagManifest` (Task 4) consumes. `error` is capped at 500 chars. The tool's success-path terminal `ToolChunk.state` is now `ToolResultState.SUCCESS`.

- [ ] **Step 1: Update the existing whole-structure runner tests to expect the enriched `files` shape (failing test)**

In `tests/subagent_dag_runner_test.py`, replace the assertion block in `test_render_failure_marks_node_failed_and_no_prompt_file` (currently lines 253-263):

```python
            self.assertEqual(
                result.files,
                [
                    {
                        "node": "A",
                        "subagent": "s",
                        "depends_on": [],
                        "status": "failed",
                        "prompt_file": None,
                        "output_file": None,
                        "error": AnyString(),
                    },
                ],
            )
```

And replace the `result.files` assertion in `test_whole_result_structure_for_diamond` (currently lines 407-418):

```python
        self.assertEqual(
            result.files,
            [
                {
                    "node": nid,
                    "subagent": "s",
                    "depends_on": deps,
                    "status": "completed",
                    "prompt_file": AnyString(),
                    "output_file": AnyString(),
                    "error": None,
                }
                for nid, deps in (
                    ("A", []),
                    ("B", ["A"]),
                    ("C", ["A"]),
                    ("D", ["B", "C"]),
                )
            ],
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/subagent_dag_runner_test.py -v`
Expected: FAIL — the two updated tests assert `subagent`/`depends_on`/`error` keys the current `files` entries don't have.

- [ ] **Step 3: Enrich `_finalize` in `_runner.py`**

Add a module-level constant near the top of `src/agentscope/subagent/_dag/_runner.py` (after the imports, before `DagRunResult`):

```python
_MAX_ERROR_CHARS = 500
```

In `_finalize`, replace the `files.append({...})` block (currently lines 354-361) with:

```python
        error = errors.get(nid)
        if error is not None and len(error) > _MAX_ERROR_CHARS:
            error = error[:_MAX_ERROR_CHARS] + "... (truncated)"
        files.append(
            {
                "node": nid,
                "subagent": node.subagent,
                "depends_on": node.depends_on,
                "status": status[nid],
                "prompt_file": prompt_file,
                "output_file": output_file,
                "error": error,
            },
        )
```

- [ ] **Step 4: Change the tool's success-path terminal chunk state in `_tool.py`**

In `src/agentscope/subagent/_dag/_tool.py`, in the final `yield ToolChunk(...)` (currently line 168-179), change `state=ToolResultState.RUNNING` to `state=ToolResultState.SUCCESS`.

- [ ] **Step 5: Add a state assertion to the tool test**

In `tests/subagent_dag_tool_test.py`, add this test method to `SubAgentDagToolTest` (after `test_runs_two_node_chain`):

```python
    async def test_success_terminal_chunk_state_is_success(self) -> None:
        """A completed DAG returns a SUCCESS terminal chunk (not RUNNING)."""
        with tempfile.TemporaryDirectory() as workdir:
            tool = SubAgentDagTool(
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
            )
            chunks = await _collect(
                tool.call(
                    nodes=[
                        {"id": "A", "subagent": "s", "prompt_template": "go"},
                    ],
                ),
            )
            self.assertEqual(chunks[-1].state, ToolResultState.SUCCESS)
            entry = chunks[-1].metadata["files"][0]
            self.assertEqual(entry["subagent"], "s")
            self.assertEqual(entry["depends_on"], [])
            self.assertIsNone(entry["error"])
```

- [ ] **Step 6: Run the DAG test suite to verify everything passes**

Run: `python -m pytest tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py tests/subagent_dag_integration_test.py -v`
Expected: PASS (all).

- [ ] **Step 7: Lint and commit**

```bash
pre-commit run --files src/agentscope/subagent/_dag/_runner.py src/agentscope/subagent/_dag/_tool.py tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py
git add src/agentscope/subagent/_dag/_runner.py src/agentscope/subagent/_dag/_tool.py tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): enrich DAG result files + resolve terminal chunk state

Add subagent/depends_on/capped error to each returned files entry so the
final DAG graph is self-describing from metadata, and yield the success-path
terminal chunk as SUCCESS instead of RUNNING.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Emit live per-node progress events from the runner

Thread an optional `progress_publisher` through the runner and emit `dag_run_started`, per-node `running`/`completed`/`failed`, and cascade `skipped` events. The publisher is guarded so it can never fail a node.

**Files:**
- Modify: `src/agentscope/subagent/_dag/_runner.py` (type alias + `_emit` helper; thread + emit in `run_dag`, `_run_group`, `_run_node`)
- Modify: `src/agentscope/subagent/_dag/_tool.py` (accept `progress_publisher`, pass to `run_dag`)
- Test: `tests/subagent_dag_progress_test.py` (new)

**Interfaces:**
- Consumes: the enriched runner from Task 1.
- Produces:
  - `ProgressPublisher = Callable[[str, dict], Awaitable[None]]` (module-level alias in `_runner.py`).
  - `run_dag(..., progress_publisher: ProgressPublisher | None = None)` — keyword-only.
  - `SubAgentDagTool.__init__(..., progress_publisher: Callable[[str, dict], Awaitable[None]] | None = None)`.
  - Emitted events (`(name, value)` passed to the publisher):
    - `("dag_run_started", {"run_id": str, "nodes": [{"id": str, "subagent": str, "depends_on": list[str]}]})`
    - `("dag_node_updated", {"run_id": str, "node": str, "status": "running"|"completed"|"failed"|"skipped"})`

- [ ] **Step 1: Write the failing progress test**

Create `tests/subagent_dag_progress_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for live per-node progress events from the DAG runner."""

import tempfile
import unittest
from typing import AsyncGenerator

from agentscope.message import TextBlock, ToolResultState
from agentscope.subagent._dag._graph import parse_dag_spec
from agentscope.subagent._dag._runner import run_dag
from agentscope.tool import LocalBackend, ToolChunk
from tests.utils import AnyString


class _FakeSubAgent:
    """Fake sub-agent echoing '<name>:<prompt>' to the output file."""

    def __init__(self, name: str, fail: bool = False) -> None:
        self._name = name
        self._fail = fail

    async def call(  # pylint: disable=unused-argument
        self,
        prompt: str,
        instance: str | None = None,
        prompt_file: str | None = None,
        output_file: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Echo a tagged result, or fail."""
        backend = LocalBackend()
        text = (await backend.read_file(prompt_file)).decode("utf-8")
        if self._fail:
            yield ToolChunk(
                content=[TextBlock(text="boom")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
        out = f"{self._name}:{text}"
        await backend.write_file(output_file, out.encode("utf-8"))
        yield ToolChunk(
            content=[TextBlock(text=out)],
            state=ToolResultState.RUNNING,
            is_last=True,
        )


class _Recorder:
    """Capturing progress publisher: records ``(name, value)`` tuples."""

    def __init__(self, fail: bool = False) -> None:
        self.events: list[tuple[str, dict]] = []
        self._fail = fail

    async def __call__(self, name: str, value: dict) -> None:
        if self._fail:
            raise RuntimeError("bus down")
        self.events.append((name, value))


class DagProgressTest(unittest.IsolatedAsyncioTestCase):
    """The runner publishes structured per-node transitions."""

    async def test_emits_started_and_terminal_events_with_skip(self) -> None:
        """A fails → B skipped; events cover started/running/failed/skip."""
        rec = _Recorder()
        with tempfile.TemporaryDirectory() as workdir:
            spec = parse_dag_spec(
                {
                    "nodes": [
                        {
                            "id": "A",
                            "subagent": "bad",
                            "prompt_template": "go",
                        },
                        {
                            "id": "B",
                            "subagent": "ok",
                            "depends_on": ["A"],
                            "prompt_template": "{{ A.output }}",
                        },
                    ],
                },
            )
            await run_dag(
                spec,
                subagents={
                    "bad": _FakeSubAgent("bad", fail=True),
                    "ok": _FakeSubAgent("ok"),
                },
                backend=LocalBackend(),
                workdir=workdir,
                progress_publisher=rec,
            )
        self.assertEqual(
            rec.events,
            [
                (
                    "dag_run_started",
                    {
                        "run_id": AnyString(),
                        "nodes": [
                            {"id": "A", "subagent": "bad", "depends_on": []},
                            {
                                "id": "B",
                                "subagent": "ok",
                                "depends_on": ["A"],
                            },
                        ],
                    },
                ),
                (
                    "dag_node_updated",
                    {"run_id": AnyString(), "node": "A", "status": "running"},
                ),
                (
                    "dag_node_updated",
                    {"run_id": AnyString(), "node": "A", "status": "failed"},
                ),
                (
                    "dag_node_updated",
                    {"run_id": AnyString(), "node": "B", "status": "skipped"},
                ),
            ],
        )

    async def test_none_publisher_emits_nothing_and_runs(self) -> None:
        """No publisher → no events, run still completes."""
        with tempfile.TemporaryDirectory() as workdir:
            result = await run_dag(
                parse_dag_spec(
                    {
                        "nodes": [
                            {
                                "id": "A",
                                "subagent": "s",
                                "prompt_template": "go",
                            },
                        ],
                    },
                ),
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
            )
        self.assertEqual(result.summary["completed"], 1)

    async def test_publisher_exception_does_not_fail_node(self) -> None:
        """A raising publisher is swallowed; the node still completes."""
        rec = _Recorder(fail=True)
        with tempfile.TemporaryDirectory() as workdir:
            result = await run_dag(
                parse_dag_spec(
                    {
                        "nodes": [
                            {
                                "id": "A",
                                "subagent": "s",
                                "prompt_template": "go",
                            },
                        ],
                    },
                ),
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
                progress_publisher=rec,
            )
        statuses = {f["node"]: f["status"] for f in result.files}
        self.assertEqual(statuses, {"A": "completed"})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/subagent_dag_progress_test.py -v`
Expected: FAIL — `run_dag()` has no `progress_publisher` keyword.

- [ ] **Step 3: Add the alias + guarded `_emit` helper in `_runner.py`**

At the top of `src/agentscope/subagent/_dag/_runner.py`, add to the imports:

```python
from collections.abc import Awaitable, Callable
```

After the imports (near `_MAX_ERROR_CHARS` from Task 1), add:

```python
ProgressPublisher = Callable[[str, dict], Awaitable[None]]


async def _emit(
    publisher: ProgressPublisher | None,
    name: str,
    value: dict,
) -> None:
    """Publish one progress event, swallowing any publisher failure.

    Args:
        publisher (`ProgressPublisher | None`):
            The progress callback, or ``None`` to no-op.
        name (`str`):
            The event name (``dag_run_started`` / ``dag_node_updated``).
        value (`dict`):
            The event payload.
    """
    if publisher is None:
        return
    try:
        await publisher(name, value)
    except Exception:  # noqa: BLE001 - progress must never fail a node
        logger.warning("DAG progress publish failed: %s", name, exc_info=True)
```

- [ ] **Step 4: Thread + emit in `run_dag`**

Change the `run_dag` signature (currently lines 42-49) to add the keyword-only param:

```python
async def run_dag(
    spec: SubAgentDagSpec,
    *,
    subagents: dict[str, Any],
    backend: Any,
    workdir: str,
    max_concurrency: int = 5,
    progress_publisher: ProgressPublisher | None = None,
) -> DagRunResult:
```

After `await store.init(spec.model_dump_json())` (currently line 86), emit the start event and set up skip tracking:

```python
    await _emit(
        progress_publisher,
        "dag_run_started",
        {
            "run_id": store.run_id,
            "nodes": [
                {
                    "id": node.id,
                    "subagent": node.subagent,
                    "depends_on": node.depends_on,
                }
                for node in spec.nodes
            ],
        },
    )
    published_skips: set[str] = set()
```

In the `while True:` loop, right after `_cascade_failures(by_id, status)` (currently line 100), emit newly-skipped nodes:

```python
        for nid, st in status.items():
            if st == "skipped" and nid not in published_skips:
                published_skips.add(nid)
                await _emit(
                    progress_publisher,
                    "dag_node_updated",
                    {
                        "run_id": store.run_id,
                        "node": nid,
                        "status": "skipped",
                    },
                )
```

In the `_run_group(...)` call inside `asyncio.gather` (currently lines 121-133), add the kwarg:

```python
                    progress_publisher=progress_publisher,
```

- [ ] **Step 5: Forward through `_run_group`**

Change the `_run_group` signature (currently lines 175-188) to add `progress_publisher: ProgressPublisher | None = None` as a keyword-only param (after `semaphore`), and add it to the docstring `Args:`. In its `await _run_node(...)` call (currently lines 216-227), add:

```python
            progress_publisher=progress_publisher,
```

- [ ] **Step 6: Emit running + terminal status in `_run_node`**

Change the `_run_node` signature (currently lines 230-242) to add `progress_publisher: ProgressPublisher | None = None` as a keyword-only param (after `semaphore`), and document it in `Args:`.

Inside `async with semaphore:` (currently line 268), emit `running` as the first statement, then keep the existing `try:`/`except` block, then emit the terminal status after it. The body becomes:

```python
    async with semaphore:
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {
                "run_id": store.run_id,
                "node": node.id,
                "status": "running",
            },
        )
        try:
            prompt = await render_prompt(
                node,
                backend=backend,
                cwd=workdir,
                output_paths=output_paths,
            )
            prompt_path = store.prompt_path(node.id)
            output_path = store.output_path(node.id)
            await store.write_text(prompt_path, prompt)
            prompt_written.add(node.id)
            last = None
            async for chunk in tool.call(
                prompt=prompt,
                instance=node.instance,
                prompt_file=prompt_path,
                output_file=output_path,
            ):
                last = chunk
            if last is None or last.state == ToolResultState.ERROR:
                status[node.id] = "failed"
                if (
                    last is not None
                    and last.content
                    and isinstance(last.content[0], TextBlock)
                ):
                    errors[node.id] = last.content[0].text
                elif last is not None:
                    errors[node.id] = "sub-agent error"
                else:
                    errors[node.id] = "sub-agent produced no output"
            else:
                status[node.id] = "completed"
                output_paths[node.id] = output_path
        except Exception as exc:  # noqa: BLE001 - record and continue
            logger.warning(
                "DAG node %s failed: %s",
                node.id,
                exc,
                exc_info=True,
            )
            status[node.id] = "failed"
            errors[node.id] = str(exc)
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {
                "run_id": store.run_id,
                "node": node.id,
                "status": status[node.id],
            },
        )
```

- [ ] **Step 7: Accept + forward `progress_publisher` in `SubAgentDagTool`**

In `src/agentscope/subagent/_dag/_tool.py`, add to the imports:

```python
from collections.abc import Awaitable, Callable
```

Add a param to `__init__` (currently lines 81-88), after `description`:

```python
        progress_publisher: (
            Callable[[str, dict], Awaitable[None]] | None
        ) = None,
```

Document it in the `__init__` `Args:` and store it (after `self._max_concurrency = max_concurrency`, line 109):

```python
        self._progress_publisher = progress_publisher
```

In `call`, pass it into `run_dag(...)` (currently lines 140-146):

```python
            result = await run_dag(
                spec,
                subagents=self._subagents,
                backend=self._backend,
                workdir=self._workdir,
                max_concurrency=self._max_concurrency,
                progress_publisher=self._progress_publisher,
            )
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `python -m pytest tests/subagent_dag_progress_test.py tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py -v`
Expected: PASS (all).

- [ ] **Step 9: Lint and commit**

```bash
pre-commit run --files src/agentscope/subagent/_dag/_runner.py src/agentscope/subagent/_dag/_tool.py tests/subagent_dag_progress_test.py
git add src/agentscope/subagent/_dag/_runner.py src/agentscope/subagent/_dag/_tool.py tests/subagent_dag_progress_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): emit live per-node DAG progress events

Thread an optional progress_publisher through run_dag/_run_group/_run_node
and emit dag_run_started plus per-node running/completed/failed/skipped
events. Publishes are guarded so a bus failure never fails a node.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Wire the message bus into the tool factory

Give the per-session DAG tool a publisher that pushes those events onto the session's SSE stream, by injecting the app-global bus into `make_subagent_tool_factory` and building the callback inside the per-turn closure. Update the example service.

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py` (factory signature + per-session publisher)
- Modify: `examples/agent_service/main.py` (hoist + pass the bus)
- Test: `tests/subagent_agent_tools_test.py` (add a publisher-wiring test)

**Interfaces:**
- Consumes: `SubAgentDagTool(progress_publisher=...)` from Task 2; `publish_session_event` (`agentscope.app._bus_ops`), `CustomEvent` (`agentscope.event`).
- Produces: `make_subagent_tool_factory(storage, workspace_manager, message_bus=None)` — the new optional 3rd param. When a bus is passed, the DAG tool's `progress_publisher` publishes `CustomEvent(name, value)` on the session channel.

- [ ] **Step 1: Inspect the existing factory test for the fixture pattern**

Run: `python -m pytest tests/subagent_agent_tools_test.py -v` and read the file to see how `make_subagent_tool_factory` is exercised (fake storage/workspace_manager, how the returned tool list is asserted). Reuse those fakes in Step 2.

- [ ] **Step 2: Write the failing publisher-wiring test**

Add to `tests/subagent_agent_tools_test.py` a test that builds the factory with a fake bus and asserts the DAG tool's publisher reaches `publish_session_event`. Use the file's existing fakes for `storage`/`workspace_manager`; add a minimal fake bus. Concretely, append this test class (adapt the fake storage/workspace-manager construction to match the ones already in the file):

```python
class DagProgressWiringTest(unittest.IsolatedAsyncioTestCase):
    """The factory wires a bus-backed publisher onto the DAG tool."""

    async def test_dag_tool_publisher_publishes_custom_event(self) -> None:
        """Calling the DAG tool's publisher fans out a CustomEvent."""

        class _FakeBus:
            def __init__(self) -> None:
                self.published: list[tuple[str, dict]] = []

            async def log_append(self, key, event, max_len):  # noqa: ANN001
                return "1-0"

            async def publish(self, key, event):  # noqa: ANN001
                self.published.append((key, event))

        bus = _FakeBus()
        # Build the factory with the fake bus + the same fake storage /
        # workspace_manager used by the other tests in this file, then run
        # the closure for one turn and locate the run_subagent_dag tool.
        factory = make_subagent_tool_factory(
            _make_fake_storage_with_one_cli_subagent(),
            _make_fake_workspace_manager(),
            bus,
        )
        tools = await factory("u1", "a1", "s1")
        dag_tool = next(t for t in tools if t.name == "run_subagent_dag")
        await dag_tool._progress_publisher(  # noqa: SLF001
            "dag_node_updated",
            {"run_id": "r1", "node": "A", "status": "running"},
        )
        self.assertEqual(len(bus.published), 1)
        _key, event = bus.published[0]
        self.assertEqual(event["type"], "custom")
        self.assertEqual(event["name"], "dag_node_updated")
        self.assertEqual(
            event["value"],
            {"run_id": "r1", "node": "A", "status": "running"},
        )
```

> Implementer note: `_make_fake_storage_with_one_cli_subagent()` /
> `_make_fake_workspace_manager()` stand in for whatever fixtures the file
> already provides — reuse the existing ones so exactly one CLI sub-agent is
> registered and a real workspace backend + workdir resolve (both are
> required for the DAG tool to be constructed, per `_agent_tools.py:197`).
> The event dict is the pydantic `model_dump(mode="json")` of `CustomEvent`,
> whose `type` serializes to the string `"custom"`.

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/subagent_agent_tools_test.py::DagProgressWiringTest -v`
Expected: FAIL — `make_subagent_tool_factory` takes only 2 args; the DAG tool has no bus-backed publisher.

- [ ] **Step 4: Add the `message_bus` param + per-session publisher in `_agent_tools.py`**

Change the factory signature (currently lines 59-62) to:

```python
def make_subagent_tool_factory(
    storage: Any,
    workspace_manager: Any,
    message_bus: Any = None,
) -> Callable[[str, str, str], Awaitable[list[ToolBase]]]:
```

Add a `message_bus` line to the `Args:` docstring (optional; wires live DAG progress events when provided).

In the `_factory` closure, replace the `SubAgentDagTool` construction block (currently lines 196-204) with:

```python
        subagent_tools = {t.name: t for t in tools}
        if subagent_tools and backend is not None and session_workdir:
            progress_publisher = None
            if message_bus is not None:

                async def progress_publisher(
                    name: str,
                    value: dict,
                    _bus: Any = message_bus,
                    _sid: str = session_id,
                ) -> None:
                    """Publish a live DAG progress CustomEvent.

                    Imported lazily to avoid an
                    ``agentscope.subagent`` -> ``agentscope.app`` cycle
                    (mirrors the ``DeliverFiles`` import above). Publish
                    failures are swallowed by the runner's ``_emit``.

                    Args:
                        name (`str`):
                            The event name.
                        value (`dict`):
                            The event payload.
                        _bus (`Any`):
                            The captured message bus.
                        _sid (`str`):
                            The captured session id.
                    """
                    from ..app._bus_ops import publish_session_event
                    from ..event import CustomEvent

                    event = CustomEvent(name=name, value=value)
                    await publish_session_event(
                        _bus,
                        _sid,
                        event.model_dump(mode="json"),
                    )

            tools.append(
                SubAgentDagTool(
                    subagents=subagent_tools,
                    backend=backend,
                    workdir=session_workdir,
                    progress_publisher=progress_publisher,
                ),
            )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS.

- [ ] **Step 6: Hoist + pass the bus in the example service**

In `examples/agent_service/main.py`, replace the inline `message_bus=InMemoryMessageBus(),` inside `create_app(...)` (line 77) and the `extra_agent_tools=make_subagent_tool_factory(storage, workspace_manager)` call (lines 97-100). First, add a variable just before `app = create_app(` (before line 75):

```python
message_bus = InMemoryMessageBus()
```

Then in `create_app(...)` set `message_bus=message_bus,` (replacing line 77), and change the factory call to:

```python
    extra_agent_tools=make_subagent_tool_factory(
        storage,
        workspace_manager,
        message_bus,
    ),
```

- [ ] **Step 7: Verify the example service imports cleanly**

Run: `python -c "import ast; ast.parse(open('examples/agent_service/main.py').read()); print('ok')"`
Expected: `ok` (syntax check — a full import needs Redis).

- [ ] **Step 8: Lint and commit**

```bash
pre-commit run --files src/agentscope/subagent/_agent_tools.py examples/agent_service/main.py tests/subagent_agent_tools_test.py
git add src/agentscope/subagent/_agent_tools.py examples/agent_service/main.py tests/subagent_agent_tools_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): wire message bus into the DAG tool factory

make_subagent_tool_factory accepts an optional message_bus and builds a
per-session progress publisher that fans out dag_* CustomEvents on the
session channel; the example service passes its bus through.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Frontend foundations — dependency + pure helpers + context

Add React Flow and the leaf modules the renderer depends on: the typed metadata reader, the layout function, and the live-state context (created now so later tasks can consume it before it is populated).

**Files:**
- Modify: `examples/web_ui/frontend/package.json` (add `@xyflow/react`)
- Create: `examples/web_ui/frontend/src/components/dag/deriveDag.ts`
- Create: `examples/web_ui/frontend/src/components/dag/layoutDag.ts`
- Create: `examples/web_ui/frontend/src/components/chat/DagRunsContext.tsx`

**Interfaces:**
- Produces:
  - `deriveDag.ts`: `RUN_SUBAGENT_DAG_TOOL = 'run_subagent_dag'`; types `DagFileEntry`, `DagSummary`, `DagTerminalOutput`, `DagManifest`; `DagNodeStatus = 'pending'|'running'|'completed'|'failed'|'skipped'`; `readDagManifest(metadata) => DagManifest | null`; `toStatus(s) => DagNodeStatus`.
  - `layoutDag.ts`: type `DagVizNode` (`{ id; subagent?; depends_on: string[]; status: DagNodeStatus; outputFile?; promptFile?; error?; terminalOutput? }`); type `DagNodeData` (`{ label; subagent?; status }`); type `DagFlowNode = Node<DagNodeData, 'dagNode'>`; `layoutDag(nodes: DagVizNode[]) => { rfNodes: DagFlowNode[]; rfEdges: Edge[] }`.
  - `DagRunsContext.tsx`: types `DagRunLiveNode`, `DagRunLive`, `DagRunsState`; `DagRunsContext` (default `{ dagRuns: {}, latestRunId: null }`); `useDagRuns() => DagRunsState`.

- [ ] **Step 1: Add the React Flow dependency**

From `examples/web_ui/frontend/`:

```bash
cd examples/web_ui/frontend && pnpm add @xyflow/react
```

Verify `@xyflow/react` (v12.x) now appears under `dependencies` in `package.json`.

- [ ] **Step 2: Create `deriveDag.ts`**

Create `examples/web_ui/frontend/src/components/dag/deriveDag.ts`:

```ts
/** The tool whose results carry a DAG manifest. */
export const RUN_SUBAGENT_DAG_TOOL = 'run_subagent_dag';

/** Per-node status vocabulary shared by the live overlay and the manifest. */
export type DagNodeStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped';

/** One node entry in a run_subagent_dag result manifest. */
export interface DagFileEntry {
	node: string;
	subagent?: string;
	depends_on?: string[];
	status: string;
	prompt_file?: string | null;
	output_file?: string | null;
	error?: string | null;
}

export interface DagSummary {
	total: number;
	completed: number;
	failed: number;
	skipped: number;
}

export interface DagTerminalOutput {
	node: string;
	text: string;
}

/** The structured metadata a run_subagent_dag tool result carries. */
export interface DagManifest {
	run_id: string;
	dir?: string;
	files: DagFileEntry[];
	summary?: DagSummary;
	terminal_outputs: DagTerminalOutput[];
}

/**
 * Read the DAG manifest from a tool_result block's metadata, returning `null`
 * when absent or malformed (mirrors the delivery `readManifest` guard).
 */
export function readDagManifest(
	metadata: Record<string, unknown> | undefined,
): DagManifest | null {
	if (!metadata) return null;
	const runId = metadata.run_id;
	const files = metadata.files;
	if (typeof runId !== 'string' || !Array.isArray(files)) return null;
	const terminal = Array.isArray(metadata.terminal_outputs)
		? (metadata.terminal_outputs as DagTerminalOutput[])
		: [];
	return {
		run_id: runId,
		dir: typeof metadata.dir === 'string' ? metadata.dir : undefined,
		files: files as DagFileEntry[],
		summary: (metadata.summary as DagSummary) ?? undefined,
		terminal_outputs: terminal,
	};
}

/** Coerce an arbitrary status string to a known `DagNodeStatus`. */
export function toStatus(s: string | undefined): DagNodeStatus {
	return s === 'running' || s === 'completed' || s === 'failed' || s === 'skipped'
		? s
		: 'pending';
}
```

- [ ] **Step 3: Create `layoutDag.ts`**

Create `examples/web_ui/frontend/src/components/dag/layoutDag.ts`:

```ts
import type { Edge, Node } from '@xyflow/react';

import type { DagNodeStatus } from './deriveDag';

/** A node ready to render — structure, status, and click-detail fields. */
export interface DagVizNode {
	id: string;
	subagent?: string;
	depends_on: string[];
	status: DagNodeStatus;
	outputFile?: string | null;
	promptFile?: string | null;
	error?: string | null;
	terminalOutput?: string | null;
}

/** The `data` payload carried by each React Flow node. Must be an object
 * type (not an interface) so it satisfies React Flow's `Record` bound. */
export type DagNodeData = {
	label: string;
	subagent?: string;
	status: DagNodeStatus;
};

export type DagFlowNode = Node<DagNodeData, 'dagNode'>;

const COL_WIDTH = 220;
const ROW_HEIGHT = 96;

/**
 * Lay a DAG out left→right: a node's column is its longest-path depth from a
 * root (a node with no in-graph dependencies); rows within a column are
 * assigned in input order. Deterministic and dependency-free. A `visiting`
 * guard makes it safe even against an accidental cycle.
 */
export function layoutDag(nodes: DagVizNode[]): {
	rfNodes: DagFlowNode[];
	rfEdges: Edge[];
} {
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const depthCache = new Map<string, number>();
	const visiting = new Set<string>();

	const depthOf = (id: string): number => {
		const cached = depthCache.get(id);
		if (cached !== undefined) return cached;
		const node = byId.get(id);
		if (!node || node.depends_on.length === 0 || visiting.has(id)) {
			depthCache.set(id, 0);
			return 0;
		}
		visiting.add(id);
		let max = 0;
		for (const dep of node.depends_on) {
			if (byId.has(dep)) max = Math.max(max, depthOf(dep) + 1);
		}
		visiting.delete(id);
		depthCache.set(id, max);
		return max;
	};

	const rowCounters = new Map<number, number>();
	const rfNodes: DagFlowNode[] = nodes.map((n) => {
		const depth = depthOf(n.id);
		const row = rowCounters.get(depth) ?? 0;
		rowCounters.set(depth, row + 1);
		return {
			id: n.id,
			type: 'dagNode',
			position: { x: depth * COL_WIDTH, y: row * ROW_HEIGHT },
			data: { label: n.id, subagent: n.subagent, status: n.status },
		};
	});

	const rfEdges: Edge[] = [];
	for (const n of nodes) {
		for (const dep of n.depends_on) {
			if (!byId.has(dep)) continue;
			rfEdges.push({
				id: `${dep}->${n.id}`,
				source: dep,
				target: n.id,
				animated: n.status === 'running',
			});
		}
	}
	return { rfNodes, rfEdges };
}
```

- [ ] **Step 4: Create `DagRunsContext.tsx`**

Create `examples/web_ui/frontend/src/components/chat/DagRunsContext.tsx`:

```tsx
import { createContext, useContext } from 'react';

/** One node in a live DAG run's structure (from `dag_run_started`). */
export interface DagRunLiveNode {
	id: string;
	subagent?: string;
	depends_on: string[];
}

/** Live state for one DAG run: its node structure + a node→status map. */
export interface DagRunLive {
	nodes: DagRunLiveNode[];
	byNode: Record<string, string>;
}

/** The shared live-DAG state exposed to tool renderers. */
export interface DagRunsState {
	dagRuns: Record<string, DagRunLive>;
	latestRunId: string | null;
}

/**
 * Live per-run DAG node status, keyed by server-generated `run_id`. Populated
 * by `ChatViewport` from `dag_run_started` / `dag_node_updated` CustomEvents.
 * The default (no provider) is empty, so a renderer safely falls back to the
 * final result metadata / call args.
 */
export const DagRunsContext = createContext<DagRunsState>({
	dagRuns: {},
	latestRunId: null,
});

/** Read the current live-DAG state. */
export function useDagRuns(): DagRunsState {
	return useContext(DagRunsContext);
}
```

- [ ] **Step 5: Typecheck + lint**

Run (from `examples/web_ui/frontend/`): `pnpm build && pnpm lint`
Expected: build succeeds (these modules are self-contained and unused-but-valid), lint clean.

- [ ] **Step 6: Commit**

```bash
git add examples/web_ui/frontend/package.json examples/web_ui/frontend/pnpm-lock.yaml examples/web_ui/frontend/src/components/dag/deriveDag.ts examples/web_ui/frontend/src/components/dag/layoutDag.ts examples/web_ui/frontend/src/components/chat/DagRunsContext.tsx
git commit -m "$(cat <<'EOF'
feat(web-ui): add DAG viz foundations (react-flow dep, helpers, context)

Add @xyflow/react and the leaf modules the DAG renderer builds on: the typed
manifest reader, the layered layout function, and the live-run context.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: DAG graph component + tool renderer (final-metadata render)

Build the React Flow graph + node-detail panel and register a `run_subagent_dag` renderer. At this stage it renders from the final result metadata (completed runs show their full graph) and, while running, shows the planned structure from the call args (all pending) — live animation is added in Task 6.

**Files:**
- Create: `examples/web_ui/frontend/src/components/dag/DagGraph.tsx`
- Create: `examples/web_ui/frontend/src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx`
- Modify: `examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx` (register)

**Interfaces:**
- Consumes: `layoutDag`, `DagVizNode`, `DagNodeData`, `DagFlowNode` (Task 4); `readDagManifest`, `toStatus` (Task 4); `useDagRuns` (Task 4, returns default until Task 6); `parseInput`, `getResultText`, `toolLabelClass`, `toolArgClass` (`_shared.tsx`); `ToolRenderer`, `ToolCallWithResult` (`types.ts`).
- Produces: `DagGraph({ nodes: DagVizNode[] })`; `RunSubagentDagRenderer: ToolRenderer`; registry key `run_subagent_dag`.

- [ ] **Step 1: Create `DagGraph.tsx`**

Create `examples/web_ui/frontend/src/components/dag/DagGraph.tsx`:

```tsx
import {
	Background,
	Controls,
	Handle,
	MiniMap,
	Position,
	ReactFlow,
} from '@xyflow/react';
import type { NodeProps } from '@xyflow/react';
import { CheckCircle2, Circle, LoaderCircle, MinusCircle, XCircle } from 'lucide-react';
import { useTheme } from 'next-themes';
import type { ReactNode } from 'react';
import { useMemo, useState } from 'react';

import type { DagNodeStatus } from './deriveDag';
import { layoutDag } from './layoutDag';
import type { DagFlowNode, DagVizNode } from './layoutDag';
import { cn } from '@/lib/utils';

import '@xyflow/react/dist/style.css';

const STATUS_STYLE: Record<DagNodeStatus, { box: string; icon: ReactNode }> = {
	pending: {
		box: 'border-border text-muted-foreground',
		icon: <Circle className="size-3 shrink-0" />,
	},
	running: {
		box: 'border-blue-500 text-blue-600 dark:text-blue-400',
		icon: <LoaderCircle className="size-3 shrink-0 animate-spin" />,
	},
	completed: {
		box: 'border-emerald-500 text-emerald-600 dark:text-emerald-400',
		icon: <CheckCircle2 className="size-3 shrink-0" />,
	},
	failed: {
		box: 'border-red-500 text-red-600 dark:text-red-400',
		icon: <XCircle className="size-3 shrink-0" />,
	},
	skipped: {
		box: 'border-dashed border-border text-muted-foreground opacity-70',
		icon: <MinusCircle className="size-3 shrink-0" />,
	},
};

function DagStatusNode({ data }: NodeProps<DagFlowNode>) {
	const style = STATUS_STYLE[data.status];
	return (
		<div
			className={cn(
				'rounded-md border bg-background px-3 py-2 text-xs shadow-sm min-w-[140px]',
				style.box,
			)}
		>
			<Handle type="target" position={Position.Left} className="!bg-muted-foreground" />
			<div className="flex items-center gap-1.5">
				{style.icon}
				<span className="font-medium truncate text-foreground">{data.label}</span>
			</div>
			{data.subagent && (
				<div className="mt-0.5 text-[10px] text-muted-foreground truncate">
					{data.subagent}
				</div>
			)}
			<Handle type="source" position={Position.Right} className="!bg-muted-foreground" />
		</div>
	);
}

// Defined at module scope so the reference is stable across renders (React
// Flow warns when nodeTypes is recreated each render).
const nodeTypes = { dagNode: DagStatusNode };

function DagNodeDetail({ node }: { node: DagVizNode }) {
	return (
		<div className="flex flex-col gap-1 rounded-md border bg-background p-3 text-xs">
			<div className="flex items-center gap-2">
				<span className="font-medium text-foreground">{node.id}</span>
				<span className="text-muted-foreground">{node.status}</span>
			</div>
			{node.subagent && (
				<div>
					<span className="text-muted-foreground">subagent: </span>
					{node.subagent}
				</div>
			)}
			{node.depends_on.length > 0 && (
				<div>
					<span className="text-muted-foreground">depends on: </span>
					{node.depends_on.join(', ')}
				</div>
			)}
			{node.promptFile && (
				<div className="truncate">
					<span className="text-muted-foreground">prompt: </span>
					{node.promptFile}
				</div>
			)}
			{node.outputFile && (
				<div className="truncate">
					<span className="text-muted-foreground">output: </span>
					{node.outputFile}
				</div>
			)}
			{node.error && (
				<pre className="max-h-40 overflow-auto whitespace-pre-wrap text-red-600 dark:text-red-400">
					{node.error}
				</pre>
			)}
			{node.terminalOutput && (
				<pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted p-2">
					{node.terminalOutput}
				</pre>
			)}
		</div>
	);
}

/**
 * Interactive DAG graph rendered inside a tool-call box. Nodes are laid out
 * by dependency depth; scroll-zoom is disabled so the canvas never hijacks
 * page scroll. Clicking a node reveals its detail below the canvas.
 */
export function DagGraph({ nodes }: { nodes: DagVizNode[] }) {
	const { resolvedTheme } = useTheme();
	const [selectedId, setSelectedId] = useState<string | null>(null);
	const { rfNodes, rfEdges } = useMemo(() => layoutDag(nodes), [nodes]);
	const selected = selectedId ? (nodes.find((n) => n.id === selectedId) ?? null) : null;

	return (
		<div className="flex flex-col gap-2">
			<div className="h-80 w-full overflow-hidden rounded-md border bg-muted/30">
				<ReactFlow
					nodes={rfNodes}
					edges={rfEdges}
					nodeTypes={nodeTypes}
					fitView
					fitViewOptions={{ padding: 0.2 }}
					nodesConnectable={false}
					nodesDraggable
					elementsSelectable
					zoomOnScroll={false}
					panOnScroll={false}
					zoomOnDoubleClick={false}
					minZoom={0.2}
					maxZoom={1.5}
					colorMode={resolvedTheme === 'dark' ? 'dark' : 'light'}
					proOptions={{ hideAttribution: true }}
					onNodeClick={(_, node) => setSelectedId(node.id)}
					onPaneClick={() => setSelectedId(null)}
				>
					<Background gap={16} />
					<Controls showInteractive={false} />
					{nodes.length > 6 && <MiniMap pannable zoomable />}
				</ReactFlow>
			</div>
			{selected && <DagNodeDetail node={selected} />}
		</div>
	);
}
```

- [ ] **Step 2: Create `RunSubagentDagRenderer.tsx`**

Create `examples/web_ui/frontend/src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx`:

```tsx
import { getResultText, parseInput, toolArgClass, toolLabelClass } from './_shared';
import type { ToolCallWithResult, ToolRenderer } from './types';
import { useDagRuns } from '@/components/chat/DagRunsContext';
import { readDagManifest, toStatus } from '@/components/dag/deriveDag';
import { DagGraph } from '@/components/dag/DagGraph';
import type { DagVizNode } from '@/components/dag/layoutDag';

function nodeCountOf(pair: ToolCallWithResult): number {
	const manifest = readDagManifest(pair.result?.metadata);
	if (manifest) return manifest.files.length;
	const parsed = parseInput(pair.call.input) as { nodes?: unknown[] };
	return Array.isArray(parsed.nodes) ? parsed.nodes.length : 0;
}

/**
 * Inner component so it can read the live-DAG context (a `renderBody` is a
 * plain function, not a React component, and cannot call hooks). Prefers the
 * authoritative final metadata; while the result is absent it falls back to
 * the live overlay (Task 6) and then to the call args (all pending).
 */
function DagBody({ pair }: { pair: ToolCallWithResult }) {
	const { dagRuns, latestRunId } = useDagRuns();
	const manifest = readDagManifest(pair.result?.metadata);

	let vizNodes: DagVizNode[] = [];
	if (manifest) {
		const terminal = new Map(manifest.terminal_outputs.map((t) => [t.node, t.text]));
		vizNodes = manifest.files.map((f) => ({
			id: f.node,
			subagent: f.subagent,
			depends_on: f.depends_on ?? [],
			status: toStatus(f.status),
			outputFile: f.output_file ?? null,
			promptFile: f.prompt_file ?? null,
			error: f.error ?? null,
			terminalOutput: terminal.get(f.node) ?? null,
		}));
	} else {
		const live = latestRunId ? dagRuns[latestRunId] : undefined;
		if (live && live.nodes.length > 0) {
			vizNodes = live.nodes.map((n) => ({
				id: n.id,
				subagent: n.subagent,
				depends_on: n.depends_on ?? [],
				status: toStatus(live.byNode[n.id]),
			}));
		} else {
			const parsed = parseInput(pair.call.input) as {
				nodes?: Array<{ id?: string; subagent?: string; depends_on?: string[] }>;
			};
			const raw = Array.isArray(parsed.nodes) ? parsed.nodes : [];
			vizNodes = raw
				.filter((n) => typeof n.id === 'string')
				.map((n) => ({
					id: n.id as string,
					subagent: n.subagent,
					depends_on: Array.isArray(n.depends_on) ? n.depends_on : [],
					status: 'pending' as const,
				}));
		}
	}

	if (vizNodes.length === 0) {
		const text = getResultText(pair.result);
		return text ? (
			<pre className="whitespace-pre-wrap text-xs">{text}</pre>
		) : (
			<p className="text-xs text-muted-foreground">Preparing DAG…</p>
		);
	}
	return <DagGraph nodes={vizNodes} />;
}

export const RunSubagentDagRenderer: ToolRenderer = {
	getDisplayName: () => 'Run sub-agent DAG',

	renderHeader: (pair) => {
		const count = nodeCountOf(pair);
		return (
			<>
				<span className={toolLabelClass}>Sub-agent DAG</span>
				{count > 0 && (
					<span className={toolArgClass}>
						{count} node{count === 1 ? '' : 's'}
					</span>
				)}
			</>
		);
	},

	renderBody: (pair) => <DagBody pair={pair} />,
};
```

- [ ] **Step 3: Register the renderer**

In `examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx`, add the import (after the `DeliverFilesRenderer` import, line 13):

```tsx
import { RunSubagentDagRenderer } from './RunSubagentDagRenderer';
```

And add to the `renderers` map (after `DeliverFiles: DeliverFilesRenderer,`, line 30):

```tsx
	run_subagent_dag: RunSubagentDagRenderer,
```

- [ ] **Step 4: Typecheck + lint**

Run (from `examples/web_ui/frontend/`): `pnpm build && pnpm lint`
Expected: build + lint clean.

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/components/dag/DagGraph.tsx examples/web_ui/frontend/src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx
git commit -m "$(cat <<'EOF'
feat(web-ui): render run_subagent_dag as an interactive DAG graph

Add a React Flow DAG graph + node-detail panel and register a
run_subagent_dag tool renderer that draws the graph from the final result
metadata (and the planned structure from call args while running).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Live overlay wiring — route events, hold state, animate

Route the two new CustomEvents into a `ChatViewport` overlay shared via `DagRunsContext`, so nodes animate in real time during a run. The renderer's live branch (Task 5) starts consuming real data.

**Files:**
- Modify: `examples/web_ui/frontend/src/hooks/useMessages.ts` (options + CUSTOM branches)
- Modify: `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx` (state, handlers, reset, provider)

**Interfaces:**
- Consumes: `useMessages` options; `DagRunsContext`, `DagRunLive`, `DagRunLiveNode`, `DagRunsState` (Task 4).
- Produces: `useMessages` options gain `onDagRunStarted?`/`onDagNodeUpdated?`; `ChatViewport` provides a populated `DagRunsContext` around its tree.

- [ ] **Step 1: Add options + CUSTOM branches in `useMessages.ts`**

In the `options?` object type (currently ends at line 125, after `onStateUpdated`), add:

```ts
		/**
		 * Called on a ``CUSTOM`` event ``name="dag_run_started"`` — a
		 * run_subagent_dag run began. ``value`` is
		 * ``{ run_id, nodes: [{ id, subagent, depends_on }] }``.
		 */
		onDagRunStarted?: (value: Record<string, unknown>) => void;
		/**
		 * Called on a ``CUSTOM`` event ``name="dag_node_updated"`` — a DAG
		 * node changed state. ``value`` is ``{ run_id, node, status }``.
		 */
		onDagNodeUpdated?: (value: Record<string, unknown>) => void;
```

In `processEvent`'s CUSTOM block, add two branches before the closing `}` of the `if (event.type === EventType.CUSTOM)` block — i.e. after the `subagent_user_confirm_result` branch (currently ends line 187), insert:

```ts
				} else if (custom.name === 'dag_run_started' && custom.value) {
					optionsRef.current?.onDagRunStarted?.(
						custom.value as Record<string, unknown>,
					);
				} else if (custom.name === 'dag_node_updated' && custom.value) {
					optionsRef.current?.onDagNodeUpdated?.(
						custom.value as Record<string, unknown>,
					);
```

(These become additional `else if` arms of the existing `if (custom.name === 'team_updated') { ... }` chain, kept before the `return;`.)

- [ ] **Step 2: Add imports + live state in `ChatViewport.tsx`**

Add imports near the other chat-component imports (beside line 11's `SubagentNamesContext` import):

```tsx
import { DagRunsContext } from '@/components/chat/DagRunsContext';
import type { DagRunLive, DagRunLiveNode } from '@/components/chat/DagRunsContext';
```

Also ensure `useMemo` is imported from `react` (it is already used at line 345, so it is in scope).

Add live state next to `tasksContext`/`permissionContext` (after line 158):

```tsx
	const [dagRuns, setDagRuns] = useState<Record<string, DagRunLive>>({});
	const [latestDagRunId, setLatestDagRunId] = useState<string | null>(null);
```

- [ ] **Step 3: Add the event handlers + pass them to `useMessages`**

After `handleStateUpdated` (currently ends line 170), add:

```tsx
	const handleDagRunStarted = useCallback((value: Record<string, unknown>) => {
		const runId = value.run_id as string | undefined;
		if (!runId) return;
		const rawNodes = Array.isArray(value.nodes)
			? (value.nodes as DagRunLiveNode[])
			: [];
		setDagRuns((prev) => ({
			...prev,
			[runId]: {
				nodes: rawNodes,
				byNode: Object.fromEntries(rawNodes.map((n) => [n.id, 'pending'])),
			},
		}));
		setLatestDagRunId(runId);
	}, []);

	const handleDagNodeUpdated = useCallback((value: Record<string, unknown>) => {
		const runId = value.run_id as string | undefined;
		const node = value.node as string | undefined;
		const status = value.status as string | undefined;
		if (!runId || !node || !status) return;
		setDagRuns((prev) => {
			const existing = prev[runId] ?? { nodes: [], byNode: {} };
			return {
				...prev,
				[runId]: {
					...existing,
					byNode: { ...existing.byNode, [node]: status },
				},
			};
		});
		setLatestDagRunId(runId);
	}, []);
```

Extend the `useMessages(...)` options object (currently lines 173-176) to:

```tsx
		useMessages(agentId, sessionId, {
			onTeamUpdated: handleTeamUpdated,
			onStateUpdated: handleStateUpdated,
			onDagRunStarted: handleDagRunStarted,
			onDagNodeUpdated: handleDagNodeUpdated,
		});
```

- [ ] **Step 4: Reset the overlay on session change + memoize the context value**

In the session-change reset effect (currently lines 368-374), add the two resets so a prior session's DAG never bleeds through:

```tsx
	useEffect(() => {
		setSelectedModel(null);
		setSelectedFallbackModel(null);
		setSelectedTTSModel(null);
		setSelectedKnowledgeConfig(null);
		setSelectedWorkDir(null);
		setDagRuns({});
		setLatestDagRunId(null);
	}, [sessionId]);
```

Add a memoized context value near `subagentNameSet` (after line 345):

```tsx
	const dagRunsState = useMemo(
		() => ({ dagRuns, latestRunId: latestDagRunId }),
		[dagRuns, latestDagRunId],
	);
```

- [ ] **Step 5: Provide the context around the tree**

Wrap the existing `<main>` inside a `DagRunsContext.Provider`. Change the opening of the return (currently lines 568-570):

```tsx
	return (
		<SubagentNamesContext.Provider value={subagentNameSet}>
			<DagRunsContext.Provider value={dagRunsState}>
				<main className="flex size-full">
```

And the closing (currently lines 783-784, `</main>` then `</SubagentNamesContext.Provider>`):

```tsx
				</main>
			</DagRunsContext.Provider>
		</SubagentNamesContext.Provider>
	);
```

(Adjust indentation of the wrapped block by one tab if the linter requires; `pnpm lint` will flag it.)

- [ ] **Step 6: Typecheck + lint**

Run (from `examples/web_ui/frontend/`): `pnpm build && pnpm lint`
Expected: build + lint clean.

- [ ] **Step 7: Commit**

```bash
git add examples/web_ui/frontend/src/hooks/useMessages.ts examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx
git commit -m "$(cat <<'EOF'
feat(web-ui): live-animate the DAG graph from progress events

Route dag_run_started/dag_node_updated CustomEvents into a ChatViewport
overlay shared via DagRunsContext; the DAG renderer animates nodes live
while running and reconciles to the final metadata when it lands.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Group-summary label + i18n

Give a collapsed tool-call group a meaningful "ran N sub-agent DAGs" summary instead of the generic "called N tools".

**Files:**
- Modify: `examples/web_ui/frontend/src/components/chat/MessageBubble.tsx` (`summarizeToolGroup`)
- Modify: `examples/web_ui/frontend/src/i18n/locales/en.json`, `examples/web_ui/frontend/src/i18n/locales/zh.json`

**Interfaces:**
- Consumes: `RUN_SUBAGENT_DAG_TOOL` (Task 4) — imported for the exact tool name.
- Produces: `tool.summary.dag_one` / `tool.summary.dag_other` i18n keys and a `run_subagent_dag` branch in `summarizeToolGroup`.

- [ ] **Step 1: Add the i18n keys (en)**

In `examples/web_ui/frontend/src/i18n/locales/en.json`, inside `tool.summary` (after `mcp_other`, line 88), add:

```json
			"dag_one": "ran {{count}} sub-agent DAG",
			"dag_other": "ran {{count}} sub-agent DAGs",
```

- [ ] **Step 2: Add the i18n keys (zh)**

In `examples/web_ui/frontend/src/i18n/locales/zh.json`, inside `tool.summary` (after `mcp_other`, line 88), add:

```json
			"dag_one": "运行 {{count}} 个子智能体 DAG",
			"dag_other": "运行 {{count}} 个子智能体 DAG",
```

- [ ] **Step 3: Count DAG calls in `summarizeToolGroup`**

In `examples/web_ui/frontend/src/components/chat/MessageBubble.tsx`, add the import (after line 30's `_shared` import):

```tsx
import { RUN_SUBAGENT_DAG_TOOL } from '@/components/dag/deriveDag';
```

In `summarizeToolGroup`, add a counter beside the others (after `let nSubagent = 0;`, line 318):

```tsx
	let nDag = 0;
```

Add a branch in the loop (before the `MCP_TOOL_PREFIX` check at line 343, so an exact match wins over any prefix logic):

```tsx
		} else if (name === RUN_SUBAGENT_DAG_TOOL) {
			nDag += 1;
```

Add its part (after the `nMCP` push, line 354):

```tsx
		if (nDag > 0) parts.push(t('tool.summary.dag', { count: nDag }));
```

- [ ] **Step 4: Typecheck + lint**

Run (from `examples/web_ui/frontend/`): `pnpm build && pnpm lint`
Expected: build + lint clean.

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/components/chat/MessageBubble.tsx examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "$(cat <<'EOF'
feat(web-ui): summarize run_subagent_dag calls in tool-group titles

Add tool.summary.dag i18n keys (en/zh) and a run_subagent_dag branch to
summarizeToolGroup so a collapsed group reads "ran N sub-agent DAGs".

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: End-to-end acceptance smoke

Drive the real web UI to prove the feature works end-to-end. Uses the `verify` skill.

**Files:** none (verification only).

- [ ] **Step 1: Run the full backend DAG suite once more**

Run: `python -m pytest tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py tests/subagent_dag_progress_test.py tests/subagent_dag_integration_test.py tests/subagent_agent_tools_test.py -v`
Expected: PASS (all).

- [ ] **Step 2: Boot the stack**

Follow the RavenX web-UI run setup: activate the `ravenx` env; ensure Redis is running (docker); start the service (`cd examples/agent_service && python main.py`, on the project's configured port); in a second terminal `cd examples/web_ui/frontend && pnpm dev`. Register at least one CLI sub-agent (e.g. a claude/codex preset) so `run_subagent_dag` is available to the leader.

- [ ] **Step 3: Trigger a multi-node DAG with a failure→skip**

Prompt the leader to run a small DAG via `run_subagent_dag` with at least one dependency edge and one node that fails (so a dependent is skipped) — e.g. two independent nodes feeding a third, plus a node whose sub-agent will error.

- [ ] **Step 4: Observe and confirm (the acceptance checklist)**

Using the browser (chrome-devtools / playwright MCP or manual), confirm:
- The tool-call box shows a **Sub-agent DAG · N nodes** header; expanding it reveals the graph with correct dependency edges.
- Nodes **animate live**: pending → running (spinner) → completed (green) / failed (red) / skipped (dashed grey) as the run proceeds.
- Clicking a node opens the detail panel with status, subagent, output-file path; a **failed** node shows its error text; a terminal node shows its inline output.
- When the run finishes, the box chrome **resolves** (no perpetual spinner).
- **Reload** the page: the completed DAG rebuilds the identical final graph from metadata (no live overlay needed).
- A collapsed tool group reads **"ran 1 sub-agent DAG"**.

- [ ] **Step 5: Fix-forward any smoke failures**

If any check fails, debug against the relevant task's files, fix, re-run `pnpm build && pnpm lint` (frontend) or the backend suite, and re-verify. Commit fixes with a `fix(...)` message.

- [ ] **Step 6: Final review + integrate**

Once the checklist passes, use `superpowers:finishing-a-development-branch` to decide how to integrate `feat/subagent-dag-viz` (the project convention is merge to local `main`).

---

## Self-Review

**1. Spec coverage:**
- §4.2 threading → Task 2 (steps 4-7). §4.3 emission points (started/running/completed/failed/skipped) → Task 2. §4.4 factory bus wiring + main.py → Task 3. §4.5 metadata enrichment → Task 1. §4.6 state fix → Task 1. §5 event contract → Task 2 (payload shapes asserted in the test). §6.1 dep → Task 4. §6.2 event routing → Task 6. §6.3 overlay state + reset + provider → Task 6. §6.4 context → Task 4. §6.5 deriveDag/layoutDag/DagGraph → Tasks 4-5. §6.6 renderer + reconciliation → Task 5 (+ live branch activated in Task 6). §6.7 node visuals → Task 5. §6.8 node detail → Task 5. §6.9 summary + i18n → Task 7. §7 single-active-run binding → Task 6 (`latestRunId`). §9 testing → Tasks 1-3 (backend), Task 8 (smoke). All covered.

**2. Placeholder scan:** No TBD/TODO. The only prose-not-code note (Task 3 Step 2 `_make_fake_*`) explicitly instructs reuse of the file's existing fixtures because their exact form is file-local; the surrounding test code is complete.

**3. Type consistency:** `DagVizNode`, `DagNodeData`, `DagFlowNode`, `DagManifest`, `DagFileEntry`, `DagRunLive`, `DagRunLiveNode`, `DagRunsState`, `readDagManifest`, `toStatus`, `layoutDag`, `useDagRuns`, `RUN_SUBAGENT_DAG_TOOL` are defined in Task 4 and consumed with the same names/signatures in Tasks 5-7. Backend `ProgressPublisher`, `_emit`, `run_dag(..., progress_publisher=...)`, `SubAgentDagTool(..., progress_publisher=...)`, and the `{run_id, node, status}` / `{run_id, nodes:[...]}` payloads are consistent across Tasks 2-3 and the Task 3 wiring test. The enriched `files` entry shape in Task 1 matches `DagFileEntry` in Task 4.
