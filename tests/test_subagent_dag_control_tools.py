"""Model-facing controls for an in-flight DAG run (control_tools.py).

cancel_dag / dag_status / resolve_dag_node are schema-hidden tools: the provider
never sees them, so two texts are their whole advertisement -- the DAG tool's
acceptance text, and a suspended node's exception report for resolve_dag_node --
and each names tool_call, the only route by which a model can invoke a tool that
is not in its list. The tools are exercised directly, their conversation scoping
is asserted, their own cross-references are held to naming that route, and the
hiding surface (registry definitions + the tool_search catalog) is pinned to keep
them out of every discovery path except those texts.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.loop.wiring import WiringMixin
from raven.agent.subagent import dag_tool as raven_agent_subagent
from raven.agent.subagent.dag_adjudication import REPLAN, ReplanPlan
from raven.agent.subagent.dag_control_tools import CancelDagTool, DagStatusTool, ResolveDagNodeTool
from raven.agent.subagent.dag_graph import parse_dag_spec
from raven.agent.subagent.dag_live import awaiting_decision, resolve_node
from raven.agent.subagent.dag_reader import DagReadError
from raven.agent.subagent.dag_runner import _exception_report
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.dag_verdict import Verdict
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.tool_search import TOOL_CALL_NAME, ToolCallTool, ToolSearchController
from raven.config.schema import ThirdPartyCliSubagentConfig, ToolSearchConfig
from raven.contracts.tool import Tool
from raven.providers.base import LLMProvider, LLMResponse


class _Registry:
    def __init__(self, tool: Any) -> None:
        self._tool = tool

    def get(self, name: str) -> Any:
        return self._tool if name == "run_subagent_dag" else None


class _Loop:
    """A duck-typed host with the two accessors the tools ask through."""

    def __init__(self, live: set[str] | None = None, tool: Any = None) -> None:
        self._live = live or set()
        self.tools = _Registry(tool)
        self.cancel_calls: list[str] = []

    def active_dag_run_ids(self) -> set[str]:
        return set(self._live)

    def cancel_dag_run(self, run_id: str) -> bool:
        self.cancel_calls.append(run_id)
        return run_id in self._live


class _DagTool:
    def __init__(
        self,
        run: dict[str, Any] | None = None,
        error: str | None = None,
        session_runs: set[str] | None = None,
        fail_read_after: int | None = None,
    ) -> None:
        self._run = run
        self._error = error
        self._session_runs = session_runs
        self._fail_read_after = fail_read_after
        self.reads = 0

    async def read_run(self, run_id: str, session_key: str | None = None) -> dict[str, Any]:
        self.reads += 1
        if self._error is not None:
            raise DagReadError(self._error)
        if self._fail_read_after is not None and self.reads > self._fail_read_after:
            raise DagReadError("gone mid-race")
        if self._run is not None and self._run.get("run_id") == run_id:
            return self._run
        raise DagReadError(f"no readable DAG run for {run_id}")

    async def session_run_ids(self, session_key: str | None = None) -> set[str]:
        return set(self._session_runs or set())


def _finished_run() -> dict[str, Any]:
    long_template = "\n".join(f"line{i}" for i in range(1, 13))
    return {
        "run_id": "r1",
        "dir": "/tmp/runs/r1",
        "nodes_root": "/tmp/nodes",
        "finalized": True,
        "task_summary": "Plan, then write, then review.",
        "files": [
            {
                "node": "a",
                "status": "completed",
                "node_summary": "Draft the plan.",
                "subagent": "Coder",
                "inputs": {"topic": "x"},
                "prompt_template": long_template,
                "prompt_file": "/tmp/nodes/a.prompt.md",
                "instance": "i-1",
                "output_file": "/tmp/nodes/a.out.md",
                "memory_file": "/tmp/nodes/a.memory.json",
                "started_at": 1000,
                "ended_at": 3000,
            },
            {
                "node": "b",
                "status": "failed",
                "subagent": "Writer",
                "inputs": None,
                "prompt_template": None,
                "error": "boom",
            },
        ],
        "terminal_outputs": [],
        "summary": {"total": 2, "completed": 1, "failed": 1, "cancelled": 0, "skipped": 0},
    }


async def test_cancel_dag_reports_the_stop_with_the_nodes() -> None:
    tool = CancelDagTool(loop=_Loop(live={"r1"}, tool=_DagTool(_finished_run())))
    tool.set_context("web", "chat-1", "sess-1")

    out = await tool.execute("r1")

    assert "Cancellation requested for DAG run r1" in out
    assert "nothing further is announced" in out
    assert "task_summary: Plan, then write, then review." in out
    assert "- a [completed]" in out
    assert "- b [failed]" in out


async def test_cancel_dag_reports_the_head_alone_when_the_run_cannot_be_read_back() -> None:
    # Ownership check reads once; the reconciled read after the cancel is the
    # one that fails mid-race. The head must stand on its own then.
    tool = CancelDagTool(loop=_Loop(live={"r1"}, tool=_DagTool(_finished_run(), fail_read_after=1)))

    out = await tool.execute("r1")

    assert "Cancellation requested for DAG run r1" in out
    assert "- a [" not in out


async def test_cancel_dag_reports_when_nothing_matches() -> None:
    # Owned (resolves under this conversation) but not running.
    out = await CancelDagTool(loop=_Loop(tool=_DagTool(_finished_run()))).execute("r1")

    assert "No in-flight DAG run r1 to cancel" in out
    assert 'tool_call name "dag_status" with no run_id' in out


async def test_cancel_dag_refuses_when_ownership_cannot_be_resolved() -> None:
    # No graph tool registered means no session-scoped reader, so the gate must
    # fail closed rather than signal a cancel it cannot attribute.
    host = _Loop(live={"r1"})
    out = await CancelDagTool(loop=host).execute("r1")

    assert "Cannot cancel DAG run r1" in out
    assert "nothing was signalled" in out
    assert host.cancel_calls == []


async def test_cancel_dag_refuses_a_run_from_another_conversation() -> None:
    host = _Loop(live={"theirs"}, tool=_DagTool(_finished_run()))
    out = await CancelDagTool(loop=host).execute("theirs")

    assert "No DAG run theirs in this conversation" in out
    assert host.cancel_calls == [], "a foreign run id must never reach the cancel"


async def test_dag_status_lists_the_runs_in_flight_and_points_at_one() -> None:
    host = _Loop(live={"b-run", "a-run"}, tool=_DagTool(session_runs={"a-run", "b-run"}))
    out = await DagStatusTool(loop=host).execute()

    assert out == (
        'In-flight DAG runs: a-run, b-run. Call tool_call name "dag_status" '
        'arguments {"run_id": "<run_id>"} for one run\'s per-node status.'
    )


async def test_dag_status_listing_is_scoped_to_the_conversation() -> None:
    host = _Loop(live={"mine", "theirs"}, tool=_DagTool(session_runs={"mine"}))
    out = await DagStatusTool(loop=host).execute()

    assert "In-flight DAG runs: mine." in out
    assert "theirs" not in out


async def test_dag_status_listing_degrades_to_empty_when_the_index_is_unknown() -> None:
    host = _Loop(live={"mine"}, tool=_DagTool(session_runs=None))
    out = await DagStatusTool(loop=host).execute()

    assert out == "No DAG runs are currently in flight."


async def test_dag_status_reports_nothing_when_idle() -> None:
    out = await DagStatusTool(loop=_Loop()).execute()

    assert out == "No DAG runs are currently in flight."


async def test_dag_status_reads_one_run_back_with_every_node_field() -> None:
    tool = DagStatusTool(loop=_Loop(tool=_DagTool(_finished_run())))
    tool.set_context("web", "chat-1", "sess-1")

    out = await tool.execute("r1")

    assert "DAG run r1: 1 completed, 1 failed, 0 cancelled, 0 skipped (of 2)." in out
    assert "task_summary: Plan, then write, then review." in out
    assert "- a [completed]" in out
    assert "    node_summary: Draft the plan." in out
    assert "    subagent: Coder" in out
    assert '    inputs: {"topic": "x"}' in out
    assert "    instance: i-1" in out
    assert "    output_file: /tmp/nodes/a.out.md" in out
    assert "    memory_file: /tmp/nodes/a.memory.json" in out
    assert "    started_at: 1000" in out
    assert "    ended_at: 3000" in out
    # The long template is cut to its head and points at the full prompt file.
    assert "    prompt_template: line1" in out
    assert "line10" in out
    assert "line11" not in out
    assert "truncated, 2 more lines" in out
    assert "full prompt in /tmp/nodes/a.prompt.md" in out
    # A node whose fields never got written renders (none) for each of them.
    assert "- b [failed]" in out
    assert "    node_summary: (none)" in out
    assert "    inputs: (none)" in out
    assert "    prompt_template: (none)" in out
    assert "    instance: (none)" in out
    assert "    error: boom" in out
    assert "Run dir: /tmp/runs/r1" in out


async def test_dag_status_derives_the_prompt_path_when_truncated_without_one() -> None:
    run = _finished_run()
    run["files"][0]["prompt_file"] = None
    tool = DagStatusTool(loop=_Loop(tool=_DagTool(run)))

    out = await tool.execute("r1")

    assert "truncated, 2 more lines" in out
    assert "full prompt in /tmp/nodes/a.prompt.md" in out


async def test_dag_status_reports_an_unknown_run() -> None:
    tool = DagStatusTool(loop=_Loop(tool=_DagTool(error="no readable DAG run")))

    out = await tool.execute("r9")

    assert "No DAG run r9 found" in out
    assert 'tool_call name "dag_status" with no run_id' in out


class _ResolvableDagTool(_DagTool):
    """A run_subagent_dag double: read_run succeeds, resolve_node is recorded, and a
    foreground run hands scripted events to await_run. `calls` records the replan
    hops in the order they ran, for tests that check emit_replanned lands between
    resolve_node and await_finalized rather than just checking each one happened."""

    def __init__(
        self,
        resolves: bool = True,
        *,
        foreground: bool = False,
        events: list[Any] | None = None,
        plan: Any = None,
        raise_on_emit: BaseException | None = None,
        raise_on_finalize: BaseException | None = None,
        awaiting: bool = True,
    ) -> None:
        super().__init__(_finished_run())
        self._resolves = resolves
        self._foreground = foreground
        self.events: list[Any] = list(events or [])
        self.resolved: tuple[str, str, str, str | None] | None = None
        self.awaited: list[str] = []
        self.aborted: list[str] = []
        self.release_taker = asyncio.Event()
        self._plan = plan
        self._raise_on_emit = raise_on_emit
        self._raise_on_finalize = raise_on_finalize
        self.calls: list[str] = []
        self.is_foreground_calls: list[str] = []
        self.awaiting_calls: list[tuple[str, str]] = []
        self._awaiting = awaiting
        self.interrupted: tuple[str, Any, str] | None = None

    def is_awaiting_decision(self, run_id: str, node_id: str) -> bool:
        self.awaiting_calls.append((run_id, node_id))
        return self._awaiting

    def resolve_node(self, run_id: str, node_id: str, decision: str, message: str | None, plan: Any = None) -> bool:
        # A replan's plan is not folded into `resolved`: every existing caller that
        # drives continue/abandon through this double asserts the 4-tuple exactly,
        # and none of them cares that the parameter now exists.
        self.calls.append("resolve_node")
        self.resolved = (run_id, node_id, decision, message)
        return self._resolves

    def is_foreground(self, run_id: str) -> bool:
        self.is_foreground_calls.append(run_id)
        return self._foreground and run_id == "r1"

    def active_run_ids(self) -> list[str]:
        return ["r1"]

    async def await_run(self, run_id: str) -> Any:
        self.awaited.append(run_id)
        if not self.is_foreground(run_id):
            return None
        if self.events:
            return self.events.pop(0)
        await self.release_taker.wait()
        return None

    def abort_run(self, run_id: str) -> None:
        self.aborted.append(run_id)

    def render_event(self, run_id: str, event: Any) -> str:
        return f"RENDERED {run_id} {event}"

    async def prepare_replan(
        self, run_id: str, from_node: str, nodes: list, reason: str, session_key: str | None, live: dict
    ) -> Any:
        self.calls.append("prepare_replan")
        return self._plan

    async def emit_replanned(self, run_id: str, plan: Any) -> None:
        self.calls.append("emit_replanned")
        if self._raise_on_emit is not None:
            raise self._raise_on_emit

    async def await_finalized(self, run_id: str) -> None:
        self.calls.append("await_finalized")
        if self._raise_on_finalize is not None:
            raise self._raise_on_finalize

    async def record_interrupted_replan(self, run_id: str, plan: Any, reason: str) -> None:
        self.calls.append("record_interrupted_replan")
        self.interrupted = (run_id, plan, reason)

    async def start_replan(self, run_id: str, plan: Any, *, bound: bool = False) -> Any:
        self.calls.append("start_replan")
        return f"replan started as {plan.run_id}"


class _LoopWithRun(_Loop):
    """A loop whose registered graph tool owns run r1 and answers resolve_node."""

    def __init__(self, resolves: bool = True, **kwargs: Any) -> None:
        self._dag_tool = _ResolvableDagTool(resolves=resolves, **kwargs)
        super().__init__(tool=self._dag_tool)

    @property
    def resolved(self) -> tuple[str, str, str, str | None] | None:
        return self._dag_tool.resolved


class _LoopWithoutRun(_Loop):
    """A loop whose registered graph tool cannot resolve run r1 for this session."""

    def __init__(self) -> None:
        super().__init__(tool=_DagTool(error="no readable DAG run for r1"))


class _NonOwningDagTool(_ResolvableDagTool):
    """Registered on the model's table, but never dispatched r1: read_run still
    answers from the shared session index (a real host's two instances agree on
    that), but active_run_ids admits nothing -- the shape owning_tool has to
    skip past rather than settle for."""

    def active_run_ids(self) -> list[str]:
        return []


class _LoopWithSecondOwner(_Loop):
    """Two graph tool instances, the shape dag_live's module docstring describes:
    one on the model's table, one privately dispatching r1 (a playbook engine's
    private instance, in reality). Both read r1's history the same way, but only
    the private instance's active_run_ids admits owning it, so owning_tool's
    fan-out -- not the registered-tool shortcut every _LoopWithRun test above
    takes -- is what every prepare_replan / is_foreground / emit_replanned /
    await_finalized / start_replan call below has to go through.
    """

    def __init__(self, resolves: bool = True, plan: Any = None) -> None:
        self._registered = _NonOwningDagTool(resolves=resolves, plan=plan)
        self._owner = _ResolvableDagTool(resolves=resolves, foreground=True, plan=plan)
        super().__init__(tool=self._registered)

    def dag_tools(self) -> list[Any]:
        return [self._registered, self._owner]


async def test_resolve_requires_a_message_when_continuing():
    # "message" alone also matches the CONTINUE success text ("...with your
    # message."), so pin the exact guard text and confirm resolve_node was
    # never reached -- either alone would already catch a deleted guard.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="continue")
    assert out == (
        "Error: continue on node 'a' needs a message telling it what to do differently. "
        "Supply what the report said was missing."
    )
    assert loop.resolved is None, "an unmet guard must never reach resolve_node"


async def test_resolve_rejects_an_unknown_decision():
    # A loose "continue" and "abandon" substring check also matches the abandon
    # success text ("...continues... abandoned..."), so pin the exact guard
    # text and confirm resolve_node was never reached.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="maybe", message="x")
    assert out == "Error: decision must be 'continue', 'abandon' or 'replan', not 'maybe'."
    assert loop.resolved is None, "an unmet guard must never reach resolve_node"


async def test_resolve_refuses_a_run_this_conversation_does_not_own():
    tool = ResolveDagNodeTool(loop=_LoopWithoutRun())
    out = await tool.execute(run_id="r1", node_id="a", decision="abandon")
    assert "No DAG run r1 in this conversation" in out


async def test_resolve_says_when_nobody_is_waiting():
    tool = ResolveDagNodeTool(loop=_LoopWithRun(resolves=False))
    out = await tool.execute(run_id="r1", node_id="a", decision="abandon")
    assert "no longer waiting" in out.lower()


async def test_resolve_confirms_a_continue():
    # "a" in out matches almost any sentence, including the abandon text, so it
    # would not notice the continue/abandon response branches being swapped.
    # Pin the exact continue-only wording instead.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")
    assert out == "Node 'a' of run r1 will run again with your message."
    assert loop.resolved == ("r1", "a", "continue", "use staging")


async def test_resolve_blocks_only_for_a_bound_foreground_run():
    background = ResolveDagNodeTool(loop=_LoopWithRun())
    foreground = ResolveDagNodeTool(loop=_LoopWithRun(foreground=True))

    assert background.blocking_for({"run_id": "r1"}) is False
    assert foreground.blocking_for({"run_id": "r1"}) is True
    assert foreground.blocking_for({"run_id": "other"}) is False
    assert ResolveDagNodeTool(loop=_Loop()).blocking_for({"run_id": "r1"}) is False, (
        "no graph tool, nothing to block on"
    )


async def test_a_foreground_resolve_returns_the_next_event_rendered():
    loop = _LoopWithRun(foreground=True, events=["next report"])
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")

    assert loop.resolved == ("r1", "a", "continue", "use staging")
    assert loop._dag_tool.awaited == ["r1"]
    assert out == "RENDERED r1 next report"


async def test_a_background_resolve_does_not_wait():
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="continue", message="use staging")

    assert out == "Node 'a' of run r1 will run again with your message."
    assert loop._dag_tool.awaited == ["r1"], "asked, and told there is nothing to wait on"


async def test_a_refused_resolve_never_waits():
    loop = _LoopWithRun(resolves=False, foreground=True, events=["would be wrong"])
    tool = ResolveDagNodeTool(loop=loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="abandon")

    assert "no longer waiting" in out.lower()
    assert loop._dag_tool.awaited == []


async def test_cancelling_a_waiting_resolve_aborts_the_run():
    loop = _LoopWithRun(foreground=True)
    tool = ResolveDagNodeTool(loop=loop)
    call = asyncio.create_task(tool.execute(run_id="r1", node_id="a", decision="abandon"))
    await asyncio.sleep(0)
    assert loop._dag_tool.awaited == ["r1"]

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert loop._dag_tool.aborted == ["r1"]


class _Hidden(Tool):
    @property
    def name(self) -> str:
        return "hidden_tool"

    @property
    def description(self) -> str:
        return "Hidden from the schema."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "hidden ran"


class _HiddenTwo(_Hidden):
    @property
    def name(self) -> str:
        return "hidden_tool_two"


class _Dummy(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "dummy"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


async def test_hidden_tools_leave_the_schema_but_stay_callable_and_unsearchable() -> None:
    registry = ToolRegistry()
    registry.register(_Hidden())
    registry.hide_from_schema("hidden_tool")

    assert "hidden_tool" not in {t["function"]["name"] for t in registry.get_definitions()}

    ctrl = ToolSearchController(registry, always_visible=set())
    ctrl.refresh()
    assert ctrl.search("hidden tool") == []
    assert ctrl.resolve_target("hidden_tool").tool is not None

    assert await registry.execute("hidden_tool", {}) == "hidden ran"


def test_tool_call_availability_answers_whether_tool_call_ships() -> None:
    registry = ToolRegistry()
    registry.register(_Hidden())
    registry.register(_HiddenTwo())
    # A threshold far above this catalog: the fold would never engage here, and
    # the answer must not depend on that -- a schema-hidden tool needs the name
    # route at every catalog size, which is why the predicate stopped reading it.
    ctrl = ToolSearchController(registry, always_visible=set(), compaction_threshold=99)

    # Without the meta-tool registered there is no route.
    assert ctrl.tool_call_available() is False

    registry.register(ToolCallTool(ctrl))
    assert ctrl.tool_call_available() is True

    # An operator off switch on the meta-tool closes the route again.
    registry.set_withheld_source(lambda: frozenset({TOOL_CALL_NAME}))
    assert ctrl.tool_call_available() is False


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


async def test_the_loop_registers_all_three_tools_outside_the_schema(workspace: Path) -> None:
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True, tool_search_config=ToolSearchConfig(enabled=True)),
    )

    assert loop.tools.has("cancel_dag")
    assert loop.tools.has("dag_status")
    assert loop.tools.has("resolve_dag_node")
    names = {t["function"]["name"] for t in loop.tools.get_definitions()}
    assert "cancel_dag" not in names
    assert "dag_status" not in names
    assert "resolve_dag_node" not in names

    out = await loop.tools.execute("cancel_dag", {"run_id": "r1"})
    assert "No DAG run r1 in this conversation" in out

    out = await loop.tools.execute("resolve_dag_node", {"run_id": "r1", "node_id": "a", "decision": "abandon"})
    assert "No DAG run r1 in this conversation" in out

    assert loop.dag_control_reachable() is True, "tool_call carries the route to the hidden tools"

    for i in range(51):
        loop.tools.register(_Dummy(f"dummy_{i}"))
    assert loop.dag_control_reachable() is True, "and still does once the catalog is above the fold"


async def test_a_default_loop_can_still_reach_the_hidden_tools(workspace: Path) -> None:
    # Progressive disclosure is off by default, which used to take tool_call with
    # it and leave all three controls advertised but unnameable -- a suspended
    # node then waited out its whole adjudication timeout for a decision the
    # model had no way to send.
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        # no tool_search_config: the default deploy
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    assert loop.tools.has("tool_call")
    assert loop.strategies.get("tool_search") is None, "the fold itself stays off"
    assert loop.dag_control_reachable() is True


_HIDDEN = ("cancel_dag", "dag_status", "resolve_dag_node")


async def _cross_referencing_texts(tmp_path) -> list[tuple[str, str]]:
    """Every model-facing string that points a model at one of the hidden controls.

    The graph tool's acceptance text belongs here as much as the control tools' own
    replies: this file's docstring calls those two texts the whole advertisement the
    controls get, and a guard that reaches only one of them lets the defect this
    branch removes come back in the other with the suite green.
    """
    graph = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="echo", command="cat")],
        control_reachable=lambda: True,
    )
    graph.set_context("web", "default", "web:xref")
    accepted = await graph.execute(
        task_summary="run the graph under test",
        nodes=[{"id": "a", "subagent": "echo", "node_summary": "say hello", "prompt_template": "hi"}],
    )
    guide = (
        Path(raven_agent_subagent.__file__).parents[2]
        / "memory_engine"
        / "skills"
        / "subagent-dag-orchestration"
        / "SKILL.md"
    )
    texts = [
        ("graph tool: background acceptance text", accepted.model_text),
        # run_subagent_dag makes reading this a required first step, so it is the
        # first place a model learns how to answer a suspended node -- earlier than
        # the report, and wrong for longer if it disagrees with it.
        ("orchestration guide: the required first read", guide.read_text(encoding="utf-8")),
        # The text a suspended node hands the model, and the one this branch is
        # named after. It renders without a tool instance, so it joins the roster
        # the same way the guide does.
        (
            "exception report: what a suspended node hands the model",
            _exception_report(
                run_id="r1",
                node=parse_dag_spec(
                    {
                        "task_summary": "one node",
                        "nodes": [{"id": "a", "subagent": "echo", "node_summary": "first", "prompt_template": "do a"}],
                    }
                ).nodes[0],
                verdict=Verdict(accomplished=False, category="tool_failure", what_is_missing="a token"),
                attempt=1,
                remaining=2,
                blocked=["b"],
                timeout_s=600.0,
            ),
        ),
        ("cancel: unresolvable ownership", await CancelDagTool(loop=_Loop(live={"r1"})).execute("r1")),
        ("cancel: nothing matches", await CancelDagTool(loop=_Loop(tool=_DagTool(_finished_run()))).execute("r9")),
        (
            "status: listing",
            await DagStatusTool(loop=_Loop(live={"a-run"}, tool=_DagTool(session_runs={"a-run"}))).execute(),
        ),
        ("status: unknown run", await DagStatusTool(loop=_Loop(tool=_DagTool(error="gone"))).execute("r1")),
        (
            "resolve: node no longer waiting",
            await ResolveDagNodeTool(loop=_LoopWithRun(resolves=False)).execute(
                run_id="r1", node_id="a", decision="abandon"
            ),
        ),
        (
            "resolve: abandoned",
            await ResolveDagNodeTool(loop=_LoopWithRun()).execute(run_id="r1", node_id="a", decision="abandon"),
        ),
        (
            "resolve: decision parameter",
            ResolveDagNodeTool(loop=_LoopWithRun()).parameters["properties"]["decision"]["description"],
        ),
    ]
    return [(label, text) for label, text in texts if any(name in text for name in _HIDDEN)]


async def test_every_cross_reference_between_the_controls_names_the_route(tmp_path) -> None:
    """These tools point the model at each other, and none of them is in its schema.

    Naming a sibling as a bare call spells an invocation the model cannot issue --
    the same defect that made a suspended node wait out its adjudication timeout,
    one layer in. A reader here has necessarily reached this tool through
    ``tool_call`` already, so the fix is consistency of spelling rather than a
    repeated explanation.
    """
    referencing = await _cross_referencing_texts(tmp_path)
    assert len(referencing) >= 9, "the sites under test must actually still cross-reference"

    offenders = [label for label, text in referencing if "tool_call" not in text]
    assert offenders == [], f"these name a schema-hidden tool without naming the route: {offenders}"


async def test_no_cross_reference_spells_a_bare_call_syntax(tmp_path) -> None:
    """`dag_status("r1")` reads as a callable form. There is no such call to make."""
    for label, text in await _cross_referencing_texts(tmp_path):
        for name in _HIDDEN:
            # The offending line, not the whole text: one of these is a file.
            offending = [ln.strip() for ln in text.splitlines() if f'{name}("' in ln]
            assert not offending, f"{label} spells a call syntax the model cannot use: {offending[:3]}"


def test_the_resolve_tool_parks_its_taker_before_it_yields() -> None:
    """A structural claim, read structurally, because behaviour cannot pin it.

    `_retire` drops a finished run's outbox from the index `release_turn` reaches
    runs through, so a run that completes before a taker exists has nowhere left to
    deliver its result. Nothing opens that window because this tool signals the desk
    and enters `await_run` with no yield between the two.

    `test_a_resolve_through_the_control_tool_is_handed_the_run_it_completes` drives
    that call site and catches a yield that lasts long enough to lose the race. It
    cannot catch every yield, and the claim is about every yield: `await
    asyncio.sleep(0)` inserted here passes a single tick, the run has not finished,
    the outbox is still indexed, and the delivery test stays green while the
    invariant it stands for is gone. So this one reads the source instead -- the
    first await after the resolve must be the take itself.
    """
    import ast
    from pathlib import Path

    import raven

    module = Path(raven.__file__).parent / "agent" / "subagent" / "dag_control_tools.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    execute = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "ResolveDagNodeTool"
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute"
    )

    def _calls(node: ast.AST, name: str) -> list[ast.Call]:
        return [
            n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        ]

    resolve_calls = _calls(execute, "resolve_node")
    (take,) = _calls(execute, "await_run")
    # The replan branch calls resolve_node too, to fold its plan into the same
    # decision record, but that call returns before the tail's await_run is ever
    # reached, so both calls precede the take -- this test's claim is about the
    # continue/abandon call, the textually-last one, immediately before it.
    resolve = max(resolve_calls, key=lambda c: c.lineno)
    assert resolve.lineno < take.lineno, "the continue/abandon resolve_node call must precede its own await_run take"
    awaits = sorted((n for n in ast.walk(execute) if isinstance(n, ast.Await)), key=lambda n: (n.lineno, n.col_offset))
    after_resolve = [n for n in awaits if n.lineno > resolve.lineno]

    assert after_resolve, "the taker's own await is missing, so this test is reading the wrong function"
    first = after_resolve[0]
    assert first.value is take, (
        f"a yield was added between the resolve and the take, at line {first.lineno}: "
        "the run can finish there with no taker registered, and `_retire` then drops the outbox "
        "it would have delivered into. Either keep them adjacent or stop claiming they are."
    )


async def test_preflight_refuses_a_disabled_agent_before_any_dispatch(tmp_path) -> None:
    """The refusal `_execute` produced inline must survive the extraction verbatim."""
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="coder", command="true", enabled=False)],
    )
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


def _resolve_tool(loop: Any = None) -> ResolveDagNodeTool:
    if loop is None:
        loop = _LoopWithRun()
    elif hasattr(loop, "node_schema"):
        # _registered_tool looks the graph tool up as loop.tools.get("run_subagent_dag"),
        # so a bare tool must be wrapped in a loop or the schema degrades to {"type": "object"}.
        loop = _Loop(tool=loop)
    tool = ResolveDagNodeTool(loop)
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
    return SubAgentDagTool(
        workspace=Path(tempfile.gettempdir()),
        agents=[ThirdPartyCliSubagentConfig(name="x", command="true")],
    )


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


def _replan_plan() -> ReplanPlan:
    """A minimal, equality-stable plan: these tests check pass-through, not content."""
    return ReplanPlan(
        run_id="r2",
        from_node="a",
        reason="the plan was wrong",
        nodes=(),
        backends={},
        auto_instances=frozenset(),
        notices=(),
    )


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


async def test_the_real_loop_hop_forwards_the_plan_too() -> None:
    """`_LoopWithMethod` above proves dag_live.resolve_node finds and calls a loop's
    resolve_dag_node when one exists; it never runs the real method's body, because
    that class hand-rolls its own copy rather than inheriting one. This subclasses
    the real `WiringMixin` and stubs only `dag_tools()` -- the one collaborator its
    actual resolve_dag_node touches -- so the forwarding call in that method's own
    body is what executes. Dropping `plan` from that call turns this test red; the
    other test would not notice.
    """
    seen: list[Any] = []

    class _Owner:
        def resolve_node(self, run_id, node_id, decision, message, plan=None):
            seen.append(plan)
            return True

    class _RealMixinHost(WiringMixin):
        def __init__(self, tool: Any) -> None:
            self._tool = tool

        def dag_tools(self):
            return [self._tool]

    assert resolve_node(_RealMixinHost(_Owner()), "r1", "a", REPLAN, "wrong", _replan_plan()) is True
    assert seen == [_replan_plan()], "the real resolve_dag_node must not swallow the plan"


async def test_a_successful_replan_emits_between_resolve_and_await_finalized() -> None:
    """The wire event fires once resolve_node succeeds, before await_finalized --
    the old run has not wound down yet at that point, which is what the web UI's
    live tracking (keyed by run_id, dropped on that run's own completion) needs."""
    plan = _replan_plan()
    loop = _LoopWithRun(plan=plan)
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == [
        "prepare_replan",
        "resolve_node",
        "emit_replanned",
        "await_finalized",
        "start_replan",
    ]
    assert "replan started" in out


async def test_a_replan_nobody_waits_for_never_emits() -> None:
    """resolve_node returning False is a real, tested outcome elsewhere (an answer
    for a node that already got one) -- emitting for it would announce a replan
    that never happened, and nothing durable corrects that claim on this path."""
    plan = _replan_plan()
    loop = _LoopWithRun(resolves=False, plan=plan)
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == ["prepare_replan", "resolve_node"]
    assert "no longer waiting for a decision" in out


async def test_a_replan_for_a_node_nobody_awaits_is_refused_before_it_costs_anything() -> None:
    """The cost of a replan is paid inside `prepare_replan` -- the confirm question,
    the dispatch quota, the minted instances -- and `resolve_node` is only reached
    afterwards. So a node that is plainly not suspended has to be turned away
    before that call, or a replan aimed at the wrong id spends a dispatch from the
    session's hourly budget and dispatches nothing: enough of those and a
    legitimate replan is refused for a budget the model never got to use.

    Distinct from `test_a_replan_nobody_waits_for_never_emits` above, which is the
    race -- the node stopped waiting *while* the replacement graph was being
    validated. That one still has to pay, and still has to refuse.
    """
    loop = _LoopWithRun(awaiting=False, plan=_replan_plan())
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == [], "nothing that costs budget may run for a node nobody is waiting on"
    assert loop.resolved is None
    assert "no longer waiting for a decision" in out
    assert "still running as submitted" in out


async def test_the_two_not_waiting_refusals_read_identically() -> None:
    """One situation, one sentence: the model cannot act differently on "refused
    before validation" than on "refused after it", and a second wording would only
    invite it to try to."""
    early = await _resolve_tool(_LoopWithRun(awaiting=False, plan=_replan_plan())).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )
    raced = await _resolve_tool(_LoopWithRun(resolves=False, plan=_replan_plan())).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert early == raced


async def test_a_graph_tool_that_cannot_say_whether_a_node_waits_still_replans() -> None:
    """The predicate is advisory, like every other duck-typed hop in this module:
    a host built before it existed must keep replanning rather than have every
    replan refused by a missing attribute. `awaiting_decision` answers None there,
    which is why it is three-valued and why the caller tests `is False`."""

    class _NoPredicate(_ResolvableDagTool):
        is_awaiting_decision = None

    loop = _Loop(tool=_NoPredicate(plan=_replan_plan()))
    assert awaiting_decision(loop, "r1", "a") is None

    out = await _resolve_tool(loop).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert "replan started" in out


async def test_the_pre_check_asks_every_instance_the_hand_off_would() -> None:
    """The shape dag_live's module docstring describes: the run is held by the
    playbook engine's private instance, not by the one on the model's table.
    `WiringMixin.resolve_dag_node` answers from whichever instance holds it, so a
    pre-check that asked only the registered tool -- or only `owning_tool`, which
    falls back to it when no instance claims the run -- would refuse a replan the
    hand-off was going to accept.
    """

    class _Loop2(_Loop):
        def __init__(self) -> None:
            self._registered = _NonOwningDagTool(awaiting=False, plan=_replan_plan())
            self._owner = _ResolvableDagTool(awaiting=True, plan=_replan_plan())
            super().__init__(tool=self._registered)

        def dag_tools(self) -> list[Any]:
            return [self._registered, self._owner]

    loop = _Loop2()
    assert awaiting_decision(loop, "r1", "a") is True, "one instance saying yes settles it"

    out = await _resolve_tool(loop).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert "replan started" in out


async def test_an_instance_that_raises_does_not_refuse_the_replan() -> None:
    """Same contract as every other helper in dag_live: advisory, never fatal. A
    raising instance must not be read as "not waiting" -- that would turn a broken
    predicate into a refusal the model cannot act on."""

    class _Raises(_ResolvableDagTool):
        def is_awaiting_decision(self, run_id: str, node_id: str) -> bool:
            raise RuntimeError("boom")

    loop = _Loop(tool=_Raises(plan=_replan_plan()))
    assert awaiting_decision(loop, "r1", "a") is None, "an instance that cannot answer has not answered no"

    out = await _resolve_tool(loop).execute(
        run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")]
    )

    assert "replan started" in out


async def test_continue_and_abandon_do_not_consult_the_predicate() -> None:
    """Neither charges anything, so neither needs the pre-check -- and adding one
    would turn `resolve_node`'s own False into a second, earlier refusal for a
    decision that was always free to attempt."""
    for decision, kwargs in (("continue", {"message": "try again"}), ("abandon", {})):
        loop = _LoopWithRun(awaiting=False)
        out = await _resolve_tool(loop).execute(run_id="r1", node_id="a", decision=decision, **kwargs)

        assert loop._dag_tool.awaiting_calls == [], decision
        assert "no longer waiting" not in out, decision


async def test_a_replan_resolves_through_the_owning_tool_not_just_the_registered_one() -> None:
    """dag_live's module docstring: two tool instances can be dispatching runs at
    once, and the registered one only shares the on-disk session index with the
    other -- its in-memory bookkeeping (_outboxes, _runs, _cancels) knows nothing
    about a run the other instance actually dispatched. Routing prepare_replan,
    is_foreground, emit_replanned, await_finalized and start_replan through the
    merely-registered instance instead of owning_tool's fan-out would silently
    read that instance's own empty bookkeeping (is_foreground always False,
    await_finalized returning at once with nothing to wait for) rather than
    raising, so this pins *which object* receives each call, not just that the
    calls happened -- the registered-tool shortcut every _LoopWithRun test above
    takes would pass this assertion trivially, since there the two are the same
    object.
    """
    plan = _replan_plan()
    loop = _LoopWithSecondOwner(plan=plan)
    tool = _resolve_tool(loop)

    out = await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._owner.calls == ["prepare_replan", "emit_replanned", "await_finalized", "start_replan"]
    assert loop._owner.is_foreground_calls == ["r1"]
    assert loop._registered.calls == ["resolve_node"]
    assert loop._registered.is_foreground_calls == []
    assert "replan started" in out


async def test_an_interrupted_emit_still_records_the_link_before_reraising() -> None:
    """The stretch between resolve_node succeeding and start_replan itself is not
    covered by start_replan's own _record_link -- emit_replanned and
    await_finalized run before it, and an interruption there (a /stop cancelling
    this call is realistic) must not leave the old run's graph.json silent about
    a replan the desk already answered. record_interrupted_replan has to fire
    with the exact reason, and the exception still has to propagate rather than
    being swallowed by that bookkeeping.
    """
    plan = _replan_plan()
    loop = _LoopWithRun(plan=plan, raise_on_emit=RuntimeError("boom"))
    tool = _resolve_tool(loop)

    with pytest.raises(RuntimeError, match="boom"):
        await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == ["prepare_replan", "resolve_node", "emit_replanned", "record_interrupted_replan"]
    assert loop._dag_tool.interrupted == (
        "r1",
        plan,
        "Interrupted between the node hand-off and starting the replan.",
    )


async def test_a_cancelled_await_finalized_still_records_the_link_and_reraises() -> None:
    """CancelledError is the realistic shape of the interruption record_interrupted_replan
    exists for (a /stop mid-wait): it still has to propagate, not be swallowed by
    the wind-down bookkeeping, or the caller waiting on this call would never
    learn the turn was cancelled.
    """
    plan = _replan_plan()
    loop = _LoopWithRun(plan=plan, raise_on_finalize=asyncio.CancelledError())
    tool = _resolve_tool(loop)

    with pytest.raises(asyncio.CancelledError):
        await tool.execute(run_id="r1", node_id="a", decision="replan", message="wrong", nodes=[_node("fresh")])

    assert loop._dag_tool.calls == [
        "prepare_replan",
        "resolve_node",
        "emit_replanned",
        "await_finalized",
        "record_interrupted_replan",
    ]
    assert loop._dag_tool.interrupted == (
        "r1",
        plan,
        "Interrupted between the node hand-off and starting the replan.",
    )


async def test_resolve_accepts_action_as_the_name_the_model_reaches_for():
    # Three runs in a row (2026-09-03/04) the main agent wrote `action` and lost
    # a call to "missing required decision". The schema keeps `decision`; the
    # alias is accepted and lands the same resolve.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", action="abandon")
    assert loop.resolved is not None, "the alias must reach resolve_node"
    assert "abandon" in out
    assert "decision" not in tool.parameters["required"]


async def test_resolve_with_neither_name_says_which_field_is_missing():
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a")
    assert out == "Error: decision is required: 'continue', 'abandon' or 'replan'."
    assert loop.resolved is None


class TestTheHiddenControlsAdvertiseTheirRealSchema:
    """These three are withheld from the provider schema, so the only thing the
    model ever reads about them is another tool's result text. That text used to
    retell the arguments by hand in three places, and a retelling is not a
    schema: the model reached for `action` where the tool declares `decision`.
    """

    def test_resolve_declares_its_schema_dynamic(self) -> None:
        """Its node shape carries a roster the agent table can change at runtime,
        so the snapshot the registry takes at admission would go stale."""
        from raven.agent.tools.registry import admit_tool

        assert admit_tool(ResolveDagNodeTool(loop=None)).schema_dynamic
        # The two static ones make no such claim, and should not.
        assert not admit_tool(CancelDagTool(loop=None)).schema_dynamic
        assert not admit_tool(DagStatusTool(loop=None)).schema_dynamic

    def test_the_advert_carries_every_field_the_tool_declares(self) -> None:
        """Asserted against the schema, not a literal: rename a field and this
        fails, instead of the advertisement quietly describing the old one."""
        from raven.agent.subagent.dag_control_advert import render

        tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
        text = render(tool.to_schema())

        assert text is not None
        declared = tool.parameters["properties"]
        assert set(declared) >= {"run_id", "node_id", "decision", "message", "nodes"}, (
            "an empty or shrunken property set would make the loop below vacuous"
        )
        for field in declared:
            assert f'"{field}"' in text, f"{field} is declared but not advertised"
        for decision in tool.parameters["properties"]["decision"]["enum"]:
            assert decision in text

    def test_the_advert_points_at_the_graph_tools_node_shape_instead_of_copying_it(self) -> None:
        """`run_subagent_dag` is not hidden, so its node shape is already in the
        model's tool list; inlining it again cost ~845 tokens to repeat that."""
        from raven.agent.subagent.dag_control_advert import render

        tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
        inlined = tool.to_schema()["function"]["parameters"]["properties"]["nodes"]["items"]
        text = render(tool.to_schema())

        assert text is not None
        assert "run_subagent_dag" in text, "the reference has to name where the shape is"
        # Above the compaction threshold the graph tool is cataloged rather than
        # offered (it is deliberately absent from DEFAULT_ALWAYS_VISIBLE), so a
        # pointer that only asserts presence is false in that mode.
        assert "tool_search" in text, "the pointer must name a route true in both disclosure modes"
        assert "prompt_template" not in text, "the node shape must not be inlined here"
        assert "prompt_template" in str(inlined), "...but it is what was inlined before"
        assert len(text) < len(render_without_dedupe(tool)), "the reference must be the smaller one"

    def test_rendering_leaves_the_registrys_own_copy_alone(self) -> None:
        from raven.agent.subagent.dag_control_advert import render

        tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
        definition = tool.to_schema()
        render(definition)

        assert "items" in definition["function"]["parameters"]["properties"]["nodes"]
        assert "prompt_template" in str(definition["function"]["parameters"]["properties"]["nodes"]["items"])


class _GraphOnTheTable:
    """The graph tool as ``_registered_tool`` finds it, serving the real node schema."""

    def node_schema(self) -> dict[str, Any]:
        from raven.agent.subagent.dag_tool import _NODE_SCHEMA

        return _NODE_SCHEMA


class _NodeSchemaToolTable:
    def get(self, name: str) -> Any:
        return _GraphOnTheTable() if name == "run_subagent_dag" else None


class _LoopWithNodeSchema:
    """A loop whose graph tool serves the real node schema.

    The resolve tool's ``nodes`` field is ``run_subagent_dag``'s own node shape,
    so a double that omits it leaves ``items`` a bare object and hides the very
    duplication these tests are about.
    """

    tools = _NodeSchemaToolTable()


def render_without_dedupe(tool: Any) -> str:
    import json

    return json.dumps(tool.to_schema())


class TestTheGraphToolsResultTextCarriesTheControlDefinitions:
    """The other two advertisement sites. The background-start text named the
    three tools, and the foreground report named ``resolve_dag_node`` and no
    arguments at all -- so the bound lane told the model strictly less than the
    background lane did.
    """

    def _tool(self, advert: Any) -> Any:
        return raven_agent_subagent.SubAgentDagTool(
            workspace=Path(tempfile.mkdtemp()),
            registry=None,
            control_advert=advert,
        )

    def test_the_named_definitions_are_appended(self) -> None:
        tool = self._tool(lambda name: f"<{name}-schema>")

        text = tool._advert_for("dag_status", "cancel_dag")

        assert "dag_status: <dag_status-schema>" in text
        assert "cancel_dag: <cancel_dag-schema>" in text
        assert "resolve_dag_node" not in text, "only what the caller asked for"

    def test_no_renderer_wired_appends_nothing(self) -> None:
        # A tool built without a loop, and every test that does the same: the
        # caller concatenates unconditionally, so this has to be a bare string.
        assert self._tool(None)._advert_for("dag_status") == ""

    def test_a_renderer_that_raises_costs_the_hint_and_nothing_else(self) -> None:
        def _boom(_name: str) -> str:
            raise RuntimeError("registry is gone")

        assert self._tool(_boom)._advert_for("dag_status", "cancel_dag") == ""

    def test_a_name_with_no_definition_is_skipped_not_rendered_empty(self) -> None:
        tool = self._tool(lambda name: None if name == "cancel_dag" else "<s>")

        text = tool._advert_for("dag_status", "cancel_dag")

        assert "dag_status: <s>" in text
        assert "cancel_dag" not in text


def test_the_foreground_report_advertises_resolve_once_not_twice() -> None:
    """The report `render_event` fences already carries the schema.

    `_exception_report` appends it, and a Report exists only past the
    awaiting-decision gate -- which implies `deliverable` and `remaining > 0`,
    so neither of that function's early returns was taken. Appending it here
    too emitted the definition twice, once as fenced node evidence and once
    trusted.
    """
    from raven.agent.subagent.dag_adjudication import Report
    from raven.agent.subagent.dag_control_advert import render
    from raven.agent.subagent.dag_runner import _exception_report
    from raven.agent.subagent.dag_verdict import Verdict

    tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
    advert = raven_agent_subagent.SubAgentDagTool(
        workspace=Path(tempfile.mkdtemp()),
        registry=None,
        control_advert=lambda name: render(tool.to_schema()) if name == "resolve_dag_node" else None,
    )
    spec = parse_dag_spec(
        {
            "task_summary": "t",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "s", "prompt_template": "p"}],
        }
    )
    report = _exception_report(
        run_id="r1",
        node=spec.nodes[0],
        verdict=Verdict(accomplished=False, category="other", what_is_missing="a token"),
        attempt=1,
        remaining=2,
        blocked=[],
        timeout_s=600.0,
        control_advert=lambda name: render(tool.to_schema()),
    )
    rendered = advert.render_event("r1", Report(node_id="a", text=report)).model_text

    assert rendered.count('"name": "resolve_dag_node"') == 1, "one advertisement, not two"
    assert "resolve_dag_node" in rendered


def test_the_node_shape_pointer_travels_in_a_key_the_model_is_shown() -> None:
    """`$comment` would read as the right key for a note and is the wrong one.

    JSON Schema defines it as a note to developers that implementations MUST
    NOT present, so a provider that normalizes the schema is entitled to drop
    it -- leaving `items` an empty object and the node shape nowhere, which is
    the failure the reference exists to avoid. The module's docstring gives
    that reason; this holds it, because flipping the key left the suite green.
    """
    from raven.agent.subagent.dag_control_advert import _NODE_SHAPE_REF, render

    assert "description" in _NODE_SHAPE_REF, "the pointer has to be presented to the model"
    assert "$comment" not in _NODE_SHAPE_REF, "$comment is a note implementations must not present"

    tool = ResolveDagNodeTool(loop=_LoopWithNodeSchema())
    text = render(tool.to_schema())
    assert text is not None
    assert "$comment" not in text, "and it must not reach the rendered advertisement either"
