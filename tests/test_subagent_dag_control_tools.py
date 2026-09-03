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

import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.subagent import dag_tool as raven_agent_subagent
from raven.agent.subagent.dag_control_tools import CancelDagTool, DagStatusTool, ResolveDagNodeTool
from raven.agent.subagent.dag_graph import parse_dag_spec
from raven.agent.subagent.dag_reader import DagReadError
from raven.agent.subagent.dag_runner import _exception_report
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.dag_verdict import Verdict
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
                "prompt_file": "/tmp/runs/r1/a.prompt.md",
                "instance": "i-1",
                "output_file": "/tmp/runs/r1/a.out.md",
                "memory_file": "/tmp/runs/r1/a.memory.json",
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
    assert "    output_file: /tmp/runs/r1/a.out.md" in out
    assert "    memory_file: /tmp/runs/r1/a.memory.json" in out
    assert "    started_at: 1000" in out
    assert "    ended_at: 3000" in out
    # The long template is cut to its head and points at the full prompt file.
    assert "    prompt_template: line1" in out
    assert "line10" in out
    assert "line11" not in out
    assert "truncated, 2 more lines" in out
    assert "full prompt in /tmp/runs/r1/a.prompt.md" in out
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
    assert "full prompt in /tmp/runs/r1/a.prompt.md" in out


async def test_dag_status_reports_an_unknown_run() -> None:
    tool = DagStatusTool(loop=_Loop(tool=_DagTool(error="no readable DAG run")))

    out = await tool.execute("r9")

    assert "No DAG run r9 found" in out
    assert 'tool_call name "dag_status" with no run_id' in out


class _ResolvableDagTool(_DagTool):
    """A run_subagent_dag double whose read_run succeeds and records resolve_node calls."""

    def __init__(self, resolves: bool = True) -> None:
        super().__init__(_finished_run())
        self._resolves = resolves
        self.resolved: tuple[str, str, str, str | None] | None = None

    def resolve_node(self, run_id: str, node_id: str, decision: str, message: str | None) -> bool:
        self.resolved = (run_id, node_id, decision, message)
        return self._resolves


class _LoopWithRun(_Loop):
    """A loop whose registered graph tool owns run r1 and answers resolve_node."""

    def __init__(self, resolves: bool = True) -> None:
        self._dag_tool = _ResolvableDagTool(resolves=resolves)
        super().__init__(tool=self._dag_tool)

    @property
    def resolved(self) -> tuple[str, str, str, str | None] | None:
        return self._dag_tool.resolved


class _LoopWithoutRun(_Loop):
    """A loop whose registered graph tool cannot resolve run r1 for this session."""

    def __init__(self) -> None:
        super().__init__(tool=_DagTool(error="no readable DAG run for r1"))


async def test_resolve_requires_a_message_when_continuing():
    # "message" alone also matches the CONTINUE success text ("...with your
    # message."), so pin the exact guard text and confirm resolve_node was
    # never reached -- either alone would already catch a deleted guard.
    loop = _LoopWithRun()
    tool = ResolveDagNodeTool(loop=loop)
    out = await tool.execute(run_id="r1", node_id="a", decision="continue")
    assert out == (
        "Error: continuing node 'a' needs a message telling it what to do differently. "
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
    assert out == "Error: decision must be 'continue' or 'abandon', not 'maybe'."
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
