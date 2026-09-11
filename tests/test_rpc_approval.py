"""The approval.respond RPC method: resolution, validation, and registration."""

from __future__ import annotations

import asyncio

from raven.rpc.approval_broker import ApprovalBroker
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods.approval import approval_respond, register_approval_methods


async def test_approval_respond_resolves_matching_request() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send)
    waiting = asyncio.create_task(
        broker.await_approval(
            conversation_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            command="rm file.txt",
            description="Delete files",
        )
    )
    for _ in range(20):
        if frames:
            break
        await asyncio.sleep(0)
    approval_id = frames[0]["params"]["approval_id"]

    result = await approval_respond(
        {
            "approval_id": approval_id,
            "session_id": "session-a",
            "choice": "allow",
        },
        approval_broker=broker,
    )

    assert result == {"ok": True}
    assert (await waiting).approved


async def test_approval_respond_rejects_missing_fields() -> None:
    async def send(frame: dict) -> None:
        pass

    broker = ApprovalBroker(send)

    assert await approval_respond({}, approval_broker=broker) == {"ok": False}
    assert await approval_respond(
        {"approval_id": "missing", "session_id": "session-a", "choice": "always"},
        approval_broker=broker,
    ) == {"ok": False}


def test_register_approval_methods_adds_real_handler() -> None:
    async def send(frame: dict) -> None:
        pass

    dispatcher = Dispatcher()
    register_approval_methods(dispatcher, approval_broker=ApprovalBroker(send))

    assert "approval.respond" in dispatcher.methods()


async def test_approval_respond_forwards_the_pattern() -> None:
    class Broker:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def resolve(self, approval_id, choice, *, conversation_id, feedback="", pattern=""):
            self.calls.append(
                {"id": approval_id, "choice": choice, "conv": conversation_id, "feedback": feedback, "pattern": pattern}
            )
            return True

    broker = Broker()
    out = await approval_respond(
        {"approval_id": "a1", "session_id": "s1", "choice": "allow_always", "pattern": "git push *"},
        approval_broker=broker,
    )
    assert out == {"ok": True}
    assert broker.calls == [
        {"id": "a1", "choice": "allow_always", "conv": "s1", "feedback": "", "pattern": "git push *"}
    ]
