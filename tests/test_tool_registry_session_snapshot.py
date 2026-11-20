"""The tool array is frozen for a session and recomputed only at a refresh point.

The array is the prompt-cache prefix, so a live availability switch must not
move it. Inside ``session_scope_for`` the registry serves the snapshot it took
at the session's first assembly; outside one (a sub-agent, a test)
it keeps the live view. Refresh points are a new session, a ``log_id`` change
(``/clear``), an explicit invalidation (compaction), and a release.

What a snapshot includes is the *registered* set, not the offered set: a tool a
live switch has withheld is still in it, so flipping the switch either way
mid-session only changes whether calls run, never the array. The constructor's
hard subset is the exception -- it cannot change for the life of the process,
so keeping it out costs no cache and preserves the eval-harness contract.
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


def test_unavailable_reason_reads_back_the_source() -> None:
    reg = _registry("exec", "grep")
    reg.set_withheld_source(lambda: frozenset({"exec"}))
    reg.set_withheld_reason_source(lambda: {"exec": "switched off in settings"})

    with reg.session_scope_for("s1"):
        assert _defs(reg) == ["exec", "grep"]
        assert reg.unavailable_reason("exec") == "switched off in settings"
        assert reg.unavailable_reason("grep") is None
        assert reg.unavailable_reason("missing") is None


def test_session_definition_names_are_the_snapshot_not_the_offered_set() -> None:
    reg = _registry("exec", "grep")
    reg.set_withheld_source(lambda: frozenset({"grep"}))

    with reg.session_scope_for("s1"):
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
