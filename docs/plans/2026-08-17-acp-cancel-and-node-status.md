# Cancelled node status and ACP turn cancellation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record a DAG node that was stopped while running as `cancelled` rather than `skipped`, and make an ACP-transport node or spawn actually stop when it is cancelled instead of running to completion unwatched.

**Architecture:** Two halves that meet in the same MR. The DAG half turns the in-memory `status` map into an honest one - it gains a real `running` value, which is the discriminator every downstream surface needs to tell "was cut off" from "never ran". The ACP half sends the protocol's own `session/cancel` notification and waits, under a bounded budget, for the turn to settle; when the budget expires the stateful binding is dropped so the half-finished remote session is never prompted again. Cancellation cannot be answered by killing a process here, because one ACP connection is shared by every session of that agent.

**Tech Stack:** Python 3.12 (uv-managed), pytest with `asyncio_mode=auto`, pydantic v2 for the RPC models; React 19 + TypeScript for the frontend files.

**Spec:** `docs/specs/2026-08-17-acp-cancel-and-node-status-design.md`

## Global Constraints

- Branch: cut from `origin/main` at `5432d692`. **Confirm the base with the user before cutting** (AGENTS.md section 2.2). Suggested name: `feat/acp_cancel_and_node_status`.
- **Do not commit unprompted.** AGENTS.md section 3.4 is explicit that a "commit" step written in a plan is *not* pre-authorization. Each task below ends with the exact `git commit` command, but the executor stops after the tests pass, reports, and waits for the user to say commit.
- Run every Python command through uv: `uv run pytest ...`, never bare `pytest` (AGENTS.md section 4).
- Do not create new test files. Every test below lands in an existing file (AGENTS.md sections 5.1, 5.4).
- Code comments only where the logic is non-obvious or a constraint is hidden, and in English (AGENTS.md section 1). Comments are supplied verbatim in the code blocks below; do not add others.
- Commit messages: Conventional Commits, all-English, ASCII-only, header <= 100 chars, `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>` as the last line after a blank line.
- Frontend gate (there is no JS unit-test runner): `pnpm -C frontend lint` must report 0 errors and `pnpm -C frontend build` must succeed, both run from `ui-webui/`. Prettier style there is tabs, single quotes, semicolons.
- Status vocabulary, fixed for the whole plan. `pending`: not dispatched. `running`: dispatched, in flight. `completed` / `failed`: terminal outcomes of a node that ran. `skipped`: never ran (cascade victim, or still queued when the run was stopped). `cancelled`: was running when the run was stopped. `interrupted`: **not** produced by the runner - a reader infers it for a node the registry calls running on a run nothing is executing.

---

### Task 1: The runner records `cancelled`

**Files:**
- Modify: `raven/agent/subagent_dag/runner.py`
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `run_dag` returns a `DagRunResult` whose `files[i]["status"]` may now be `"cancelled"` and whose `summary` dict has a fifth key, `"cancelled": int`. A `dag_node_updated` progress event may carry `"status": "cancelled"`. The session index written by `_record_outcome` may carry `"cancelled"` as a node state.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_dag_runner.py`:

```python
class _BlockingExec(_FakeExec):
    """Holds one node inside its dispatch until the test lets go of it."""

    def __init__(self, block_id: str) -> None:
        super().__init__()
        self.block_id = block_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, task: str, *, task_id: str, **kwargs: Any) -> str:
        if task_id == self.block_id:
            self.entered.set()
            await self.release.wait()
        return await super().run(task, task_id=task_id, **kwargs)


def _two_node_spec() -> dict:
    return {
        "nodes": [
            {"id": "a", "subagent": "x", "prompt_template": "hi"},
            {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
        ]
    }


async def test_a_stop_separates_the_node_that_was_running_from_the_one_that_never_ran() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()
    events: list[dict] = []

    async def publisher(name: str, payload: dict) -> None:
        if name == "dag_node_updated":
            events.append(payload)

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            subagents={"x": agent},
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
            progress_publisher=publisher,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    result = await asyncio.wait_for(task, 5)

    assert {e["node"]: e["status"] for e in result.files} == {"a": "cancelled", "b": "skipped"}
    assert result.summary["cancelled"] == 1
    assert result.summary["skipped"] == 1
    assert result.summary["completed"] == 0
    # The last thing a client heard about `a` was that it started, so the stop
    # has to be published or the node is drawn as running forever.
    assert {"node": "a", "status": "cancelled"}.items() <= events[-1].items() or any(
        e.get("node") == "a" and e.get("status") == "cancelled" for e in events
    )


async def test_a_cancelled_node_keeps_the_start_time_its_dispatch_gave_it() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            subagents={"x": agent},
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    result = await asyncio.wait_for(task, 5)

    entry = next(e for e in result.files if e["node"] == "a")
    assert entry["started_at"] is not None
    assert entry["ended_at"] >= entry["started_at"]


async def test_an_outer_cancellation_records_the_running_node_as_cancelled() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            subagents={"x": agent},
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    entries = json.loads(backend.files["/hist/mas_dag/index.json"].decode())
    assert entries[-1]["status"] == {"a": "cancelled", "b": "skipped"}, entries[-1]

    nodes = await read_session_nodes(backend, "/hist/mas_dag")
    assert nodes.state["a"] == "cancelled"
    assert not nodes.is_readable("a")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k "separates_the_node or keeps_the_start_time or records_the_running_node" -v`
Expected: FAIL. The first two fail on `assert {...} == {"a": "cancelled", "b": "skipped"}` reporting `"a": "skipped"`; the third fails the same way on the index entry.

- [ ] **Step 3: Give the status map a real `running` value**

In `raven/agent/subagent_dag/runner.py`, inside `_run_node` (currently line 511-514), after `node_started_at[node.id] = started_at_ms`:

```python
    async with semaphore:
        started_at_ms = _now_ms()
        did = None
        node_started_at[node.id] = started_at_ms
        # Set inside the gate, so a node still queued for a concurrency slot
        # stays `pending`: this is what tells a stop which nodes actually ran.
        status[node.id] = "running"
        await _emit(
```

- [ ] **Step 4: Split both stop handlers**

Replace the cooperative handler (currently `runner.py:242-248`):

```python
            if cancel is not None and cancel.is_set():
                # A node still `running` here was cut off mid-flight (see
                # _run_ready_groups below) and never reached a terminal status
                # because CancelledError bypasses _run_node's except-Exception.
                # A `pending` one never got dispatched at all, which is what
                # `skipped` means everywhere else.
                for nid, st in status.items():
                    if st == "running":
                        status[nid] = "cancelled"
                    elif st == "pending":
                        status[nid] = "skipped"
```

Replace the outer handler (currently `runner.py:318-327`), keeping its existing comment and appending the second paragraph:

```python
    except asyncio.CancelledError:
        # `/stop` and the shutdown sweep stop a background run by cancelling its
        # task rather than setting `cancel`, so `_finalize` never runs. Without
        # this, the ids claimed at `init` would keep their index entry with no
        # `status`, and `read_session_nodes` would report them `running` forever:
        # neither reusable nor readable, for a run that is definitively over --
        # and the two refusals that produces contradict each other.
        for nid, st in status.items():
            if st == "running":
                status[nid] = "cancelled"
            elif st == "pending":
                status[nid] = "skipped"
        await _record_outcome(store, status, cancelled=True)
        raise
```

`_cascade_failures` is deliberately not touched: `cancelled` is only ever written by these two handlers, and each is immediately followed by the run ending, so no scheduling round can see a cancelled node with pending work still downstream of it.

- [ ] **Step 5: Publish the stop for a cancelled node too**

Rename `published_skips` to `published_terminal` at its declaration (`runner.py:196`):

```python
    published_terminal: set[str] = set()
```

and replace the publish loop (currently `runner.py:249-259`) with:

```python
            for nid, st in status.items():
                if st not in ("skipped", "cancelled") or nid in published_terminal:
                    continue
                published_terminal.add(nid)
                now = _now_ms()
                # A cancelled node already has a real start time from its
                # dispatch; only a skipped one needs both stamps invented.
                node_started_at.setdefault(nid, now)
                node_ended_at[nid] = now
                await _emit(
                    progress_publisher,
                    "dag_node_updated",
                    {"run_id": store.run_id, "node": nid, "status": st},
                )
                await _write_node_status(session_key, store.run_id, nid, by_id[nid].subagent, st)
```

- [ ] **Step 6: Count the new state**

Replace `_tally` (`runner.py:374-382`):

```python
def _tally(status: dict[str, str]) -> dict:
    """Count this run's nodes by terminal state."""
    return {
        "total": len(status),
        "completed": sum(1 for s in status.values() if s == "completed"),
        "failed": sum(1 for s in status.values() if s == "failed"),
        "skipped": sum(1 for s in status.values() if s == "skipped"),
        "cancelled": sum(1 for s in status.values() if s == "cancelled"),
    }
```

- [ ] **Step 7: Update the seven existing exact-summary assertions**

Every one of these compares the whole dict, so the new key breaks them. In `tests/test_subagent_dag_runner.py`, add `"cancelled": 0` to the dict literal on lines 111, 139, 231, 311, 368, 398 and 1617. For example line 111 becomes:

```python
    assert result.summary == {"total": 2, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0}
```

and line 1617 becomes:

```python
    assert second.summary == {"completed": 1, "failed": 0, "skipped": 0, "cancelled": 0, "total": 1}
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py -q`
Expected: PASS, no failures.

- [ ] **Step 9: Commit**

```bash
git add raven/agent/subagent_dag/runner.py tests/test_subagent_dag_runner.py
git commit -m "$(cat <<'EOF'
feat(agent): record a dag node stopped mid-flight as cancelled, not skipped

The status map never held `running`, so a stop could not tell a dispatched
node from one still queued and collapsed both into `skipped`. `_run_node` now
writes `running` inside the concurrency gate, and both stop handlers split on
it. `skipped` keeps its one meaning: the node never ran.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Carry `cancelled` across the wire and the readers

**Files:**
- Modify: `raven/rpc/models.py:391`, `:426-430`, `:460`
- Modify: `raven/agent/subagent_dag/_reader.py:129-140`
- Modify: `raven/agent/subagent_dag/_resume.py:69-74`
- Modify: `raven/agent/subagent_dag/tool.py:462-477`, `:788-793`
- Test: `tests/test_rpc_dag.py`, `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: the `"cancelled"` status and summary key produced by Task 1.
- Produces: `DagNodeStatus` and `DagSnapshotNodeStatus` accept `"cancelled"`; `DagRunSummary` has a `cancelled: int | None = None` field. `read_run` and `read_run_reconciled` both return a `summary` with a `cancelled` count.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rpc_dag.py`:

```python
def test_the_dag_event_models_accept_a_cancelled_node() -> None:
    from raven.rpc.models import DagRunCompletedPayload

    payload = DagRunCompletedPayload(
        run_id="r1",
        dir="/w/.raven_dag/r1",
        summary={"total": 2, "completed": 0, "failed": 0, "skipped": 1, "cancelled": 1},
        files=[
            {"node": "a", "status": "cancelled"},
            {"node": "b", "status": "skipped"},
        ],
    )
    assert payload.summary.cancelled == 1
    assert payload.files[0].status == "cancelled"
```

Append to `tests/test_subagent_dag_runner.py`:

```python
async def test_the_transcript_label_names_the_cancelled_nodes() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec(_two_node_spec()),
            subagents={"x": agent},
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    result = await asyncio.wait_for(task, 5)

    label = SubAgentDagTool._result_label(result)
    assert "1 cancelled" in label
    assert "1 skipped" in label
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rpc_dag.py::test_the_dag_event_models_accept_a_cancelled_node tests/test_subagent_dag_runner.py::test_the_transcript_label_names_the_cancelled_nodes -v`
Expected: FAIL. The first with a pydantic `ValidationError` naming `summary.cancelled` as an extra field and `files.0.status` as an invalid literal; the second with `assert '1 cancelled' in ...`.

- [ ] **Step 3: Widen the models**

In `raven/rpc/models.py`, line 391:

```python
DagNodeStatus = Literal["pending", "running", "completed", "failed", "skipped", "cancelled"]
```

lines 426-430:

```python
class DagRunSummary(_Strict):
    total: int | None = None
    completed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    cancelled: int | None = None
```

and line 460, keeping the comment above it and extending it:

```python
# Wider than DagNodeStatus: a snapshot can report ``interrupted``, which the
# server infers for a node the registry still calls running on a run nothing is
# executing. Nothing on the event wire may claim that. ``cancelled`` is the
# opposite kind of fact -- the runner recorded it -- so both surfaces carry it.
DagSnapshotNodeStatus = Literal[
    "pending", "running", "completed", "failed", "skipped", "cancelled", "interrupted"
]
```

- [ ] **Step 4: Teach both summary readers the new key**

In `raven/agent/subagent_dag/_reader.py`, the `summary` dict at lines 134-139:

```python
        "summary": {
            "total": len(files),
            "completed": statuses.count("completed"),
            "failed": statuses.count("failed"),
            "skipped": statuses.count("skipped"),
            "cancelled": statuses.count("cancelled"),
        },
```

In `raven/agent/subagent_dag/_resume.py`, the `run["summary"]` assignment at lines 69-74:

```python
    run["summary"] = {
        "total": len(statuses),
        "completed": statuses.count("completed"),
        "failed": statuses.count("failed"),
        "skipped": statuses.count("skipped"),
        "cancelled": statuses.count("cancelled"),
    }
```

- [ ] **Step 5: Say it in the two model-facing texts**

In `raven/agent/subagent_dag/tool.py`, in `_result_label`, after the `skipped` clause (currently line 475):

```python
        if cancelled := summary.get("cancelled", 0):
            parts.append(f"{cancelled} cancelled")
        if skipped := summary.get("skipped", 0):
            parts.append(f"{skipped} skipped")
```

and in the announce lines (currently `tool.py:788-793`):

```python
        lines: list[str] = [
            f"DAG run {result.run_id} finished: "
            f"{result.summary.get('completed', 0)} completed, "
            f"{result.summary.get('failed', 0)} failed, "
            f"{result.summary.get('cancelled', 0)} cancelled, "
            f"{result.summary.get('skipped', 0)} skipped (of {result.summary.get('total', 0)}).",
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rpc_dag.py tests/test_subagent_dag_runner.py tests/test_rpc_subagent_calls.py -q`
Expected: PASS, no failures.

- [ ] **Step 7: Commit**

```bash
git add raven/rpc/models.py raven/agent/subagent_dag/_reader.py raven/agent/subagent_dag/_resume.py raven/agent/subagent_dag/tool.py tests/test_rpc_dag.py tests/test_subagent_dag_runner.py
git commit -m "$(cat <<'EOF'
feat(rpc): carry the cancelled node status across the dag wire and readers

The event models are strict, so a run that stopped a node mid-flight could not
be published at all until the literal accepted it. Both summary readers gain
the count as well, so one run does not read three different ways depending on
which entry point asked.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: A cancelled node's id explains itself

**Files:**
- Modify: `raven/agent/subagent_dag/_graph.py:328-335`
- Modify: `raven/agent/subagent_dag/_store.py:138-141`
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: `cancelled` as a value in `SessionNodes.state`, written by Task 1's `_record_outcome`.
- Produces: nothing new; only the refusal text changes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_dag_runner.py`:

```python
async def test_referencing_a_cancelled_node_says_it_was_stopped_not_skipped() -> None:
    backend = _InMemBackend()
    agent = _BlockingExec("a")
    cancel = asyncio.Event()

    task = asyncio.create_task(
        run_dag(
            parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "prompt_template": "hi"}]}),
            subagents={"x": agent},
            backend=backend,
            workdir="/w",
            run_root="/hist/mas_dag",
            subagents_root="/hist",
            cancel=cancel,
        )
    )
    await asyncio.wait_for(agent.entered.wait(), 5)
    cancel.set()
    await asyncio.wait_for(task, 5)

    with pytest.raises(DagValidationError, match="was stopped mid-run"):
        await _run(
            [{"id": "next", "subagent": "x", "prompt_template": "{{ a.output }}"}],
            backend,
            "/hist/mas_dag",
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_dag_runner.py::test_referencing_a_cancelled_node_says_it_was_stopped_not_skipped -v`
Expected: FAIL with `DagValidationError` whose message says "which run '...' skipped, so it wrote no output" - the wrong explanation, not a missing one.

- [ ] **Step 3: Add the branch**

In `raven/agent/subagent_dag/_graph.py`, in `_unreadable`, after the existing `skipped` branch (line 331-332):

```python
    if state == "skipped":
        return f"node '{node_id}' references {what}, which run '{owner}' skipped, so it wrote no output. Re-do it under a new id"
    if state == "cancelled":
        return f"node '{node_id}' references {what}, which was stopped mid-run in run '{owner}', so it wrote no output. Re-do it under a new id"
```

- [ ] **Step 4: Extend the vocabulary docstring**

In `raven/agent/subagent_dag/_store.py`, the `state` attribute description (lines 138-141):

```python
        state (`dict[str, str]`):
            Node id to ``"completed"``, ``"failed"``, ``"skipped"``,
            ``"cancelled"``, or one of :data:`RUNNING` / :data:`UNRECORDED` for
            a run whose per-node outcome the index does not carry.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py -q`
Expected: PASS, no failures.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/subagent_dag/_graph.py raven/agent/subagent_dag/_store.py tests/test_subagent_dag_runner.py
git commit -m "$(cat <<'EOF'
feat(agent): tell a later graph a node was stopped, not skipped

The two states need different next moves and the skipped wording was wrong
about what happened. Same advice, accurate cause.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: A cancelled node keeps its row in the instance list

**Files:**
- Modify: `raven/rpc/methods/subagent.py:194-201`, `:274-278`
- Test: `tests/test_rpc_subagent_calls.py`

**Interfaces:**
- Consumes: `"cancelled"` as a manifest and registry status.
- Produces: an instance row with `"status": "cancelled"` for a stopped node; `skipped` nodes stay filtered out.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rpc_subagent_calls.py`. Follow the module's existing fixture for laying down a run directory - locate the test that writes a `graph.json` plus a `manifest.json` under a run dir and reuse the same construction, then:

```python
async def test_a_cancelled_dag_node_keeps_its_row_and_a_skipped_one_does_not(tmp_path: Path) -> None:
    run = tmp_path / "sessions" / "s1" / ".raven_dag" / "run-1"
    run.mkdir(parents=True)
    (run / "graph.json").write_text(
        json.dumps({"nodes": [{"id": "a", "subagent": "x"}, {"id": "b", "subagent": "x"}]}),
        encoding="utf-8",
    )
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "a": {"status": "cancelled", "subagent": "x", "started_at": 1, "ended_at": 2},
                "b": {"status": "skipped", "subagent": "x"},
            }
        ),
        encoding="utf-8",
    )

    rows = _dag_rows("s1", lambda: None)

    by_node = {row["node"]: row for row in rows}
    assert "b" not in by_node, "a node that never ran still has nothing to open"
    assert by_node["a"]["status"] == "cancelled"
```

Import whatever the module already imports for this - the private row builder and `_session_dir` override - matching the existing DAG-row test in that file rather than inventing a second harness. If the existing test drives the rows through the public `subagents.calls` dispatcher instead of the private function, drive this one the same way and assert on the same payload shape.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_rpc_subagent_calls.py -k cancelled_dag_node -v`
Expected: FAIL with `by_node["a"]["status"] == "run"` - the unknown status falls through `_DAG_WIRE_STATUS.get(status, "run")` to the default.

- [ ] **Step 3: Map the status and leave the filter alone**

In `raven/rpc/methods/subagent.py`, the map at lines 194-201:

```python
_DAG_WIRE_STATUS = {
    "pending": "queued",
    "running": "run",
    "completed": "ok",
    "failed": "error",
    "skipped": "skipped",
    "cancelled": "cancelled",
    "interrupted": "error",
}
```

and the filter's comment at lines 274-278, which now has to say why it is keyed on one status rather than on "did not finish":

```python
            # A skipped node never ran: it has no transcript, no cost and no
            # clock -- a row for it pads the list with entries that open onto
            # nothing. The graph view still shows it, where "skipped because
            # its upstream failed" is legible structure rather than noise. A
            # cancelled node is the opposite case: it ran, so its row opens
            # onto a real transcript and stays.
            if status == "skipped":
                continue
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rpc_subagent_calls.py tests/test_rpc_subagents.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Commit**

```bash
git add raven/rpc/methods/subagent.py tests/test_rpc_subagent_calls.py
git commit -m "$(cat <<'EOF'
fix(rpc): keep a stopped dag node on the instance list

The filter dropped every non-finishing node because skipped was the only way to
express one. A cancelled node has a transcript, a cost and a clock, so the row
opens onto something and belongs on the list.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The graph view draws `cancelled` as itself

**Files:**
- Modify: `ui-webui/frontend/src/components/dag/deriveDag.ts:20-45`, `:65-70`, `:105-116`
- Modify: `ui-webui/frontend/src/components/dag/DagGraph.tsx:35-64`
- Modify: `ui-webui/frontend/src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx:68-73`
- Modify: `ui-webui/frontend/src/i18n/locales/en.json:545-553`, `ui-webui/frontend/src/i18n/locales/zh.json:545-553`
- Test: none - this repo has no JS unit-test runner. The gate is lint plus build.

**Interfaces:**
- Consumes: `"cancelled"` on the wire, from Tasks 1 and 2.
- Produces: `DagNodeStatus` gains `'cancelled'`; `DagSummary` gains `cancelled: number`.

Note what is already in place and must **not** be re-added: `InstanceRowStatus` in `deriveInstances.ts:305-315` already carries `'cancelled'`, and both locales already define `subagent-monitor.nodeStatus.cancelled`. `rowStatusOf` also needs no change - a stateful instance whose last turn was cancelled should stay `cancelled` rather than collapse to `idle`, which is what the existing `last === 'completed' || last === 'skipped'` test already does.

- [ ] **Step 1: Widen the graph status union**

In `ui-webui/frontend/src/components/dag/deriveDag.ts`, the union at lines 20-27:

```typescript
export type DagNodeStatus =
	| 'pending'
	| 'running'
	| 'completed'
	| 'failed'
	| 'skipped'
	| 'cancelled'
	| 'interrupted'
	| 'reused';
```

and the label map at lines 37-45:

```typescript
export const DAG_STATUS_LABEL_KEY: Record<DagNodeStatus, string> = {
	pending: 'dag.status.pending',
	running: 'dag.status.running',
	completed: 'dag.status.completed',
	failed: 'dag.status.failed',
	skipped: 'dag.status.skipped',
	cancelled: 'dag.status.cancelled',
	interrupted: 'dag.status.interrupted',
	reused: 'dag.status.reused',
};
```

- [ ] **Step 2: Stop coercing `cancelled` to `interrupted`**

Replace `toStatus` and its comment (lines 105-116):

```typescript
/** Coerce an arbitrary status string to a known `DagNodeStatus`. `cancelled`
 *  and `interrupted` are both kept: the runner records the first when it stops
 *  a node, while the second is a reader's inference about a node no run is
 *  executing any more. Neither may fall through to `pending`, which would draw
 *  a node that is never going to run again as though it were still queued. */
export function toStatus(s: string | undefined): DagNodeStatus {
	return s === 'running' ||
		s === 'completed' ||
		s === 'failed' ||
		s === 'skipped' ||
		s === 'cancelled' ||
		s === 'interrupted'
		? s
		: 'pending';
}
```

- [ ] **Step 3: Widen the summary interface**

In the same file, lines 65-70:

```typescript
export interface DagSummary {
	total: number;
	completed: number;
	failed: number;
	skipped: number;
	cancelled: number;
}
```

- [ ] **Step 4: Give it a node style**

In `ui-webui/frontend/src/components/dag/DagGraph.tsx`, inside `STATUS_STYLE`, between `skipped` and `interrupted` (after line 55):

```typescript
	cancelled: {
		box: 'border-destructive/70 text-destructive/90',
		icon: <MinusCircle className="size-3 shrink-0" />,
	},
```

Solid border rather than the dashed one `interrupted` uses: a stopped node is a fact raven recorded, not a gap it inferred.

- [ ] **Step 5: Add the summary chip**

In `ui-webui/frontend/src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx`, the `items` array (lines 68-73):

```typescript
	const items: Array<[string, string, number]> = [
		['total', 'dag.summary.total', summary.total],
		['completed', DAG_STATUS_LABEL_KEY.completed, summary.completed],
		['failed', DAG_STATUS_LABEL_KEY.failed, summary.failed],
		['cancelled', DAG_STATUS_LABEL_KEY.cancelled, summary.cancelled],
		['skipped', DAG_STATUS_LABEL_KEY.skipped, summary.skipped],
	];
```

- [ ] **Step 6: Translate it**

In `ui-webui/frontend/src/i18n/locales/en.json`, inside `dag.status`, after `"skipped"`:

```json
			"cancelled": "cancelled",
```

In `ui-webui/frontend/src/i18n/locales/zh.json`, the same position:

```json
			"cancelled": "已停止",
```

`已停止` rather than `已取消`, matching `subagent-monitor.nodeStatus.cancelled` which is already `已停止` in that file.

- [ ] **Step 7: Run the frontend gate**

Run, from `ui-webui/`:

```bash
pnpm -C frontend lint
pnpm -C frontend build
```

Expected: lint reports 0 errors; build succeeds. A missing `STATUS_STYLE` or `DAG_STATUS_LABEL_KEY` entry is a TypeScript error on the `Record<DagNodeStatus, ...>` type, so the build is the real check that Steps 1, 4 and 5 agree.

- [ ] **Step 8: Commit**

```bash
git add ui-webui/frontend/src/components/dag/deriveDag.ts ui-webui/frontend/src/components/dag/DagGraph.tsx ui-webui/frontend/src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json
git commit -m "$(cat <<'EOF'
feat(ui-webui): draw a stopped dag node as cancelled rather than interrupted

The client coerced the two together while nothing could send cancelled. They
now mean different things: one is recorded, the other inferred.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: The ACP client cancels the turn it is abandoning

**Files:**
- Modify: `raven/agent/acp/client.py` (module constants, `AcpClient.__init__`, `request`, new `_cancel_turn` and `take_unsettled_cancel`)
- Modify: `raven/agent/acp/pool.py:266-276`
- Modify: `tests/acp_stub_server.py`
- Test: `tests/test_subagent_acp.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `raven.agent.acp.client.begin_drain() -> None`, `end_drain() -> None`, `is_draining() -> bool`
  - `AcpClient.request(method, params=None, *, timeout=None, cancel_session: str | None = None)`
  - `AcpClient.take_unsettled_cancel(session_id: str) -> bool` - true once if that session's cancel went unanswered within the budget, then false
  - Stub modes `cancel_aware` and `cancel_deaf` selected by `ACP_STUB_MODE`

- [ ] **Step 1: Add the two stub modes**

In `tests/acp_stub_server.py`, extend the module docstring's mode list after the `cancelled` entry:

```
- ``cancel_aware``  - holds the prompt open and answers it with
                      ``stopReason: "cancelled"`` only after a ``session/cancel``
                      notification arrives. Proves raven both sends the
                      notification and waits for the turn to settle.
- ``cancel_deaf``   - holds the prompt open and ignores ``session/cancel``
                      entirely, so the settle budget expires. Proves the timeout
                      path, which unbinds the session rather than reusing it.
```

Add a held-prompt list beside `_AWAITING_PERMISSION` (after line 96):

```python
# Prompts held open until the client cancels them (cancel_aware / cancel_deaf).
_AWAITING_CANCEL: list = []
```

In `handle_prompt`, before the `if MODE == "cancelled":` branch:

```python
    if MODE in ("cancel_aware", "cancel_deaf"):
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "working"}})
        _AWAITING_CANCEL.append((request_id, session_id))
        return
```

Add the notification handler after `handle_prompt`:

```python
def handle_cancel(params) -> None:
    """Settle a held prompt on ``session/cancel``, unless this mode ignores it."""
    if MODE != "cancel_aware":
        return
    session_id = params.get("sessionId")
    for held in list(_AWAITING_CANCEL):
        if held[1] != session_id:
            continue
        _AWAITING_CANCEL.remove(held)
        ok(held[0], {"stopReason": "cancelled"})
```

and route it in `main`'s dispatch, immediately after the `initialize` branch:

```python
        if method == "session/cancel":
            handle_cancel(params)
            continue
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_subagent_acp.py`:

```python
async def test_a_cancelled_turn_is_cancelled_on_the_agent_too() -> None:
    from raven.agent.acp.pool import get_pool

    connection = await get_pool().acquire(
        name="stub",
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": "cancel_aware"},
        ready_timeout_s=15.0,
    )
    client = connection.client
    session = (await client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=15.0))["sessionId"]

    task = asyncio.create_task(
        client.request(
            "session/prompt",
            {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]},
            cancel_session=session,
        )
    )
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The stub only answers a held prompt when it is told to stop, so a settled
    # turn is the proof the notification went out and was waited for.
    assert client.take_unsettled_cancel(session) is False


async def test_an_agent_that_ignores_the_cancel_marks_the_session_unsettled() -> None:
    from raven.agent.acp import client as client_mod
    from raven.agent.acp.pool import get_pool

    monkeyed = client_mod._CANCEL_SETTLE_S
    client_mod._CANCEL_SETTLE_S = 0.3
    try:
        connection = await get_pool().acquire(
            name="stub",
            command=f"{sys.executable} {_STUB}",
            env={"ACP_STUB_MODE": "cancel_deaf"},
            ready_timeout_s=15.0,
        )
        client = connection.client
        session = (await client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=15.0))["sessionId"]

        task = asyncio.create_task(
            client.request(
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]},
                cancel_session=session,
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.take_unsettled_cancel(session) is True
        # Consumed: a second read must not unbind a second time.
        assert client.take_unsettled_cancel(session) is False
    finally:
        client_mod._CANCEL_SETTLE_S = monkeyed


async def test_draining_notifies_without_waiting_for_the_turn_to_settle() -> None:
    from raven.agent.acp import client as client_mod
    from raven.agent.acp.pool import get_pool

    connection = await get_pool().acquire(
        name="stub",
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": "cancel_deaf"},
        ready_timeout_s=15.0,
    )
    client = connection.client
    session = (await client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=15.0))["sessionId"]

    task = asyncio.create_task(
        client.request(
            "session/prompt",
            {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]},
            cancel_session=session,
        )
    )
    await asyncio.sleep(0.5)
    client_mod.begin_drain()
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started

    # The default budget is seconds; draining must not pay any of it.
    assert elapsed < 1.0
    assert client.take_unsettled_cancel(session) is False
    assert client_mod.is_draining() is True


async def test_closing_the_pool_leaves_drain_mode() -> None:
    from raven.agent.acp import client as client_mod
    from raven.agent.acp.pool import close_pool

    client_mod.begin_drain()
    await close_pool()
    assert client_mod.is_draining() is False
```

Add `import time` to that file's imports if it is not already present.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_acp.py -k "cancelled_on_the_agent or ignores_the_cancel or draining_notifies or leaves_drain_mode" -v`
Expected: FAIL with `TypeError: request() got an unexpected keyword argument 'cancel_session'` on the first three, and `AttributeError: module ... has no attribute 'begin_drain'` on the fourth.

- [ ] **Step 4: Add the budget and the drain flag**

In `raven/agent/acp/client.py`, beside the existing module-level constants:

```python
_CANCEL_SETTLE_S = 5.0
"""How long a cancelled turn is given to settle with ``stopReason: cancelled``.

A ceiling, not an expectation. The agent's obligation on ``session/cancel`` is
to stop model requests and abort tool calls "as soon as possible" -- the same
class of work raven's own process-group kill finishes in milliseconds.
Exceeding it is a handled state (the caller unbinds the session instead of
prompting it again), which is what lets the bound stay short.
"""

_DRAINING = False


def begin_drain() -> None:
    """Stop waiting for cancelled turns to settle: the process is going away.

    The pool teardown that follows kills every server, so the wait buys nothing
    there. The notification is still sent, so an adapter that persists session
    state can record the turn as cancelled rather than have it truncated.

    Process-global because asyncio delivers cancellation as a bare
    ``CancelledError`` into the target task: the canceller cannot hand an
    argument or a contextvar to the code that handles it.
    """
    global _DRAINING
    _DRAINING = True


def end_drain() -> None:
    """Leave drain mode. Called by ``close_pool``, which ends the teardown."""
    global _DRAINING
    _DRAINING = False


def is_draining() -> bool:
    """Whether cancelled turns are currently abandoned rather than awaited."""
    return _DRAINING
```

- [ ] **Step 5: Track unsettled cancels**

In `AcpClient.__init__`, beside `self._pending`:

```python
        self._unsettled_cancels: set[str] = set()
```

and add the reader next to the other diagnostics accessors:

```python
    def take_unsettled_cancel(self, session_id: str) -> bool:
        """Whether this session's cancel went unanswered. Consumes the flag.

        Consumed rather than sticky: the caller acts on it by dropping the
        session binding, and a second reader acting on the same fact would
        unbind a session that has already been replaced.
        """
        if session_id in self._unsettled_cancels:
            self._unsettled_cancels.remove(session_id)
            return True
        return False
```

- [ ] **Step 6: Cancel the turn on the way out**

In `AcpClient.request`, add the parameter and the handler. The full method becomes:

```python
    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        cancel_session: str | None = None,
    ) -> Any:
        """Send a request and await its result.

        Raises :class:`AcpRemoteError` if the agent answers with an error,
        :class:`AcpTimeoutError` on budget expiry, :class:`AcpConnectionError` if
        the connection is gone.

        ``cancel_session`` names the session to stop if *this* request is
        cancelled. Without it a cancelled prompt is only abandoned locally and
        the agent runs the turn to completion, answering an id nobody is
        waiting on.
        """
        if not self.alive:
            raise AcpConnectionError(f"acp agent {self.name!r}: connection is not open")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(protocol.request(request_id, method, params))
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise AcpTimeoutError(f"acp agent {self.name!r}: {method} timed out after {timeout}s") from None
        except asyncio.CancelledError:
            if cancel_session is not None:
                await self._cancel_turn(cancel_session, future)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _cancel_turn(self, session_id: str, future: asyncio.Future) -> None:
        """Tell the agent to stop this turn, and give it a bounded chance to.

        Killing the process is not available here the way it is for the cli
        transport: one connection carries every session of this agent, so a kill
        would abort unrelated in-flight work.

        Awaiting inside a cancel handler is sound because every canceller in
        this codebase cancels once and then gathers -- the same property
        ``CliAgentBackend._kill_process_group`` relies on to await the child.
        """
        try:
            await self.notify("session/cancel", {"sessionId": session_id})
        except Exception:  # noqa: BLE001 - a connection already gone has nothing to settle
            return
        if is_draining():
            return
        done, _ = await asyncio.wait({future}, timeout=_CANCEL_SETTLE_S)
        if done:
            return
        self._unsettled_cancels.add(session_id)
        logger.warning(
            "acp agent {!r}: session {!r} did not settle within {}s of session/cancel",
            self.name,
            session_id,
            _CANCEL_SETTLE_S,
        )
```

- [ ] **Step 7: Clear the flag when the teardown ends**

In `raven/agent/acp/pool.py`, import the helper alongside the existing client import:

```python
from raven.agent.acp.client import AcpClient, end_drain
```

and call it first in `close_pool`:

```python
async def close_pool() -> None:
    """Close every pooled connection and forget the pool.

    Separate from ``close_all`` so a test can return the process to a clean
    state, rather than leaving a pool whose connections are all dead. Leaving
    drain mode is part of that clean state: the flag is process-global, so a
    test that set it would otherwise change how the next one cancels.
    """
    global _POOL
    end_drain()
    if _POOL is not None:
        await _POOL.close_all()
        _POOL = None
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_acp.py -q`
Expected: PASS, no failures.

- [ ] **Step 9: Commit**

```bash
git add raven/agent/acp/client.py raven/agent/acp/pool.py tests/acp_stub_server.py tests/test_subagent_acp.py
git commit -m "$(cat <<'EOF'
feat(agent): stop an acp turn on the agent, not only locally

A cancelled request dropped its pending future and told the agent nothing, so
the turn ran to completion and answered an id nobody was waiting on. The client
now sends the protocol's session/cancel and waits, under a bounded budget, for
the turn to settle. Killing the process is not an option: one connection
carries every session of that agent.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The ACP backend quarantines a session that would not stop

**Files:**
- Modify: `raven/agent/subagent/backends/acp_agent.py:22-27` (imports), `:370-384`
- Test: `tests/test_subagent_acp.py`

**Interfaces:**
- Consumes: `cancel_session=` and `take_unsettled_cancel` from Task 6.
- Produces: nothing new; after a settle timeout the stateful binding for that handle is gone, so the next dispatch opens a fresh session.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_acp.py`:

```python
async def test_a_turn_that_would_not_stop_drops_its_session_binding(tmp_path: Path) -> None:
    from raven.agent.acp import client as client_mod
    from raven.agent.subagent.backends import build_third_party_backend

    registry = InstanceRegistry(path=tmp_path / "inst.json")
    backend = build_third_party_backend(
        stub_config("stub", mode="cancel_deaf", stateful=True), registry=registry
    )
    monkeyed = client_mod._CANCEL_SETTLE_S
    client_mod._CANCEL_SETTLE_S = 0.3
    try:
        task = asyncio.create_task(
            backend.run(
                "hi",
                task_id="t1",
                workspace=tmp_path,
                executor=None,
                session_key="web:s1",
                instance="h1",
            )
        )
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        client_mod._CANCEL_SETTLE_S = monkeyed

    assert await registry.lookup("web:s1", "stub", "h1", kind="acp") is None
```

Check `build_third_party_backend`'s and `stub_config`'s keyword names against the module's existing stateful-ACP test before running this; reuse whatever that test passes for `stateful` and for the registry override rather than a second spelling.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_acp.py -k would_not_stop -v`
Expected: FAIL on the final assertion - the binding is still there, because nothing unbinds it.

- [ ] **Step 3: Import asyncio**

`raven/agent/subagent/backends/acp_agent.py` does not import it today. Add it in alphabetical order at the top of the stdlib block (before `json`):

```python
import asyncio
import json
import time
```

- [ ] **Step 4: Pass the session and act on the timeout**

Replace the prompt block (currently `acp_agent.py:375-384`):

```python
            async with connection.session_lock(session_id):
                connection.router.attach(session_id, collector)
                try:
                    result = await client.request(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": [{"type": "text", "text": task}]},
                        timeout=self.timeout,
                        cancel_session=session_id,
                    )
                except asyncio.CancelledError:
                    # The turn outlived its cancel budget, so it is still running
                    # on the agent while this lock is about to be released --
                    # prompting the same session again would collide with it.
                    # Dropping the binding is the same recovery `_open_session`
                    # makes when a resume fails: the instance keeps its handle
                    # and the next dispatch opens a fresh session under it.
                    if self.is_stateful and client.take_unsettled_cancel(session_id):
                        await self._registry.unbind(skey, self.name, handle)
                    raise
                finally:
                    connection.router.detach(session_id, collector)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_acp.py -q`
Expected: PASS, no failures.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/subagent/backends/acp_agent.py tests/test_subagent_acp.py
git commit -m "$(cat <<'EOF'
fix(agent): drop an acp session whose turn outlived its cancel budget

The session lock is released the moment the caller leaves, so a turn still
running on the agent would be collided with by the next prompt on that handle.
Unbinding sends the next dispatch to a fresh session instead.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Every host drains, cancels, then closes

**Files:**
- Modify: `raven/cli/gateway_commands.py:695-720`
- Modify: `raven/rpc/bootstrap.py:167-199`
- Modify: `raven/cli/tui_commands.py:816-825`
- Test: `tests/test_cli_gateway_commands.py`

**Interfaces:**
- Consumes: `begin_drain` from Task 6, `close_pool` from `raven.agent.acp.pool`.
- Produces: nothing new. The observable change is ordering, plus a pool close the gateway never had.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli_gateway_commands.py` a test that asserts the shutdown sequence by recording call order. Follow the module's existing pattern for driving the gateway's teardown - if it exercises the shutdown block through a helper, use that helper; otherwise assert on the source order, which is the honest test for a sequence this file expresses only as statements:

```python
def test_the_gateway_shutdown_cancels_subagents_before_it_closes_the_transports() -> None:
    """An ACP connection closed first fails every pending turn with a connection
    error, which records as a failure rather than as the stop it is. And the
    gateway never closed the ACP pool at all, so its servers outlived it."""
    src = Path("raven/cli/gateway_commands.py").read_text(encoding="utf-8")
    drain = src.index("begin_drain()")
    cancel = src.index("await agent.subagents.cancel_all()")
    web = src.index("await web_teardown()")
    pool = src.index("await close_pool()")

    assert drain < cancel < web < pool
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli_gateway_commands.py -k shutdown_cancels_subagents -v`
Expected: FAIL with `ValueError: substring not found` on `begin_drain()`.

- [ ] **Step 3: Fix the gateway sequence**

In `raven/cli/gateway_commands.py`, add the imports at the top of the shutdown block's enclosing scope (place them with the other local imports in that function):

```python
                from raven.agent.acp.client import begin_drain
                from raven.agent.acp.pool import close_pool
```

Then reorder the block. Insert before the `web_teardown` call (currently line 703):

```python
                # Before anything tears a transport down: an ACP connection
                # closed first fails every pending turn with a connection error,
                # which records as a failure rather than as the stop it is. A
                # CLI subagent's process group is detached from the gateway's
                # own (start_new_session=True), so nothing else reaches it
                # either. Draining first, because the pool close below kills
                # every ACP server anyway and no cancelled turn is worth waiting
                # on when its process is about to go.
                begin_drain()
                await agent.subagents.cancel_all()
```

Delete the old `cancel_all()` call and its comment (currently lines 707-712), and add the pool close immediately after `gw_teardown()`:

```python
                if gw_teardown is not None:
                    await gw_teardown()
                # ACP agents are launched with start_new_session, so they do not
                # get this process's signals and outlive it unless the pool is
                # closed.
                await close_pool()
                await agent.close_mcp()
```

- [ ] **Step 4: Fix the serve stack's sequence**

In `raven/rpc/bootstrap.py`, inside `teardown`, before the browser close and the existing pool close:

```python
        # Same order every host follows: drain, cancel, then close. Cancelling
        # after the pool closed would report each in-flight turn as a connection
        # failure instead of as the stop it is.
        try:
            from raven.agent.acp.client import begin_drain

            begin_drain()
            if agent_loop is not None:
                await agent_loop.subagents.cancel_all()
        except Exception:
            logger.exception("serve: cancelling in-flight sub-agents failed; continuing shutdown")
```

- [ ] **Step 5: Fix the TUI's sequence**

In `raven/cli/tui_commands.py`, immediately before the existing `close_pool` block (currently line 816):

```python
        try:
            from raven.agent.acp.client import begin_drain

            begin_drain()
            if agent_loop is not None:
                await agent_loop.subagents.cancel_all()
        except Exception:
            from loguru import logger as _logger

            _logger.exception("tui: cancelling in-flight sub-agents failed; continuing shutdown")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_gateway_commands.py tests/test_cli_serve_commands.py tests/test_subagent_acp.py -q`
Expected: PASS, no failures.

- [ ] **Step 7: Commit**

```bash
git add raven/cli/gateway_commands.py raven/rpc/bootstrap.py raven/cli/tui_commands.py tests/test_cli_gateway_commands.py
git commit -m "$(cat <<'EOF'
fix(cli): close the acp pool on the gateway, and cancel before tearing down

The gateway never closed the pool at all, so every exit orphaned its adapter
processes -- they are launched with start_new_session and get no signals. The
hosts that did close it cancelled afterwards, which turned each in-flight turn
into a connection failure instead of a stop. All three now drain, cancel, then
close.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Replace the guessed budget with a measured one

**Files:**
- Modify: `raven/agent/acp/client.py` (the `_CANCEL_SETTLE_S` docstring, and its value if the measurement calls for it)

**Interfaces:**
- Consumes: everything above.
- Produces: a constant whose docstring cites a measurement rather than an argument.

- [ ] **Step 1: Measure both real adapters**

Write a throwaway script under the scratchpad directory (not the repo) that, for each of `npx -y @agentclientprotocol/claude-agent-acp@0.66.0` and `npx -y @agentclientprotocol/codex-acp@1.1.14`:

1. acquires a pooled connection,
2. opens a session and sends a prompt that will take a while ("list every file under /usr/include and summarise them"),
3. waits 5 seconds so the agent is genuinely mid-turn, ideally inside a tool call,
4. cancels the request task with `cancel_session` set,
5. records the wall-clock time between the `session/cancel` write and the prompt future resolving, and the `stopReason` that came back.

Repeat three times per adapter. Record the maximum.

- [ ] **Step 2: Record what was measured**

Update the docstring in `raven/agent/acp/client.py` with the numbers, keeping the reasoning and replacing the last sentence:

```python
_CANCEL_SETTLE_S = 5.0
"""How long a cancelled turn is given to settle with ``stopReason: cancelled``.

A ceiling, not an expectation. The agent's obligation on ``session/cancel`` is
to stop model requests and abort tool calls "as soon as possible" -- the same
class of work raven's own process-group kill finishes in milliseconds.
Exceeding it is a handled state (the caller unbinds the session instead of
prompting it again), which is what lets the bound stay short.

Measured mid-tool-call, worst of three runs each: claude-agent-acp@0.66.0
settled in <MEASURED>s, codex-acp@1.1.14 in <MEASURED>s.
"""
```

Replace both `<MEASURED>` placeholders with the recorded numbers. If either adapter exceeded 5.0 seconds, raise the constant to twice that adapter's worst case and say so in the docstring; do not lower it below 5.0 even if both are fast, because the measurement is of two adapters and the constant governs all of them.

- [ ] **Step 3: Re-run the ACP suite**

Run: `uv run pytest tests/test_subagent_acp.py -q`
Expected: PASS, no failures.

- [ ] **Step 4: Commit**

```bash
git add raven/agent/acp/client.py
git commit -m "$(cat <<'EOF'
docs(agent): cite the measured settle time behind the acp cancel budget

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification, before the MR

- [ ] `uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py tests/test_subagent_acp.py tests/test_rpc_dag.py tests/test_rpc_subagent_calls.py tests/test_rpc_subagents.py tests/test_cli_gateway_commands.py tests/test_cli_serve_commands.py -q`
- [ ] `uv run pytest -q` (full suite - the status vocabulary reaches further than the files touched here)
- [ ] From `ui-webui/`: `pnpm -C frontend lint` and `pnpm -C frontend build`
- [ ] `make check-large-files`
- [ ] Manual: start the gateway, run a DAG whose node blocks, stop it from the WebUI, and confirm the node reads `cancelled` in the graph, keeps its row in the instance list, and opens onto its partial transcript.
- [ ] Manual: with an ACP sub-agent configured, repeat the above and confirm the adapter process stops working rather than finishing the turn (watch its CPU, or its own log).
- [ ] Manual: stop the gateway with an ACP agent connected and confirm no `npx ...acp` process survives (`pgrep -af acp`).

## Self-review notes

Checked against the spec:

- **Spec coverage.** Every design section maps to a task: discriminator and assignment (Task 1), tally and wire (Tasks 1-2), reuse (Task 3), instance rows (Task 4), frontend (Task 5), client cancel plus drain flag (Task 6), quarantine (Task 7), shutdown order plus the gateway's missing pool close (Task 8), measurement (Task 9). The spec's "cascade left alone" decision is carried as an explicit non-change in Task 1 Step 4.
- **Type consistency.** `cancelled` is spelled the same in the Python literals, the summary key, the TypeScript union and both locale files. `take_unsettled_cancel` and `cancel_session` are named identically in Tasks 6 and 7. `published_terminal` replaces `published_skips` in one task, not across two.
- **Known soft spots**, called out rather than papered over: Task 4 Step 1 and Task 7 Step 1 both tell the executor to match an existing harness in the target test file rather than reproducing one, because the exact fixture shape in `tests/test_rpc_subagent_calls.py` and the stateful-ACP setup in `tests/test_subagent_acp.py` were not read line by line while writing this. Both steps name what to check before running. Task 9 carries deliberate `<MEASURED>` markers that the same task fills in - that is the task's whole deliverable, not an unfinished plan step.
