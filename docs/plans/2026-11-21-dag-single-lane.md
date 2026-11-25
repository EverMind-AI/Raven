# Retire the `run_subagent_dag` foreground lane - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the synchronous mode of `run_subagent_dag` and its outbox/bound/release machinery, while keeping `raven playbook run` and Task-mode workflow capture working through a private completion wait and a post-turn capture finalizer.

**Architecture:** Three stages. First add a per-run completion future and an internal `execute_to_completion` method; then switch the CLI and Task capture onto it; then delete the model-facing `background` parameter and every foreground symbol. Tests are migrated last, in the same task as the deletion, so every intermediate state is green.

**Tech Stack:** Python 3.12, asyncio, pytest, uv.

**Spec:** `docs/specs/2026-11-21-dag-single-lane-design.md`

## Global Constraints

- Base: `origin/main`. Confirm the base with the owner before cutting. Branch:
  `refactor/retire_dag_foreground`. Worktree:
  `.worktrees/retire_dag_foreground`.
- No commits until the owner explicitly approves them (AGENTS.md section 3.4).
  Each task ends at "tests green", not at a commit. Commits, push, and PR are
  the final approval-gate task only.
- Test command is always `uv run --frozen --python 3.12 --all-extras pytest`.
  On this host `uv` is at `/root/.hermes/bin/uv`; run
  `export PATH=/root/.hermes/bin:$PATH` once per shell.
- Keep test runs light: do not pass `-n` or a high worker count.
- Source and comments stay English. Markdown files are self-governing but use
  ASCII punctuation.
- Add tests to the existing files named below. Do not create new test files
  (AGENTS.md section 5.1).
- Do not touch `evolver/`; it is an independent tree.
- The spec's deleted symbols must not survive under new names. Acceptance greps
  are listed in Task 4.

## Review Focus

These are the failure modes no single task's happy-path test covers. Each has a
pinning test in the task named beside it.

1. A stale model or stored playbook sends `background: false` after the switch
   is gone: the key is ignored, the run still starts in the background, and the
   receipt says the switch was retired. Task 3.
2. The caller of `execute_to_completion` is cancelled (Ctrl-C or test
   teardown): the run keeps going and is not left with a dead waiter. Task 1.
3. A capture turn ends before its DAG: a success compiles the Workflow and saves
   the run record exactly once, a failure saves no Workflow, a cancelled turn
   compiles nothing later, and a late run completion cannot double-save.
   Task 2.
4. An announcement turn runs with `agentHarness: generate`: it must not generate
   a Task Harness or save a Task artifact. Task 2.
5. `resolve_dag_node` after the wait removal: it records the decision and
   returns immediately, and the registry does not treat it as blocking.
   Task 3.

---

### Task 0: Worktree and baseline

**Files:** none.

**Interfaces:**
- Consumes: `origin/main`.
- Produces: a clean worktree on `refactor/retire_dag_foreground` with the spec
  and plan copied in, and a recorded baseline for the targeted suites.

- [ ] **Step 1: Create the worktree from the confirmed base**

```bash
git fetch origin main
git worktree add .worktrees/retire_dag_foreground -b refactor/retire_dag_foreground origin/main
```

Expected: `git worktree list` shows the new tree at
`.worktrees/retire_dag_foreground`.

- [ ] **Step 2: Copy the spec and this plan into the worktree**

```bash
MAIN=/Evermind/sh_evermind/xuedizhan/Raven
WT=$MAIN/.worktrees/retire_dag_foreground
mkdir -p "$WT/docs/plans"
cp "$MAIN/docs/specs/2026-11-21-dag-single-lane-design.md" "$WT/docs/specs/"
cp "$MAIN/docs/plans/2026-11-21-dag-single-lane.md" "$WT/docs/plans/"
```

Expected: both files exist under the worktree.

- [ ] **Step 3: Sync the environment and run the baseline**

```bash
cd /Evermind/sh_evermind/xuedizhan/Raven/.worktrees/retire_dag_foreground
export PATH=/root/.hermes/bin:$PATH
uv sync --frozen --all-extras
uv run --frozen --python 3.12 --all-extras pytest -q \
  tests/test_subagent_dag_runner.py \
  tests/test_subagent_dag_control_tools.py \
  tests/test_subagent_dag_adjudication.py \
  tests/test_subagent_dag_core.py \
  tests/test_cli_playbook_commands.py
```

Expected: pass. Record any pre-existing failure by name; do not fix unrelated
failures in this branch.

- [ ] **Step 4: Working checkpoint, no commit**

Report the worktree path, base SHA, and baseline result. Wait for the owner's
go-ahead before Task 1.

---

### Task 1: Private completion wait

**Files:**
- Modify: `raven/agent/subagent/dag_tool.py`
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: the existing `SubAgentDagTool`, `_execute`, `_dispatch`,
  `_run_detached`.
- Produces:
  - `SubAgentDagTool.execute_to_completion(nodes: list[dict], task_summary: str = "",
    confirm: bool = False, mcp_servers=None, mcp_scope: str | None = None,
    mcp_credential_gaps=None) -> str | ToolResult`
  - `_dispatch(..., result_future: asyncio.Future | None = None) -> tuple[str | ToolResult, bool]`
    where the bool says whether a run actually started.
  - `_run_detached(..., result_future: asyncio.Future | None = None) -> None`,
    resolving the future on every terminal path.
  - `_execute(..., wait: bool = False)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_subagent_dag_runner.py`, next to the other `_echo_tool`
tests:

```python
async def test_execute_to_completion_returns_the_final_result(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _echo_tool(tmp_path, None)
        out = await tool.execute_to_completion(task_summary="wait", nodes=_SOLO)
    assert "1 completed" in str(out)


async def test_execute_to_completion_returns_a_refusal_without_waiting(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _echo_tool(tmp_path, None)
        out = await tool.execute_to_completion(task_summary="bad", nodes=[])
    assert str(out).startswith("Error")
    assert not tool._runs


async def test_cancelling_the_waiter_leaves_the_run_alive(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _echo_tool(tmp_path, None)
        call = asyncio.create_task(tool.execute_to_completion(task_summary="wait", nodes=_SOLO))
        await _wait_until(lambda: bool(tool._runs))
        (run_id,) = list(tool._runs)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert run_id in tool.active_run_ids()
        tool.request_cancel(run_id)
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q tests/test_subagent_dag_runner.py \
  -k "execute_to_completion or cancelling_the_waiter"
```

Expected: FAIL with `AttributeError: 'SubAgentDagTool' object has no attribute 'execute_to_completion'`.

- [ ] **Step 3: Implement the scoped entry points and the future**

In `raven/agent/subagent/dag_tool.py`:

1. Add a private pass-through shared by `execute` and `execute_to_completion`:

```python
async def execute(
    self,
    nodes: list[dict],
    task_summary: str = "",
    background: bool = True,
    confirm: bool = False,
    mcp_servers: "Mapping[str, MCPServerConfig] | Callable[[], Mapping[str, MCPServerConfig]] | None" = None,
    mcp_scope: str | None = None,
    mcp_credential_gaps: "Callable[[], frozenset[str]] | None" = None,
    **kwargs: Any,
) -> str | ToolResult:
    return await self._execute_scoped(
        nodes,
        background=background,
        wait=False,
        task_summary=task_summary,
        confirm=confirm,
        mcp_servers=mcp_servers,
        mcp_scope=mcp_scope,
        mcp_credential_gaps=mcp_credential_gaps,
    )

async def execute_to_completion(
    self,
    nodes: list[dict],
    task_summary: str = "",
    confirm: bool = False,
    mcp_servers: "Mapping[str, MCPServerConfig] | Callable[[], Mapping[str, MCPServerConfig]] | None" = None,
    mcp_scope: str | None = None,
    mcp_credential_gaps: "Callable[[], frozenset[str]] | None" = None,
) -> str | ToolResult:
    """Dispatch a graph and wait for its final result. Not model-facing."""
    return await self._execute_scoped(
        nodes,
        background=True,
        wait=True,
        task_summary=task_summary,
        confirm=confirm,
        mcp_servers=mcp_servers,
        mcp_scope=mcp_scope,
        mcp_credential_gaps=mcp_credential_gaps,
    )
```

Move the current `execute` body (the `IN_SUBAGENT_RUN` check, `current_capture`,
the capture-forces-background branch, and the `run_mcp_scope` wrapper) into:

```python
async def _execute_scoped(self, nodes, *, background, wait, task_summary, confirm,
                          mcp_servers, mcp_scope, mcp_credential_gaps):
    ...
    with run_mcp_scope(mcp_servers, scope=mcp_scope, credential_gaps=mcp_credential_gaps):
        return await self._execute(
            nodes,
            background,
            confirm=confirm,
            task_summary=task_summary,
            capture_workflow=capture_workflow,
            playbook_capture=playbook_capture,
            mcp_servers=mcp_servers,
            mcp_scope=mcp_scope,
            mcp_credential_gaps=mcp_credential_gaps,
            wait=wait,
        )
```

Do not change the capture branch in this task.

2. In `_execute`, add `wait: bool = False`, create the future before
   `_dispatch`, and change the dispatch call:

```python
result_future = asyncio.get_running_loop().create_future() if wait else None
result, started = await self._dispatch(..., result_future=result_future)
if wait and started and result_future is not None:
    result = await result_future
```

Keep the notice and capture-notice wrapping after that line.

3. In `_dispatch`, add `result_future: asyncio.Future | None = None`, pass it to
   `_run_detached`, and return `(value, True)` at the end. On the adoption
   refusal return `(refusal, False)`. The outbox branch returns
   `(self.render_event(run_id, event), True)`.

4. In `_run_detached`, add `result_future: asyncio.Future | None = None` and a
   helper:

```python
@staticmethod
def _resolve_result(future: "asyncio.Future[Any] | None", result: str | ToolResult) -> None:
    if future is not None and not future.done():
        future.set_result(result)
```

Resolve it on every terminal path:
- `except asyncio.CancelledError`: `if result_future is not None: result_future.cancel()`,
  then re-raise.
- after `await outbox.put_final(...)` in the outbox branch.
- before the early return for `cancel.is_set() and not plan_finished`.
- before the early return when `self._announce is None`.
- before `await self._announce(...)`.
- The stint hand-over branch (`final is None`) does not resolve: `run_round`
  never passes a future, and the run is not terminal.

`run_round` keeps calling `_execute(..., background=True)`; `wait` defaults to
False.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q tests/test_subagent_dag_runner.py \
  -k "execute_to_completion or cancelling_the_waiter"
```

Expected: 3 passed.

- [ ] **Step 5: Run the rest of the DAG runner suite**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q tests/test_subagent_dag_runner.py
```

Expected: all pass; the foreground lane still exists and is untouched.

- [ ] **Step 6: Working checkpoint, no commit**

Report the new method, the future's resolution points, and the test result.

---

### Task 2: Async Task capture and the CLI wait

**Files:**
- Modify: `raven/playbook/run_record.py`
- Modify: `raven/agent/subagent/dag_tool.py`
- Modify: `raven/agent/loop/main.py`
- Modify: `raven/agent/loop/wiring.py`
- Modify: `raven/playbook/executor.py`
- Modify: `raven/cli/playbook_commands.py`
- Test: `tests/test_unified_playbook_e2e.py`
- Test: `tests/test_agent_loop_playbook_entry.py`
- Test: `tests/test_cli_playbook_commands.py`
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: `SubAgentDagTool.execute_to_completion` from Task 1.
- Produces:
  - `PlaybookRunCapture.active_run_id: str | None`,
    `.deferred: bool`, `.finalized: bool`,
    `.on_finished: Callable[[str], Awaitable[None]] | None`.
  - `wiring._capture_run_finished(resolution, capture, binding, run_id) -> None`
    and `wiring._finalize_capture(resolution, capture, binding) -> None`.
  - `AgentLoop._finalize_failed_capture(capture, *, flush: bool, failure: BaseException | None) -> None`
    for the turn's failure path.
  - `PlaybookExecutor(wait_for_result: bool = False)`.
  - `SubAgentDagTool._capture_tasks: set[asyncio.Task]`.

- [ ] **Step 1: Write the failing capture tests**

Add to `tests/test_unified_playbook_e2e.py`:

```python
@pytest.mark.asyncio
async def test_a_deferred_capture_compiles_when_the_run_finishes(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    projected, extended, harness = _draft_projection()
    provider = _EchoWorkflowProvider()
    loop = _capture_loop(tmp_path, provider, playbook_root)
    capture = PlaybookRunCapture(query="write the brief", capture_workflow=True)
    capture.active_run_id = "run-1"
    resolution = _task_resolution(harness, persisted=False)
    binding = SimpleNamespace(provider=provider, model="stub")

    await loop._finish_playbook_turn(resolution, capture, binding)
    assert capture.deferred is True and capture.finalized is False
    assert list((playbook_root / ".runs").glob("*.json")) == []

    record_completed_dag(projected, "run-1", capture, harness=extended)
    await loop._capture_run_finished(resolution, capture, binding, "run-1")

    assert capture.finalized is True
    assert len(provider.calls) == 1
    assert len(list((playbook_root / ".runs").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_a_finalized_capture_is_not_compiled_by_a_late_run_finish(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    _projected, _extended, harness = _draft_projection()
    provider = _EchoWorkflowProvider()
    loop = _capture_loop(tmp_path, provider, playbook_root)
    capture = PlaybookRunCapture(query="write the brief", capture_workflow=True)
    capture.active_run_id = "run-1"
    capture.deferred = True
    capture.finalized = True

    await loop._capture_run_finished(
        _task_resolution(harness, persisted=False),
        capture,
        SimpleNamespace(provider=provider, model="stub"),
        "run-1",
    )

    assert provider.calls == []
    assert list((playbook_root / ".runs").glob("*.json")) == []


@pytest.mark.asyncio
async def test_only_the_last_captured_run_owns_the_capture(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    _projected, _extended, harness = _draft_projection()
    provider = _EchoWorkflowProvider()
    loop = _capture_loop(tmp_path, provider, playbook_root)
    capture = PlaybookRunCapture(query="write the brief", capture_workflow=True)
    capture.active_run_id = "run-2"
    resolution = _task_resolution(harness, persisted=False)
    binding = SimpleNamespace(provider=provider, model="stub")

    await loop._capture_run_finished(resolution, capture, binding, "run-1")
    assert capture.active_run_id == "run-2"
    assert capture.finalized is False

    await loop._capture_run_finished(resolution, capture, binding, "run-2")
    assert capture.finalized is True


@pytest.mark.asyncio
async def test_a_deferred_capture_with_a_failed_run_saves_no_workflow(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    _projected, _extended, harness = _draft_projection()
    provider = _EchoWorkflowProvider()
    loop = _capture_loop(tmp_path, provider, playbook_root)
    capture = PlaybookRunCapture(query="write the brief", capture_workflow=True)
    capture.active_run_id = "run-1"
    resolution = _task_resolution(harness, persisted=False)
    binding = SimpleNamespace(provider=provider, model="stub")

    await loop._finish_playbook_turn(resolution, capture, binding)
    assert capture.deferred is True

    await loop._capture_run_finished(resolution, capture, binding, "run-1")

    assert capture.finalized is True
    assert provider.calls == []
    assert len(list((playbook_root / ".runs").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_a_cancelled_turn_finalizes_a_capture_and_a_late_finish_does_nothing(tmp_path) -> None:
    playbook_root = tmp_path / "playbooks"
    _projected, _extended, harness = _draft_projection()
    provider = _EchoWorkflowProvider()
    loop = _capture_loop(tmp_path, provider, playbook_root)
    capture = PlaybookRunCapture(query="write the brief", capture_workflow=True)
    capture.active_run_id = "run-1"
    resolution = _task_resolution(harness, persisted=False)
    binding = SimpleNamespace(provider=provider, model="stub")

    loop._finalize_failed_capture(capture, flush=False, failure=None)
    assert capture.finalized is True and capture.status == "cancelled"

    await loop._capture_run_finished(resolution, capture, binding, "run-1")

    assert provider.calls == []
    assert len(list((playbook_root / ".runs").glob("*.json"))) == 1
```

Add to `tests/test_agent_loop_playbook_entry.py`:

```python
async def test_an_announcement_turn_does_not_generate_a_harness(tmp_path) -> None:
    from types import SimpleNamespace
    from raven.config.schema import PlaybookConfig
    from raven.agent.loop.bundles import EngineWiring

    provider = _Provider()
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        model="fake/default",
        engine=EngineWiring(
            playbook_config=PlaybookConfig(enabled=True, agentHarness="generate")
        ),
    )
    req = TurnRequest(
        origin=Origin.SUBAGENT,
        source=Source(channel="web", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="DAG run dag-1 finished: 1 completed.",
        conversation="web:abc",
    )

    resolution = await loop._resolve_playbook_turn(
        req, "web:abc", SimpleNamespace(provider=provider, model="fake/default")
    )

    assert not resolution.active
    assert provider.calls == 0
```

Add to `tests/test_subagent_dag_runner.py`:

```python
async def test_a_captured_run_schedules_its_finisher_without_staying_live(tmp_path: Path) -> None:
    from raven.playbook.run_record import PlaybookRunCapture, capture_scope

    started = asyncio.Event()
    release = asyncio.Event()

    async def _finished(run_id: str) -> None:
        started.set()
        await release.wait()

    capture = PlaybookRunCapture(query="q", capture_workflow=True)
    capture.on_finished = _finished
    async with draining_dag_runs():
        tool = _echo_tool(tmp_path, None)
        with capture_scope(capture):
            out = await tool.execute(task_summary="capture", nodes=_SOLO)
        assert "started in the background" in str(out)
        await _wait_until(started.is_set)
        assert tool.active_run_ids() == set(), "the run ended before its finalizer"
        release.set()
```

In `tests/test_cli_playbook_commands.py`, change the assertion in
`test_run_executes_by_name_with_kv_params` from
`._background is False` to `._wait_for_result is True`.

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q \
  tests/test_unified_playbook_e2e.py \
  tests/test_agent_loop_playbook_entry.py \
  tests/test_subagent_dag_runner.py \
  tests/test_cli_playbook_commands.py \
  -k "deferred_capture or last_captured or failed_run or finalized_capture or cancelled_turn or announcement_turn or captured_run or run_executes_by_name"
```

Expected: FAIL on the missing attributes/methods and the old executor field.

- [ ] **Step 3: Add the capture fields and methods**

In `raven/playbook/run_record.py`, add to `PlaybookRunCapture`:

```python
active_run_id: str | None = None
deferred: bool = False
finalized: bool = False
on_finished: Callable[[str], Awaitable[None]] | None = field(
    default=None, repr=False, compare=False
)
```

Import `Awaitable` and `Callable` from `collections.abc` under `TYPE_CHECKING`
or at module level. None of the new fields appears in `as_dict()`.

- [ ] **Step 4: Schedule the callback from the DAG tool**

In `SubAgentDagTool.__init__`, add `self._capture_tasks: set[asyncio.Task] = set()`.

In `_dispatch`, after adoption succeeds and before the return, set the active
run when the capture exists:

```python
if capture is not None:
    capture[0].active_run_id = run_id
```

In `_run_detached`, wrap the body's terminal logic so that a capture schedules
its finisher without being awaited:

```python
def _schedule_capture_finished(self, capture: tuple[Any, SubAgentDagSpec] | None, run_id: str) -> None:
    if capture is None:
        return
    finished = getattr(capture[0], "on_finished", None)
    if finished is None:
        return
    task = asyncio.create_task(finished(run_id))
    self._capture_tasks.add(task)

    def _retire(t: asyncio.Task) -> None:
        self._capture_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.opt(exception=t.exception()).warning("capture finalizer failed")

    task.add_done_callback(_retire)
```

Call `self._schedule_capture_finished(capture, identity.submitted)` in a
`finally` around the `_run_detached` body, so cancellation schedules it too.
The scheduled task is the strong reference; the run task must not await it.

Change the capture branch in `_execute_scoped` from `background = False` to
`background = True` with a comment that a capture turn no longer waits.

- [ ] **Step 5: Split the playbook turn finalization**

In `raven/agent/loop/wiring.py`:

```python
async def _finish_playbook_turn(self, resolution, capture, binding) -> None:
    if self._playbooks is None or capture.finalized:
        return
    if capture.active_run_id is not None:
        capture.deferred = True
        return
    await self._finalize_capture(resolution, capture, binding)

async def _finalize_capture(self, resolution, capture, binding) -> None:
    if self._playbooks is None or capture.finalized:
        return
    capture.finalized = True
    capture.active_run_id = None
    ...  # the current body of _finish_playbook_turn
```

`_finalize_capture` keeps the current compile, `saved_playbook`, `finish()`,
and run-record save logic unchanged.

Add:

```python
async def _capture_run_finished(self, resolution, capture, binding, run_id: str) -> None:
    if capture.finalized or capture.active_run_id != run_id:
        return
    capture.active_run_id = None
    if capture.deferred:
        await self._finalize_capture(resolution, capture, binding)
```

Add the announcement guard at the top of `_resolve_playbook_turn`, after the
`mode == "off"` check and before any `self.subagents` access:

```python
from raven.spine.turn import Origin
if getattr(req, "origin", None) == Origin.SUBAGENT:
    return HarnessResolution()
```

- [ ] **Step 6: Attach the callback and fix the failure path**

In `raven/agent/loop/main.py`, after creating the capture:

```python
capture.on_finished = lambda run_id: self._capture_run_finished(
    resolution, capture, binding, run_id
)
```

Extract the turn's existing capture failure path into a method so it is
testable on its own:

```python
def _finalize_failed_capture(
    self, capture: Any, *, flush: bool, failure: BaseException | None
) -> None:
    if capture.status != "running" or self._playbooks is None:
        return
    if capture.deferred and failure is None and flush:
        return  # the run's completion finalizes this one
    capture.active_run_id = None
    capture.finalized = True
    capture.finish(
        status="cancelled" if not flush else "failed",
        error=None if failure is None else f"{type(failure).__name__}: {failure}",
    )
    ...  # save the run record as today
```

In the turn's `finally`, call:

```python
if capture is not None:
    self._finalize_failed_capture(capture, flush=flush, failure=failure)
```

The existing `RunRecordStore` save and its warning stay inside
`_finalize_failed_capture`.

- [ ] **Step 7: Switch the CLI executor to the completion wait**

In `raven/playbook/executor.py`:

- Rename the constructor parameter and field `background` to
  `wait_for_result`; keep the default `False`.
- In `_dispatch_playbook`, set the callable before the existing `await`:

```python
dispatch = self._dag_tool.execute_to_completion if self._wait_for_result else self._dag_tool.execute
```

- Replace `await self._dag_tool.execute(` with `await dispatch(`, then delete
  the `background=self._background` keyword. The `tool_nodes`, `task_summary`,
  `mcp_servers`, `mcp_scope`, `mcp_credential_gaps`, and `confirm` keywords
  stay byte for byte. Both methods accept that same set.

In `raven/cli/playbook_commands.py`, pass `wait_for_result=True` and keep the
command docstring's run-to-completion contract.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q \
  tests/test_unified_playbook_e2e.py \
  tests/test_agent_loop_playbook_entry.py \
  tests/test_subagent_dag_runner.py \
  tests/test_cli_playbook_commands.py
```

Expected: all pass. The old `background` parameter still exists and its
foreground tests still pass.

- [ ] **Step 9: Working checkpoint, no commit**

Report the capture state machine, the origin guard, and the CLI change.

---

### Task 3: Delete the foreground lane and the model-facing switch

**Files:**
- Modify: `raven/agent/subagent/dag_adjudication.py`
- Modify: `raven/agent/subagent/dag_tool.py`
- Modify: `raven/agent/subagent/dag_control_tools.py`
- Modify: `raven/agent/subagent/dag_runner.py`
- Modify: `raven/agent/subagent/dag_live.py`
- Modify: `raven/agent/subagent/manager.py`
- Modify: `raven/agent/loop/main.py`
- Modify: `raven/agent/loop/wiring.py`
- Modify: `raven/agent/tools/tool_search.py`
- Modify: `raven/rpc/spine.py`
- Modify: `CONTEXT.md`
- Modify: `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md`
- Test: `tests/test_subagent_dag_core.py`
- Test: `tests/test_subagent_dag_runner.py`
- Test: `tests/test_subagent_dag_control_tools.py`
- Test: `tests/test_subagent_dag_adjudication.py`
- Test: `tests/test_subagent_dag_mcp_scope.py`
- Test: `tests/test_agent_loop_run_emit.py`
- Test: `tests/test_agent_playbook_delegate.py`
- Test: `tests/test_playbook_stint.py`
- Test: `tests/test_unified_playbook.py`
- Test: `tests/test_security_untrusted_context.py`
- Test: `tests/test_rpc_dag.py`

**Interfaces:**
- Consumes: the completion wait from Task 1 and the capture finalizer from
  Task 2.
- Produces: a single-lane `SubAgentDagTool` whose schema and behavior contain no
  `background` mode.

- [ ] **Step 1: Write the failing structural tests**

Add to `tests/test_subagent_dag_core.py`:

```python
def test_the_dag_tool_has_no_background_parameter(tmp_path: Path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(workspace=tmp_path)
    assert "background" not in tool.parameters["properties"]
    assert "The graph runs in the background" in tool.description
    assert "By default" not in tool.description


def test_the_foreground_lane_is_gone(tmp_path: Path) -> None:
    import raven.agent.subagent.dag_adjudication as adjudication
    from raven.agent.subagent.dag_control_tools import ResolveDagNodeTool
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(workspace=tmp_path)
    assert not hasattr(adjudication, "Outbox")
    for name in (
        "is_foreground",
        "await_run",
        "abort_run",
        "release_turn",
        "render_event",
        "retire_reports",
    ):
        assert not hasattr(SubAgentDagTool, name)
    assert not hasattr(tool, "_outboxes")
    assert ResolveDagNodeTool(loop=object()).blocking_for({"run_id": "r1"}) is False
```

Add to `tests/test_subagent_dag_runner.py`:

```python
async def test_a_legacy_background_key_is_ignored_with_a_notice(tmp_path: Path) -> None:
    async with draining_dag_runs():
        tool = _echo_tool(tmp_path, None)
        out = await tool.execute(
            task_summary="legacy",
            nodes=_SOLO,
            background=False,
        )
        assert "no longer has a background switch" in str(out)
        assert tool.active_run_ids()
        for run_id in list(tool.active_run_ids()):
            tool.request_cancel(run_id)
```

Add to `tests/test_subagent_dag_control_tools.py`:

```python
async def test_a_resolve_returns_immediately_after_recording_the_decision() -> None:
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")
    assert out == "Node 'a' of run r1 will run again with your message."
    assert loop._dag_tool.awaited == []
```

(A `_LoopWithRun` whose fake tool has no `await_run`; adjust the fake if it still
has one.)

Add to `tests/test_subagent_dag_core.py` the guide pin:

```python
def test_the_guide_no_longer_offers_a_background_false_call() -> None:
    text = _guide_text()
    assert "background: false" not in text
    assert "background` (see" not in text
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q \
  tests/test_subagent_dag_core.py tests/test_subagent_dag_control_tools.py \
  -k "no_background_parameter or foreground_lane_is_gone or legacy_background or returns_immediately or guide_no_longer"
```

Expected: FAIL on the still-present parameter, symbols, and skill text.

- [ ] **Step 3: Delete the outbox and its event types**

In `raven/agent/subagent/dag_adjudication.py`:
- Delete `Report`, `Final`, `Stopped`, `Event`, `AnnounceReport`,
  `AnnounceFinal`, `_describe`, and `Outbox` (lines 318-601 at the current
  revision). Keep `deliver_report`, its constants, and `AdjudicationDesk`.
- Update the module docstring to describe only the desk.
- Remove `deque` and `partial` imports; keep `Awaitable`, `Callable`, and
  `logger` because `deliver_report` uses them.

- [ ] **Step 4: Make the DAG tool single-lane**

In `raven/agent/subagent/dag_tool.py`:
- Update the import block: remove `Final`, `Outbox`, `Report`, `Stopped`.
- Replace `_successful_final(event)` with:

```python
def _successful_result(result: DagRunResult, *, stopped: bool) -> bool:
    if stopped or not isinstance(result, DagRunResult):
        return False
    summary = result.summary or {}
    return (
        bool(summary.get("total"))
        and summary.get("completed", 0) + summary.get(NOT_EXECUTED, 0) == summary.get("total")
        and not any(summary.get(key, 0) for key in ("failed", "cancelled", "skipped"))
    )
```

Update its call site to `_successful_result(result, stopped=cancel.is_set())`.

- Delete `_FOREGROUND_REPORT_TAIL`, `is_foreground`, `await_run`, `abort_run`,
  `release_turn`, `render_event`, `retire_reports`, and the `_outboxes` field.
  Remove `_outboxes` from `_rekey_run`.
- Remove `background` from `parameters` and from `execute`. In `execute`, read
  the legacy key from `**kwargs` and pass `retired_background="background" in
  kwargs` into the scoped helper. In `_execute`, after `_dispatch` returns
  `started=True`, append:

```python
_BACKGROUND_RETIRED_NOTICE = (
    "Note: run_subagent_dag no longer has a background switch; this run was "
    "started in the background and its result will be announced when it finishes."
)
```

to `notices` when `retired_background` is true. Do not add it on a refusal.
- Delete the `background` parameter from `_execute`, `_execute_scoped`, and
  `_dispatch`. In `run_round`, delete the second positional `True` from its
  `_execute(nodes, True, ...)` call and keep `confirm=confirm` as a keyword.
  `_dispatch` always creates the task and returns `(result, started)`.
- In `_dispatch`, delete the outbox construction, the `_outboxes` registration,
  the adoption-refusal `outbox.stop()`, the `await outbox.take()` branch, and
  the `cancel/desk/outbox` retirement entries for `_outboxes`. Keep the control
  hint and the background receipt.
- In `_run_detached`, delete the `outbox` parameter and branches; keep the
  `result_future` resolution and the capture scheduling from Tasks 1 and 2.
  The `except asyncio.CancelledError` branch no longer stops an outbox.
- In `_run`, delete the `outbox` parameter, the `_to_outbox` closure, the
  `released` local, and the `released=released` argument to `run_dag`.
- Keep `blocking_interaction = True` and update its comment: the tool still
  waits on the user's confirmation, never on the graph.

- [ ] **Step 5: Make the control tools return immediately**

In `raven/agent/subagent/dag_control_tools.py`:
- Delete the `blocking_for` override.
- Delete the `await_run`/`render_event`/`abort_run` tails in the reassign,
  replan, continue, and abandon paths, and the `was_bound` branch with its
  `retire_reports` call.
- Keep the `except asyncio.CancelledError` around the replan proposal wait; it
  still withdraws a proposal.
- Update the class docstring and `description`: the tool answers a suspended
  node and returns; it never waits.

- [ ] **Step 6: Remove the runner and loop release plumbing**

In `raven/agent/subagent/dag_runner.py`:
- Remove `released` from `run_dag` and `_await_adjudications`.
- Remove the `unbind` task, the `deadline = None` branch, and the
  `released=released` arguments at the two `_await_adjudications` call sites.
- Delete the `released` comments at lines 222, 678, and 2555-2559; the deadline
  always starts at entry.

In `raven/agent/loop/main.py`:
- Delete `_release_dag_runs` and the call in the turn's `finally`.
- Leave the capture failure path and the `on_finished` attachment from Task 2.

- [ ] **Step 7: Update the remaining comments and docs**

- `raven/agent/subagent/dag_live.py`: the `owning_tool` docstring lists
  `prepare_replan`, `is_foreground`, `propose_replan`, `retire_reports`,
  `await_run`; keep only the methods that still exist.
- `raven/agent/subagent/manager.py`: update the two comments that compare the
  announce route with a foreground route (lines 2425 and 2488).
- `raven/rpc/spine.py`: update the `dag_node_stalled` comment; the notice now
  always announces.
- `raven/agent/tools/tool_search.py`: `target_is_blocking`'s docstring no longer
  uses `resolve_dag_node` as its param-dependent example; keep the general
  statement.
- `raven/agent/loop/wiring.py`: update the plugin-gate comment that mentions a
  foreground graph.
- `CONTEXT.md`: delete the `outbox` and `bound / released` entries (lines
  4322-4345).
- `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md`:
  - line 67: drop `background` from the call's top-level fields;
  - delete the `background: false` bullet (lines 310-312);
  - delete the "In a `background: false` call ..." sentence (lines 324-325);
  - line 376: change "a background run's announcement or a blocking call's
    return value" to "the announcement that follows a run".
- `raven/agent/subagent/dag_tool.py` module docstring: remove the
  `background=false` paragraph and describe the single lane.

- [ ] **Step 8: Delete and adapt the tests**

Delete the tests that specify the removed behavior:
- `tests/test_subagent_dag_adjudication.py`: every test that constructs
  `Outbox` or imports `Final`/`Report`/`Stopped`. Keep the desk tests and the
  `deliver_report` tests.
- `tests/test_subagent_dag_runner.py`: `test_a_foreground_call_returns_the_first_report_while_the_graph_keeps_running`,
  `test_resolving_the_node_and_awaiting_returns_the_final_result`,
  `test_a_second_suspended_node_is_handed_over_on_the_next_await`,
  `test_a_foreground_run_with_no_suspension_returns_the_summary_as_before`,
  `test_a_terminal_report_does_not_return_the_blocking_call`,
  `test_cancelling_the_awaiting_call_cancels_the_run`,
  every `test_release_*`, `test_a_released_run_announces_a_terminal_report_as_a_notification`,
  `test_resolving_a_queued_node_is_answered_with_the_other_node_not_its_own`,
  `test_deciding_the_node_the_call_was_not_handed_still_ends_the_tool_call`,
  `test_a_resolve_through_the_control_tool_is_handed_the_run_it_completes`,
  `test_a_stopped_run_tells_the_waiting_call_that_it_was_stopped`,
  `test_release_touches_only_the_named_conversation`,
  `test_release_turn_survives_a_raising_outbox_and_still_releases_the_rest`,
  `test_a_released_foreground_graph_links_each_report_to_its_attempt_and_its_result_to_the_dispatch`,
  and the four `released`-deadline tests at lines 7901-8012.
  Keep `test_a_backgrounded_run_still_announces_a_terminal_report`,
  `test_a_stint_driver_passes_a_notification_on_with_its_output`, and
  `test_a_suspended_node_holds_no_dispatch_slot`; adapt the last two to a
  background dispatch (run id from `tool.active_run_ids()` after
  `_wait_until(lambda: bool(tool._desks))`).
- `tests/test_subagent_dag_control_tools.py`:
  `test_resolve_blocks_only_for_a_bound_foreground_run`,
  `test_a_foreground_resolve_returns_the_next_event_rendered`,
  `test_a_refused_resolve_never_waits` (keep only its non-wait assertion if it
  still reads usefully),
  `test_cancelling_a_waiting_resolve_aborts_the_run`,
  `test_the_resolve_tool_parks_its_taker_before_it_yields`,
  `test_the_foreground_report_advertises_resolve_once_not_twice`,
  `test_a_foreground_reassign_waits_on_the_owning_tools_outbox`,
  `test_cancelling_a_waiting_reassign_aborts_the_run`,
  `test_a_bound_replan_stays_bound_and_returns_the_next_event`.
  Remove `_LANES` and the `bound` parametrization from the four research tests;
  run them background-only and delete their `if background:` branches.
  Simplify `_ResolvableDagTool`, `_LoopWithRun`, and `_suspended` by removing
  `foreground`, `events`, `awaited`, `await_run`, `render_event`,
  `is_foreground`, and `retire_reports`.
- `tests/test_agent_loop_run_emit.py`: delete the four
  `test_run_turn_releases_*` / `test_a_cancelled_turn_releases_*` tests.
- `tests/test_unified_playbook.py`: update `_successful_final` calls to the new
  `_successful_result(DagRunResult, stopped=...)` shape.

Adapt the convenience sites:
- Replace `background=False` with `execute_to_completion` and drop the keyword
  in `tests/test_subagent_dag_mcp_scope.py` (10 sites),
  `tests/test_playbook_stint.py` (2), `tests/test_agent_playbook_delegate.py`
  (2), `tests/test_subagent_dag_core.py` (1), and the remaining non-foreground
  sites in `tests/test_subagent_dag_runner.py`.
- Drop `background=True` arguments everywhere.
- `tests/test_subagent_dag_core.py`: update the `_outboxes` re-key test to the
  reduced index tuple; rewrite
  `test_the_dag_description_says_background_is_the_default` into
  `test_the_dag_description_has_one_lane`.
- `tests/test_security_untrusted_context.py`: remove `"background": False` from
  the DAG call payload.
- `tests/test_rpc_dag.py`: update the comment about a bound foreground run.

- [ ] **Step 9: Run the targeted suites**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q \
  tests/test_subagent_dag_core.py \
  tests/test_subagent_dag_adjudication.py \
  tests/test_subagent_dag_control_tools.py \
  tests/test_subagent_dag_runner.py \
  tests/test_subagent_dag_mcp_scope.py \
  tests/test_agent_loop_run_emit.py \
  tests/test_agent_playbook_delegate.py \
  tests/test_playbook_stint.py \
  tests/test_unified_playbook.py \
  tests/test_security_untrusted_context.py \
  tests/test_rpc_dag.py \
  tests/test_cli_playbook_commands.py
```

Expected: all pass. Fix only failures caused by this change; record anything
pre-existing from Task 0.

- [ ] **Step 10: Working checkpoint, no commit**

Report the deleted symbol inventory, the migrated test count, and the targeted
suite result.

---

### Task 4: Full verification and owner review

**Files:** none.

**Interfaces:**
- Consumes: the branch from Task 3.
- Produces: evidence for the owner's review.

- [ ] **Step 1: Run the structural acceptance greps**

```bash
rg -n "Outbox|is_foreground|await_run|abort_run|release_turn|retire_reports|_outboxes|_FOREGROUND_REPORT_TAIL" \
  raven/agent/subagent/ raven/agent/loop/ raven/rpc/ raven/agent/tools/
rg -n "released=" raven/agent/subagent/dag_tool.py raven/agent/subagent/dag_runner.py
rg -n "background: false|background=false" raven/ CONTEXT.md
rg -n '"background"' raven/agent/subagent/dag_tool.py
```

Expected: no production references to the deleted symbols; the only
`background` mention left in `dag_tool.py` is the legacy-key notice and its
comment.

- [ ] **Step 2: Run the full Python suite**

```bash
uv run --frozen --python 3.12 --all-extras pytest -q
```

Expected: pass, or only the pre-existing failures recorded in Task 0. Do not
use `-n`.

- [ ] **Step 3: Run the repository gates**

```bash
make lint-python
make check-source-language
make check-large-files
```

Expected: all pass. If `make` cannot find `uv`, export
`PATH=/root/.hermes/bin:$PATH` first.

- [ ] **Step 4: Review the diff against the spec**

Read the branch diff and check each spec section has landed:
- one lane and no `background` in the schema;
- outbox and event types deleted;
- runner `released` and turn release deleted;
- `execute_to_completion` used by the CLI;
- capture deferral and finalizer;
- announcement origin guard;
- docs, skill, and CONTEXT terms updated;
- foreground tests deleted, convenience sites migrated.

- [ ] **Step 5: Working checkpoint, no commit**

Report the full-suite result, gate results, and a short spec-coverage table.
Wait for the owner's review.

---

### Task 5: Approval gate for commit, push, and PR

**Files:** none.

**Interfaces:**
- Consumes: the reviewed branch.
- Produces: the owner-approved commit(s), push, and PR.

- [ ] **Step 1: Present the commit plan and exact messages**

Propose one conventional commit for the whole change, for example:

```
refactor(agent): retire the run_subagent_dag foreground lane
```

The body is English and ASCII. The owner may instead ask for a split by task.
Wait for explicit commit approval (AGENTS.md section 3.4).

- [ ] **Step 2: Commit only after approval**

```bash
git add -A
git commit -m "refactor(agent): retire the run_subagent_dag foreground lane"
```

`git add -A` includes the spec and this plan. If the owner asked for a split
commit series, stage the paths they named instead. Follow the repository's
trailer rule in force at execution time.

- [ ] **Step 3: Sync before push**

```bash
git fetch origin main
git merge-tree --write-tree HEAD origin/main
git rebase origin/main
uv run --frozen --python 3.12 --all-extras pytest -q
```

Expected: clean merge-tree, clean rebase, tests still pass.

- [ ] **Step 4: Push and offer the PR**

Push only when the owner says so, using `--force-with-lease` after a rebase.
Then offer `gh pr create` with an all-English title and body per the repository
template.
