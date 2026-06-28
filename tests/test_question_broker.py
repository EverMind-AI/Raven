"""Tests for the QuestionBroker (ask-user round-trip, keyed by conversation_id).

Mirrors the ConfirmBroker tests: pending_req before/after, reply resolves the
future (by either handle), idempotent late/duplicate reply, timeout fail-safe to
default, cancel_all, and overlapping-question replacement.
"""

from __future__ import annotations

import asyncio

from raven.tui_rpc.methods.question import question_respond, register_question_methods
from raven.tui_rpc.question_broker import QuestionBroker

CID = "telegram:123"


def _frame_collector() -> tuple[list[dict], object]:
    frames: list[dict] = []

    async def send_frame(frame: dict) -> None:
        frames.append(frame)

    return frames, send_frame


async def _wait_for_frame(frames: list[dict], timeout: float = 1.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while not frames:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("clarify.request frame never emitted")
        await asyncio.sleep(0.005)
    return frames[0]


async def test_question_request_notification_emitted() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(
        broker.await_question(CID, prompt="Which?", choices=["a", "b"], default="x")
    )
    frame = await _wait_for_frame(frames)

    assert "id" not in frame
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "clarify.request"
    params = frame["params"]
    assert params["conversation_id"] == CID
    assert isinstance(params["request_id"], str) and params["request_id"]
    assert params["question"] == "Which?"
    assert params["choices"] == ["a", "b"]

    broker.reply(CID, "a")
    assert await task == "a"


async def test_pending_req_before_and_after() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    assert broker.pending_req(CID) is None

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    frame = await _wait_for_frame(frames)
    rid = frame["params"]["request_id"]

    assert broker.pending_req(CID) == rid

    broker.reply(CID, "answer")
    await task
    assert broker.pending_req(CID) is None


async def test_reply_by_conversation_id_resolves() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)

    assert broker.reply(CID, "yes") is True
    assert await task == "yes"


async def test_reply_by_request_id_resolves() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    frame = await _wait_for_frame(frames)
    rid = frame["params"]["request_id"]

    assert broker.reply(rid, "via-rid") is True
    assert await task == "via-rid"


async def test_reply_idempotent_late_and_duplicate() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)

    assert broker.reply(CID, "first") is True
    assert await task == "first"
    # registry cleaned up — a duplicate / late reply is a no-op
    assert broker.reply(CID, "second") is False


async def test_reply_unknown_key_idempotent() -> None:
    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    assert broker.reply("does-not-exist", "x") is False


async def test_timeout_failsafe_to_default() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    result = await broker.await_question(CID, prompt="?", default="fallback", timeout_s=0.05)
    assert result == "fallback"
    await _wait_for_frame(frames)  # it did emit the request first
    assert broker.pending_req(CID) is None


async def test_cancel_all_failsafe() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="bye"))
    await _wait_for_frame(frames)

    broker.cancel_all()
    assert await task == "bye"


async def test_overlapping_question_replaces_stale() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    first = asyncio.create_task(broker.await_question(CID, prompt="q1", default="d1"))
    await _wait_for_frame(frames)

    second = asyncio.create_task(broker.await_question(CID, prompt="q2", default="d2"))
    # The stale first question fail-safes to its own default.
    assert await first == "d1"

    # Let the second emit, then resolve it.
    while len(frames) < 2:
        await asyncio.sleep(0.005)
    broker.reply(CID, "q2-answer")
    assert await second == "q2-answer"


async def test_question_respond_handler_resolves() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)
    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)

    result = await question_respond({"conversation_id": CID, "answer": "ok"}, question_broker=broker)

    assert result == {"ok": True}
    assert await task == "ok"


async def test_question_respond_handler_unknown_returns_not_ok() -> None:
    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    result = await question_respond({"conversation_id": "nope", "answer": "x"}, question_broker=broker)

    assert result == {"ok": False}


async def test_register_question_methods_adds_respond() -> None:
    from raven.tui_rpc.dispatcher import Dispatcher

    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)
    dispatcher = Dispatcher()
    register_question_methods(dispatcher, question_broker=broker)

    assert "clarify.respond" in dispatcher.methods()
