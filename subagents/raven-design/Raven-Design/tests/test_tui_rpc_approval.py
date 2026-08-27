from __future__ import annotations

import asyncio

from raven.tui_rpc.approval_broker import ApprovalBroker
from raven.tui_rpc.dispatcher import Dispatcher
from raven.tui_rpc.methods.approval import approval_respond, register_approval_methods


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
    assert await waiting is True


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
