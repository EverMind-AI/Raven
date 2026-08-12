from __future__ import annotations

import asyncio
import hashlib

import pytest

from raven.tui_rpc.approval_broker import ApprovalBroker


async def _wait_for_frame(frames: list[dict]) -> dict:
    for _ in range(20):
        if frames:
            return frames[0]
        await asyncio.sleep(0)
    raise AssertionError("approval request was not emitted")


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("allow", True), ("deny", False)],
)
async def test_response_resolves_matching_request(choice: str, expected: bool) -> None:
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
    frame = await _wait_for_frame(frames)
    params = frame["params"]

    assert frame["method"] == "approval.request"
    assert params["conversation_id"] == "session-a"
    assert params["turn_id"] == "turn-a"
    assert params["tool_call_id"] == "call-a"
    assert params["command"] == "rm file.txt"
    assert params["action_digest"] == hashlib.sha256(b"rm file.txt").hexdigest()
    assert broker.resolve(params["approval_id"], choice, conversation_id="session-a") is True
    assert await waiting is expected


async def test_wrong_session_cannot_resolve_request() -> None:
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
    params = (await _wait_for_frame(frames))["params"]

    assert broker.resolve(params["approval_id"], "allow", conversation_id="session-b") is False
    assert broker.resolve(params["approval_id"], "deny", conversation_id="session-a") is True
    assert await waiting is False


async def test_duplicate_and_invalid_responses_are_rejected() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send)
    waiting = asyncio.create_task(
        broker.await_approval(
            conversation_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            command="unlink file.txt",
            description="Delete files",
        )
    )
    approval_id = (await _wait_for_frame(frames))["params"]["approval_id"]

    assert broker.resolve(approval_id, "always", conversation_id="session-a") is False
    assert broker.resolve(approval_id, "allow", conversation_id="session-a") is True
    assert broker.resolve(approval_id, "deny", conversation_id="session-a") is False
    assert await waiting is True


async def test_timeout_denies_and_expires_request() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send, visible_timeout_s=0.01, hard_timeout_s=0.02)
    result = await broker.await_approval(
        conversation_id="session-a",
        turn_id="turn-a",
        tool_call_id="call-a",
        command="rm file.txt",
        description="Delete files",
    )
    approval_id = frames[0]["params"]["approval_id"]

    assert result is False
    assert frames[1] == {
        "jsonrpc": "2.0",
        "method": "approval.closed",
        "params": {
            "approval_id": approval_id,
            "conversation_id": "session-a",
            "reason": "timeout",
        },
    }
    assert broker.resolve(approval_id, "allow", conversation_id="session-a") is False


async def test_request_exposes_the_shorter_frontend_deadline() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send, visible_timeout_s=30, hard_timeout_s=35)
    waiting = asyncio.create_task(
        broker.await_approval(
            conversation_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            command="rm file.txt",
            description="Delete files",
        )
    )
    params = (await _wait_for_frame(frames))["params"]

    assert 29 <= params["expires_at"] - params["created_at"] <= 30
    assert broker.resolve(params["approval_id"], "deny", conversation_id="session-a") is True
    assert await waiting is False
    assert frames[-1]["method"] == "approval.closed"
    assert frames[-1]["params"]["reason"] == "deny"


async def test_cancel_all_denies_every_pending_request() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send)
    waits = [
        asyncio.create_task(
            broker.await_approval(
                conversation_id=f"session-{index}",
                turn_id=f"turn-{index}",
                tool_call_id=f"call-{index}",
                command=f"rm file-{index}",
                description="Delete files",
            )
        )
        for index in range(2)
    ]
    for _ in range(20):
        if len(frames) == 2:
            break
        await asyncio.sleep(0)

    broker.cancel_all()

    assert await asyncio.gather(*waits) == [False, False]


async def test_task_cancellation_expires_request() -> None:
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
    approval_id = (await _wait_for_frame(frames))["params"]["approval_id"]

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert broker.resolve(approval_id, "allow", conversation_id="session-a") is False


async def test_send_failure_denies_request() -> None:
    async def send(frame: dict) -> None:
        raise RuntimeError("disconnected")

    broker = ApprovalBroker(send)

    assert (
        await broker.await_approval(
            conversation_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            command="rm file.txt",
            description="Delete files",
        )
        is False
    )
