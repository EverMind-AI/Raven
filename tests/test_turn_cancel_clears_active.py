"""turn.cancel must drop the active-turn slot even if the sink has not yet (issue #115)."""

from __future__ import annotations

from raven.tui_rpc.methods import turn as turn_mod


class _FakeHandle:
    """Stand-in for a TurnHandle: cancel marks it, result resolves to None."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    async def result(self) -> None:
        return None


async def test_turn_cancel_clears_active_slot() -> None:
    session_key = "sess-cancel-test"
    turn_mod._active_turns[session_key] = _FakeHandle()

    result = await turn_mod.turn_cancel({"session_key": session_key}, emitter=None)

    assert result == {"cancelled": True}
    assert turn_mod.is_turn_active(session_key) is False


async def test_turn_cancel_without_active_turn_is_noop() -> None:
    session_key = "sess-idle"
    turn_mod._active_turns.pop(session_key, None)

    result = await turn_mod.turn_cancel({"session_key": session_key}, emitter=None)

    assert result == {"cancelled": False}
    assert turn_mod.is_turn_active(session_key) is False
