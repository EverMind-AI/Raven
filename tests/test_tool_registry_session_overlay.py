"""Tools one session brought: visible to its own turns, to nothing else.

One registry serves every session on a connection, so "this session's tools"
cannot be expressed by registering them -- a registered tool stays reachable
from the next session's turn and from the next prompt on the same process. The
overlay is how it is expressed instead: a binding that lives as long as the
session, and a visibility that lives as long as the turn running under it.

The four cases here are the ones a wrong overlay is silently wrong in: with an
overlay, without one, with an overlay whose name is already taken process-wide,
and with two sessions' turns in flight at once. The last is not a stress test --
it is the whole reason the visibility is a ContextVar rather than a field, and it
is the case that a field passes only by accident of ordering.

A fifth: progressive disclosure is a second discovery path, and a tool the
model cannot find is not one it has. ``TestToolSearchFindsThem`` covers it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry


class _Stub(Tool):
    def __init__(self, name: str, *, answer: str = "ran", description: str = "stub") -> None:
        self._name = name
        self._answer = answer
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return self._answer


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(_Stub(name))
    return reg


def _offered(reg: ToolRegistry) -> set[str]:
    return {d["function"]["name"] for d in reg.get_definitions()}


class TestInsideTheScope:
    def test_a_session_tool_answers_every_lookup_a_turn_makes(self) -> None:
        """One list, because a tool found by one lookup and missed by another is
        worse than one that was never there: the model is offered a tool and then
        told it does not exist."""
        reg = _registry("read_file")
        session_tool = _Stub("mcp_bridged_probe")

        with reg.session_scope({session_tool.name: session_tool}):
            assert reg.get("mcp_bridged_probe") is session_tool
            assert reg.has("mcp_bridged_probe")
            assert reg.offers_by_name("mcp_bridged_probe")
            assert reg.resolve_configured("mcp_bridged_probe") == ["mcp_bridged_probe"]
            assert "mcp_bridged_probe" in _offered(reg)

    async def test_and_can_actually_be_called(self) -> None:
        """The point of being visible. Advertising a tool ``execute`` then refuses
        is the one failure the overlay must not produce."""
        reg = _registry("read_file")
        session_tool = _Stub("mcp_bridged_probe", answer="from the session server")

        with reg.session_scope({session_tool.name: session_tool}):
            result = await reg.execute("mcp_bridged_probe", {})

        assert str(result) == "from the session server"

    def test_the_process_tools_are_still_there(self) -> None:
        reg = _registry("read_file")

        with reg.session_scope({"mcp_bridged_probe": _Stub("mcp_bridged_probe")}):
            assert reg.get("read_file") is not None
            assert _offered(reg) == {"read_file", "mcp_bridged_probe"}

    def test_an_off_switch_still_wins_over_a_session_tool(self) -> None:
        """The overlay decides who can see a tool, not whether the operator
        allowed it."""
        reg = _registry("read_file")
        reg.set_withheld_source(lambda: frozenset({"mcp_bridged_probe"}))

        with reg.session_scope({"mcp_bridged_probe": _Stub("mcp_bridged_probe")}):
            assert "mcp_bridged_probe" not in _offered(reg)
            assert not reg.offers_by_name("mcp_bridged_probe")


class TestOutsideTheScope:
    def test_a_bound_session_tool_is_invisible_until_its_turn_runs(self) -> None:
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_bridged_probe": _Stub("mcp_bridged_probe")})

        assert reg.get("mcp_bridged_probe") is None
        assert not reg.has("mcp_bridged_probe")
        assert "mcp_bridged_probe" not in _offered(reg)

    def test_and_is_invisible_again_after_it(self) -> None:
        reg = _registry("read_file")
        with reg.session_scope_for("acp:a"):
            pass
        reg.bind_session_tools("acp:a", {"mcp_bridged_probe": _Stub("mcp_bridged_probe")})
        with reg.session_scope_for("acp:a"):
            assert reg.has("mcp_bridged_probe")

        assert not reg.has("mcp_bridged_probe")

    def test_another_sessions_turn_does_not_see_it(self) -> None:
        """The isolation the whole mechanism exists for: same registry, same
        connection, a session that brought nothing."""
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_bridged_probe": _Stub("mcp_bridged_probe")})

        with reg.session_scope_for("acp:b"):
            assert reg.get("mcp_bridged_probe") is None
            assert _offered(reg) == {"read_file"}

    def test_a_released_session_takes_its_tools_with_it(self) -> None:
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_bridged_probe": _Stub("mcp_bridged_probe")})
        reg.release_session_tools("acp:a")

        with reg.session_scope_for("acp:a"):
            assert reg.get("mcp_bridged_probe") is None
            assert _offered(reg) == {"read_file"}

    def test_releasing_a_session_that_brought_nothing_is_fine(self) -> None:
        """Every session leaves through this path, and most brought no servers."""
        reg = _registry("read_file")

        reg.release_session_tools("acp:never-seen")

        assert reg.get("read_file") is not None


class TestAClashWithAProcessTool:
    def test_the_session_tool_wins_inside_the_scope(self) -> None:
        """A session brings a server whose tool name is already taken. Its own
        turn must reach its own server -- resolving to the process tool would run
        somebody else's code for it."""
        process_tool = _Stub("mcp_shared_probe", answer="process")
        reg = ToolRegistry()
        reg.register(process_tool)
        session_tool = _Stub("mcp_shared_probe", answer="session")

        with reg.session_scope({"mcp_shared_probe": session_tool}):
            assert reg.get("mcp_shared_probe") is session_tool
            assert len(_offered(reg)) == 1

    async def test_and_the_call_goes_to_the_session_tool(self) -> None:
        reg = ToolRegistry()
        reg.register(_Stub("mcp_shared_probe", answer="process"))

        with reg.session_scope({"mcp_shared_probe": _Stub("mcp_shared_probe", answer="session")}):
            shadowed = await reg.execute("mcp_shared_probe", {})
        plain = await reg.execute("mcp_shared_probe", {})

        assert str(shadowed) == "session"
        assert str(plain) == "process"

    def test_the_process_tool_is_back_afterwards(self) -> None:
        process_tool = _Stub("mcp_shared_probe", answer="process")
        reg = ToolRegistry()
        reg.register(process_tool)

        with reg.session_scope({"mcp_shared_probe": _Stub("mcp_shared_probe", answer="session")}):
            pass

        assert reg.get("mcp_shared_probe") is process_tool


class TestTwoSessionsAtOnce:
    """Concurrent turns on one registry -- the case a field cannot pass.

    Both turns are held inside their own scope at the same time before either
    one looks anything up, so the overlay each sees is the one set second. A
    field would hand both turns session B's tools (and, after A finishes, would
    restore whatever it read as the previous value); a task-local one hands each
    turn its own.
    """

    async def _turn(self, reg: ToolRegistry, key: str, entered: asyncio.Event, both_in: asyncio.Event) -> set[str]:
        with reg.session_scope_for(key):
            entered.set()
            # Not a sleep: the assertions must run with the *other* turn's scope
            # provably open, and a timed wait would pass on a machine that
            # happened to schedule them apart.
            await both_in.wait()
            return _offered(reg)

    async def test_each_turn_sees_only_its_own_session_tools(self) -> None:
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_a_probe": _Stub("mcp_a_probe")})
        reg.bind_session_tools("acp:b", {"mcp_b_probe": _Stub("mcp_b_probe")})
        in_a, in_b, both_in = asyncio.Event(), asyncio.Event(), asyncio.Event()

        task_a = asyncio.create_task(self._turn(reg, "acp:a", in_a, both_in))
        task_b = asyncio.create_task(self._turn(reg, "acp:b", in_b, both_in))
        await asyncio.wait_for(asyncio.gather(in_a.wait(), in_b.wait()), timeout=5)
        both_in.set()
        offered_a, offered_b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)

        assert offered_a == {"read_file", "mcp_a_probe"}
        assert offered_b == {"read_file", "mcp_b_probe"}

    async def test_a_session_with_no_tools_of_its_own_sees_none_of_theirs(self) -> None:
        """The reason an empty scope is entered rather than skipped: a turn that
        brought nothing must not inherit whatever the surrounding context held."""
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_a_probe": _Stub("mcp_a_probe")})
        in_a, in_plain, both_in = asyncio.Event(), asyncio.Event(), asyncio.Event()

        task_a = asyncio.create_task(self._turn(reg, "acp:a", in_a, both_in))
        task_plain = asyncio.create_task(self._turn(reg, "acp:plain", in_plain, both_in))
        await asyncio.wait_for(asyncio.gather(in_a.wait(), in_plain.wait()), timeout=5)
        both_in.set()
        offered_a, offered_plain = await asyncio.wait_for(asyncio.gather(task_a, task_plain), timeout=5)

        assert offered_a == {"read_file", "mcp_a_probe"}
        assert offered_plain == {"read_file"}

    async def test_a_nested_scope_does_not_leak_out_of_the_task_that_opened_it(self) -> None:
        """A turn that opens a scope inside another one -- the shape a tool that
        runs its own turn leaves -- restores what its caller could see."""
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_a_probe": _Stub("mcp_a_probe")})
        reg.bind_session_tools("acp:b", {"mcp_b_probe": _Stub("mcp_b_probe")})

        with reg.session_scope_for("acp:a"):
            with reg.session_scope_for("acp:b"):
                assert _offered(reg) == {"read_file", "mcp_b_probe"}
            assert _offered(reg) == {"read_file", "mcp_a_probe"}


class TestTheTurnIsWhereTheScopeOpens:
    """A real ``AgentLoop``, because the binding is worth nothing unheld.

    The scope cannot be opened where the servers were accepted: an ACP request
    handler submits the turn onto the spine and the turn runs on a task that
    inherits none of the handler's context (``turn.send`` says so in as many
    words). ``run_turn`` is where that task begins, so that is where a session's
    tools become visible -- and if it stops doing so, every test above still
    passes while no turn can reach a single session tool.
    """

    @staticmethod
    def _loop(workspace: Any) -> Any:
        from raven.agent.loop.main import AgentLoop
        from raven.providers.base import LLMProvider

        class _Provider(LLMProvider):
            def __init__(self) -> None:
                super().__init__(api_key="test")

            async def chat(self, messages, **kwargs):
                raise AssertionError("no completion in this test")

            def get_default_model(self) -> str:
                return "stub"

        return AgentLoop(provider=_Provider(), workspace=workspace, model="stub", max_iterations=1)

    @staticmethod
    def _request(conversation: str) -> Any:
        from raven.spine.message import ChatType, Source
        from raven.spine.turn import Origin, TurnRequest

        return TurnRequest(
            origin=Origin.USER,
            source=Source(channel="acp", chat_id="chat1", sender_id="user", chat_type=ChatType.DM),
            text="hello",
            conversation=conversation,
        )

    async def test_a_turn_sees_the_tools_bound_to_its_own_session_and_no_others(self, tmp_path) -> None:
        loop = self._loop(tmp_path)
        loop.tools.bind_session_tools("acp:a", {"mcp_a_probe": _Stub("mcp_a_probe")})
        seen: dict[str, bool] = {}

        async def fake_run_turn(req, emit, drain, **kw):
            seen[req.conversation] = loop.tools.has("mcp_a_probe")
            return None

        loop._run_turn = fake_run_turn
        await loop.run_turn(self._request("acp:a"), lambda *a, **k: None, None)
        await loop.run_turn(self._request("acp:b"), lambda *a, **k: None, None)

        assert seen == {"acp:a": True, "acp:b": False}

    async def test_and_the_visibility_is_gone_when_the_turn_is(self, tmp_path) -> None:
        loop = self._loop(tmp_path)
        loop.tools.bind_session_tools("acp:a", {"mcp_a_probe": _Stub("mcp_a_probe")})

        async def fake_run_turn(req, emit, drain, **kw):
            assert loop.tools.has("mcp_a_probe")
            return None

        loop._run_turn = fake_run_turn
        await loop.run_turn(self._request("acp:a"), lambda *a, **k: None, None)

        assert not loop.tools.has("mcp_a_probe")


class TestToolSearchFindsThem:
    """The overlay on the second discovery path: progressive disclosure.

    Above ``compaction_threshold`` tools the model is not handed every schema --
    it is handed ``tool_search`` and told to look. A session tool absent from
    that answer is dispatchable (``tool_call`` resolves by registry) and
    undiscoverable, which is the same thing as missing.

    The BM25 index cannot carry them: it is built once and read by every
    concurrent turn, so a per-session index would have two sessions swapping it
    out from under each other. The visibility sits on the read instead, which is
    what these tests pin -- both halves of it, the finding and the not-leaking.
    """

    @staticmethod
    def _controller(reg: ToolRegistry) -> Any:
        from raven.agent.tools.tool_search import ToolSearchController

        ctrl = ToolSearchController(reg, always_visible=set())
        ctrl.refresh()
        return ctrl

    @staticmethod
    def _session_tool() -> _Stub:
        return _Stub("mcp_bridged_summarise", description="summarise a long web page into bullet points")

    def test_a_session_tool_is_searchable_inside_its_scope(self) -> None:
        reg = _registry("read_file")
        ctrl = self._controller(reg)
        tool = self._session_tool()

        with reg.session_scope({tool.name: tool}):
            hits = ctrl.search("summarise a web page")

        assert [h["name"] for h in hits] == ["mcp_bridged_summarise"]
        # A hit has to be callable straight from the result: the whole point of
        # carrying the schema is that no second lookup is needed.
        assert hits[0]["description"] == tool.description
        assert hits[0]["parameters"] == tool.parameters

    def test_and_resolve_target_forwards_to_it(self) -> None:
        """``tool_search`` promising a tool that ``tool_call`` then refuses is
        the one pair of answers that must not disagree."""
        reg = _registry("read_file")
        ctrl = self._controller(reg)
        tool = self._session_tool()

        with reg.session_scope({tool.name: tool}):
            target = ctrl.resolve_target(tool.name)

        assert target.tool is tool
        assert target.refusal is None

    def test_outside_any_scope_it_is_neither_found_nor_resolved(self) -> None:
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_bridged_summarise": self._session_tool()})
        ctrl = self._controller(reg)

        assert ctrl.search("summarise a web page") == []
        refusal = ctrl.resolve_target("mcp_bridged_summarise").refusal
        assert refusal is not None and "not available" in refusal

    def test_another_sessions_turn_does_not_find_it(self) -> None:
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_bridged_summarise": self._session_tool()})
        ctrl = self._controller(reg)

        with reg.session_scope_for("acp:b"):
            assert ctrl.search("summarise a web page") == []

    def test_a_registered_tool_still_ranks_ahead_of_a_session_one(self) -> None:
        """The catalog's own ranking is the trustworthy one -- it is scored over
        hundreds of tools, the session's over a handful -- so merging must not
        let a weak session hit take the top slot from a strong catalog hit."""
        reg = _registry("read_file")
        reg.register(_Stub("summarise_page", description="summarise a long web page into bullet points"))
        ctrl = self._controller(reg)
        tool = self._session_tool()

        with reg.session_scope({tool.name: tool}):
            names = [h["name"] for h in ctrl.search("summarise a web page")]

        assert names == ["summarise_page", "mcp_bridged_summarise"]

    def test_an_off_switch_still_wins_over_a_searchable_session_tool(self) -> None:
        """The read-side filter has to run over the merged list, or the off
        switch closes the schema and leaves search advertising the tool."""
        reg = _registry("read_file")
        reg.set_withheld_source(lambda: frozenset({"mcp_bridged_summarise"}))
        ctrl = self._controller(reg)
        tool = self._session_tool()

        with reg.session_scope({tool.name: tool}):
            assert ctrl.search("summarise a web page") == []

    def test_the_shared_index_stays_the_registration_view(self) -> None:
        """What the index is built from must not depend on who is asking."""
        reg = _registry("read_file")
        ctrl = self._controller(reg)
        tool = self._session_tool()

        with reg.session_scope({tool.name: tool}):
            ctrl.refresh()
            assert [t.name for t in ctrl._catalog_tools()] == ["read_file"]

    def test_the_registration_view_is_untouched(self) -> None:
        """``names()`` is diffed by the MCP teardown path to learn what a connect
        added, so it answers "registered", not "visible to this turn"."""
        reg = _registry("read_file")
        tool = self._session_tool()

        with reg.session_scope({tool.name: tool}):
            assert reg.tool_names == ["read_file"]
            assert reg.names() == ["read_file"]
            assert "mcp_bridged_summarise" not in reg
            assert len(reg) == 1

    def test_ranking_a_session_set_leaves_the_shared_index_slot_alone(self) -> None:
        """Why the session set gets its own throwaway BM25 rather than a
        ``ToolIndex``: the process-level slot holds one signature, so routing a
        per-session set through it would evict the catalog every search and
        rebuild it on the next one."""
        from raven.agent.tools import tool_index

        reg = _registry("read_file")
        ctrl = self._controller(reg)
        tool = self._session_tool()
        before = tool_index._cached_sig

        with reg.session_scope({tool.name: tool}):
            assert ctrl.search("summarise a web page")

        assert tool_index._cached_sig == before

    async def _search_in_turn(
        self,
        reg: ToolRegistry,
        ctrl: Any,
        key: str,
        entered: asyncio.Event,
        both_in: asyncio.Event,
    ) -> list[str]:
        with reg.session_scope_for(key):
            entered.set()
            # Not a sleep: the search must run with the *other* turn's scope
            # provably open, and a timed wait would pass on a machine that
            # happened to schedule the two apart.
            await both_in.wait()
            return [h["name"] for h in ctrl.search("probe a capability")]

    async def test_two_turns_at_once_each_find_only_their_own(self) -> None:
        reg = _registry("read_file")
        reg.bind_session_tools("acp:a", {"mcp_a_probe": _Stub("mcp_a_probe", description="probe the alpha capability")})
        reg.bind_session_tools("acp:b", {"mcp_b_probe": _Stub("mcp_b_probe", description="probe the beta capability")})
        ctrl = self._controller(reg)
        in_a, in_b, both_in = asyncio.Event(), asyncio.Event(), asyncio.Event()

        task_a = asyncio.create_task(self._search_in_turn(reg, ctrl, "acp:a", in_a, both_in))
        task_b = asyncio.create_task(self._search_in_turn(reg, ctrl, "acp:b", in_b, both_in))
        await asyncio.wait_for(asyncio.gather(in_a.wait(), in_b.wait()), timeout=5)
        both_in.set()
        found_a, found_b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)

        assert found_a == ["mcp_a_probe"]
        assert found_b == ["mcp_b_probe"]
