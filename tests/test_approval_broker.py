"""The ApprovalBroker's request lifecycle: matching, sessions, duplicates, and timeouts."""

from __future__ import annotations

import asyncio

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
    # allow_always with nothing to persist is not an answer this side can act on.
    assert broker.resolve(approval_id, "allow_always", conversation_id="session-a") is False
    assert broker.resolve(approval_id, "allow", conversation_id="session-a") is True
    assert broker.resolve(approval_id, "deny", conversation_id="session-a") is False
    assert (await waiting).approved


async def test_timeout_denies_and_expires_request() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send, hard_timeout_s=0.02)
    result = await broker.await_approval(
        conversation_id="session-a",
        turn_id="turn-a",
        tool_call_id="call-a",
        command="rm file.txt",
        description="Delete files",
    )
    approval_id = frames[0]["params"]["approval_id"]

    assert result.choice is ApprovalChoice.DENY
    # Denied, and by nobody. The gate says the two differently, and this is the
    # only place that knows which of them happened.
    assert result.answered is False
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


async def test_the_request_carries_the_prompts_view_and_no_deadline() -> None:
    """What a surface draws the prompt from rides on the request as given, and
    nothing on it says when the question expires, because it does not."""
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send)
    waiting = asyncio.create_task(
        broker.await_approval(
            conversation_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            command="rm coverage.xml",
            description="Delete files",
            kind="shell.exec",
            family="delete_command",
            origin="subagent",
            origin_name="raven-code",
            evidence={"command": "rm coverage.xml", "cwd": "/w"},
        )
    )
    params = (await _wait_for_frame(frames))["params"]

    assert params["kind"] == "shell.exec"
    assert params["family"] == "delete_command"
    assert params["origin"] == {"kind": "subagent", "name": "raven-code"}
    assert params["evidence"] == {"command": "rm coverage.xml", "cwd": "/w"}
    assert not {"created_at", "expires_at", "action_digest"} & params.keys()
    await asyncio.sleep(0.05)
    assert not waiting.done(), "with no ceiling set the question stays open"
    assert broker.resolve(params["approval_id"], "deny", conversation_id="session-a") is True
    assert (await waiting).choice is ApprovalChoice.DENY
    assert frames[-1]["method"] == "approval.closed"
    assert frames[-1]["params"]["reason"] == "deny"


async def test_a_request_without_a_view_still_names_its_kind() -> None:
    """A caller handing over only the action line -- an older responder, a
    fake -- still produces a frame a client can lay out."""
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

    assert params["kind"] == "unknown"
    assert params["family"] == ""
    assert params["origin"] == {"kind": "", "name": ""}
    assert params["evidence"] == {}
    assert broker.resolve(params["approval_id"], "allow", conversation_id="session-a") is True
    assert (await waiting).approved


def test_a_ceiling_must_be_positive_when_set() -> None:
    async def send(frame: dict) -> None:
        pass

    with pytest.raises(ValueError):
        ApprovalBroker(send, hard_timeout_s=0)


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
    # A request that never reached anybody is the clearest case of nobody having
    # answered it, and the gate's two sentences turn on exactly that.
    assert outcome.answered is False


async def test_teardown_cancellation_is_not_a_refusal() -> None:
    """`cancel_all` puts a synthetic "cancelled" on the future, which is not a
    wire choice and lands in the same `ValueError` fallback as a client sending
    something outside the enum. Both fail closed, and neither is anyone's
    answer -- a turn torn down mid-approval must not tell the model the user
    refused."""
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
    await _wait_for_frame(frames)

    broker.cancel_all()
    outcome = await waiting

    assert outcome.choice is ApprovalChoice.DENY
    assert outcome.answered is False


async def test_a_choice_outside_the_enum_is_not_a_refusal_either() -> None:
    """A client sending a value this side cannot read said nothing this side can
    act on. Same fallback, same reason."""
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

    broker._pending[params["approval_id"]].future.set_result(("maybe", ""))
    outcome = await waiting

    assert outcome.choice is ApprovalChoice.DENY
    assert outcome.answered is False


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
    # A person clicked deny, so this one WAS answered -- the other half of the
    # distinction, without which every refusal would read as a lapse.
    assert outcome.answered is True
    assert outcome.choice is ApprovalChoice.DENY
    assert outcome.feedback == "keep it, move it aside"


@pytest.mark.asyncio
async def test_the_session_grant_and_the_pattern_travel_back() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send, hard_timeout_s=2.0)
    waiting = asyncio.ensure_future(
        broker.await_approval(
            conversation_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-1",
            command="git push origin HEAD",
            description="Push",
            suggested_pattern="git push *",
        )
    )
    await asyncio.sleep(0)
    params = frames[0]["params"]
    assert params["suggested_pattern"] == "git push *"

    assert (
        broker.resolve(params["approval_id"], "allow_always", conversation_id="session-a", pattern=" git push * ")
        is True
    )
    outcome = await waiting
    assert outcome.choice is ApprovalChoice.ALLOW_ALWAYS
    assert outcome.approved is True
    assert outcome.pattern == "git push *"

    waiting = asyncio.ensure_future(
        broker.await_approval(
            conversation_id="session-a", turn_id="turn-a", tool_call_id="call-2", command="git push", description="Push"
        )
    )
    await asyncio.sleep(0)
    assert frames[-1]["params"]["suggested_pattern"] == ""
    assert broker.resolve(frames[-1]["params"]["approval_id"], "allow_session", conversation_id="session-a") is True
    outcome = await waiting
    assert outcome.choice is ApprovalChoice.ALLOW_SESSION
    assert outcome.approved is True
    assert outcome.pattern == ""


async def test_a_second_request_in_the_same_conversation_waits_for_the_first() -> None:
    """Both surfaces draw one prompt per conversation and replace it when
    another lands, so two open at once would take the first off the screen
    with nobody having answered it -- and nothing behind it would ever end
    that call. The second is sent once the first is answered."""
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send)
    ask = lambda call, command: broker.await_approval(  # noqa: E731
        conversation_id="session-a", turn_id="turn-a", tool_call_id=call, command=command, description="Delete"
    )
    first = asyncio.create_task(ask("call-1", "rm a"))
    second = asyncio.create_task(ask("call-2", "rm b"))
    await _wait_for_frame(frames)
    await asyncio.sleep(0.02)

    requests = [f["params"]["command"] for f in frames if f["method"] == "approval.request"]
    assert requests == ["rm a"], "the second request is not on the wire while the first is open"
    assert broker.pending("session-a") and broker.pending("session-a")[0]["command"] == "rm a"

    assert broker.resolve(frames[0]["params"]["approval_id"], "allow", conversation_id="session-a") is True
    assert (await first).approved
    for _ in range(20):
        if len([f for f in frames if f["method"] == "approval.request"]) == 2:
            break
        await asyncio.sleep(0)
    requests = [f["params"]["command"] for f in frames if f["method"] == "approval.request"]
    assert requests == ["rm a", "rm b"]
    assert broker.resolve(frames[-1]["params"]["approval_id"], "deny", conversation_id="session-a") is True
    assert (await second).choice is ApprovalChoice.DENY


async def test_conversations_do_not_wait_on_one_another() -> None:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    broker = ApprovalBroker(send)
    tasks = [
        asyncio.create_task(
            broker.await_approval(
                conversation_id=f"session-{i}",
                turn_id="t",
                tool_call_id=f"c{i}",
                command=f"rm {i}",
                description="Delete",
            )
        )
        for i in range(2)
    ]
    for _ in range(20):
        if len(frames) == 2:
            break
        await asyncio.sleep(0)

    assert sorted(f["params"]["conversation_id"] for f in frames) == ["session-0", "session-1"]
    assert len(broker.pending()) == 2
    assert [p["command"] for p in broker.pending("session-1")] == ["rm 1"]
    for frame in frames:
        broker.resolve(frame["params"]["approval_id"], "deny", conversation_id=frame["params"]["conversation_id"])
    await asyncio.gather(*tasks)
    assert broker.pending() == []


def test_the_default_ceiling_is_a_floor_nobody_present_reaches() -> None:
    """A day: long enough that a person who stepped away finds the prompt
    still waiting, short enough that "waits forever" is not a state."""
    from raven.rpc.approval_broker import DEFAULT_HARD_TIMEOUT_S

    async def send(frame: dict) -> None:
        pass

    assert DEFAULT_HARD_TIMEOUT_S == 24 * 3600
    assert ApprovalBroker(send)._hard_timeout_s == DEFAULT_HARD_TIMEOUT_S
