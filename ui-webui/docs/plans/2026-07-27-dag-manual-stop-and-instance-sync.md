# DAG Manual Stop + Instance Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every automatic sub-agent timeout with a manual stop control in the chat instance list that also frees the concurrency slot, and make running and finished DAG nodes visible there and restorable after a page reload.

**Architecture:** The instance registry (`~/.raven/subagent_instances.json`) becomes the single place the UI reads sub-agent state from, holding stateful-CLI instances, in-flight spawns, and DAG nodes. The DAG runner writes a node's status on every transition and honours a per-run cancellation event; the manager does the same for spawns and gains a per-instance cancel. Cancellation is always a real `task.cancel()`, never a status-only mark, because that is what unwinds the `async with semaphore` and returns the concurrency slot. The `blocking_interaction` seam removes the tool-level timeout ceiling, and `timeout` becomes opt-in rather than a default.

**Why cancellation must cancel the task.** Both concurrency limits are `asyncio.Semaphore` held across an `async with`: the manager's four-slot `_gate` (`raven/agent/subagent/manager.py:190`) and the DAG's own `Semaphore(max_concurrency)` (`raven/agent/subagent_dag/runner.py:112`). Cancelling the owning task unwinds the block and frees the slot for free; marking a row "skipped" without cancelling would leave the slot held forever, which with no timeouts is a permanent stall.

**Tech Stack:** Python 3.13 / pydantic v2 / pytest (raven core, `uv`); React 19 + Vite + Tailwind v4 (ui-webui frontend, `pnpm`); FastAPI (ui-webui service).

**Branch:** `feat/unified_subagents_page`, continuing from `1ed31ef`.

## Global Constraints

- Python deps and tests via `uv` only: `uv run pytest ...`, never bare `pytest`.
- Do not create new test files. Extend `tests/test_subagent_third_party.py`, `tests/test_web_rpc_config.py`, and `tests/test_subagent_dag_runner.py`.
- House lint gate on touched Python: `uv run ruff format --check <files>` and `uv run ruff check <files>` both clean. (CI's `make lint-python` does not cover `raven/`, but keep the diff consistent.)
- Frontend gates: `pnpm -C ui-webui/frontend lint` (0 errors; 18 pre-existing warnings) and `pnpm -C ui-webui/frontend build`. Run `npx prettier --write` from `ui-webui/frontend` on touched frontend files.
- i18n: targeted edits to `src/i18n/locales/{en,zh}.json`, both locales in sync, never a full re-serialize, and touch only the keys a task names.
- Source comments in English, only where non-obvious.
- Commits: Conventional Commits, all-ASCII, `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>`.
- **Do not run `git commit`.** The controller commits after each task's review.

## Sequencing constraint

Task 6 removes every automatic timeout. It must land **last**: until the stop control exists end to end, removing the timeouts would leave no way at all to end a runaway run.

## File Structure

**Raven core (modified):**
- `raven/agent/subagent/instances.py` — record `kind` and `status`, `upsert_dag_node`, `upsert_spawn`.
- `raven/agent/subagent_dag/runner.py` — write node transitions to the registry; observe the cancel event.
- `raven/agent/subagent_dag/tool.py` — own the per-run cancel events; expose a lookup for the RPC; `blocking_interaction`.
- `raven/agent/subagent/manager.py` — record a spawn's running/terminal state; index in-flight tasks by instance handle; `cancel_by_instance`.
- `raven/agent/subagent/backends/cli_agent.py` — kill the process group on cancellation; optional timeout; narrow the resume retry.
- `raven/agent/subagent/backends/openai_api.py` — optional timeout.
- `raven/config/schema.py` — `timeout` becomes `int | None = None`.
- `raven/agent/subagent/presets.py` — drop the `timeout` values.
- `raven/web_rpc/methods_config.py` — the two cancel RPCs.

**ui-webui service (modified):** `service/raven_config_routes.py` — the two cancel routes.

**Frontend (modified):** `src/api/ravenConfig.ts`, `src/components/subagent/SubagentInstanceMonitor.tsx`, `src/i18n/locales/{en,zh}.json`.

---

## Task 1: Registry holds DAG nodes

**Files:**
- Modify: `raven/agent/subagent/instances.py`
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - Every record gains `kind: "cli" | "dag-node"`. A record loaded without `kind` is treated as `"cli"` — existing files must keep working.
  - A `dag-node` record carries `runId: str`, `nodeId: str`, `status: str` (`pending` | `running` | `completed` | `failed` | `skipped`), and `agent: str`; its `handle` is `f"{runId}/{nodeId}"` so it cannot collide with a user-chosen CLI handle.
  - `async upsert_dag_node(session_key, run_id, node_id, agent, status) -> None` — creates or updates, preserving `createdAtMs`, bumping `updatedAtMs`.
  - `list_instances(session_key=None)` unchanged in signature; it now returns both kinds, still most-recently-updated first.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`, next to the other registry tests:

```python
async def test_registry_upsert_dag_node_roundtrip(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_dag_node("web:s1", "run-1", "analyze", "claude_code", "running")
    rows = reg.list_instances("web:s1")
    assert len(rows) == 1
    assert rows[0]["kind"] == "dag-node"
    assert rows[0]["handle"] == "run-1/analyze"
    assert rows[0]["runId"] == "run-1"
    assert rows[0]["nodeId"] == "analyze"
    assert rows[0]["status"] == "running"

    created = rows[0]["createdAtMs"]
    await reg.upsert_dag_node("web:s1", "run-1", "analyze", "claude_code", "completed")
    rows = reg.list_instances("web:s1")
    # An update, not a second row, and the creation time survives.
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["createdAtMs"] == created


async def test_registry_dag_node_persists_and_coexists_with_cli(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    reg = InstanceRegistry(path=path)
    await reg.commit("web:s1", "claude_code", "refactor", "sess-a")
    await reg.upsert_dag_node("web:s1", "run-1", "review", "codex", "running")

    reloaded = InstanceRegistry(path=path)
    kinds = {r["handle"]: r["kind"] for r in reloaded.list_instances("web:s1")}
    assert kinds == {"refactor": "cli", "run-1/review": "dag-node"}
    # The CLI lookup path must not see the DAG row.
    assert await reloaded.lookup("web:s1", "claude_code", "refactor") == "sess-a"


async def test_registry_legacy_record_without_kind_reads_as_cli(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "sessionKey": "web:s1",
                        "agent": "claude_code",
                        "handle": "old",
                        "agentId": "x",
                        "createdAtMs": 1,
                        "updatedAtMs": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = InstanceRegistry(path=path)
    assert reg.list_instances()[0]["kind"] == "cli"
    assert await reg.lookup("web:s1", "claude_code", "old") == "x"


async def test_registry_delete_session_removes_dag_nodes_too(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "h", "sess-a")
    await reg.upsert_dag_node("web:s1", "run-1", "n", "codex", "running")
    await reg.upsert_dag_node("web:s2", "run-2", "n", "codex", "running")
    assert await reg.delete_session("web:s1") == 2
    assert [r["handle"] for r in reg.list_instances()] == ["run-2/n"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "dag_node or legacy_record" -v`
Expected: FAIL — `InstanceRegistry` has no attribute `upsert_dag_node`, and `list_instances` rows have no `kind`.

- [ ] **Step 3: Implement**

In `raven/agent/subagent/instances.py`:

- In `_load`, default a missing `kind` to `"cli"` when reading a record, so files written before this change keep working. Do it where the record is accepted into the dict, not by mutating the file.
- In `commit`, write `"kind": "cli"` alongside the existing fields.
- Add `upsert_dag_node` mirroring `commit`'s lock-load-mutate-flush shape:

```python
    async def upsert_dag_node(
        self, session_key: str, run_id: str, node_id: str, agent: str, status: str
    ) -> None:
        """Record or update one DAG node's status.

        The handle is namespaced by run id so a node can never collide with a
        user-chosen CLI instance handle.
        """
        handle = f"{run_id}/{node_id}"
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            existing = records.get(key) or {}
            records[key] = {
                "kind": "dag-node",
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "runId": run_id,
                "nodeId": node_id,
                "status": status,
                "createdAtMs": existing.get("createdAtMs", now),
                "updatedAtMs": now,
            }
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning(
                    "subagent instance registry write failed (dag node state live in-process, "
                    "will not survive restart): {}",
                    e,
                )
```

`lookup` must keep returning only CLI records' `agentId`. A `dag-node` record has no `agentId`, so `rec.get("agentId")` already yields `None` — but add an explicit `kind` check so the intent is on the page rather than implied.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_subagent_third_party.py -v`
Expected: all pass, including the pre-existing registry tests.

- [ ] **Step 5: Lint**

Run both ruff gates on `raven/agent/subagent/instances.py` and `tests/test_subagent_third_party.py`.

---

## Task 2: Runner reports node state and honours cancellation

**Files:**
- Modify: `raven/agent/subagent_dag/runner.py`, `raven/agent/subagent_dag/tool.py`
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: `InstanceRegistry.upsert_dag_node` (Task 1).
- Produces:
  - `run_dag(..., session_key: str | None = None, cancel: asyncio.Event | None = None)`.
  - The tool owns `dict[str, asyncio.Event]` keyed by `run_id`, with `request_cancel(run_id) -> bool` and `active_run_ids() -> list[str]`.
  - Every node status transition is written to the registry when `session_key` is set.

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_subagent_dag_runner.py`. Match its existing fixture style — read the file first and reuse whatever fake backend and graph helpers it already has rather than inventing new ones.

Three cases:

1. **Status transitions reach the registry.** Run a two-node graph with a `session_key` and an injected temp-path registry; assert the registry ends with both nodes at `completed`, and that `running` was observed for at least one of them (record the statuses seen via a wrapper around `upsert_dag_node`).
2. **Cancellation skips the unfinished.** Set the cancel event while node `b` is in flight; assert `b` and its dependents end `skipped`, node `a` stays `completed`, and the manifest reflects it.
3. **No session key, no registry writes.** With `session_key=None`, assert `upsert_dag_node` is never called — a CLI or cron-triggered DAG must not write rows nothing will ever reclaim.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_runner.py -v`
Expected: FAIL — `run_dag` takes no `cancel` argument and nothing writes to a registry.

- [ ] **Step 3: Implement**

`runner.py`:

- Add `session_key: str | None = None` and `cancel: asyncio.Event | None = None` to `run_dag`, threading both to the group and node helpers exactly as `session_key` is already threaded.
- Where the runner sets a node's status (the `pending` -> `running` transition, and each terminal transition), also `await registry.upsert_dag_node(session_key, store.run_id, nid, node.subagent, status)` when `session_key` is set. Use `get_registry()`; do not construct one.
- At the top of the scheduling loop, if `cancel is not None and cancel.is_set()`, mark every node not already terminal as `skipped`, emit the same progress event the existing skip path emits, write those statuses to the registry, and break out so the manifest is still written. Cancellation must produce a normal finished run, not an exception.
- A node already in flight when cancellation lands: cancel its task and let `_exec`'s handling (Task 5) kill the child. Await the cancelled task so the process is reaped before `run_dag` returns.

`tool.py`:

- Hold `self._cancels: dict[str, asyncio.Event]`. Create an event per run before calling `run_dag`, pass it in, and remove it in a `finally`.
- `request_cancel(run_id) -> bool` sets the event if the run is known, returns whether it was.
- `active_run_ids() -> list[str]` returns the live keys.
- Pass `session_key=self._conversation.get()` — the same value already threaded for the backend call.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py -v`
Expected: all pass. The DAG suites are the regression net for this task; a failure here means the scheduler changed behaviour.

- [ ] **Step 5: Lint** both ruff gates on the touched files.

---

## Task 3: Manager records spawn state and cancels one instance

**Files:**
- Modify: `raven/agent/subagent/manager.py`
- Modify: `raven/agent/subagent/instances.py` (add `upsert_spawn`)
- Test: `tests/test_subagent_third_party.py`

**Why this task exists:** a spawn only reaches the registry today after a *successful create commit*, so a sub-agent that is still running — or that is wedged, which is exactly the one worth stopping — has no row and therefore nowhere to put a stop button. And `SubagentManager` has only `cancel_by_session`, which cancels every spawn in the conversation. Freeing one slot needs one-instance granularity.

**Interfaces:**
- Consumes: the registry from Task 1.
- Produces:
  - `async InstanceRegistry.upsert_spawn(session_key, agent, handle, status, agent_id=None) -> None` — a `kind: "cli"` row carrying `status`. When `agent_id` is None it must not erase an `agentId` a previous `commit` already stored; a create that later commits must end up with both its status and its session id.
  - `SubagentManager.cancel_by_instance(session_key, agent, handle) -> bool` — cancels that spawn's task and returns whether one was live.
  - `SubagentManager.live_handles(session_key) -> set[tuple[str, str]]` — the `(agent, handle)` pairs currently in flight, so a reader can tell a genuinely running row from one orphaned by a restart.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`. Reuse the file's existing `_mgr` helper for constructing a manager.

```python
async def test_registry_upsert_spawn_preserves_a_committed_agent_id(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_spawn("web:s1", "claude_code", "h", "running")
    await reg.commit("web:s1", "claude_code", "h", "sess-a")
    await reg.upsert_spawn("web:s1", "claude_code", "h", "completed")
    row = reg.list_instances("web:s1")[0]
    # Status and session id must coexist: the terminal status write must not
    # clobber the id the create committed.
    assert row["status"] == "completed"
    assert row["agentId"] == "sess-a"
    assert await reg.lookup("web:s1", "claude_code", "h") == "sess-a"


async def test_manager_records_and_cancels_one_instance(tmp_path: Path) -> None:
    started = asyncio.Event()

    class _Hang:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            started.set()
            await asyncio.sleep(3600)
            return "never"

    mgr = _mgr(tmp_path, [])
    mgr._backends["hang"] = _Hang()
    mgr.set_submit(lambda req: None)
    await mgr.spawn("t", session_key="web:s1", agent="hang", instance="h1")
    await asyncio.wait_for(started.wait(), timeout=5)

    assert ("hang", "h1") in mgr.live_handles("web:s1")
    assert await mgr.cancel_by_instance("web:s1", "hang", "h1") is True
    # A second cancel finds nothing live.
    assert await mgr.cancel_by_instance("web:s1", "hang", "h1") is False
    assert mgr.live_handles("web:s1") == set()


async def test_manager_cancel_releases_the_concurrency_slot(tmp_path: Path) -> None:
    # The point of cancelling rather than marking: the `async with self._gate`
    # must unwind so the slot is reusable.
    class _Hang:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            await asyncio.sleep(3600)
            return "never"

    mgr = _mgr(tmp_path, [])
    mgr._backends["hang"] = _Hang()
    mgr.set_submit(lambda req: None)
    free_before = mgr._gate._value
    await mgr.spawn("t", session_key="web:s1", agent="hang", instance="h1")
    for _ in range(50):
        if mgr._gate._value < free_before:
            break
        await asyncio.sleep(0.02)
    assert mgr._gate._value == free_before - 1, "the spawn should hold one slot"

    assert await mgr.cancel_by_instance("web:s1", "hang", "h1") is True
    for _ in range(50):
        if mgr._gate._value == free_before:
            break
        await asyncio.sleep(0.02)
    assert mgr._gate._value == free_before, "cancelling must return the slot"
```

Add `import asyncio` to the test module's imports if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "upsert_spawn or cancels_one_instance or releases_the_concurrency_slot" -v`
Expected: FAIL — no `upsert_spawn`, no `cancel_by_instance`, no `live_handles`.

- [ ] **Step 3: Implement**

`instances.py` — `upsert_spawn` mirrors `upsert_dag_node`'s shape but writes `kind: "cli"` and carries `agentId` forward from any existing record when the argument is None:

```python
                "agentId": agent_id or existing.get("agentId"),
```

`manager.py`:

- Keep a second index alongside `_running_tasks`: `self._instance_tasks: dict[tuple[str, str, str], str]` mapping `(session_key, agent, handle)` to `task_id`. Populate it in `spawn` and clear it in the existing `_cleanup` done-callback, next to where `_session_tasks` is pruned — that callback already runs on cancellation, so it is the right place and needs no new teardown path.
- The handle is `instance or task_id`, matching `CliAgentBackend`'s own derivation. Compute it once in `spawn` and carry it in `origin` so the two cannot drift.
- In `_run_subagent_inner`, write `upsert_spawn(..., "running")` before calling the backend and the terminal status after, and in the exception path write `"failed"`. Only when `session_key` and `agent` are both set — a default raven-loop subagent has no third-party agent name and should not appear as an instance row.
- On `asyncio.CancelledError`, write `"cancelled"` and re-raise. Do not swallow it.
- `cancel_by_instance` looks the task up in the new index, cancels it, awaits it with `return_exceptions` semantics so the process is reaped, and returns whether a live task was found.
- `live_handles(session_key)` returns the `(agent, handle)` pairs whose task is present and not done.

Extend the `status` vocabulary note in the registry docstring: a `cli` row's status is one of `running` | `completed` | `failed` | `cancelled`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_subagent_third_party.py -v`
Expected: all pass. The concurrency-slot test reads `_gate._value`, a private asyncio attribute — if it proves brittle on this Python, assert observable behaviour instead: hold all four slots, cancel one, and confirm a fifth spawn then starts.

- [ ] **Step 5: Lint** both ruff gates on the touched files.

---

## Task 4: Cancel RPCs and service routes

**Files:**
- Modify: `raven/web_rpc/methods_config.py`, `ui-webui/service/raven_config_routes.py`
- Test: `tests/test_web_rpc_config.py`

**Interfaces:**
- Consumes: `request_cancel` / `active_run_ids` (Task 2); `cancel_by_instance` / `live_handles` (Task 3).
- Produces:
  - RPC `raven.subagents.dag.cancel`, param `run_id`, result `{"cancelled": bool}`.
  - RPC `raven.subagents.instances.cancel`, params `session_key`, `agent`, `handle`, result `{"cancelled": bool}`.
  - `POST /raven/subagents/dag/{run_id}/cancel` and `POST /raven/subagents/instances/cancel` on the service.
  - The existing `raven.subagents.instances` list handler gains **read-time reconciliation** for both kinds, so a row can never be reported as live when it is not:
    - a `cli` row claiming `running` whose `(agent, handle)` the manager does not report as live becomes `status: "interrupted"`;
    - a `dag-node` row claiming `pending` or `running` whose `runId` is not in the DAG tool's `active_run_ids()` becomes `status: "interrupted"`.

    The disk record is left alone in both cases. A gateway restart orphans `running` rows, and reporting them as live would offer a stop button that can never work. The DAG half also closes a residual from Task 2: a node's terminal registry write is deliberately best-effort (it must never hang or fail a node), so under a registry that is failing to accept writes a row can be left at a stale `running`. Reconciling against the live run set means the UI still shows the truth.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_rpc_config.py`, following the file's existing `_FakeAgent` / `_dispatch` pattern:

```python
async def test_dag_cancel_routes_to_the_tool() -> None:
    class _FakeDagTool:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def request_cancel(self, run_id: str) -> bool:
            self.asked.append(run_id)
            return run_id == "run-live"

    class _AgentWithDag:
        def __init__(self, tool: object) -> None:
            self.tools = {"run_subagent_dag": tool}

    tool = _FakeDagTool()
    d = Dispatcher()
    register_config_methods(d, agent=_AgentWithDag(tool))

    resp = await _dispatch(d, "raven.subagents.dag.cancel", {"run_id": "run-live"})
    assert resp["result"]["cancelled"] is True
    resp = await _dispatch(d, "raven.subagents.dag.cancel", {"run_id": "run-gone"})
    assert resp["result"]["cancelled"] is False
    assert tool.asked == ["run-live", "run-gone"]


async def test_dag_cancel_without_a_dag_tool_is_false() -> None:
    d = Dispatcher()
    register_config_methods(d, agent=None)
    resp = await _dispatch(d, "raven.subagents.dag.cancel", {"run_id": "x"})
    assert resp["result"]["cancelled"] is False
```

Before writing the handler, read how `register_config_methods` already reaches the live agent (it takes `agent=` and calls `apply_third_party_subagents` on it) and how `AgentLoop` stores tools, so the lookup matches reality rather than the fake. Adjust the fake to the real shape if they differ — the test must exercise the real accessor.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_web_rpc_config.py -k dag_cancel -v`
Expected: FAIL — method not found.

- [ ] **Step 3: Implement**

In `register_config_methods`, add three things:

1. A handler resolving the DAG tool off the agent and calling `request_cancel`, returning `{"cancelled": False}` when there is no agent or no such tool. Register as `raven.subagents.dag.cancel`.
2. A handler resolving the agent's subagent manager and calling `cancel_by_instance`, same `{"cancelled": False}` fallback. Register as `raven.subagents.instances.cancel`. Find how the loop exposes its manager before writing the accessor — do not guess an attribute name.
3. Read-time reconciliation in the existing `_instances` handler: ask the manager for `live_handles(session_key)` and rewrite any `cli` row that claims `running` but is not live to `"interrupted"`. Build new dicts rather than mutating the registry's cached records, or the next read will see the rewritten value as if it were on disk.

Extend the function's docstring the way the other methods are documented.

In `ui-webui/service/raven_config_routes.py`, add both routes using the file's `client = await GatewayClient.shared()` pattern:

```python
    @router.post("/subagents/dag/{run_id}/cancel")
    async def cancel_dag_run(run_id: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.dag.cancel", {"run_id": run_id})

    @router.post("/subagents/instances/cancel")
    async def cancel_subagent_instance(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        return await client.call(
            "raven.subagents.instances.cancel",
            {
                "session_key": body.get("session_key", ""),
                "agent": body.get("agent", ""),
                "handle": body.get("handle", ""),
            },
        )
```

Also add a test for the reconciliation: a registry with a `running` cli row and a manager reporting no live handles must return that row as `interrupted`, while a row the manager does report stays `running`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_web_rpc_config.py -v`

- [ ] **Step 5: Lint** both ruff gates on the touched Python.

---

## Task 5: Stop control in the chat instance list

**Files:**
- Modify: `ui-webui/frontend/src/api/ravenConfig.ts`, `src/components/subagent/SubagentInstanceMonitor.tsx`, `src/i18n/locales/{en,zh}.json`

**Interfaces:**
- Consumes: the cancel route (Task 3) and the `kind`/DAG fields on instance rows (Task 1).
- Produces: `RavenSubagentInstance` gains `kind: 'cli' | 'dag-node'` and optional `runId`, `nodeId`, `status`; `ravenConfigApi.cancelDagRun(runId)`.

- [ ] **Step 1: Extend the types and the API**

In `src/api/ravenConfig.ts`:

```ts
export interface RavenSubagentInstance {
	kind: 'cli' | 'dag-node';
	sessionKey: string;
	agent: string;
	handle: string;
	agentId?: string;
	runId?: string;
	nodeId?: string;
	status?: 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
	createdAtMs: number;
	updatedAtMs: number;
}
```

plus, on `RavenSubagentInstance`, `status` widened to include the CLI vocabulary: `'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'cancelled' | 'interrupted'`.

Add two methods, matching how the file's other methods call `client`:

- `cancelDagRun(runId)` -> `POST /raven/subagents/dag/<runId>/cancel`, encoding the id into the path.
- `cancelSubagentInstance(body: { session_key: string; agent: string; handle: string })` -> `POST /raven/subagents/instances/cancel` with that body.

- [ ] **Step 2: Add the i18n keys**

Targeted edits inside `subagent-monitor` in both locales. English:

```json
		"dagRun": "DAG run",
		"stopRun": "Stop run",
		"stopRunConfirm": "Stop this DAG run? Nodes still pending or running will be marked skipped.",
		"stopInstance": "Stop",
		"stopInstanceConfirm": "Stop this sub-agent? Its process is killed and its concurrency slot is freed.",
		"stopRequested": "Stop requested.",
		"stopFailed": "Could not stop it - it may have already finished.",
		"nodeStatus": { "pending": "queued", "running": "running", "completed": "done", "failed": "failed", "skipped": "skipped", "cancelled": "stopped", "interrupted": "interrupted" }
```

Mirror in `zh.json`. Do not touch any other key.

- [ ] **Step 3: Render DAG rows and the stop control**

In `SubagentInstanceMonitor.tsx`:

- Group registry rows whose `kind === 'dag-node'` by `runId`, and render one row per run with its node count and a per-node status breakdown, above or below the CLI instance rows — follow the component's existing row markup rather than inventing a new visual language.
- Put the DAG stop button on the **run** row, not on individual nodes: the chosen semantics is whole-run cancellation.
- Show the DAG stop button only when the run has at least one node in `pending` or `running`. A finished run has nothing to stop.
- Give **CLI instance rows a stop button too**, shown only when `status === 'running'`. That is the row that holds one of the manager's four concurrency slots, so it is the one a user needs to stop to free capacity. It calls `cancelSubagentInstance({ session_key, agent, handle })`.
- A row reported as `interrupted` gets no stop button — the process is already gone and the RPC would return `cancelled: false`. Render the status so the user can tell it apart from `running`.
- On click, confirm via the existing dialog idiom used elsewhere in the app (see how `DeleteDialog` is used) rather than `window.confirm`, then call the matching cancel, then refetch the registry so the rows reflect the new statuses. Toast on both outcomes with the two keys above.
- Keep the existing CLI-instance rows' transcript-derived prompts as they are.

- [ ] **Step 4: Gates**

`npx prettier --write` on the touched files, then `pnpm -C ui-webui/frontend lint` and `pnpm -C ui-webui/frontend build`.

---

## Task 6: Remove every automatic timeout

**Files:**
- Modify: `raven/config/schema.py`, `raven/agent/subagent/presets.py`, `raven/agent/subagent/backends/cli_agent.py`, `raven/agent/subagent/backends/openai_api.py`, `raven/agent/subagent_dag/tool.py`
- Test: `tests/test_subagent_third_party.py`

**This task lands last.** Until Task 5 ships, removing the timeouts leaves no way to end a runaway run.

**Interfaces:**
- Produces: `timeout: int | None = None` on both third-party config models, meaning no limit; `SubAgentDagTool.blocking_interaction = True`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`:

```python
async def test_cli_backend_without_timeout_waits(tmp_path: Path) -> None:
    # No timeout configured: a slow child runs to completion instead of being killed.
    be = CliAgentBackend(name="slow", command="sh -c 'sleep 2; printf done'", timeout=None)
    assert await be.run("x", task_id="t1", workspace=tmp_path, executor=None) == "done"


async def test_cli_backend_with_timeout_still_kills(tmp_path: Path) -> None:
    # An explicit timeout remains an opt-in backstop.
    be = CliAgentBackend(name="slow", command="sleep 5", timeout=1)
    with pytest.raises(RuntimeError, match="timed out"):
        await be.run("x", task_id="t1", workspace=tmp_path, executor=None)


def test_presets_have_no_timeout() -> None:
    for preset in third_party_subagent_presets():
        assert preset.get("timeout") is None, preset["name"]


def test_dag_tool_is_not_timer_killed() -> None:
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    # The registry skips asyncio.wait_for for blocking_interaction tools, which is
    # what lets a long DAG run to completion under manual stop control.
    assert SubAgentDagTool.blocking_interaction is True
```

Read `tests/test_subagent_third_party.py` for how `CliAgentBackend` is constructed in the existing tests and match it; and check the real class name and import path for the DAG tool before writing that last test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "timeout or blocking or no_timeout" -v`

- [ ] **Step 3: Implement**

- `schema.py`: `timeout: int | None = None` on `ThirdPartyCliSubagentConfig` and `ThirdPartyOpenAISubagentConfig`. Document in each docstring that `None` means no automatic limit and the run is ended manually.
- `presets.py`: drop the `timeout` entries from all three presets.
- `cli_agent.py`: `timeout: int | None = None` in `__init__`; in `_exec`, only wrap `proc.communicate(...)` in `asyncio.wait_for` when `self.timeout` is set, and await it directly otherwise. **Also:** launch the child with `start_new_session=True` and, on both `TimeoutError` and `CancelledError`, kill the whole process group (`os.killpg(os.getpgid(proc.pid), signal.SIGKILL)`, tolerating `ProcessLookupError`) rather than only `proc.kill()` — a `codex` child reparents its real worker, so killing the launcher alone leaves it running and the manual stop would not actually stop anything. Re-raise `CancelledError` after reaping.
- `openai_api.py`: `timeout: int | None = None`; pass `aiohttp.ClientTimeout(total=self.timeout)` only when set, otherwise no total.
- `subagent_dag/tool.py`: `blocking_interaction = True`, with a comment saying the stop control replaces the ceiling. Leave `timeout_seconds` alone — it is inert once `blocking_interaction` is set, and removing it would be a second change with no effect.
- **Narrow the resume retry.** `cli_agent.py`'s recreate-after-failed-resume currently fires on any exception. Give the timeout path its own exception type in `_exec` and re-raise it, plus the parsed-transcript `is_error` path, ahead of the broad `except` — so only a hard non-zero exit (a genuinely pruned session) triggers forget-and-recreate. A timeout or a rate-limit error must not discard a valid handle. Add a test that a timeout on resume leaves the record intact.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_subagent_third_party.py tests/test_web_rpc_config.py tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py -v`

- [ ] **Step 5: Lint** both ruff gates on all touched Python.

---

## Self-review notes

**Requirement coverage:**
- No automatic DAG timeout, manual stop instead -> Tasks 2, 4, 5, 6.
- All timeouts removed -> Task 6.
- Cancelling frees the concurrency slot -> Task 3 (real `task.cancel()` unwinds `async with self._gate`, with a test asserting the slot returns) and Task 2 (the DAG's own semaphore, same mechanism).
- Running and finished DAG nodes in the instance list, restorable after reload -> Tasks 1, 2, 5. The registry is on disk, so a reload re-reads it; Task 4's reconciliation keeps a restart from showing dead rows as live.
- Presets checked against the real CLIs -> done in `1ed31ef`; claude and codex verified create-and-resume end to end through the actual backend.

**Consequence the user accepted, now mitigated:** with no automatic timeout, a wedged sub-agent would hold one of four slots indefinitely. Task 3's per-instance stop is what makes that recoverable without a restart, and the opt-in `timeout` field remains for anyone who wants an automatic backstop.

**Ordering within the plan:** Task 6 last, per the sequencing constraint. Tasks 1 and 2 are also prerequisites for 4 and 5, and Task 3 must precede 4 because the reconciliation and the instance-cancel RPC both call methods it adds.

**Open follow-up, not in this plan:** `mirothinker` is stateless in Raven — `OpenAIApiBackend` makes one Chat Completions call per spawn and ignores `session_key` / `instance`, so it can never appear as a resumable instance. If a persistent research thread is wanted, that is a separate feature.
