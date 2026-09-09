"""The per-session tool overlay on ToolRegistry (raven/agent/tools/registry.py).

One registry serves every session on an ACP connection, so the tools a session
brings cannot be *registered*: a register publishes them process-wide, and the
next session on the same process would find them. They are held aside by session
key instead and made visible only inside ``session_scope_for``, whose visibility
is turn-local.

Turn-local is the load-bearing word, and it is why the overlay is a ContextVar
rather than a field. A playbook can put two nodes on the same fork and run them
in parallel -- two sessions, two turns, one registry, both live at once -- and a
field would let whichever turn entered last decide what the other one can see.
The concurrency test below is the one that pins that down; it holds both scopes
open on an ``asyncio.Event`` rather than a sleep, so it fails for the right
reason instead of racing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry


class _Stub(Tool):
    def __init__(self, name: str, reply: str = "ran") -> None:
        self._name = name
        self._reply = reply

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return self._reply


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_Stub("read_file"))
    return reg


def _offered(reg: ToolRegistry) -> set[str]:
    return {d["function"]["name"] for d in reg.get_definitions()}


class TestBindingDoesNotPublish:
    def test_a_bound_tool_is_invisible_outside_any_scope(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})

        assert registry.get("mcp_a_probe") is None
        assert not registry.has("mcp_a_probe")
        assert "mcp_a_probe" not in registry
        assert _offered(registry) == {"read_file"}

    def test_binding_leaves_the_registration_view_alone(self, registry) -> None:
        """``tool_names`` / ``len`` stay the registration view: the tool-search
        index is built off them once and shared by every concurrent turn."""
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})

        with registry.session_scope_for("s1"):
            assert registry.tool_names == ["read_file"]
            assert len(registry) == 1
            assert registry.session_tools_in_scope() == {"mcp_a_probe": registry.get("mcp_a_probe")}


class TestTheScopeMakesThemReachable:
    def test_every_lookup_agrees_inside_the_scope(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})

        with registry.session_scope_for("s1"):
            assert registry.get("mcp_a_probe") is not None
            assert registry.has("mcp_a_probe")
            assert "mcp_a_probe" in registry
            assert registry.canonical_name("mcp_a_probe") == "mcp_a_probe"
            assert _offered(registry) == {"read_file", "mcp_a_probe"}

    async def test_a_session_tool_is_callable_inside_the_scope(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe", "probe ran")})

        with registry.session_scope_for("s1"):
            assert str(await registry.execute("mcp_a_probe", {})) == "probe ran"

    async def test_and_not_callable_outside_it(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})

        out = str(await registry.execute("mcp_a_probe", {}))

        assert "not found" in out

    def test_a_session_tool_shadows_a_process_tool_of_the_same_name(self, registry) -> None:
        registry.bind_session_tools("s1", {"read_file": _Stub("read_file", "session copy")})

        with registry.session_scope_for("s1"):
            assert registry.get("read_file")._reply == "session copy"
        assert registry.get("read_file")._reply == "ran"

    def test_an_empty_scope_states_no_tools_rather_than_inheriting(self, registry) -> None:
        """A turn whose session brought nothing must not see the outer scope's
        tools -- which is why entering with no tools clears the overlay."""
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})

        with registry.session_scope_for("s1"):
            with registry.session_scope_for("s2"):
                assert registry.get("mcp_a_probe") is None
                assert _offered(registry) == {"read_file"}
            assert registry.get("mcp_a_probe") is not None


class TestRelease:
    def test_release_forgets_the_binding(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})
        registry.release_session_tools("s1")

        with registry.session_scope_for("s1"):
            assert registry.get("mcp_a_probe") is None

    def test_release_is_idempotent(self, registry) -> None:
        registry.release_session_tools("never-bound")
        registry.release_session_tools("never-bound")

    def test_rebinding_replaces_rather_than_merges(self, registry) -> None:
        """``session/load`` on a session that already brought servers means the
        new set, not both."""
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})
        registry.bind_session_tools("s1", {"mcp_b_probe": _Stub("mcp_b_probe")})

        with registry.session_scope_for("s1"):
            assert registry.get("mcp_a_probe") is None
            assert registry.get("mcp_b_probe") is not None


class TestTwoTurnsAtOnce:
    """The requirement a playbook actually makes: two nodes on one fork, parallel.

    Two barriers, not one, and the second is the one that does the work. With a
    single "both entered" barrier the task that entered *first* only resumes once
    the second has already left its scope, so a plain instance field would pass:
    the writes happen to unwind in the order that hides the bug. Parking both
    tasks inside their scopes until both have read is what forces the interleave
    a field cannot survive -- verified by swapping the ContextVar for a field and
    watching these go red.
    """

    @staticmethod
    async def _both_inside(registry, keys, observe):
        entered = {key: asyncio.Event() for key in keys}
        has_read = {key: asyncio.Event() for key in keys}

        async def turn(mine: str, other: str):
            with registry.session_scope_for(mine):
                entered[mine].set()
                await asyncio.wait_for(entered[other].wait(), timeout=5)
                observed = observe(mine)
                # Held here, still inside the scope, until the other task has
                # read too: leaving would restore whatever it saved on entry.
                has_read[mine].set()
                await asyncio.wait_for(has_read[other].wait(), timeout=5)
                return observed

        first, second = keys
        return await asyncio.gather(turn(first, second), turn(second, first))

    async def test_neither_turn_can_see_the_other_s_tools(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})
        registry.bind_session_tools("s2", {"mcp_b_probe": _Stub("mcp_b_probe")})

        def observe(_mine: str) -> dict[str, Any]:
            return {
                "a": registry.get("mcp_a_probe") is not None,
                "b": registry.get("mcp_b_probe") is not None,
                "offered": _offered(registry),
                "in_scope": set(registry.session_tools_in_scope()),
            }

        one, two = await self._both_inside(registry, ("s1", "s2"), observe)

        assert one["a"] and not one["b"], "session 1's turn could see session 2's tools"
        assert two["b"] and not two["a"], "session 2's turn could see session 1's tools"
        assert one["offered"] == {"read_file", "mcp_a_probe"}
        assert two["offered"] == {"read_file", "mcp_b_probe"}
        assert one["in_scope"] == {"mcp_a_probe"}
        assert two["in_scope"] == {"mcp_b_probe"}

    async def test_a_turn_that_brought_nothing_sees_nothing_of_a_live_sibling(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})

        one, two = await self._both_inside(registry, ("s1", "s2"), lambda _mine: _offered(registry))

        assert one == {"read_file", "mcp_a_probe"}
        assert two == {"read_file"}

    async def test_a_shadowing_session_tool_does_not_shadow_the_sibling_s(self, registry) -> None:
        """Both sessions bring their own ``read_file``. Each turn must reach its
        own, and the process copy must survive both."""
        registry.bind_session_tools("s1", {"read_file": _Stub("read_file", "one")})
        registry.bind_session_tools("s2", {"read_file": _Stub("read_file", "two")})

        one, two = await self._both_inside(registry, ("s1", "s2"), lambda _mine: registry.get("read_file")._reply)

        assert (one, two) == ("one", "two")
        assert registry.get("read_file")._reply == "ran"

    async def test_the_scope_does_not_leak_into_a_task_it_spawns_after_exit(self, registry) -> None:
        registry.bind_session_tools("s1", {"mcp_a_probe": _Stub("mcp_a_probe")})

        async def look() -> bool:
            return registry.get("mcp_a_probe") is not None

        with registry.session_scope_for("s1"):
            inherited = await asyncio.create_task(look())
        after = await asyncio.create_task(look())

        # A task created inside the scope inherits the context, which is what
        # makes the turn's own helpers work; one created after it does not.
        assert inherited is True
        assert after is False
