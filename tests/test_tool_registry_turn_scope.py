"""The tool array a turn sends is frozen at the turn's entry.

The array is the first segment of the prompt-cache prefix, so a tool that
registers while a turn runs -- an MCP handshake finishing in the background is
the ordinary case since ``prewarm_mcp`` -- would rebuild the whole cached
prompt between two model calls of the same turn. ``turn_scope`` masks such
arrivals until the next turn; the arrival loses nothing by waiting, because
``_mcp_tool_notices`` already tells the model the server is still connecting.

Only arrivals are masked. The operator's off switch stays live per assembly
(that reversibility is the point of the withheld axis), a tool unregistered
mid-turn has no schema to serve anyway, and session-overlay tools enter with
the turn that carries them. A registry that never enters the scope -- a
sub-agent's, the curator's -- advertises everything, exactly as before.
"""

from __future__ import annotations

from typing import Any

from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import Tool


class _Stub(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ran"


def _registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(_Stub(name))
    return reg


def _offered(reg: ToolRegistry) -> set[str]:
    return {d["function"]["name"] for d in reg.get_definitions()}


def test_a_mid_turn_arrival_joins_the_next_turn() -> None:
    reg = _registry("read_file")
    with reg.turn_scope():
        reg.register(_Stub("mcp_late_tool"))
        assert _offered(reg) == {"read_file"}, "the array must not move inside the turn"
    assert _offered(reg) == {"read_file", "mcp_late_tool"}, "the next assembly outside the scope serves it"


def test_a_mid_turn_arrival_is_registered_just_not_offered() -> None:
    # The freeze lives in ``offers`` -- the one predicate behind the schema AND
    # tool-search -- so a late arrival cannot be hidden from one surface and
    # found through the other. The registry itself keeps the tool: it is
    # admitted, and the next turn serves it with no further action.
    reg = _registry("read_file")
    with reg.turn_scope():
        reg.register(_Stub("mcp_late_tool"))
        assert reg.has("mcp_late_tool")
        assert not reg.offers_by_name("mcp_late_tool")
    assert reg.offers_by_name("mcp_late_tool")


def test_the_off_switch_stays_live_inside_the_scope() -> None:
    # Tightening must bind the running turn; the freeze is only against
    # additions to the array, never against removals the operator asked for.
    reg = _registry("read_file", "exec")
    switched: set[str] = set()
    reg.set_withheld_source(lambda: frozenset(switched))
    with reg.turn_scope():
        assert _offered(reg) == {"read_file", "exec"}
        switched.add("exec")
        assert _offered(reg) == {"read_file"}


def test_an_unregistration_is_not_masked() -> None:
    reg = _registry("read_file", "mcp_gone")
    with reg.turn_scope():
        reg.unregister("mcp_gone")
        assert _offered(reg) == {"read_file"}


def test_session_overlay_tools_enter_with_their_turn() -> None:
    # The overlay is set inside run_turn alongside the scope, after the freeze
    # captured the base registry -- exempting it is what keeps a session's own
    # tools from vanishing on the very turn that brought them.
    reg = _registry("read_file")
    reg.bind_session_tools("s1", {"session_tool": _Stub("session_tool")})
    with reg.turn_scope(), reg.session_scope_for("s1"):
        assert _offered(reg) == {"read_file", "session_tool"}


def test_scopes_do_not_leak_between_registries() -> None:
    # A sub-agent turn runs inside the parent's context; its own registry has
    # its own ContextVar, so the parent's freeze must not filter it.
    parent = _registry("read_file")
    child = _registry("child_tool")
    with parent.turn_scope():
        child.register(_Stub("child_late"))
        assert _offered(child) == {"child_tool", "child_late"}


def test_an_unwithheld_tool_stays_hidden_for_the_turn() -> None:
    # The other way a tool can be ADDED to the array mid-turn: a live off
    # switch flipping back on -- a media key written mid-turn, a disabled tool
    # re-enabled. Same cache-prefix consequence as a late registration, same
    # deferral: the withheld set at entry holds for the turn.
    reg = _registry("read_file", "image_generate")
    switched = {"image_generate"}
    reg.set_withheld_source(lambda: frozenset(switched))
    with reg.turn_scope():
        assert _offered(reg) == {"read_file"}
        switched.clear()
        assert _offered(reg) == {"read_file"}, "un-withholding must land on the next turn"
    assert _offered(reg) == {"read_file", "image_generate"}


def test_tightening_still_outranks_the_entry_freeze() -> None:
    # The union can only add: a switch that flips OFF after entry binds the
    # next assembly even though the entry snapshot said the tool was on offer.
    reg = _registry("read_file", "exec")
    switched: set[str] = set()
    reg.set_withheld_source(lambda: frozenset(switched))
    with reg.turn_scope():
        switched.add("exec")
        assert "exec" not in _offered(reg)
        assert "exec" in reg.withheld_names(), "execute's own gate reads the same union"


class _Shaped(_Stub):
    def __init__(self, name: str, description: str, properties: dict[str, Any]) -> None:
        super().__init__(name)
        self._description = description
        self._properties = properties

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": self._properties}


def test_a_same_name_replacement_reads_as_the_removal_it_starts_with() -> None:
    # A re-registration under a familiar label is a removal followed by an
    # addition, and each half keeps its lawful timing: the entry drops out of
    # the running turn (a shrink, which the asymmetry allows), and the
    # replacement joins the next one. Admitting it by name would swap the
    # served schema between two model calls -- the array moving without a
    # single name changing.
    reg = ToolRegistry()
    reg.register(_Shaped("mcp_same_tool", "A", {"a": {"type": "string"}}))
    with reg.turn_scope():
        assert _offered(reg) == {"mcp_same_tool"}
        reg.register(_Shaped("mcp_same_tool", "B", {"b": {"type": "integer"}}))
        assert _offered(reg) == set(), "the replacement must not serve mid-turn under the old admission"
    defs = reg.get_definitions()
    assert [d["function"]["description"] for d in defs] == ["B"], "the next turn serves the replacement"
    assert "b" in defs[0]["function"]["parameters"]["properties"]


def test_a_same_name_replacement_is_findable_through_no_surface_mid_turn() -> None:
    # Same single-predicate argument as the plain late arrival: the schema and
    # tool-search must agree, so the replaced entry is out of both at once. The
    # registry itself keeps the new tool -- it is admitted, just waiting.
    reg = ToolRegistry()
    reg.register(_Shaped("mcp_same_tool", "A", {"a": {"type": "string"}}))
    with reg.turn_scope():
        reg.register(_Shaped("mcp_same_tool", "B", {"b": {"type": "integer"}}))
        assert reg.has("mcp_same_tool")
        assert not reg.offers_by_name("mcp_same_tool")
    assert reg.offers_by_name("mcp_same_tool")


def test_re_registering_the_identical_instance_yanks_nothing() -> None:
    # The freeze pins (name, instance) pairs, not registration events: putting
    # the very same object back is not a replacement and must not shrink the
    # turn's array.
    reg = ToolRegistry()
    tool = _Stub("read_file")
    reg.register(tool)
    with reg.turn_scope():
        reg.register(tool)
        assert _offered(reg) == {"read_file"}


async def test_a_replacement_is_not_dispatched_by_the_turn_that_saw_its_predecessor() -> None:
    # The dispatch half of the identity freeze, in its hardest shape:
    # the replacement carries an IDENTICAL schema, so validation can never
    # tell the two apart -- a reconnect commonly preserves the schema while
    # changing the session, endpoint or credentials behind it. The call was
    # composed for the entry instance and must not execute a side effect on
    # whatever now wears the name; the refusal is the same worded miss a
    # removal produces, which is what the replacement is to this turn.
    ran: list[str] = []

    class _Tagged(_Shaped):
        def __init__(self, tag: str) -> None:
            super().__init__("mcp_same_tool", "same description", {"q": {"type": "string"}})
            self._tag = tag

        async def execute(self, **kwargs: Any) -> str:
            ran.append(self._tag)
            return self._tag

    reg = ToolRegistry()
    reg.register(_Tagged("A"))
    with reg.turn_scope():
        assert [d["function"]["name"] for d in reg.get_definitions()] == ["mcp_same_tool"]
        reg.register(_Tagged("B"))
        out = await reg.execute("mcp_same_tool", {"q": "same valid input"})
        assert ran == [], "a call composed for the entry instance must not run its replacement"
        assert "not available" in out
    assert await reg.execute("mcp_same_tool", {"q": "next turn"}) == "B", "the next turn dispatches the replacement"
    assert ran == ["B"]


async def test_a_mid_turn_arrival_is_not_dispatchable_either() -> None:
    # The freeze holds on every surface or on none: a name this turn was never
    # shown cannot be reached by guessing it, and lands whole on the next turn.
    reg = _registry("read_file")
    with reg.turn_scope():
        reg.register(_Stub("mcp_late_tool"))
        assert "not available" in await reg.execute("mcp_late_tool", {})
    assert await reg.execute("mcp_late_tool", {}) == "ran"


async def test_session_overlay_tools_stay_dispatchable_in_their_turn() -> None:
    reg = _registry("read_file")
    reg.bind_session_tools("s1", {"session_tool": _Stub("session_tool")})
    with reg.turn_scope(), reg.session_scope_for("s1"):
        assert await reg.execute("session_tool", {}) == "ran"
