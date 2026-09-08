"""The ApprovalBroker's request lifecycle: matching, sessions, duplicates, and timeouts."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from raven.contracts.permissions import ApprovalChoice
from raven.rpc.approval_broker import ApprovalBroker


async def _wait_for_frame(frames: list[dict]) -> dict:
    for _ in range(20):
        if frames:
            return frames[0]
        await asyncio.sleep(0)
    raise AssertionError("approval request was not emitted")


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("allow", ApprovalChoice.ALLOW), ("deny", ApprovalChoice.DENY), ("deny_stop", ApprovalChoice.DENY_STOP)],
)
async def test_response_resolves_matching_request(choice: str, expected: ApprovalChoice) -> None:
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
    assert (await waiting).choice is expected


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
    assert (await waiting).choice is ApprovalChoice.DENY


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
    assert (await waiting).approved


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

    assert result.choice is ApprovalChoice.DENY
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
    assert (await waiting).choice is ApprovalChoice.DENY
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

    outcomes = await asyncio.gather(*waits)
    assert [o.choice for o in outcomes] == [ApprovalChoice.DENY, ApprovalChoice.DENY]


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

    outcome = await broker.await_approval(
        conversation_id="session-a",
        turn_id="turn-a",
        tool_call_id="call-a",
        command="rm file.txt",
        description="Delete files",
    )
    assert outcome.choice is ApprovalChoice.DENY


# ---------------------------------------------------------------------------
# Per-conversation scoping of the notification itself
# (connection.conversation_scoped, the same wrapper the question broker uses)
# ---------------------------------------------------------------------------


def _frame_collector() -> tuple[list[dict], object]:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    return frames, send


async def _hold_connection(sink, conversation_id: str, ready: asyncio.Event, release: asyncio.Event) -> None:
    """Stand in for one live socket: its own connection scope, held open in its
    own task, exactly as a transport holds one while it dispatches frames."""
    from raven.rpc import connection

    token = connection.bind_connection()
    connection.set_frame_sink(sink)
    connection.claim_conversation(conversation_id)
    ready.set()
    try:
        await release.wait()
    finally:
        connection.unbind_connection(token)


async def test_an_approval_reaches_only_the_surface_that_sent_the_turn() -> None:
    """Two terminals attached to one gateway. A protected command run in one
    session must not open the allow/deny overlay in the other, which is not in
    that conversation -- ``approval.request`` and the ``approval.closed`` that
    retires it both carry ``conversation_id``, so both are scopable."""
    from raven.rpc import connection

    broadcast_frames, broadcast = _frame_collector()
    mine_frames, mine_send = _frame_collector()
    other_frames, other_send = _frame_collector()

    release = asyncio.Event()
    mine_ready, other_ready = asyncio.Event(), asyncio.Event()
    mine = asyncio.create_task(_hold_connection(mine_send, "tui:mine", mine_ready, release))
    other = asyncio.create_task(_hold_connection(other_send, "tui:other", other_ready, release))
    await asyncio.wait_for(mine_ready.wait(), 1)
    await asyncio.wait_for(other_ready.wait(), 1)

    try:
        broker = ApprovalBroker(connection.conversation_scoped(broadcast))
        task = asyncio.create_task(
            broker.await_approval(
                conversation_id="tui:mine",
                turn_id="turn-a",
                tool_call_id="call-a",
                command="rm -rf build",
                description="Delete files",
            )
        )
        frame = await _wait_for_frame(mine_frames)
        assert frame["method"] == "approval.request"
        assert frame["params"]["conversation_id"] == "tui:mine"
        assert other_frames == []
        assert broadcast_frames == []

        assert broker.resolve(frame["params"]["approval_id"], "allow", conversation_id="tui:mine") is True
        assert (await task).approved
        assert [f["method"] for f in mine_frames] == ["approval.request", "approval.closed"]
        assert other_frames == []
        assert broadcast_frames == []
    finally:
        release.set()
        await asyncio.gather(mine, other)


async def test_an_unowned_approval_conversation_still_broadcasts() -> None:
    """No connection claimed this conversation (an IM turn, or a transport that
    binds nothing at all), so broadcast stays the fallback: an overlay nobody
    sees would hold the tool call until the backend deadline."""
    from raven.rpc import connection

    broadcast_frames, broadcast = _frame_collector()
    broker = ApprovalBroker(connection.conversation_scoped(broadcast))

    task = asyncio.create_task(
        broker.await_approval(
            conversation_id="weixin:u1",
            turn_id="turn-a",
            tool_call_id="call-a",
            command="rm -rf build",
            description="Delete files",
        )
    )
    frame = await _wait_for_frame(broadcast_frames)
    assert frame["params"]["conversation_id"] == "weixin:u1"
    assert broker.resolve(frame["params"]["approval_id"], "deny", conversation_id="weixin:u1") is True
    assert (await task).choice is ApprovalChoice.DENY


async def test_feedback_rides_along_on_a_refusal() -> None:
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

    assert broker.resolve(params["approval_id"], "deny", conversation_id="session-a", feedback="keep it, move it aside")
    outcome = await waiting
    assert outcome.choice is ApprovalChoice.DENY
    assert outcome.feedback == "keep it, move it aside"
