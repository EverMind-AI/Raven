# DAG replan decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the main agent a third answer to a suspended DAG node -- hand back a new node list, which winds the answered run down and starts a new run from that list.

**Architecture:** Chained runs, not an in-place splice. The decision is validated in the resolve call, the answered run winds down and finalizes (making its completed nodes referenceable by id), and a new run is submitted under a run id minted before the hand-off. The new graph passes the same preflight a submitted graph does.

**Tech Stack:** Python 3.12, pydantic v2, asyncio, pytest (`uv run pytest`); TypeScript + vitest for `ui-tui` and `ui-web`.

**Spec:** `docs/specs/2026-09-04-dag-replan-design.md` -- read it before Task 1. Every "why" below is argued there.

## Global Constraints

- **Base branch is `refactor/raven_v0_2_0`**, not `main`. This work sits on `feat/dag_replan_decision`.
- **Comments:** English only, and only where the logic is non-obvious or a constraint is hidden (`AGENTS.md` 1.1). Do not annotate what the code already says. New modules need a module docstring; no new module is created here.
- **Commit messages:** Conventional Commits, all-English, ASCII-only (no em-dash, curly quotes, ellipsis). Header `<type>(<scope>): <subject>` under 100 chars, lowercase subject, no trailing period. Trailer `Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>`.
- **Do not commit unprompted** (`AGENTS.md` 3.4). Each task's commit step is a *proposal*; get the user's word first. Never `--amend`.
- **Tests go in existing files** (`AGENTS.md` 5.1). No new test file is created by this plan.
- **Dependencies:** `uv` only. This plan adds none.
- **Git in this worktree:** the RTK hook rewrites `git` to `rtk git` and the worktree fence then refuses it. Call git by absolute path: `/usr/bin/git`.
- **Test command:** `uv run pytest <file> -x`. A bare `uv run pytest` silently drops ~4300 ppt tests; use `--all-extras` for a full run.
- **Pre-commit hooks are disabled in this clone** (`core.hooksPath` points nowhere), so `ruff format` does not run at commit. Run `make lint-python` before proposing any commit that touches `.py`, or CI goes red on formatting alone.
- **Domain terms:** the decision is spelled `replan` everywhere -- value, field, event, prose. Never `reorchestrate`, `regraph`, or `re-plan`.

## File Structure

| File | Responsibility in this change |
|---|---|
| `raven/agent/subagent/dag_adjudication.py` | the `REPLAN` value, `ReplanPlan`, `Adjudication.plan`, and the desk's `replanned` event |
| `raven/agent/subagent/dag_runner.py` | interrupting the round, the wind-down status/reason mapping, `DagRunResult.replanned_into` |
| `raven/agent/subagent/dag_tool.py` | the shared preflight, `prepare_replan`, `await_finalized`, `start_replan`, the link record, the `dag_run_replanned` emit |
| `raven/agent/subagent/dag_control_tools.py` | the `resolve_dag_node` surface: the third enum value, `nodes`, and the refusal paths |
| `raven/agent/subagent/dag_store.py` | `record_replan`, which writes the link key into an existing run's `graph.json` |
| `raven/agent/subagent/dag_reader.py` | module docstring: the reserved `replan` key |
| `rpc-schema/openrpc.json`, `raven/rpc/models.py`, `raven/rpc/spine.py`, `raven/acp/updates.py` | the `dag.run_replanned` wire event |
| `ui-tui/src/rpc/generated.ts`, `ui-web/src/rpc/generated.ts` | regenerated, never hand-edited |
| `ui-tui/src/domain/dagRun.ts`, `ui-tui/src/app/chatStream.ts`, `ui-tui/src/components/dagPanel.tsx` | the TUI fold and its line of rendering |
| `ui-web/src/features/dag/*`, `ui-web/src/features/transcript/store.ts` | the same for the web UI |
| `raven/agent/subagent/dag_runner.py` (`_exception_report`), `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md`, `CONTEXT.md` | what the model and the next reader are told |

Task order is dependency order: primitives, then the refactor the tool path needs, then the runner, then the tool, then the wire, then the two front ends, then the prose.

---

### Task 1: The replan primitives

**Files:**
- Modify: `raven/agent/subagent/dag_adjudication.py:57-60` (the decision constants), `:62-68` (`Adjudication`), `:71-118` (`AdjudicationDesk`)
- Test: `tests/test_subagent_dag_adjudication.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `REPLAN: str = "replan"`, and `DECISIONS = (CONTINUE, ABANDON, REPLAN)`
  - `@dataclass(frozen=True) class ReplanPlan` with fields `run_id: str`, `from_node: str`, `reason: str`, `nodes: tuple[DagNodeSpec, ...]`, `backends: dict[str, Any]`, `auto_instances: frozenset[str]`, `notices: tuple[str, ...]`
  - `Adjudication(decision: str, message: str | None = None, plan: ReplanPlan | None = None)`
  - `AdjudicationDesk.replanned: asyncio.Event`
  - `AdjudicationDesk.resolve(node_id, decision, message, plan=None) -> bool` -- sets `replanned` when `decision == REPLAN` and the answer was recorded
  - `AdjudicationDesk.take_plan() -> ReplanPlan | None` -- the plan of whichever node was replanned, consumed

`ReplanPlan` imports `DagNodeSpec` from `raven.agent.subagent.dag_graph`. Check for an import cycle first: `dag_graph` imports from `dag_store`, `prompt_errors`, `prompt_paths`, `prompt_placeholders`, `prompt_render` -- not from `dag_adjudication` -- so the edge is safe. If a cycle appears anyway, type the field `tuple[Any, ...]` and note why.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_dag_adjudication.py`. The file's existing tests cover the outbox; add a desk section.

```python
from raven.agent.subagent.dag_adjudication import (
    ABANDON,
    CONTINUE,
    DECISIONS,
    REPLAN,
    AdjudicationDesk,
    ReplanPlan,
)


def _plan(run_id: str = "run-new", from_node: str = "a") -> ReplanPlan:
    return ReplanPlan(
        run_id=run_id,
        from_node=from_node,
        reason="the plan was wrong",
        nodes=(),
        backends={},
        auto_instances=frozenset(),
        notices=(),
    )


def test_replan_is_a_decision() -> None:
    assert REPLAN == "replan"
    assert DECISIONS == (CONTINUE, ABANDON, REPLAN)


def test_resolving_a_replan_sets_the_replanned_event() -> None:
    desk = AdjudicationDesk()
    desk.open("a")
    assert not desk.replanned.is_set()

    assert desk.resolve("a", REPLAN, "the plan was wrong", plan=_plan()) is True

    assert desk.replanned.is_set()
    assert desk.take_plan() == _plan()


def test_resolving_a_continue_leaves_the_replanned_event_alone() -> None:
    desk = AdjudicationDesk()
    desk.open("a")

    assert desk.resolve("a", CONTINUE, "try again") is True

    assert not desk.replanned.is_set()
    assert desk.take_plan() is None


def test_a_replan_nobody_waits_for_sets_nothing() -> None:
    desk = AdjudicationDesk()

    assert desk.resolve("gone", REPLAN, "too late", plan=_plan()) is False

    assert not desk.replanned.is_set(), "an unrecorded answer must not interrupt the run"
    assert desk.take_plan() is None


def test_take_plan_consumes() -> None:
    desk = AdjudicationDesk()
    desk.open("a")
    desk.resolve("a", REPLAN, "the plan was wrong", plan=_plan())

    assert desk.take_plan() is not None
    assert desk.take_plan() is None


def test_the_replanned_answer_is_still_taken_per_node() -> None:
    desk = AdjudicationDesk()
    desk.open("a")
    desk.resolve("a", REPLAN, "the plan was wrong", plan=_plan())

    answer = desk.take("a")
    assert answer is not None
    assert answer.decision == REPLAN
    assert answer.plan is not None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_adjudication.py -x -k "replan or take_plan"`
Expected: FAIL at import -- `ImportError: cannot import name 'REPLAN'`.

- [ ] **Step 3: Implement**

In `dag_adjudication.py`, replace the constants block:

```python
CONTINUE = "continue"
ABANDON = "abandon"
REPLAN = "replan"
DECISIONS = (CONTINUE, ABANDON, REPLAN)


@dataclass(frozen=True)
class ReplanPlan:
    """A validated replacement graph, on its way from the resolve call to the run.

    Carries its own ``run_id`` because the run winding down names it: a node reason
    saying it was superseded with no destination leaves the reader of that run
    nowhere to go, and the id cannot be minted later than the wind-down that cites
    it. ``backends`` travels here rather than being resolved by the runner because
    the dispatch map is a closure owned by ``SubAgentDagTool._run``.
    """

    run_id: str
    from_node: str
    reason: str
    nodes: tuple[DagNodeSpec, ...]
    backends: dict[str, Any]
    auto_instances: frozenset[str]
    notices: tuple[str, ...]


@dataclass(frozen=True)
class Adjudication:
    """What the main agent decided about one suspended node."""

    decision: str
    message: str | None = None
    plan: ReplanPlan | None = None
```

Add the import at the top: `from raven.agent.subagent.dag_graph import DagNodeSpec`.

In `AdjudicationDesk.__init__`, add:

```python
        self.replanned = asyncio.Event()
        self._plan: ReplanPlan | None = None
```

Replace `resolve`:

```python
    def resolve(self, node_id: str, decision: str, message: str | None, plan: "ReplanPlan | None" = None) -> bool:
        """Record an answer and wake the waiter. False when nobody was waiting.

        The caller reports that False to the model rather than swallowing it: by
        the time an answer arrives the node may have timed out or the run may
        have been cancelled, and a silently discarded decision looks to the model
        exactly like one that was applied.

        A replan also fires ``replanned``, which is what lets the scheduling round
        in flight be interrupted rather than drained -- but only once the answer
        is recorded, so an answer nobody was waiting for cannot tear down a run
        it was never going to reach.
        """
        event = self._waiting.get(node_id)
        if event is None:
            return False
        self._answers[node_id] = Adjudication(decision=decision, message=message, plan=plan)
        if decision == REPLAN:
            self._plan = plan
            self.replanned.set()
        event.set()
        return True

    def take_plan(self) -> "ReplanPlan | None":
        """The replacement graph a replan landed, consumed."""
        plan, self._plan = self._plan, None
        return plan
```

- [ ] **Step 4: Run the whole file**

Run: `uv run pytest tests/test_subagent_dag_adjudication.py -x`
Expected: PASS, including every pre-existing outbox test.

- [ ] **Step 5: Lint, then propose a commit**

Run: `make lint-python`

```bash
/usr/bin/git add raven/agent/subagent/dag_adjudication.py tests/test_subagent_dag_adjudication.py
# propose, do not run unprompted:
# feat(agent): the adjudication desk can carry a replacement graph
```

---

### Task 2: Extract the shared preflight (pure refactor, no behaviour change)

**Files:**
- Modify: `raven/agent/subagent/dag_tool.py:940-999` -- `_execute` starts at `:925`; the block to move runs from `capabilities = self._capability_map()` (`:940`) through the `with_machine_facts` assignment (`:999`) inclusive
- Test: `tests/test_subagent_dag_core.py`, `tests/test_subagent_dag_control_tools.py`, `tests/test_subagent_dag_machines.py`, `tests/test_subagent_dag_mcp_scope.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces on `SubAgentDagTool`:
  - `@dataclass class Preflight` with `spec: SubAgentDagSpec`, `backends: dict[str, Any]`, `notices: list[str]`, `capabilities: dict[str, AgentCapabilities]`
  - The fourth field is load-bearing, not tidiness: `_execute` calls `_mint_missing_instances(spec, capabilities)` **below** the moved block (`dag_tool.py:1026` pre-refactor), so moving `capabilities = self._capability_map()` out leaves that call with an undefined name. Thread the snapshot back out rather than letting `_execute` recompute it: a second `_capability_map()` call would read the agent table *after* `_preflight`'s awaits (the async MCP resolver, the async machines check) instead of before, which is a real behaviour change under a concurrent hot-apply that the original single-snapshot code did not have.
  - `async def _preflight(self, spec: SubAgentDagSpec) -> Preflight` -- raises `DagValidationError` for every refusal it finds. Runs, in order: `validate_capabilities`, per-node agent-table membership and `enabled`, MCP grant resolution with its downgrade notices and `_DispatchBackend` wrapping, then `machines_verdict_async` / `machines_refusal` / `unnamed_machine` / `with_machine_facts`.
  - `def node_schema(self) -> dict[str, Any]` -- returns `self._node_schema()`, so the control tool can reuse it rather than copy it.

This task changes no behaviour. It exists as its own task because the replan path must reach exactly these checks, and a reviewer should be able to confirm the move is faithful before any new caller depends on it. Without the extraction, a replan is a route around the agent table, the capability gates, MCP resolution and the machines check.

- [ ] **Step 1: Pin current behaviour with a characterisation test**

Append to `tests/test_subagent_dag_control_tools.py` -- it already builds a graph tool double; reuse that fixture. If the fixture there is too thin, use the one in `tests/test_subagent_dag_core.py` instead and put the test there.

**`_tool_with_agents` does not exist -- build the tool the way this repo's tests already do.** Use
`SubAgentDagTool(workspace=tmp_path)` and `tool.set_agents([...])` (`dag_tool.py:396`) with a
`ThirdPartyCliSubagentConfig` whose `enabled` is False; `tests/test_subagent_dag_control_tools.py` imports
that config type at line 33 and has a `_Registry` double at line 38 if a lighter fixture is wanted. Follow
whichever the surrounding tests in your chosen file use rather than adding a third pattern.

```python
async def test_preflight_refuses_a_disabled_agent_before_any_dispatch(tmp_path) -> None:
    """The refusal `_execute` produced inline must survive the extraction verbatim."""
    tool = _tool_with_disabled_agent(tmp_path, name="coder")
    spec = parse_dag_spec(
        {
            "task_summary": "one step",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "coder",
                    "node_summary": "do it",
                    "prompt_template": "go",
                }
            ],
        }
    )

    with pytest.raises(DagValidationError) as exc:
        await tool._preflight(spec)

    assert "turned off on this machine" in str(exc.value)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_dag_control_tools.py -x -k preflight`
Expected: FAIL with `AttributeError: 'SubAgentDagTool' object has no attribute '_preflight'`.

- [ ] **Step 3: Move the block**

Cut the code in `_execute` from `capabilities = self._capability_map()` down to and including the `with_machine_facts` assignment, and put it in a new method. `_execute` keeps `parse_dag_spec` and `validate_and_order` (they need `self._reference_roots()` and `await self._session_nodes()`, which the replan path supplies differently) and keeps its own `try/except DagValidationError -> self._validation_error(exc)`.

```python
@dataclass
class Preflight:
    """What the pre-dispatch checks settled, for the two callers that dispatch from it.

    ``capabilities`` rides along because it is one snapshot of a hot-appliable table:
    ``_execute`` needs it again below this phase, and taking it twice would straddle
    this phase's awaits.
    """

    spec: SubAgentDagSpec
    backends: dict[str, Any]
    notices: list[str]
    capabilities: dict[str, AgentCapabilities]
```

```python
    async def _preflight(self, spec: SubAgentDagSpec) -> Preflight:
        """Every pre-dispatch check that needs live registry or machine data.

        Shared by ``_execute`` and the replan path so a replacement graph cannot
        reach dispatch through checks a submitted graph has to pass. Raises
        ``DagValidationError`` for a refusal; a capability or MCP gap comes back
        as a notice instead, on the terms those two already had.
        """
        capabilities = self._capability_map()
        notices = validate_capabilities(spec, capabilities)
        backends: dict[str, Any] = {}
        for node in spec.nodes:
            row = self._registry.get(node.subagent)
            if row is None:
                raise DagValidationError(f"node '{node.id}' names unknown sub-agent '{node.subagent}'")
            if not row.enabled:
                raise DagValidationError(
                    f"node '{node.id}' names agent '{node.subagent}', which is turned off on this "
                    f"machine -- enable it in the agents settings, or point the node at another agent"
                )
            backend = self._resolve_node(node)
            resolver = getattr(backend, "resolve_mcp_grant", None)
            if row.injectable.mcps and resolver is not None:
                async_resolver = getattr(backend, "resolve_mcp_grant_async", None)
                grant = await async_resolver(node.mcps) if async_resolver is not None else resolver(node.mcps)
                if note := grant.note_text():
                    notices.append(f"node '{node.id}': {note}")
                if getattr(backend, "kind", None) != "raven-loop":
                    backend = _DispatchBackend(backend, mcp_grant=grant)
            elif node.mcps is not None:
                backend = _DispatchBackend(backend, drop_mcps=True)
            backends[node.id] = backend
        if verdict := await machines_verdict_async([n.subagent for n in spec.nodes]):
            if verdict.usable == 0:
                raise DagValidationError(machines_refusal(verdict))
            if problem := unnamed_machine(spec.nodes, verdict):
                raise DagValidationError(problem)
            spec = spec.model_copy(update={"nodes": with_machine_facts(spec.nodes, verdict)})
        return Preflight(spec=spec, backends=backends, notices=notices, capabilities=capabilities)
```

Keep every comment that was attached to the moved lines -- they explain why MCP is a notice and not a refusal, why the machines check is last, and why `_resolve_node` is per node. Losing them here is a real loss; they do not belong to `_execute`.

Then in `_execute`, after `validate_and_order`:

```python
            pre = await self._preflight(spec)
            spec, dispatch_backends, notices = pre.spec, pre.backends, pre.notices
            capabilities = pre.capabilities
```

`capabilities` comes back out because `_execute` still needs it further down, at
`_mint_missing_instances(spec, capabilities)`. Rebinding it here keeps that call reading the
same snapshot the checks ran against.

Add the public accessor next to `_node_schema`:

```python
    def node_schema(self) -> dict[str, Any]:
        """This tool's node schema, for a caller that accepts a graph on its behalf.

        Public so ``resolve_dag_node`` advertises the same node shape rather than a
        copy of it: the ``subagent`` enum is built from the hot-appliable agent
        table, and a second copy would drift from ``run_subagent_dag``'s.
        """
        return self._node_schema()
```

- [ ] **Step 4: Run the affected suites**

Run: `uv run pytest tests/test_subagent_dag_core.py tests/test_subagent_dag_control_tools.py tests/test_subagent_dag_machines.py tests/test_subagent_dag_mcp_scope.py -x`
Expected: PASS. Any failure here is the extraction being unfaithful, not a new requirement -- re-read the moved block against the original.

- [ ] **Step 5: Lint, then propose a commit**

Run: `make lint-python`

```bash
/usr/bin/git add raven/agent/subagent/dag_tool.py tests/test_subagent_dag_control_tools.py
# propose:
# refactor(agent): the graph tool's pre-dispatch checks become one callable phase
```

---

### Task 3: The runner winds a replanned run down

**Files:**
- Modify: `raven/agent/subagent/dag_runner.py:141-149` (`DagRunResult`), `:319-475` (the `run_dag` loop), `:476-515` (`_run_ready_groups`)
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: `REPLAN`, `ReplanPlan`, `AdjudicationDesk.replanned`, `AdjudicationDesk.take_plan` (Task 1).
- Produces:
  - `DagRunResult.replanned_into: str | None = None`
  - `_run_ready_groups(coros, cancel, interrupt: asyncio.Event | None = None)` -- races the round against `interrupt` exactly as it already races `cancel`
  - `async def _apply_replan(plan, *, status, errors, published_terminal, node_started_at, node_ended_at, by_id, session_key, run_id, progress_publisher) -> None` -- gives every non-completed node its terminal status and reason, and publishes each transition itself

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_dag_runner.py`. **Import `AdjudicationDesk`, `Verdict`, `REPLAN` and `ReplanPlan` inside each test function**, not at module level -- that is this file's convention (ten-plus inline `from raven.agent.subagent.dag_adjudication import ...` lines; the module header imports only `DagNodeSpec`, `parse_dag_spec`, `DagRunResult`, `run_dag`, `DagRunStore`, `LocalFileBackend` and friends). `_TEST_ORIGIN` is at line 4873 and `_FakeExec` at line 262.

**Do not reach for the file's existing `_run_two_node_dag` for the in-flight test.** It builds an `a -> b` *chain* (`tests/test_subagent_dag_runner.py:4876`, `b.depends_on == ["a"]`), so when `a` suspends, `b` is `pending` and has never dispatched -- there is nothing in flight to cancel and the test would pass for the wrong reason. The in-flight case needs two *independent* nodes dispatched in the same round: one that fails its verdict quickly, one that is still running when the decision lands. Its existing signature, for the tests that can use it, is:

```python
_run_two_node_dag(tmp_path, *, desk, judge_node, announce_exception, max_continuations=2,
                  exec_backend=None, instance_a=None, origin=_TEST_ORIGIN,
                  adjudication_timeout_s=5, semaphore=None, control_reachable=None)
```

Add one local harness for the replan tests, built on `run_dag` the same way that one is:

```python
def _replan_plan(run_id="run-new", from_node="a", reason="the plan was wrong"):
    # Unannotated on purpose: ReplanPlan is imported function-locally in this file, so a
    # module-level annotation naming it fails ruff F821. Matches _run_two_node_dag's own
    # unannotated parameters.
    return ReplanPlan(
        run_id=run_id,
        from_node=from_node,
        reason=reason,
        nodes=(),
        backends={},
        auto_instances=frozenset(),
        notices=(),
    )


async def _run_replanned_dag(tmp_path, *, extra_nodes: list[dict], slow: set[str] = frozenset(), resolve_with=None):
    """`a` fails its verdict; `extra_nodes` run beside it, independent of `a`.

    Independent on purpose: a node that depends on `a` is still `pending` when `a`
    suspends, so a chain cannot express "in flight" at all.
    """
    desk = AdjudicationDesk()
    started = asyncio.Event()

    class _Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, prompt: str, **_kw: Any) -> str:
            self.calls.append(prompt)
            if any(nid in prompt for nid in slow):
                started.set()
                await asyncio.sleep(30)
            return "did it"

    backend = _Backend()

    async def _judge(*, node, store, output, error, crashed):
        return Verdict(accomplished=node.id != "a", what_is_missing="the wrong tool was used")

    async def _announce(run_id, node_id, text, origin, *, awaiting_decision):
        if slow:
            await started.wait()
        desk.resolve(node_id, REPLAN, "the plan was wrong", plan=resolve_with or _replan_plan(from_node=node_id))

    spec = parse_dag_spec(
        {
            "task_summary": "replan me",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "do a"},
                *extra_nodes,
            ],
        }
    )
    return await run_dag(
        spec,
        resolve=lambda node: backend,
        backend=LocalFileBackend(),
        workdir=str(tmp_path),
        run_root=str(tmp_path / "runs"),
        desk=desk,
        judge_node=_judge,
        announce_exception=_announce,
        origin=_TEST_ORIGIN,
        adjudication_timeout_s=5,
    )
```

Then the tests:

```python
async def test_a_replan_cancels_a_node_in_flight_rather_than_draining_it(tmp_path) -> None:
    """Cancel, not drain: chosen over waiting in the design. A slow node is cut off."""
    result = await _run_replanned_dag(
        tmp_path,
        extra_nodes=[{"id": "slowpoke", "subagent": "x", "node_summary": "slow", "prompt_template": "do slowpoke"}],
        slow={"slowpoke"},
    )

    statuses = {entry["node"]: entry["status"] for entry in result.files}
    assert statuses["a"] == "failed", "the adjudicated node is given up on, not cancelled"
    assert statuses["slowpoke"] == "cancelled", "the in-flight node was cut off, not drained"
    assert result.replanned_into == "run-new"


async def test_the_wind_down_reason_names_the_new_run(tmp_path) -> None:
    result = await _run_replanned_dag(tmp_path, extra_nodes=[])

    error = next(e for e in result.files if e["node"] == "a")["error"] or ""
    assert "run-new" in error
    assert "the plan was wrong" in error


async def test_a_pending_node_is_skipped_not_failed(tmp_path) -> None:
    result = await _run_replanned_dag(
        tmp_path,
        extra_nodes=[
            {
                "id": "never_ran",
                "subagent": "x",
                "node_summary": "downstream",
                "prompt_template": "do never_ran",
                "depends_on": ["a"],
            }
        ],
    )

    statuses = {entry["node"]: entry["status"] for entry in result.files}
    assert statuses["never_ran"] == "skipped"


async def test_a_completed_node_survives_a_replan_with_its_output(tmp_path) -> None:
    result = await _run_replanned_dag(
        tmp_path,
        extra_nodes=[{"id": "done", "subagent": "x", "node_summary": "fine", "prompt_template": "do done"}],
    )

    entry = next(e for e in result.files if e["node"] == "done")
    assert entry["status"] == "completed"
    assert entry["output_file"], "its output must stay referenceable by the new run"
    assert not entry["error"], "a completed node is untouched by the wind-down"


async def test_a_run_that_was_not_replanned_reports_no_successor(tmp_path) -> None:
    desk = AdjudicationDesk()

    async def _judge(*, node, store, output, error, crashed):
        return Verdict(accomplished=True)

    async def _announce(*_a, **_kw):
        return None

    result = await _run_two_node_dag(
        tmp_path, desk=desk, judge_node=_judge, announce_exception=_announce
    )

    assert result.replanned_into is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_runner.py -x -k replan`
Expected: FAIL -- `AttributeError: 'DagRunResult' object has no attribute 'replanned_into'`.

- [ ] **Step 3: Implement**

Add the field to `DagRunResult`:

```python
    replanned_into: str | None = None
```

Widen `_run_ready_groups`:

```python
async def _run_ready_groups(coros: Any, cancel: asyncio.Event | None, interrupt: asyncio.Event | None = None) -> None:
```

and inside it, replace the two-way race with a set of signals:

```python
    tasks = [asyncio.ensure_future(c) for c in coros]
    signals = [asyncio.ensure_future(e.wait()) for e in (cancel, interrupt) if e is not None]
    if not signals:
        await asyncio.gather(*tasks)
        return
    try:
        pending: set[asyncio.Future] = {*tasks, *signals}
        while True:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(s in done for s in signals) or all(t.done() for t in tasks):
                break
    finally:
        for signal in signals:
            if not signal.done():
                signal.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*signals, *tasks, return_exceptions=True)
```

Keep the existing docstring and extend its first paragraph to name `interrupt`: an in-flight node is cancelled the instant either signal fires. The `finally` reap keeps the reason it already documents.

Add the wind-down:

```python
async def _apply_replan(
    plan: Any,
    *,
    status: dict[str, str],
    errors: dict[str, str],
    published_terminal: set[str],
    node_started_at: dict[str, int],
    node_ended_at: dict[str, int],
    by_id: dict[str, DagNodeSpec],
    session_key: str | None,
    run_id: str,
    progress_publisher: ProgressPublisher | None,
) -> None:
    """Give this run's unfinished nodes their outcome, the replan being their cause.

    Publishes each transition here rather than leaving it to the loop's own sweep:
    that sweep only announces `skipped` and `cancelled`, and widening it to
    `failed` would double-publish every node that failed on its own merit --
    `_run_node` already announces those and does not record them as published.

    A completed node is untouched. Its output is what the new run references, and
    the whole point of finalizing this run is to make that reference legal.
    """
    reason = f"Superseded by replan into run {plan.run_id}: {plan.reason}"
    now = _now_ms()
    for nid, st in status.items():
        # Only the unfinished. A node already terminal on its own merit -- failed, or skipped
        # by an unrelated cascade -- keeps its status and its reason: overwriting them with
        # the replan's would erase why it actually ended, which is the one thing someone
        # reading this run afterwards is looking for. `_mark_stopped` guards the same way.
        if st not in ("running", "exception", "pending"):
            continue
        status[nid] = "skipped" if st == "pending" else ("cancelled" if st == "running" else "failed")
        errors[nid] = reason
        published_terminal.add(nid)
        node_started_at.setdefault(nid, now)
        node_ended_at[nid] = now
        await _emit(
            progress_publisher,
            "dag_node_updated",
            {"run_id": run_id, "node": nid, "status": status[nid]},
        )
        await _write_node_status(session_key, run_id, nid, by_id[nid].subagent, status[nid])
```

In `run_dag`, add a local beside the other accumulators:

```python
    replanned_into: str | None = None
```

At the top of the `while True` body, immediately after the `cancel` check and before the terminal-publish sweep:

```python
            if desk is not None and desk.replanned.is_set():
                plan = desk.take_plan()
                if plan is not None:
                    await _apply_replan(
                        plan,
                        status=status,
                        errors=errors,
                        published_terminal=published_terminal,
                        node_started_at=node_started_at,
                        node_ended_at=node_ended_at,
                        by_id=by_id,
                        session_key=session_key,
                        run_id=store.run_id,
                        progress_publisher=progress_publisher,
                    )
                    replanned_into = plan.run_id
                desk.replanned.clear()
                break
```

`break` rather than `continue`: every node is terminal by then, so the ready set and the suspended set are both empty and the loop would break on its own -- breaking here says so directly instead of relying on that. Clearing the event keeps a desk that outlives this loop from reporting a replan that has been applied.

Pass the interrupt into the round:

```python
            await _run_ready_groups(
                (...),
                cancel,
                interrupt=desk.replanned if desk is not None else None,
            )
```

Finally, carry the successor out:

```python
        result = await _finalize(...)
        result.replanned_into = replanned_into
        return result
```

Extend `run_dag`'s docstring where it documents `desk`: a `replan` answer interrupts the round in flight, gives every unfinished node a terminal status naming the successor run, and finalizes -- the successor itself is submitted by the caller, not here.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_subagent_dag_runner.py -x`
Expected: PASS, all of it. The pre-existing cancel tests exercise `_run_ready_groups` and must not regress.

- [ ] **Step 5: Lint, then propose a commit**

Run: `make lint-python`

```bash
/usr/bin/git add raven/agent/subagent/dag_runner.py tests/test_subagent_dag_runner.py
# propose:
# feat(agent): a replanned dag run stops where it is and names its successor
```

---

### Task 4: The `resolve_dag_node` surface and its refusals

**Files:**
- Modify: `raven/agent/subagent/dag_control_tools.py:1-30` (imports and module docstring), `:180-300` (`ResolveDagNodeTool`)
- Test: `tests/test_subagent_dag_control_tools.py`

**Interfaces:**
- Consumes: `REPLAN`, `DECISIONS` (Task 1); `SubAgentDagTool.node_schema()` (Task 2).
- Produces: `ResolveDagNodeTool.execute(run_id, node_id, decision, message=None, nodes=None)`.

This task delivers the surface and every refusal that needs no chaining. The chain itself is Task 5, so `execute` ends this task by returning a plain "not implemented on this path" string for a well-formed replan; Task 5 replaces that line. State that in the code with a single comment naming Task 5's method, and delete the comment there.

- [ ] **Step 1: Write the failing tests**

**The helper names below are real; check them before writing.** `tests/test_subagent_dag_control_tools.py`
provides `_Registry`, `_Loop`, `_DagTool`, `_ResolvableDagTool`, `_LoopWithRun`, `_LoopWithoutRun` and
`_finished_run()`. There is no `_resolve_tool`, `_graph_tool` or `_node` -- add these two local helpers at
the top of the section you are appending, and build the tool the way the file's existing resolve tests do
(`ResolveDagNodeTool(_LoopWithRun())`):

```python
def _resolve_tool(loop: Any = None) -> ResolveDagNodeTool:
    tool = ResolveDagNodeTool(loop or _LoopWithRun())
    tool.set_context("cli", "direct", None)
    return tool


def _node(node_id: str, *, depends_on: list[str] | None = None, prompt: str = "do it") -> dict[str, Any]:
    return {
        "id": node_id,
        "subagent": "x",
        "node_summary": "a step",
        "prompt_template": prompt,
        **({"depends_on": depends_on} if depends_on else {}),
    }


def _graph_tool() -> SubAgentDagTool:
    """A real graph tool, for the one test that compares against its own node schema."""
    return SubAgentDagTool(workspace=tempfile.mkdtemp())
```

`_resolve_tool` has to wrap a raw graph tool before `ResolveDagNodeTool` can find it:
`_registered_tool` looks it up as `loop.tools.get("run_subagent_dag")`, so a bare
`SubAgentDagTool` handed straight in resolves to `None` and the schema silently degrades to
`{"type": "object"}`. Detect it and wrap in `_Loop(tool=loop)`, with a one-line comment saying
why -- the indirection is not self-explanatory.

`_DagTool` is a read-only double: it implements `read_run` and `session_run_ids` and nothing else. That is
enough for every refusal in this task (they all return before reaching the graph tool's replan methods),
which is exactly why the surface and the chain are separate tasks. Task 5's tests need a real
`SubAgentDagTool` instead.

```python
async def test_replan_without_nodes_is_refused() -> None:
    tool = _resolve_tool()

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="the plan was wrong")

    assert "needs a `nodes` list" in out
    assert "no decision was recorded" in out


async def test_replan_without_a_message_is_refused() -> None:
    tool = _resolve_tool()

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", nodes=[_node("fresh")])

    assert "needs a message" in out


async def test_an_unknown_decision_names_all_three() -> None:
    tool = _resolve_tool()

    out = await tool.execute(run_id="r1", node_id="a", decision="wat")

    assert "'continue'" in out
    assert "'abandon'" in out
    assert "'replan'" in out


def test_the_decision_enum_advertises_replan() -> None:
    params = _resolve_tool().parameters

    assert params["properties"]["decision"]["enum"] == ["continue", "abandon", "replan"]


def test_the_nodes_parameter_reuses_the_graph_tools_node_schema() -> None:
    graph = _graph_tool()
    params = _resolve_tool(graph).parameters

    assert params["properties"]["nodes"]["items"] == graph.node_schema(), (
        "a copied node schema drifts from run_subagent_dag's"
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_control_tools.py -x -k "replan or decision_enum or node_schema"`
Expected: FAIL -- the enum has two values and `execute` rejects the unknown `nodes` kwarg.

- [ ] **Step 3: Implement**

Import `REPLAN` alongside `ABANDON, CONTINUE, DECISIONS`. In `ResolveDagNodeTool`:

Update `description`:

```python
        return (
            "Decide what happens to a DAG node that reported it could not accomplish its "
            "task: continue it with a message, abandon it and skip its dependents, or "
            "replan -- hand over a new node list, which stops this run and starts a new one "
            "from that list. On a run started with background=false this call also waits, "
            "and returns the next report or the run's final result."
        )
```

In `parameters`, widen the `decision` enum and its description, and add `nodes`:

```python
                "decision": {
                    "type": "string",
                    "enum": [CONTINUE, ABANDON, REPLAN],
                    "description": (
                        "'continue' sends your message to the node and lets it try again. "
                        "'abandon' fails the node and skips its dependents; the rest of the "
                        "graph carries on. 'replan' replaces what is left of the graph with "
                        "the `nodes` you supply: this run stops, and a new run starts from "
                        'your list. To stop everything instead, use tool_call name "cancel_dag".'
                    ),
                },
                "nodes": {
                    "type": "array",
                    "items": schema,
                    "description": (
                        "The replacement graph, required when replanning. Nodes this run "
                        "already completed are NOT re-declared -- name one in depends_on and "
                        "read it with {{ <id>.output }}. Every other node needs a new id."
                    ),
                },
```

`schema` comes from the registered graph tool, degrading to a bare object when none is registered (the tool is built before a graph tool has to exist):

```python
    @property
    def parameters(self) -> dict[str, Any]:
        tool = _registered_tool(self._loop)
        getter = getattr(tool, "node_schema", None)
        schema = getter() if callable(getter) else {"type": "object"}
```

In `execute`, replace the signature and the first two guards:

```python
    async def execute(
        self,
        run_id: str,
        node_id: str,
        decision: str,
        message: str | None = None,
        nodes: list[dict] | None = None,
    ) -> "str | ToolResult":
        if decision not in DECISIONS:
            return (
                f"Error: decision must be '{CONTINUE}', '{ABANDON}' or '{REPLAN}', not {decision!r}."
            )
        if decision in (CONTINUE, REPLAN) and not (message or "").strip():
            what = (
                "telling it what to do differently" if decision == CONTINUE else "saying why the plan is being changed"
            )
            return (
                f"Error: {decision} on node '{node_id}' needs a message {what}. "
                "Supply what the report said was missing."
            )
        if decision == REPLAN and not nodes:
            return (
                f"Error: replanning run {run_id} needs a `nodes` list -- the graph to run "
                "instead, and no decision was recorded, so the node is still waiting."
            )
```

The "no decision was recorded" clause is not decoration: every refusal above leaves the node suspended and its clock running, and a model that reads the refusal as terminal stops answering.

**Leave `blocking_for` as it is** -- True only for a bound foreground run. A background replan does wait (Task 5 awaits the old run's wind-down), so the honest reading would be True for every replan, but that wait is bounded and short by construction: the replan event cancels the round in flight, and what is left is marking statuses and writing a manifest. Marking it blocking would put the call in the "may go silent" class for a wait measured in milliseconds. If a background replan ever does hang, this is the first line to revisit; say so in a comment on the method rather than leaving the reasoning here only.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_subagent_dag_control_tools.py -x`
Expected: PASS.

- [ ] **Step 5: Lint, then propose a commit**

Run: `make lint-python`

```bash
/usr/bin/git add raven/agent/subagent/dag_control_tools.py tests/test_subagent_dag_control_tools.py
# propose:
# feat(agent): resolve_dag_node accepts a replacement graph
```

---

### Task 5: The chain -- validate, wind down, submit, link

**Files:**
- Modify: `raven/agent/subagent/dag_tool.py` (`resolve_node`, plus the three new methods), `raven/agent/subagent/dag_store.py` (`record_replan`), `raven/agent/subagent/dag_control_tools.py` (`execute`'s replan branch), `raven/agent/subagent/dag_reader.py` (module docstring)
- Test: `tests/test_subagent_dag_control_tools.py`, `tests/test_subagent_dag_core.py`

**Interfaces:**
- Consumes: `ReplanPlan` (Task 1); `Preflight`, `_preflight` (Task 2) -- note `Preflight` carries **four** fields including `capabilities`, and `prepare_replan` must mint from `pre.capabilities` rather than calling `self._capability_map()` again, for the straddled-awaits reason recorded in Task 2; `DagRunResult.replanned_into` (Task 3); the surface (Task 4).
- Produces on `SubAgentDagTool`:
  - `async def prepare_replan(self, run_id: str, from_node: str, nodes: list[dict], reason: str, session_key: str | None, live: dict) -> "ReplanPlan | str"` -- the validated plan, or a refusal string ready to return to the model. `live` is the caller's already-reconciled read of the old run; see the note in the implementation on why this tool cannot do that read itself.
  - `async def await_finalized(self, run_id: str) -> None`
  - `async def start_replan(self, run_id: str, plan: ReplanPlan, *, bound: bool = False) -> "str | ToolResult"`
  - the `replanned_into` branch in `_run_detached` (`dag_tool.py:1198`), which is what finally consumes the field Task 3 produced
  - `def resolve_node(self, run_id, node_id, decision, message, plan=None) -> bool`
- Produces on `DagRunStore`: `async def record_replan(self, entry: dict) -> None`

**Ordering (from the spec, and load-bearing):** validate and mint -> hand to the desk -> the run winds down and finalizes -> submit the new run -> record the link. Validation therefore runs *before* the old run's index entry carries per-node statuses, so it cannot use `is_readable` and must overlay the live read instead.

- [ ] **Step 1: Write the failing tests**

**These tests need a real `SubAgentDagTool` and a real run dir on disk**, because `prepare_replan` reads
the old run's `graph.json` and its live per-node state. Both fixtures already exist -- do not invent new
ones:

- a real tool: `SubAgentDagTool(workspace=tmp_path)` (`tests/test_subagent_dag_core.py:1230`), with
  `charge=`, `ask=` and `is_paused=` passed per test
- a seeded run dir: `_seed_run(be, run_id, *, finalized)` (`tests/test_subagent_dag_core.py:580`) writes a
  two-node `a -> b` graph plus `a.out.md` into a `_FakeBackend`, which is the shape `prepare_replan` reads

So put these tests in `tests/test_subagent_dag_core.py`, beside `_seed_run`, rather than in the control-tools
file -- the fixture they need lives there and duplicating it is how the two copies drift. Write one local
helper over the existing pair:

```python
async def _replannable_tool(tmp_path, **kw) -> tuple[SubAgentDagTool, dict]:
    """A real graph tool over a seeded, still-unfinalized run r1.

    `finalized=False` on purpose: that is the state a replan is decided in, and it
    is what makes the overlay load-bearing -- an unfinalized run's index entry
    carries no per-node status, so every node of it reads back `running`.
    """
    be = _FakeBackend()
    _seed_run(be, "r1", finalized=False)
    tool = SubAgentDagTool(workspace=tmp_path, **kw)
    tool._backend = be
    tool.set_context("cli", "direct", None)
    live = {"files": [{"node": "a", "status": "exception"}, {"node": "b", "status": "completed"}]}
    return tool, live
```

`_seed_run`'s graph names its nodes `a` and `b` with `b.depends_on == ["a"]`, so write the tests against
those ids rather than the `done` / `half` ones sketched below, and pass the `live` dict as
`prepare_replan`'s last argument.

```python
async def test_replanning_refuses_a_redeclared_id(tmp_path) -> None:
    """`b` belongs to run r1 forever, whatever became of it."""
    tool, live = await _replannable_tool(tmp_path)

    out = await tool.prepare_replan("r1", "a", [_node("b")], "the plan was wrong", None, live)

    assert isinstance(out, str)
    assert "already used by run 'r1'" in out
    assert "depends_on" in out, "the refusal has to say how to reuse it instead"


async def test_replanning_allows_a_reference_to_a_completed_node_before_the_index_is_written(tmp_path) -> None:
    """The old run has not finalized, so is_readable is False for every node of it.

    This is the test the overlay exists for: without it, `b` reads back `running`
    from the index and a reference to a plainly-completed node is refused.
    """
    tool, live = await _replannable_tool(tmp_path)

    plan = await tool.prepare_replan(
        "r1", "a", [_node("fresh", depends_on=["b"], prompt="use {{ b.output }}")], "the plan was wrong", None, live
    )

    assert not isinstance(plan, str), plan
    assert [n.id for n in plan.nodes] == ["fresh"]
    assert plan.from_node == "a"
    assert plan.run_id and plan.run_id != "r1", "the successor id is minted up front"


async def test_replanning_refuses_a_reference_to_a_node_that_did_not_complete(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path)
    live = {"files": [{"node": "a", "status": "exception"}, {"node": "b", "status": "running"}]}

    out = await tool.prepare_replan("r1", "a", [_node("fresh", depends_on=["b"])], "wrong", None, live)

    assert isinstance(out, str)
    assert "b" in out


async def test_replanning_refuses_while_delegation_is_paused(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path, is_paused=lambda: True)

    out = await tool.prepare_replan("r1", "a", [_node("fresh")], "wrong", None, live)

    assert isinstance(out, str)
    assert "delegation is paused" in out


async def test_replanning_charges_the_dispatch_quota(tmp_path) -> None:
    charged: list[str | None] = []

    def _charge(key: str | None) -> None:
        charged.append(key)
        return None

    tool, live = await _replannable_tool(tmp_path, charge=_charge)

    await tool.prepare_replan("r1", "a", [_node("fresh")], "wrong", None, live)

    assert len(charged) == 1, "a replan submits a new graph, which is the unit this quota counts"


async def test_a_spent_quota_refuses_the_replan(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path, charge=lambda key: "Error: rate limit")

    out = await tool.prepare_replan("r1", "a", [_node("fresh")], "wrong", None, live)

    assert out == "Error: rate limit"


async def test_a_declined_confirmation_refuses_the_replan(tmp_path) -> None:
    async def _ask(conversation: str, question: str) -> bool:
        return False

    tool, live = await _replannable_tool(tmp_path, ask=_ask)
    # The gate is inherited from the old run's spec, so the seeded graph has to
    # carry it -- `_seed_run` writes no `confirm` key, which parses as False.
    _set_seeded_confirm(tool, "r1", True)

    out = await tool.prepare_replan("r1", "a", [_node("fresh")], "wrong", None, live)

    assert isinstance(out, str)
    assert "did not approve" in out


async def test_the_link_is_recorded_on_the_old_runs_graph_json(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path)
    plan = await tool.prepare_replan("r1", "a", [_node("fresh")], "the plan was wrong", None, live)

    await tool.start_replan("r1", plan)

    graph = json.loads(tool._backend.files["/hist/mas_dag/r1/graph.json"].decode())
    assert graph["replan"]["run_id"] == plan.run_id
    assert graph["replan"]["from_node"] == "a"
    assert graph["replan"]["reason"] == "the plan was wrong"
    assert graph["replan"]["started"] is True
    assert graph["replan"]["decided_at"] > 0
    assert graph["nodes"], "the original graph is still there; the key is added beside it"


async def test_a_refused_submission_still_records_the_link_as_unstarted(tmp_path) -> None:
    """A dangling reason is worse than a record saying the successor never ran."""
    tool, live = await _replannable_tool(tmp_path)
    plan = await tool.prepare_replan("r1", "a", [_node("fresh")], "wrong", None, live)
    _claim_the_id_from_under_it(tool, plan.run_id, "fresh")

    out = await tool.start_replan("r1", plan)

    graph = json.loads(tool._backend.files["/hist/mas_dag/r1/graph.json"].decode())
    assert graph["replan"]["started"] is False
    assert graph["replan"]["error"]
    assert "Error" in str(getattr(out, "model_text", out))
```

Two of those need one more local helper each, both trivial over `_FakeBackend.files`:
`_set_seeded_confirm(tool, run_id, value)` rewrites the seeded `graph.json` with a `confirm` key, and
`_claim_the_id_from_under_it(tool, run_id, node_id)` appends an index entry claiming `node_id` for another
run, which is what makes the successor's own validation fail at claim time. Read `_seed_run` and the
`index.json` shape `read_index` expects before writing the second one.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_control_tools.py -x -k "replan"`
Expected: FAIL -- `AttributeError: ... has no attribute 'prepare_replan'`.

- [ ] **Step 3: Add the imports this task needs**

None of these are in `dag_tool.py` today. Its import block is `asyncio`, `collections.abc`, `contextvars`,
`copy`, `dataclasses`, `pathlib`, `typing`, then the `raven.*` group -- no `json`, no `time`:

```python
import json
import time

from raven.agent.subagent.dag_reader import run_dir_of
from raven.agent.subagent.dag_store import RUNNING
```

The existing `dag_store` import brings in `SessionNodes, index_guard, make_run_id, read_index,
read_session_nodes` only, so `RUNNING` has to be added to it. `dag_store` itself already imports `json`,
so `record_replan` needs nothing new there.

- [ ] **Step 4: Implement `record_replan` on the store**

```python
    async def record_replan(self, entry: dict) -> None:
        """Note on this run's ``graph.json`` that it was replanned, and into what.

        A reserved top-level key beside the spec's own, not a wrapper around it:
        all four readers of this file take the keys they want out of a raw dict
        (``dag_reader``, ``instance_records``, and the two rpc methods), so the
        addition is inert to every one of them. It must stay out of
        ``SubAgentDagSpec``, which is ``extra="forbid"`` and would reject the file.
        """
        path = self._backend.join_path(self.run_dir, "graph.json")
        graph = json.loads(await self.read_text(path))
        graph["replan"] = entry
        await self.write_text(path, json.dumps(graph, ensure_ascii=False))
```

- [ ] **Step 5: Implement the three tool methods**

`prepare_replan`, in the spec's check order. The overlay is the part to get exactly right.

```python
    async def prepare_replan(
        self, run_id: str, from_node: str, nodes: list[dict], reason: str, session_key: str | None, live: dict
    ) -> "ReplanPlan | str":
        """Validate a replacement graph and charge for it. The plan, or a refusal.

        Runs the same phases a submitted graph runs, in the same order and for the
        same reasons (see ``_execute``): a refused graph must cost zero dispatches
        and be refused in the caller's own turn.

        The successor's run id is minted here, before the decision is handed over,
        because the wind-down names it in every node reason it writes.

        ``live`` is passed in rather than read here: reconciling a run needs
        loop-wide liveness (``dag_live.live_run_ids``) and this tool holds no
        reference to the agent loop -- the control tool does, and its own
        ``_read_live`` already asks exactly that question with the right source.
        """
        if self._is_paused is not None and self._is_paused():
            return (
                "Error: delegation is paused. The user paused sub-agent spawning; "
                "do the work in this turn instead, or ask them to resume."
            )
        old = parse_dag_spec(json.loads(await self._read_graph_json(run_id, session_key)))
        try:
            spec = parse_dag_spec(
                {"task_summary": old.task_summary, "nodes": nodes, "confirm": old.confirm}
            )
            validate_and_order(spec, self._reference_roots(), await self._session_nodes_for_replan(run_id, live))
            pre = await self._preflight(spec)
        except DagValidationError as exc:
            return self._validation_error(exc)
        origin = self._origin.get() or self._default_origin
        if pre.spec.confirm and not await self._confirmed(pre.spec, origin):
            return (
                "The user did not approve this replan, so nothing was run and the old run is "
                "already stopped. Ask them what to change before replanning again."
            )
        if self._charge is not None and (refusal := self._charge(origin.conversation)) is not None:
            return refusal
        spec, auto = self._mint_missing_instances(pre.spec, pre.capabilities)
        return ReplanPlan(
            run_id=make_run_id(),
            from_node=from_node,
            reason=reason,
            nodes=tuple(spec.nodes),
            backends=pre.backends,
            auto_instances=auto,
            notices=tuple(pre.notices),
        )
```

The `graph.json` read, which has no existing accessor:

```python
    async def _read_graph_json(self, run_id: str, session_key: str | None) -> str:
        """One run's submitted graph, as text."""
        root = self._run_root(session_key)
        path = self._backend.join_path(run_dir_of(self._backend, root, run_id), "graph.json")
        return (await self._backend.read_file(path)).decode("utf-8", errors="replace")
```

`read_file` returns **bytes** (`prompt_backend.py:24`), so the decode is required, not decoration -- and the `errors="replace"` matches how `dag_reader._read_text` handles the same file rather than inventing a second policy for it.

`run_dir_of` comes from `dag_reader`, already imported here for `read_run` and `read_node`; add it to that
import group rather than joining the path by hand, because it is the function that validates the run id
before it becomes a path component.

The overlay itself:

```python
    async def _session_nodes_for_replan(self, run_id: str, live: dict) -> SessionNodes:
        """This session's node index, with the replanned run's live state laid over it.

        The run has not finalized when this is asked, so its index entry carries no
        per-node ``status`` and every one of its nodes reads back ``running`` --
        neither reusable nor readable. The live read is the more current of the two
        by construction: it is what the finalize about to happen will write. Without
        the overlay, a reference to a node that has plainly completed is refused
        with "that run left it with no output".
        """
        known = await self._session_nodes()
        state = dict(known.state)
        for entry in live.get("files") or []:
            state[entry["node"]] = entry.get("status") or state.get(entry["node"], RUNNING)
        return SessionNodes(owner=dict(known.owner), state=state)
```

`await_finalized`, over the task already indexed in `_runs`:

```python
    async def await_finalized(self, run_id: str) -> None:
        """Wait for one run's task to end, so its per-node outcomes are on disk.

        The successor's validation reads them, and a run whose index entry has no
        statuses reads back as still running. Returns at once for a run this tool
        no longer holds -- it has already ended.
        """
        task = self._runs.get(run_id)
        if task is not None and not task.done():
            await asyncio.wait([task])
```

`start_replan`, which submits the successor and records the link either way:

```python
    async def start_replan(self, run_id: str, plan: "ReplanPlan", *, bound: bool = False) -> "str | ToolResult":
        """Start the successor run and note the link on the run it replaces."""
        entry: dict[str, Any] = {
            "run_id": plan.run_id,
            "from_node": plan.from_node,
            "reason": plan.reason,
            "decided_at": int(time.time() * 1000),
            "started": True,
        }
        try:
            result = await self._submit_replan(plan, bound=bound)
        except DagValidationError as exc:
            entry["started"] = False
            entry["error"] = str(exc)
            result = self._validation_error(exc)
        await self._record_link(run_id, entry)
        if entry["started"]:
            await self._emit_replanned(run_id, plan)
        return result
```

`_submit_replan` is `_execute`'s post-validation half with the graph already validated and `run_id=plan.run_id` instead of a freshly minted one: it builds the `_RunDirs`, the cancel event, the outbox when the successor is to be bound, creates the task, indexes it, wires `_retire` and `_adopt`. Factor that half of `_execute` into a `_dispatch(spec, run_id, dirs, backends, auto_instances, origin, call_id, background)` and call it from both, or duplicate it -- but if you duplicate it, say in a comment which site is the original, because the two must not drift on `_retire`.

**The foreground binding transfers.** When the run being replanned was bound (`self._outboxes` held one for it), the successor gets an outbox bound to the same turn and `_submit_replan` awaits its first event, returning `self.render_event(plan.run_id, event)` -- exactly what `_execute`'s `background=False` tail does. The caller said `background: false` because it cannot continue without the outputs, and a replan is the same pursuit; returning the old run's wind-down and leaving the successor in the background would silently cancel the lane the caller chose. So:

```python
        was_bound = run_id in self._outboxes or self.is_foreground(run_id)
```

read **before** the desk hand-off, because `_retire` drops the old run's outbox the moment its task finishes, and by the time `start_replan` runs that has already happened. Thread the flag through `ReplanPlan`? No -- keep it a local captured in the control tool's branch and passed to `start_replan(run_id, plan, bound=was_bound)`; the plan describes the graph, not the lane it was asked for.

Add a test for it:

```python
async def test_a_bound_foreground_replan_returns_the_successors_first_event(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path)
    plan = await tool.prepare_replan("r1", "a", [_node("fresh")], "wrong", None, live)

    out = await tool.start_replan("r1", plan, bound=True)

    assert plan.run_id in str(getattr(out, "model_text", out))
    assert "started in the background" not in str(getattr(out, "model_text", out))
```

`_emit_replanned` publishes the link event on the old run's emitter:

```python
    async def _emit_replanned(self, run_id: str, plan: "ReplanPlan") -> None:
        origin = self._origin.get() or self._default_origin
        emit = self._emitter(origin.conversation, self._tool_call_id.get())
        await emit(
            "dag_run_replanned",
            {
                "run_id": run_id,
                "replan_run_id": plan.run_id,
                "from_node": plan.from_node,
                "reason": plan.reason,
            },
        )
```

Widen `resolve_node`:

```python
    def resolve_node(
        self, run_id: str, node_id: str, decision: str, message: str | None, plan: "ReplanPlan | None" = None
    ) -> bool:
```

and pass `plan` through to `desk.resolve`.

- [ ] **Step 6: Wire the control tool's replan branch**

In `ResolveDagNodeTool.execute`, after the ownership check and in place of Task 4's placeholder:

```python
        if decision == REPLAN:
            # The live read happens here, not in prepare_replan: reconciling a run
            # needs loop-wide liveness and only this tool holds the loop. _read_live
            # is the same call dag_status makes, so the two cannot disagree.
            try:
                live = await self._read_live(run_id)
            except DagReadError as exc:
                return (
                    f"Cannot replan run {run_id}: its state could not be read ({exc}), so no "
                    "decision was recorded and the node is still waiting."
                )
            plan = await tool.prepare_replan(
                run_id, node_id, nodes or [], (message or "").strip(), self._session.get(), live
            )
            if isinstance(plan, str):
                return plan
            # Read before the hand-off: `_retire` drops the old run's outbox as
            # soon as its task ends, so by `start_replan` the lane it was asked
            # for is no longer discoverable.
            was_bound = bool(getattr(tool, "is_foreground", lambda _r: False)(run_id))
            if not resolve_node(self._loop, run_id, node_id, decision, message, plan):
                return (
                    f"Node '{node_id}' of run {run_id} is no longer waiting for a decision, so the "
                    "replan was not applied and the old run is still running as submitted. "
                    'tool_call name "dag_status" shows where every node stands.'
                )
            await tool.await_finalized(run_id)
            return _with_notices(await tool.start_replan(run_id, plan, bound=was_bound), list(plan.notices))
```

The order inside matters and is the spec's: validate and charge first, then hand over, then wait for the wind-down, then submit. Handing over before validating would stop the old run for a graph that turns out to be unrunnable.

**Three call sites have to be widened, not one -- and the one this plan nearly missed is the only one that runs in production.** `dag_live.resolve_node` (`raven/agent/subagent/dag_live.py:69`) tries the *loop's* method first and reaches the tool only as a fallback:

```python
    fn = getattr(loop, "resolve_dag_node", None)
    if fn is None:
        tool = _registered_tool(loop)
        fn = getattr(tool, "resolve_node", None) if tool is not None else None
```

A real gateway loop has `resolve_dag_node` (`raven/agent/loop/wiring.py:1256`), so the live path always goes through it and never touches the tool directly. Widening only `dag_live` and `SubAgentDagTool` would drop the plan on every real run while every unit test that stubs the tool still passed. All three:

```python
# raven/agent/subagent/dag_live.py:69
def resolve_node(
    loop: Any, run_id: str, node_id: str, decision: str, message: str | None, plan: Any = None
) -> bool:
    ...
        return bool(fn(run_id, node_id, decision, message, plan))

# raven/agent/loop/wiring.py:1256
    def resolve_dag_node(
        self, run_id: str, node_id: str, decision: str, message: str | None, plan: Any = None
    ) -> bool:
        """Answer one suspended node, whichever instance owns its run."""
        return any(tool.resolve_node(run_id, node_id, decision, message, plan) for tool in self.dag_tools())
```

The `any(...)` fan-out stays correct for a replan: only the tool holding that run's desk has the node open, and the others return False without looking at the plan.

Pin the pass-through with a test, because a dropped argument here is invisible until a live run:

```python
async def test_the_plan_survives_both_hops_to_the_tool() -> None:
    seen: list[Any] = []

    class _Owner:
        def resolve_node(self, run_id, node_id, decision, message, plan=None):
            seen.append(plan)
            return True

    class _LoopWithMethod(_Loop):
        def resolve_dag_node(self, run_id, node_id, decision, message, plan=None):
            return any(t.resolve_node(run_id, node_id, decision, message, plan) for t in [_Owner()])

    assert resolve_node(_LoopWithMethod(), "r1", "a", REPLAN, "wrong", _replan_plan()) is True
    assert seen == [_replan_plan()], "the loop hop must not swallow the plan"
```

- [ ] **Step 7: Suppress the old run's announcement**

Task 3 produces `DagRunResult.replanned_into`; nothing consumed it until here. The announce site is
`_run_detached` in this file (`dag_tool.py:1198`), which today only checks `cancel.is_set()`:

```python
        if getattr(result, "replanned_into", None):
            # The run became another run, and the resolve call already returned both
            # halves to the agent. Announcing "3 completed, 1 failed, 2 skipped" here
            # would narrate to the agent what it just decided -- the same reason a
            # cancelled run stays silent one branch below.
            logger.info("DAG run {} was replanned into {}; not announcing a result", run_id, result.replanned_into)
            return
        if cancel.is_set():
```

It goes **before** the `cancel.is_set()` branch: a replan does not set `cancel`, so order does not change
behaviour today, but a replan that races a user stop should read as replanned rather than stopped.

Add the test:

```python
async def test_a_replanned_run_does_not_announce_its_own_outcome(tmp_path) -> None:
    announced: list[str] = []
    tool, live = await _replannable_tool(tmp_path, announce=lambda rid, text, origin: announced.append(rid))

    # `_run_detached` is the site; drive it with a result that carries a successor.
    await tool._run_detached_for_test(DagRunResult(run_id="r1", dir="/d", replanned_into="run-new"))

    assert announced == [], "the resolve call already told the agent; a second telling is narration"
```

If no seam exists to drive `_run_detached` directly, assert on the branch the other way: give the run a
`replanned_into` and check `self._announce` was not called, using whatever injection the file's existing
announce tests use. Do not add a production-only test seam to make this assertable.

- [ ] **Step 8: Document the reserved key**

In `dag_reader.py`'s module docstring, after the "structure always from `graph.json`" sentence, add:

```
A replanned run also carries a reserved ``replan`` key there, naming the run that
replaced it; it is not part of ``SubAgentDagSpec`` and must not be, that model being
``extra="forbid"``. Readers here take the keys they want and ignore it.
```

- [ ] **Step 9: Run the suites**

Run: `uv run pytest tests/test_subagent_dag_control_tools.py tests/test_subagent_dag_core.py -x`
Expected: PASS.

- [ ] **Step 10: Lint, then propose a commit**

Run: `make lint-python`

```bash
/usr/bin/git add raven/agent/subagent/dag_tool.py raven/agent/subagent/dag_store.py raven/agent/subagent/dag_control_tools.py raven/agent/subagent/dag_reader.py raven/agent/subagent/dag_live.py tests/test_subagent_dag_control_tools.py tests/test_subagent_dag_core.py
# propose:
# feat(agent): a replan stops one dag run and starts its successor
```

---

### Task 6: The `dag.run_replanned` wire event

**Files:**
- Modify: `rpc-schema/openrpc.json`, `raven/rpc/models.py:696-745` and `:980-990` and `:3965-3980`, `raven/rpc/spine.py:57-62` and `:74-105`, `raven/acp/updates.py:55-62`
- Regenerate: `ui-tui/src/rpc/generated.ts`, `ui-web/src/rpc/generated.ts`
- Test: `tests/test_rpc_schema_match.py`, `tests/test_rpc_dag.py`

**Interfaces:**
- Consumes: the `dag_run_replanned` progress event (Task 5).
- Produces: wire event `dag.run_replanned` with payload `{run_id, tool_call_id?, replan_run_id, from_node, reason}`; TS type `DagRunReplannedEvent`. There is no `DagRunReplannedPayload` TS type -- no `*Payload` interface exists in either generated file, because every event inlines its payload. Mirror `DagNodeUpdatedEvent`.

**The generated files are never hand-edited.** Edit the contract and the models, then run the generators. CI fails a schema edit that skips either.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_replanned_event_is_in_the_contract_and_the_models() -> None:
    schemas = json.loads(Path("rpc-schema/openrpc.json").read_text())["components"]["schemas"]

    assert "DagRunReplannedEvent" in schemas


def test_the_progress_event_maps_to_the_wire_event() -> None:
    from raven.rpc.spine import _DAG_WIRE_EVENT, _dag_payload

    assert _DAG_WIRE_EVENT["dag_run_replanned"] == "dag.run_replanned"
    assert _dag_payload(
        "dag_run_replanned",
        {"run_id": "r1", "replan_run_id": "r2", "from_node": "a", "reason": "wrong"},
    ) == {"run_id": "r1", "replan_run_id": "r2", "from_node": "a", "reason": "wrong"}


def test_an_unmapped_progress_event_is_still_dropped() -> None:
    from raven.rpc.spine import _DAG_WIRE_EVENT

    assert "dag_node_started" not in _DAG_WIRE_EVENT
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_rpc_dag.py -x -k replan`
Expected: FAIL with `KeyError: 'dag_run_replanned'`.

- [ ] **Step 3: Add the model**

In `raven/rpc/models.py`, after `DagNodeUpdatedEvent`:

```python
class DagRunReplannedPayload(_Strict):
    run_id: str
    tool_call_id: str | None = None
    replan_run_id: str
    from_node: str
    reason: str


class DagRunReplannedEvent(_Strict):
    type: Literal["dag.run_replanned"]
    payload: DagRunReplannedPayload
```

Add both to the `TurnEvent` union beside `DagNodeUpdatedEvent`, and both names to `__all__`.

- [ ] **Step 4: Add the bridge**

In `raven/rpc/spine.py`, add to `_DAG_WIRE_EVENT`:

```python
    "dag_run_replanned": "dag.run_replanned",
```

and in `_dag_payload`, before the `dag_run_completed` fall-through:

```python
    if name == "dag_run_replanned":
        return {
            **common,
            "replan_run_id": payload.get("replan_run_id"),
            "from_node": payload.get("from_node"),
            "reason": payload.get("reason"),
        }
```

Add `"dag.run_replanned"` to the list in `raven/acp/updates.py`.

- [ ] **Step 5: Update the contract and regenerate**

Add `DagRunReplannedEvent` and its payload to `rpc-schema/openrpc.json` under `components.schemas`, mirroring `DagNodeUpdatedEvent`'s shape exactly. Then wire it into the union: `components.schemas.TurnEvent` is a `oneOf` with **21 members today** plus a `discriminator` -- add the `$ref` to `oneOf` **and** the `dag.run_replanned` entry to the discriminator mapping. Missing the mapping is the failure mode that type-checks fine and fails at runtime deserialization. Then:

```bash
npm run gen:rpc --prefix ui-tui
node scripts/gen-rpc-client.mjs   # from ui-web/
```

- [ ] **Step 6: Verify all four gates**

Run, in order:

```bash
uv run pytest tests/test_rpc_schema_match.py tests/test_rpc_dag.py -x
npm run lint:rpc --prefix ui-tui
npm run gen:check --prefix ui-web
npm run type-check --prefix ui-web
```

Expected: all PASS. `lint:rpc` and `gen:check` failing means the generators were not run or the contract does not match the models.

`ui-tui` needs `node_modules` before its scripts run; a fresh worktree has none. `npm ci --prefix ui-tui` (the lockfiles differ from `ui-web`'s, so run each separately, and never a separate `npm ci` inside the vendored `hermes-ink` -- a second React copy breaks every render test).

- [ ] **Step 7: Lint, then propose a commit**

Run: `make lint-python`

```bash
/usr/bin/git add rpc-schema/openrpc.json raven/rpc/models.py raven/rpc/spine.py raven/acp/updates.py ui-tui/src/rpc/generated.ts ui-web/src/rpc/generated.ts tests/test_rpc_dag.py
# propose:
# feat(rpc): a replanned dag run tells a client which run took over
```

---

### Task 7: The TUI shows the chain

**Files:**
- Modify: `ui-tui/src/domain/dagRun.ts:12-22` (imports and `DagEvent`), `:56-90` (`DagRunState`), `:178-200` (`foldDagEvent`), `ui-tui/src/app/chatStream.ts:351`, `ui-tui/src/components/dagPanel.tsx`
- Also check: `ui-tui/src/app/liveAgentsStore.ts:321` and `:367` both branch on `dag.run_started`; confirm whether the strip needs the new event or is right to ignore it, and say which in the commit message
- Test: `ui-tui/src/__tests__/dagRun.test.ts`

**Interfaces:**
- Consumes: `DagRunReplannedEvent` from `../rpc/index.js` (Task 6).
- Produces: `DagRunState.replannedInto?: string`.

- [ ] **Step 1: Write the failing tests**

```typescript
it('records the successor run on the run it replaced', () => {
  const started = foldDagEvent(null, runStarted('r1', ['a']))

  const folded = foldDagEvent(started, {
    type: 'dag.run_replanned',
    payload: { run_id: 'r1', replan_run_id: 'r2', from_node: 'a', reason: 'the plan was wrong' }
  })

  expect(folded?.replannedInto).toBe('r2')
})

it('records the successor even after the run has settled', () => {
  // The tool emits this only once the successor has started, which is after the
  // old run's dag.run_completed. A fold that assumed a live run would drop it.
  const done = foldDagEvent(foldDagEvent(null, runStarted('r1', ['a'])), runCompleted('r1'))

  const folded = foldDagEvent(done, {
    type: 'dag.run_replanned',
    payload: { run_id: 'r1', replan_run_id: 'r2', from_node: 'a', reason: 'wrong' }
  })

  expect(folded?.replannedInto).toBe('r2')
  expect(folded?.done).toBe(true)
})

it('ignores a replanned event for another run', () => {
  const started = foldDagEvent(null, runStarted('r1', ['a']))

  const folded = foldDagEvent(started, {
    type: 'dag.run_replanned',
    payload: { run_id: 'other', replan_run_id: 'r2', from_node: 'a', reason: 'wrong' }
  })

  expect(folded).toBe(started)
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test --prefix ui-tui -- dagRun.test`
Expected: FAIL -- `dag.run_replanned` is not in the `DagEvent` union.

- [ ] **Step 3: Implement**

While you are in this file, its `DagEvent` doc comment says "Any of the three progress events a
DAG run emits" -- stale the moment you add the fourth. Fix it in the same edit.

Add `DagRunReplannedEvent` to the type import and to the union:

```typescript
/** Any of the four progress events a DAG run emits. */
export type DagEvent =
  | DagNodeUpdatedEvent
  | DagRunCompletedEvent
  | DagRunReplannedEvent
  | DagRunStartedEvent
```

Add to `DagRunState`:

```typescript
  /** The run that replaced this one, when the agent replanned it. Arrives after
   * `done`, since the successor has to start before it can be named. */
  replannedInto?: string
```

In `foldDagEvent`, after the `run_started` branch and the run-id guard:

```typescript
  if (event.type === 'dag.run_replanned') {
    return { ...prev, replannedInto: event.payload.replan_run_id }
  }
```

It has to sit after the guard (so another run's event is ignored) and must not touch `done` -- the event arrives on an already-settled run by design.

Add `'dag.run_replanned'` to the `case` list in `app/chatStream.ts:351` so the event reaches the fold at all.

In `components/dagPanel.tsx`, render one line under a replanned run's header, e.g. `replanned into <runId>`. Match the panel's existing label style; do not invent a new colour token.

- [ ] **Step 4: Run the suites**

Run, in this order:

```bash
npm run type-check --prefix ui-tui
npm test --prefix ui-tui -- dagRun.test
npm test --prefix ui-tui -- --pool=forks --no-file-parallelism
```

Expected: all PASS. The full `ui-tui` suite flakes above ~100 files under default worker
parallelism, so run it serially; a green serial run is the signal, and a flaky parallel run is
not evidence of anything.

The serial flag is `--no-file-parallelism`. Installed vitest is 4.1.6, whose CLI has `--pool
<pool>` and `--fileParallelism` and **no `--poolOptions` family at all** -- passing
`--poolOptions.forks.singleFork` fails with `CACError: Unknown option`, a parse error rather
than a test failure. Confirm with `./ui-tui/node_modules/.bin/vitest run --help` if it ever
looks wrong again.

**`type-check` is listed first because it is RED when you start, and closing it is part of this
task.** Task 6 added `DagRunReplannedEvent` to the `TurnEvent` union, and `chatStream.ts` ends
its switch with a deliberate `const exhaustive: never = event` guard whose comment says it
exists precisely so "a new TurnEvent variant lands [and] the type-checker will complain here,
forcing this file to be updated". So the branch carries
`chatStream.ts(408,13): error TS2322: Type 'DagRunReplannedEvent' is not assignable to type
'never'` until you handle the variant. Line 408 is the `default:` arm of the same switch whose
dag case group sits at `:351`. Do not silence the guard.

**Order matters, and the case alone is NOT the fix.** `DagEvent` in `domain/dagRun.ts:21` is a
*closed* union of the three older events, and `recordDagEvent`/`applyDagEvent` are typed against
it. Adding `case 'dag.run_replanned':` to the existing dag group first would narrow `event` to a
type `DagEvent` excludes and produce a **new** error rather than fixing the old one. So do Step 3
first -- widen `DagEvent` and give the fold its branch -- and only then add the case. If you hit
a fresh error on `recordDagEvent(event)`, that is this ordering, not a mistake.

- [ ] **Step 5: Propose a commit**

```bash
/usr/bin/git add ui-tui/src/domain/dagRun.ts ui-tui/src/__tests__/dagRun.test.ts ui-tui/src/app/chatStream.ts ui-tui/src/components/dagPanel.tsx
# propose:
# feat(ui-tui): a replanned graph points at the run that took over
```

---

### Task 8: The web UI shows the chain

**Files:**
- Modify: `ui-web/src/features/dag/types.ts`, `ui-web/src/features/dag/store.ts`, `ui-web/src/features/dag/DagSheet.tsx`, `ui-web/src/features/transcript/store.ts:955` (`dagFeed`)
- Test: `ui-web/src/features/dag/store.test.ts`

**Interfaces:**
- Consumes: `DagRunReplannedEvent` from `../../rpc/generated.ts` (Task 6).
- Produces: `DagRun.replannedInto?: string`.

- [ ] **Step 1: Write the failing test**

```typescript
it('records the successor run, including after the run settled', () => {
  set('c1', runStarted('r1', ['a']))
  applyDagEvent('c1', { type: 'dag.run_completed', payload: { run_id: 'r1', manifest: { files: [] } } })

  applyDagEvent('c1', {
    type: 'dag.run_replanned',
    payload: { run_id: 'r1', replan_run_id: 'r2', from_node: 'a', reason: 'the plan was wrong' }
  })

  expect(run('c1')?.replannedInto).toBe('r2')
})
```

The dispatch entry point is `dagFeed(type, p)`, exported from `ui-web/src/features/transcript/store.ts:955`; `dag.run_started` is its first branch, and it owns the run-id-to-card binding. Add the `dag.run_replanned` branch there and have it update the stored run through `features/dag/store.ts`'s existing `set`/`run` pair. Read `dagFeed` before writing the test -- its `p` is a loose `DagFeedPayload`, not the typed wire event.

- [ ] **Step 2: Run it to verify it fails**

Run: `npm test --prefix ui-web -- features/dag`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add `replannedInto?: string` to the `DagRun` type in `features/dag/types.ts`, add the `dag.run_replanned` branch to `dagFeed`, and set the field on the stored run without disturbing its settled state (the event arrives after `dag.run_completed` by design). Render one line in `DagSheet.tsx` naming the successor; `DagSheet.test.tsx` is the biggest suite in that directory, so check whether a snapshot there needs updating.

- [ ] **Step 4: Run the gates**

Run: `npm test --prefix ui-web -- features/dag` then `npm run type-check --prefix ui-web`
Expected: PASS on both.

Note: the full `ui-web` vitest run is ~270 red at any revision on this box (node v26 + happy-dom), so only an ID-level diff against the base branch means anything. Judge this task on the dag suite plus `type-check`.

- [ ] **Step 5: Propose a commit**

```bash
/usr/bin/git add ui-web/src/features/dag ui-web/src/features/transcript/store.ts
# propose:
# feat(ui-web): a replanned graph points at the run that took over
```

---

### Task 9: What the model and the next reader are told

**Files:**
- Modify: `raven/agent/subagent/dag_runner.py:546-607` (`_exception_report`), `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md:104-122`, `CONTEXT.md:1835-1843` (the `exception` entry) and a new `replan` entry beside it
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: everything above. Produces no code interface.

The report is the only place a model learns this decision exists -- `resolve_dag_node` is hidden from the provider schema. A shipped feature the report does not mention is a feature no model uses.

- [ ] **Step 1: Write the failing test**

**Two conventions in `tests/test_subagent_dag_runner.py` to follow, not fight.** It does not import
`AdjudicationDesk`, `Verdict`, `ReplanPlan`, `REPLAN` or `_exception_report` at module level -- it imports
runner and adjudication internals **inside each test function** (over ten inline
`from raven.agent.subagent.dag_adjudication import AdjudicationDesk` lines, plus
`from raven.agent.subagent.dag_runner import _cascade_failures, _mark_stopped, _tally` at line 4623). Do
the same in every snippet in this task and in Task 3. And `_node_spec` does not exist; `DagNodeSpec` is
imported at line 23, so define it locally:

```python
def _node_spec(node_id: str) -> DagNodeSpec:
    return DagNodeSpec(id=node_id, subagent="x", node_summary="a step", prompt_template="do it")


def test_the_report_offers_all_three_decisions() -> None:
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    report = _exception_report(
        run_id="r1",
        node=_node_spec("a"),
        verdict=Verdict(accomplished=False, what_is_missing="the wrong tool was used"),
        attempt=1,
        remaining=2,
        blocked=["b"],
        timeout_s=600.0,
    )

    assert '"decision": "continue"' in report
    assert '"decision": "abandon"' in report
    assert '"decision": "replan"' in report
    assert '"nodes"' in report, "a replan is useless without the argument that carries the graph"


def test_a_report_with_no_continuations_left_does_not_offer_replan_yet() -> None:
    """The exhausted path is unchanged by this plan. See the open question below."""
    report = _exception_report(
        run_id="r1",
        node=_node_spec("a"),
        verdict=Verdict(accomplished=False, what_is_missing="x"),
        attempt=3,
        remaining=0,
        blocked=[],
        timeout_s=600.0,
    )

    assert '"decision": "replan"' not in report
    assert "the continuation limit is reached" in report
```

**Do not widen the suspension predicate in this task.** `_apply_verdict:672` reads
`suspending = remaining > 0 and desk is not None and deliverable and origin is not None`, so a node out of
continuations has no desk open and cannot be answered at all -- including by a replan. Offering `replan` in
that branch would require dropping `remaining > 0`, which suspends nodes that today fail outright and
changes when a graph pauses. The spec does not settle it, so this plan keeps the current behaviour and the
test above pins it.

**Open question for the maintainer, worth raising before Task 9 is reviewed:** the exhausted-limit text at
`dag_runner.py:576` already ends with "Re-plan if this line matters." That advice dangles today -- no desk is
open, so `resolve_dag_node` answers "no longer waiting for a decision" -- and it will still dangle after this
feature ships, now that "replan" names a real thing the model can be told to do. Fixing it is a small change
to the same predicate, and a real behavioural one. Flag it; do not decide it here.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_subagent_dag_runner.py -x -k report`
Expected: FAIL -- no `replan` in the report.

- [ ] **Step 3: Extend the report**

In `_exception_report`, after the existing invocation paragraph:

```python
    lines.append(
        f'Or replan: tool_call with name "resolve_dag_node" and arguments '
        f'{{"run_id": "{run_id}", "node_id": "{node.id}", "decision": "replan", '
        f'"message": "<why the plan is changing>", "nodes": [<the graph to run instead>]}}. '
        "That stops this run and starts a new one from your nodes. Use it when what is "
        "missing is the plan rather than something you can hand this node: a step that "
        "cannot work as wired, a step nothing in the graph performs, work on the wrong "
        "sub-agent. Nodes this run completed are referenced, not re-declared -- name one in "
        "depends_on and read it with {{ <id>.output }}; everything else needs a new id."
    )
```

- [ ] **Step 4: Update the guide skill**

In `SKILL.md`, add the third bullet after the `abandon` one, and state the criterion where the existing text tells the model to prefer `continue`:

```markdown
- the same with `"decision": "replan"`, plus a `nodes` list, replaces what is left of the
  graph: this run stops and a new one starts from your nodes. Nodes this run completed are
  referenced (`depends_on` plus `{{ <id>.output }}`), never re-declared; every other node
  needs a new id, including a redo of the node that failed.

**Choosing between them:** when you can supply what the report says is missing, `continue`.
When what is missing is the plan itself, `replan`. `abandon` is for work that turns out not
to be needed. Replanning because a node is hard is how a graph loops without progressing --
each replan starts its nodes on a fresh attempt budget, and the only thing that stops the
loop is the session's hourly dispatch limit.
```

- [ ] **Step 5: Update CONTEXT.md**

Fix the `exception` entry -- it says a suspended node waits "to decide whether to continue or abandon it", and there are three choices now. Then add, keeping the file's entry style:

```markdown
**replan** (adjudication decision; `raven/agent/subagent/dag_adjudication.py`) -- the answer
to an exception report that replaces the plan instead of the node: the agent hands
`resolve_dag_node` a new node list, the answered run stops where it is and finalizes, and a
new run starts from that list. Chained rather than spliced -- the successor is a separate
run with its own id and dir, and the old run's `graph.json` carries a reserved `replan` key
naming it. A completed node of the old run is reused by reference (`depends_on` plus
`{{ <id>.output }}`); no id it claimed can be re-declared, so a replan gives up on the
adjudicated node and no attempt budget crosses the boundary.
_Avoid_: "reorchestrate" -- orchestration is what the main agent does with graphs generally;
this names one decision about one graph.
```

Check the placeholder spelling against `dag_graph.py`'s own docstring before committing --
`CONTEXT.md` is the file people quote from, so a malformed `{{ ... }}` here propagates.

- [ ] **Step 6: Run the suites**

Run: `uv run pytest tests/test_subagent_dag_runner.py -x`
Expected: PASS.

- [ ] **Step 7: Lint, then propose a commit**

Run: `make lint-python`

```bash
/usr/bin/git add raven/agent/subagent/dag_runner.py raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md CONTEXT.md tests/test_subagent_dag_runner.py
# propose:
# docs(agent): the exception report and the guide name the third decision
```

---

## Before the merge request

- [ ] `uv run pytest --all-extras -x` -- and compare against a baseline taken on `origin/refactor/raven_v0_2_0` at the same settings. The CLI batch is ~65 red and the full suite carries two known failures on main; only an A/B tells you whether a red is yours.
- [ ] `make lint` and `make check-large-files`
- [ ] `npm run type-check --prefix ui-web`, `npm run lint:rpc --prefix ui-tui`, `npm run gen:check --prefix ui-web`
- [ ] Run the `mr-review-patterns` pre-submit sweep over `git diff origin/refactor/raven_v0_2_0...HEAD`. This is a standing requirement in this repo, not optional.
- [ ] Open the merge request against **`refactor/raven_v0_2_0`**, not `main`, with reviewers `Blockchain-Key` (11992898) and `chandler.zhang` (26716934). The description is ASCII-only and becomes the squash commit body -- grep it for non-ASCII before posting.

## Manual verification

None of the automated tests exercise the live graph panel: the DAG panel is live-event-only and never redraws on resume, so seeing a replan chain in the TUI costs a real `run_subagent_dag` run. Do it once before the merge request:

1. Start `raven tui` from a checkout of this branch, not behind a pipe (`| tee` forces Ink to 80 columns and voids the layout).
2. Submit a two-node graph where the first node cannot succeed (ask it for a file that does not exist).
3. When the exception report arrives, answer with `replan` and a one-node graph that references nothing.
4. Confirm: the old graph settles, a line names the successor, and a second graph draws and runs.

The TUI runs a prebuilt bundle from gitignored `ui-tui/dist/entry.js`, so run `npm run build --prefix ui-tui` first or you will be testing stale code.
