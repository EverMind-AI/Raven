"""The approval.respond and approval.revoke RPC methods: resolution, validation, and registration."""

from __future__ import annotations

import asyncio

from raven.rpc.approval_broker import ApprovalBroker
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods.approval import (
    approval_pending,
    approval_respond,
    approval_revoke,
    register_approval_methods,
)


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
    assert "approval.revoke" in dispatcher.methods()
    assert "approval.pending" in dispatcher.methods()


async def _answered_grant(broker: ApprovalBroker, frames: list[dict], pattern: str) -> str:
    """One allow_always round-trip, returning the approval id it was answered under."""
    waiting = asyncio.create_task(
        broker.await_approval(
            conversation_id="session-a",
            turn_id="turn-a",
            tool_call_id="call-a",
            command="git push origin HEAD",
            description="Push",
            suggested_pattern=pattern,
        )
    )
    for _ in range(20):
        if frames:
            break
        await asyncio.sleep(0)
    approval_id = frames[0]["params"]["approval_id"]
    broker.resolve(approval_id, "allow_always", conversation_id="session-a", pattern=pattern)
    assert (await waiting).approval_id == approval_id, "the answer names the request, or no undo can be about it"
    return approval_id


def _collector() -> tuple[list[dict], object]:
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    return frames, send


async def test_approval_revoke_takes_back_only_what_that_answer_wrote(monkeypatch) -> None:
    """The undo names the answer, not the rule's text: the engine's own record of
    what its write did is what decides, so a rule the reader already had (which
    ``_persist`` merely kept) is never this prompt's to remove."""
    from raven.config import update

    removed: list[str] = []
    monkeypatch.setattr(update, "remove_exec_pattern", lambda pattern: removed.append(pattern) or True)

    frames, send = _collector()
    broker = ApprovalBroker(send)
    mine = await _answered_grant(broker, frames, "git push *")
    broker.record_grant(mine, "git push *", True)
    assert await approval_revoke({"approval_id": mine}, approval_broker=broker) == {"ok": True}
    assert removed == ["git push *"]

    # Read once: a second undo of the same grant cannot reach a rule some later
    # prompt wrote under the same text.
    assert await approval_revoke({"approval_id": mine}, approval_broker=broker) == {"ok": False}
    assert removed == ["git push *"]

    frames.clear()
    theirs = await _answered_grant(broker, frames, "git push *")
    broker.record_grant(theirs, "git push *", False)
    assert await approval_revoke({"approval_id": theirs}, approval_broker=broker) == {"ok": False}
    assert removed == ["git push *"], "a rule the reader already had is not the prompt's to take away"

    assert await approval_revoke({"approval_id": ""}, approval_broker=broker) == {"ok": False}
    assert await approval_revoke({"approval_id": "never-asked"}, approval_broker=broker) == {"ok": False}


async def test_approval_revoke_waits_for_the_engine_to_say_what_it_wrote(monkeypatch) -> None:
    """Measured: ``approval.respond`` answers the page BEFORE the gate persists.
    An undo matching on the rule's text would find nothing and report a failure
    the reader would have to disbelieve, so this one waits for the word."""
    from raven.config import update

    removed: list[str] = []
    monkeypatch.setattr(update, "remove_exec_pattern", lambda pattern: removed.append(pattern) or True)

    frames, send = _collector()
    broker = ApprovalBroker(send)
    approval_id = await _answered_grant(broker, frames, "git push *")

    undo = asyncio.create_task(approval_revoke({"approval_id": approval_id}, approval_broker=broker))
    await asyncio.sleep(0.05)
    assert not undo.done(), "the undo waits rather than answering before the write"

    broker.record_grant(approval_id, "git push *", True)
    assert await undo == {"ok": True}
    assert removed == ["git push *"]


async def test_a_grant_the_engine_never_reports_on_is_nothing_to_take_back() -> None:
    frames, send = _collector()
    broker = ApprovalBroker(send)
    approval_id = await _answered_grant(broker, frames, "git push *")

    assert await broker.written_pattern(approval_id, timeout_s=0.02) is None


async def test_approval_revoke_reports_a_failed_write_rather_than_raising(monkeypatch) -> None:
    from raven.config import update

    def boom(pattern: str) -> bool:
        raise OSError("read-only file system")

    monkeypatch.setattr(update, "remove_exec_pattern", boom)

    frames, send = _collector()
    broker = ApprovalBroker(send)
    approval_id = await _answered_grant(broker, frames, "git push *")
    broker.record_grant(approval_id, "git push *", True)

    assert await approval_revoke({"approval_id": approval_id}, approval_broker=broker) == {"ok": False}


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


async def test_approval_pending_lists_what_is_still_open_as_it_was_sent() -> None:
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
            kind="shell.exec",
        )
    )
    for _ in range(20):
        if frames:
            break
        await asyncio.sleep(0)

    everything = await approval_pending({}, approval_broker=broker)
    assert everything == {"requests": [frames[0]["params"]]}
    assert await approval_pending({"session_id": "session-a"}, approval_broker=broker) == everything
    assert await approval_pending({"session_id": "session-b"}, approval_broker=broker) == {"requests": []}

    broker.resolve(frames[0]["params"]["approval_id"], "deny", conversation_id="session-a")
    await waiting
    assert await approval_pending({}, approval_broker=broker) == {"requests": []}


async def test_approval_pending_hands_back_only_this_connection_s_own_requests() -> None:
    """The recovery is scoped the way the original notification was. A record
    carries both opaque ids, so handing another socket's request over would let
    that socket answer a command it was never asked about -- the boundary
    ``connection.conversation_scoped`` draws for the push, drawn again for the pull.
    """
    from raven.rpc import connection

    frames, send = _collector()
    broker = ApprovalBroker(send)
    waits = [
        asyncio.create_task(
            broker.await_approval(
                conversation_id=cid, turn_id="t", tool_call_id=f"c-{cid}", command=f"rm {cid}", description="Delete"
            )
        )
        for cid in ("tui:mine", "tui:theirs", "cron:nobody")
    ]
    for _ in range(40):
        if len(frames) == 3:
            break
        await asyncio.sleep(0)

    token = connection.bind_connection()
    try:
        connection.claim_conversation("tui:mine")
        # A second socket claims the other one, so it is owned and not mine.
        other = asyncio.get_running_loop().create_future()

        async def hold() -> None:
            inner = connection.bind_connection()
            connection.claim_conversation("tui:theirs")
            other.set_result(None)
            await asyncio.sleep(0.05)
            connection.unbind_connection(inner)

        held = asyncio.create_task(hold())
        await other

        answer = await approval_pending({}, approval_broker=broker)
        seen = sorted(r["conversation_id"] for r in answer["requests"])
        # Mine, and the one nobody claimed (its request was broadcast to every
        # surface anyway, so hiding it would lose a question nobody can answer).
        assert seen == ["cron:nobody", "tui:mine"]

        scoped = await approval_pending({"session_id": "tui:theirs"}, approval_broker=broker)
        assert scoped == {"requests": []}, "naming another socket's conversation does not lift the boundary"
        await held
    finally:
        connection.unbind_connection(token)
        for frame in frames:
            broker.resolve(frame["params"]["approval_id"], "deny", conversation_id=frame["params"]["conversation_id"])
        await asyncio.gather(*waits)
