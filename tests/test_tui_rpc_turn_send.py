"""Tests for ``turn.send`` real handler.

``turn.send`` submits a turn onto the spine (build_tui Scheduler) and returns
``{turn_id, accepted}`` synchronously; the turn streams out via the hub/sink.
These tests drive the handler with a fake Scheduler + emitter (the spine path
itself is covered in ``test_tui_rpc_spine.py``).

Spec source:
- ``raven/tui_rpc/models.py`` ``TurnSendParams`` / ``TurnSendResult``
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.errors import ModelNotAvailableError, RpcError, TurnInProgressError
from raven.tui_rpc.methods.turn import register_turn_methods, turn_send


class FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    async def result(self):
        return None


class FakeScheduler:
    """Records submitted requests; returns a handle. Optionally raises."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.submitted: list = []
        self._raises = raises

    def submit(self, req):
        if self._raises is not None:
            raise self._raises
        self.submitted.append(req)
        return FakeHandle()


class FakeEmitter:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, session_key: str, event: dict) -> None:
        self.emitted.append((session_key, event))

    def types(self) -> list[str]:
        return [e["type"] for _k, e in self.emitted]


@pytest.fixture(autouse=True)
def _clear_active_turns():
    from raven.tui_rpc.methods import turn as _turn_mod

    _turn_mod._active_turns.clear()
    yield
    _turn_mod._active_turns.clear()


@pytest.fixture
def dispatcher() -> Dispatcher:
    d = Dispatcher()
    register_turn_methods(d, emitter=FakeEmitter(), scheduler=FakeScheduler(), turn_ids={})
    return d


# --- Happy path ---


async def test_turn_send_happy_path_returns_turn_id_and_accepted() -> None:
    scheduler = FakeScheduler()
    turn_ids: dict[str, str] = {}
    emitter = FakeEmitter()

    result = await turn_send(
        {"session_key": "tui:default", "content": "hello"},
        emitter=emitter,
        scheduler=scheduler,
        turn_ids=turn_ids,
    )

    assert set(result) == {"turn_id", "accepted"}
    assert result["accepted"] is True
    assert isinstance(result["turn_id"], str) and len(result["turn_id"]) >= 16
    # The turn was submitted, the slot bound, message.start emitted.
    assert len(scheduler.submitted) == 1
    assert scheduler.submitted[0].conversation == "tui:default"
    assert turn_ids["tui:default"] == result["turn_id"]
    assert emitter.types() == ["message.start"]
    assert emitter.emitted[0][1]["payload"]["turn_id"] == result["turn_id"]


async def test_turn_send_generates_unique_turn_ids() -> None:
    scheduler = FakeScheduler()
    turn_ids: dict[str, str] = {}
    r1 = await turn_send({"session_key": "tui:a", "content": "x"}, scheduler=scheduler, turn_ids=turn_ids)
    r2 = await turn_send({"session_key": "tui:b", "content": "x"}, scheduler=scheduler, turn_ids=turn_ids)
    assert r1["turn_id"] != r2["turn_id"]


async def test_turn_send_binds_active_slot_after_submit() -> None:
    from raven.tui_rpc.methods import turn as turn_mod

    scheduler = FakeScheduler()
    await turn_send({"session_key": "tui:default", "content": "hi"}, scheduler=scheduler, turn_ids={})
    assert turn_mod.is_turn_active("tui:default") is True


# --- Error paths ---


async def test_turn_send_rejects_active_turn_with_minus_32003() -> None:
    scheduler = FakeScheduler()
    turn_ids: dict[str, str] = {}
    await turn_send({"session_key": "tui:default", "content": "first"}, scheduler=scheduler, turn_ids=turn_ids)

    with pytest.raises(TurnInProgressError) as excinfo:
        await turn_send(
            {"session_key": "tui:default", "content": "second"}, scheduler=scheduler, turn_ids=turn_ids
        )

    assert excinfo.value.CODE == -32003
    assert excinfo.value.MESSAGE == "turn_in_progress"


async def test_turn_send_rejects_unknown_model_with_minus_32008() -> None:
    with patch(
        "raven.tui_rpc.methods.turn._resolve_model",
        side_effect=ModelNotAvailableError("no provider configured"),
    ):
        with pytest.raises(ModelNotAvailableError) as excinfo:
            await turn_send({"session_key": "tui:default", "content": "x"}, scheduler=FakeScheduler())

    assert excinfo.value.CODE == -32008


async def test_turn_send_without_scheduler_emits_model_not_available() -> None:
    # No agent loop wired (scheduler None, no build error) → per-turn -32008 event.
    emitter = FakeEmitter()
    result = await turn_send(
        {"session_key": "tui:default", "content": "x"}, emitter=emitter, scheduler=None
    )
    assert result["accepted"] is True
    assert emitter.types() == ["message.start", "error"]
    assert emitter.emitted[-1][1]["payload"]["code"] == -32008


async def test_turn_send_when_submit_rejected_surfaces_turn_failed() -> None:
    # Server draining: submit raises → message.start + turn_failed error, no bind.
    from raven.spine.scheduler import SchedulerDrainingError
    from raven.tui_rpc.methods import turn as turn_mod

    emitter = FakeEmitter()
    turn_ids: dict[str, str] = {}
    result = await turn_send(
        {"session_key": "tui:default", "content": "x"},
        emitter=emitter,
        scheduler=FakeScheduler(raises=SchedulerDrainingError("draining")),
        turn_ids=turn_ids,
    )
    assert result["accepted"] is True
    assert emitter.types() == ["message.start", "error"]
    assert emitter.emitted[-1][1]["payload"]["message"] == "turn_failed"
    # No leak: a rejected submit binds neither map.
    assert turn_ids == {} and "tui:default" not in turn_mod._active_turns


async def test_turn_send_without_scheduler_surfaces_build_error_code() -> None:
    # A latched build error surfaces with its own code, not -32008.
    class _BuildErr(RpcError):
        CODE = -32603
        MESSAGE = "internal_error"

    emitter = FakeEmitter()
    build_error = _BuildErr("boom")
    await turn_send(
        {"session_key": "tui:default", "content": "x"},
        emitter=emitter,
        scheduler=None,
        build_error=build_error,
    )
    assert emitter.types() == ["message.start", "error"]
    assert emitter.emitted[-1][1]["payload"]["code"] == -32603


# --- Params validation ---


async def test_turn_send_rejects_missing_session_key() -> None:
    with pytest.raises(Exception):  # noqa: BLE001
        await turn_send({"content": "missing session_key"}, scheduler=FakeScheduler())


async def test_turn_send_rejects_missing_content() -> None:
    with pytest.raises(Exception):  # noqa: BLE001
        await turn_send({"session_key": "tui:default"}, scheduler=FakeScheduler())


async def test_turn_send_accepts_optional_channel_chat_id_sender_id() -> None:
    scheduler = FakeScheduler()
    result = await turn_send(
        {
            "session_key": "tui:default",
            "content": "hi",
            "channel": "tui",
            "chat_id": "default",
            "sender_id": "user",
        },
        scheduler=scheduler,
        turn_ids={},
    )
    assert result["accepted"] is True
    src = scheduler.submitted[0].source
    assert (src.channel, src.chat_id, src.sender_id) == ("tui", "default", "user")


# --- End-to-end via Dispatcher ---


async def test_turn_send_dispatches_via_dispatcher(dispatcher: Dispatcher) -> None:
    resp = await dispatcher.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "turn.send",
        "params": {"session_key": "tui:default", "content": "hello"},
    })

    assert "error" not in resp, f"turn.send unexpectedly raised: {resp}"
    assert set(resp["result"]) == {"turn_id", "accepted"}
    assert resp["result"]["accepted"] is True


async def test_turn_send_dispatcher_returns_minus_32003_on_concurrent_send(
    dispatcher: Dispatcher,
) -> None:
    await dispatcher.dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "turn.send",
        "params": {"session_key": "tui:default", "content": "first"},
    })
    resp = await dispatcher.dispatch({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "turn.send",
        "params": {"session_key": "tui:default", "content": "second"},
    })

    assert "error" in resp
    assert resp["error"]["code"] == -32003
    assert resp["error"]["message"] == "turn_in_progress"
