"""The tool array is frozen for a session and recomputed only at a refresh point.

The array is the prompt-cache prefix. Inside ``session_scope_for`` the registry
serves the snapshot it took at the session's first assembly; outside one (a
sub-agent, a test) it keeps the live view. Refresh points are a new session, a
``log_id`` change (``/clear``), an explicit invalidation (compaction), and a
release.

Capture judges the operator's availability switches: a tool that is off at the
refresh point does not enter the array, so the model is never shown a schema it
cannot call. Once captured the array does not move -- flipping a switch later
only changes whether calls run, never the array. Only the operator axis is
frozen: the turn freeze and a dispatch's charter are applied on read, so a
per-turn narrowing never becomes the session's availability. The constructor's
hard subset and schema-hidden declarations are kept out too -- neither can
change for the life of the process, so keeping them out costs no cache and
preserves the eval-harness contract.
"""

from __future__ import annotations

import asyncio
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


def _defs(reg: ToolRegistry) -> list[str]:
    return [d["function"]["name"] for d in reg.get_definitions()]


def test_the_array_does_not_move_when_the_switch_flips_mid_session() -> None:
    switches: set[str] = set()
    reg = _registry("exec", "grep")
    reg.set_withheld_source(lambda: frozenset(switches))

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "grep"]
        switches.add("grep")
        assert _defs(reg) == ["exec", "grep"]
        switches.discard("grep")
        assert _defs(reg) == ["exec", "grep"]
        switches.add("grep")
        assert _defs(reg) == ["exec", "grep"]

    assert _defs(reg) == ["exec"]


def test_outside_a_session_the_live_switch_still_filters() -> None:
    reg = _registry("exec", "grep")
    reg.set_withheld_source(lambda: frozenset({"grep"}))

    assert _defs(reg) == ["exec"]


def test_a_hard_withheld_name_never_enters_the_snapshot() -> None:
    reg = _registry("exec", "grep")
    reg.set_hard_withheld_source(lambda: frozenset({"exec"}))

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["grep"]


def test_a_schema_hidden_tool_never_enters_the_snapshot() -> None:
    reg = _registry("exec", "grep")
    reg.hide_from_schema("exec")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["grep"]


def test_invalidating_recaptures_late_registrations() -> None:
    reg = _registry("exec")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec"]
        reg.register(_Stub("find"))
        assert _defs(reg) == ["exec"]

        reg.invalidate_session_definitions("s1")

        assert _defs(reg) == ["exec", "find"]


def test_releasing_the_session_drops_the_snapshot() -> None:
    reg = _registry("exec")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec"]
        reg.register(_Stub("find"))
        assert _defs(reg) == ["exec"]

    reg.release_session_tools("s1")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "find"]


def test_a_new_log_id_refreshes_the_snapshot() -> None:
    reg = _registry("exec")
    reg.bind_session_revision("s1", "log-a")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec"]
        reg.register(_Stub("find"))
        assert _defs(reg) == ["exec"]

    reg.bind_session_revision("s1", "log-b")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "find"]


def test_a_tool_that_is_off_at_capture_never_enters_the_snapshot() -> None:
    reg = _registry("exec", "grep")
    reg.set_withheld_source(lambda: frozenset({"grep"}))

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec"]


def test_a_tool_becomes_available_again_at_the_next_refresh_point() -> None:
    switches = {"find"}
    reg = _registry("exec", "find")
    reg.set_withheld_source(lambda: frozenset(switches))
    reg.bind_session_revision("s1", "log-a")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec"]
        switches.discard("find")
        assert _defs(reg) == ["exec"], "a switch turned on mid-session must not move the array"

    reg.bind_session_revision("s1", "log-b")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "find"]


def test_a_tool_registered_after_capture_is_reported_once_until_the_refresh() -> None:
    """The report is the capture-time baseline against the live switches, so a
    name still off is silent and the array's names are excluded."""
    switches = {"find"}
    reg = _registry("exec", "find")
    reg.set_withheld_source(lambda: frozenset(switches))
    reg.bind_session_revision("s1", "log-a")

    with reg.session_scope_for("s1"):
        assert reg.newly_available(frozenset(switches)) == []
        switches.discard("find")
        assert reg.newly_available(frozenset(switches)) == ["find"]
        assert reg.newly_available(frozenset(switches)) == ["find"], "true for the whole window"

    reg.bind_session_revision("s1", "log-b")

    with reg.session_scope_for("s1"):
        assert "find" in _defs(reg)
        assert reg.newly_available(frozenset(switches)) == [], "the refresh captured it"


def test_an_mcp_tool_is_left_to_the_server_notice() -> None:
    """A connecting server has its own join line; this lane must not say it again."""
    from raven.mcp.naming import MCPToolRef

    switches = {"search"}
    reg = _registry("exec")
    reg.register(_Stub("search"), origin=MCPToolRef(name="search", server="svc", tool="search"))
    reg.set_withheld_source(lambda: frozenset(switches))
    reg.bind_session_revision("s1", "log-a")

    with reg.session_scope_for("s1"):
        switches.discard("search")
        assert reg.newly_available(frozenset(switches)) == []


def test_a_charter_is_not_frozen_into_the_session_snapshot() -> None:
    """A charter narrows one dispatch on read; it must never become the
    session's availability, or every later turn of the session inherits it."""
    from raven.agent.subagent.charter import Charter, charter_scope

    reg = _registry("exec", "grep")
    with charter_scope(Charter(tools=("grep",))):
        with reg.session_scope_for("s1"):
            assert _defs(reg) == ["grep"]
            answer = asyncio.run(reg.execute("exec", {}))
            assert "not available" in answer, "the charter still refuses at dispatch"

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "grep"], "the charter leaked into the captured array"


def test_unavailable_reason_reads_back_the_source() -> None:
    switches: set[str] = set()
    reg = _registry("exec", "grep")
    reg.set_withheld_source(lambda: frozenset(switches))
    reg.set_withheld_reason_source(lambda: {"exec": "switched off in settings"})

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "grep"]
        switches.add("exec")
        assert reg.unavailable_reason("exec") == "switched off in settings"
        assert reg.unavailable_reason("grep") is None
        assert reg.unavailable_reason("missing") is None


def test_session_definition_names_are_the_captured_array_not_a_live_read() -> None:
    switches: set[str] = set()
    reg = _registry("exec", "grep")
    reg.set_withheld_source(lambda: frozenset(switches))

    with reg.session_scope_for("s1"):
        assert reg.session_definition_names() == frozenset({"exec", "grep"})
        switches.add("grep")
        assert reg.session_definition_names() == frozenset({"exec", "grep"})


def test_a_charter_withheld_tool_has_no_settings_reason() -> None:
    """A charter narrows one dispatch; it is not the operator switching a tool
    off. The refusal still happens through ``offers``, but the reason source
    must not invent a settings fact for it."""
    from raven.agent.subagent.charter import Charter, charter_scope

    reg = _registry("exec", "grep")
    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "grep"]
        with charter_scope(Charter(tools=("grep",))):
            assert reg.unavailable_reason("exec") is None
            assert reg.unavailable_reason("grep") is None


def test_scheduling_a_refresh_waits_for_the_next_assembly() -> None:
    """A compaction turn keeps the array it was budgeted with; the next
    assembly recaptures, so the array, the notices and the system-prompt
    derivations all come from one snapshot."""
    reg = _registry("exec")
    reg.bind_session_revision("s1", "log-a")

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec"]
        reg.register(_Stub("find"))
        reg.schedule_definitions_refresh("s1")
        assert _defs(reg) == ["exec"]

    reg.bind_session_revision("s1", "log-a")

    with reg.session_scope_for("s1"):
        assert set(_defs(reg)) == {"exec", "find"}
        reg.register(_Stub("late"))

    reg.bind_session_revision("s1", "log-a")

    with reg.session_scope_for("s1"):
        assert set(_defs(reg)) == {"exec", "find"}, "the mark is cashed once; an ordinary turn keeps the array"


def test_two_sessions_keep_their_own_arrays() -> None:
    """One process-wide registry serves concurrent sessions; a snapshot belongs
    to the key it was captured under, and a later registration is not backfilled
    into an older session."""
    reg = _registry("exec")

    with reg.session_scope_for("a"):
        assert _defs(reg) == ["exec"]

    reg.register(_Stub("find"))

    with reg.session_scope_for("b"):
        assert set(_defs(reg)) == {"exec", "find"}
    with reg.session_scope_for("a"):
        assert _defs(reg) == ["exec"]


def test_a_charter_narrows_the_array_before_it_is_served() -> None:
    """A charter is a per-dispatch security narrowing, not a session
    availability switch: a maker turn must not see a writing tool at all. It is
    applied on read, over the frozen registered set."""
    from raven.agent.subagent.charter import Charter, charter_scope

    reg = _registry("exec", "grep")
    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "grep"]
        with charter_scope(Charter(tools=("grep",))):
            assert _defs(reg) == ["grep"]
        assert _defs(reg) == ["exec", "grep"]


def test_the_array_survives_more_sessions_than_the_old_cache_limit() -> None:
    """A live session's snapshot must not be evicted to make room for another
    session: a gateway can serve more than 64 conversations, and an eviction
    silently recaptures the array on that session's next ordinary turn."""
    reg = _registry("base")
    reg.bind_session_revision("s0", "log")

    with reg.session_scope_for("s0"):
        assert _defs(reg) == ["base"]

    for i in range(64):
        key = f"s{i + 1}"
        reg.bind_session_revision(key, "log")
        with reg.session_scope_for(key):
            _defs(reg)

    reg.register(_Stub("late"))

    with reg.session_scope_for("s0"):
        assert _defs(reg) == ["base"]
