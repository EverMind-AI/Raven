"""Safe pointer submission through existing terminal and native session seams."""

import json
from uuid import uuid4

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.notifications import MailboxNotifications
from tests.test_mailbox_receiver import receivers as receivers
from tests.test_mailbox_store import NOW, SCOPE, wire
from tests.test_terminal_deliver import Clock, Host


@pytest.fixture
def terminal_notice(receivers, monkeypatch):
    from raven.mailbox.receiver_runtime import MailboxReceiver
    from raven.terminal.deliver import DeliveryService

    service, refs, registry, record, _ = receivers
    host, clock = Host(), Clock()
    host.record = record
    host.activity.record = record
    host.activity.provider = "codex"
    record.status = "idle"
    service.host = host
    issued = service.grant("worker", refs[1], SCOPE, request_id=str(uuid4()), capabilities=["poll", "terminal_notify"])
    binding = service.current(issued["binding"]["binding_id"])
    ids = [service.store.send(wire(*refs), refs[0], now=NOW)["message_id"] for _ in range(2)]
    monkeypatch.setattr(MailboxNotifications, "_now", staticmethod(lambda now: NOW if now is None else now))
    runtime = MailboxReceiver(service, host=host, delivery=DeliveryService(host, clock=clock.time, sleep=clock.sleep))
    return runtime, binding, host, ids


async def test_terminal_pointer_is_one_shot_and_does_not_claim(terminal_notice):
    runtime, binding, host, ids = terminal_notice
    request = str(uuid4())
    result = await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
    assert result["stage"] == "input_accepted"
    assert host.writes.count(b"\r") == 1
    content = b"".join(host.writes).decode()
    assert all(message_id in content for message_id in ids)
    assert "idempotency_key" not in content
    assert "claim_token" not in content
    assert runtime.receivers.store.status(binding.ref.agent_id, ids[0])["phase"] == "pending"
    assert await runtime.notify(binding.binding_id, request_id=request, message_ids=ids) == result
    assert host.writes.count(b"\r") == 1


async def test_unknown_terminal_submission_is_not_entered_again(terminal_notice):
    runtime, binding, host, ids = terminal_notice
    host.accept = False
    request = str(uuid4())
    result = await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
    assert result["stage"] == "uncertain"
    assert host.writes.count(b"\r") == 1
    assert result["bytes_written"] > 0
    await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
    assert host.writes.count(b"\r") == 1
    assert len(runtime.receivers.store.peek(binding.ref, limit=2, now=NOW)) == 2


@pytest.mark.parametrize("blocked", ["working", "unknown", "permission", "startup", "human", "provider"])
async def test_unready_terminal_keeps_messages_available(terminal_notice, blocked):
    runtime, binding, host, ids = terminal_notice
    if blocked in {"working", "unknown", "permission"}:
        host.record.status = blocked
    elif blocked == "startup":
        host.activity.startup_pending = True
    elif blocked == "human":
        host.activity.composer_dirty = True
        host.activity.human_input_pending = True
    else:
        host.activity.provider = "unknown"
    result = await runtime.notify(binding.binding_id, request_id=str(uuid4()), message_ids=ids)
    assert result["stage"] == "blocked"
    assert host.writes == []
    assert len(runtime.receivers.store.peek(binding.ref, limit=2, now=NOW)) == 2


@pytest.mark.parametrize("race", ["revoke", "human_input"])
async def test_binding_or_human_change_after_paste_blocks_enter(terminal_notice, race):
    runtime, binding, host, ids = terminal_notice
    original = host.write

    async def revoke_after_paste(handle, data):
        await original(handle, data)
        if race == "revoke":
            runtime.receivers.revoke(binding.binding_id)
        else:
            host.activity.human_input_pending = True

    host.write = revoke_after_paste
    request = str(uuid4())
    result = await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
    assert result["stage"] == "uncertain"
    assert b"\r" not in host.writes
    if race == "revoke":
        with pytest.raises(MailboxError, match="receiver_revoked"):
            await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
    else:
        assert (await runtime.notify(binding.binding_id, request_id=request, message_ids=ids))["stage"] == "uncertain"
    assert len(host.writes) == 1


@pytest.fixture
async def native_notice(receivers, tmp_path, monkeypatch):
    import asyncio

    from raven.mailbox.receiver_runtime import MailboxReceiver
    from raven.session.manager import SessionManager
    from raven.spine.events import Usage
    from raven.spine.runner import TurnOutcome
    from raven.spine.scheduler import OriginPools, Scheduler

    service, refs, registry, _, _ = receivers
    key = "tui:native"
    registry.register("native", kind_ref="worker", session_key=key, task_ref="task")
    issued = service.grant("native", refs[1], SCOPE, request_id=str(uuid4()), capabilities=["poll", "native_notify"])
    binding = service.current(issued["binding"]["binding_id"])
    sessions = SessionManager(tmp_path / "sessions")
    sessions.get_or_create(key)
    calls = []
    finished = asyncio.Event()

    class Runner:
        async def run(self, req, emit, drain):
            calls.append(req)
            finished.set()
            return TurnOutcome(Usage(0, 0, 0), False)

    runtime = None

    async def sink(event):
        await runtime.on_turn_event(event)

    scheduler = Scheduler(Runner(), OriginPools(1, 1), sink)
    runtime = MailboxReceiver(service, scheduler=scheduler, sessions=sessions)
    monkeypatch.setattr(MailboxNotifications, "_now", staticmethod(lambda now: NOW if now is None else now))
    ids = [service.store.send(wire(*refs), refs[0], now=NOW)["message_id"]]
    return runtime, binding, scheduler, sessions, calls, finished, ids


async def test_native_submit_intent_and_correlated_actual_turn_are_distinct(native_notice):
    import asyncio

    from raven.spine.turn import BusyPolicy, Origin

    runtime, binding, scheduler, _, calls, finished, ids = native_notice
    request = str(uuid4())
    try:
        result = await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
        assert result["stage"] == "input_accepted"
        assert result["turn_id"] == request
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert runtime.notifications.status(request)["stage"] == "turn_started"
        assert calls[0].origin is Origin.SUBAGENT
        assert calls[0].busy is BusyPolicy.APPEND
        assert calls[0].conversation == binding.session_key
        assert runtime.receivers.store.status(binding.ref.agent_id, ids[0])["phase"] == "pending"
        await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
        assert len(calls) == 1
    finally:
        await scheduler.shutdown(grace=0)


async def test_native_queued_work_and_human_gate_preserve_pending_messages(native_notice):
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    runtime, binding, scheduler, sessions, _, _, ids = native_notice
    session = sessions.peek(binding.session_key)
    try:
        session.pending_clarification = {"question": "Review this first"}
        result = await runtime.notify(binding.binding_id, request_id=str(uuid4()), message_ids=ids)
        assert result["stage"] == "blocked" and result["detail"] == "receiver_human_gate"
        session.pending_clarification = None
        scheduler.submit(
            TurnRequest(
                Origin.USER,
                Source("tui", "native", "user", ChatType.DM),
                "Human message",
                conversation=binding.session_key,
            )
        )
        assert scheduler.has_work(binding.session_key)
        assert not scheduler.has_inflight(binding.session_key)
        result = await runtime.notify(binding.binding_id, request_id=str(uuid4()), message_ids=ids)
        assert result["stage"] == "blocked" and result["detail"] == "receiver_busy"
        assert runtime.receivers.store.status(binding.ref.agent_id, ids[0])["phase"] == "pending"
    finally:
        await scheduler.shutdown(grace=0)


async def test_native_unknown_launch_retains_known_turn_without_requeue(native_notice, monkeypatch):
    runtime, binding, scheduler, _, _, _, ids = native_notice
    request = str(uuid4())
    calls = []

    def uncertain_submit(req):
        calls.append(req)
        row = runtime.notifications.status(request)
        assert row["stage"] == "submitting" and row["turn_id"] == request
        raise RuntimeError("Response lost after submission boundary")

    monkeypatch.setattr(scheduler, "submit", uncertain_submit)
    try:
        with pytest.raises(RuntimeError):
            await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
        assert runtime.notifications.status(request)["stage"] == "uncertain"
        await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
        assert len(calls) == 1
    finally:
        await scheduler.shutdown(grace=0)


async def test_automatic_scan_notifies_pending_work_once_and_owned_task_stops(native_notice):
    import asyncio

    runtime, binding, scheduler, _, calls, finished, ids = native_notice
    try:
        runtime.start()
        watcher = runtime._watcher
        runtime.start()
        assert runtime._watcher is watcher
        await asyncio.wait_for(finished.wait(), timeout=5)
        await runtime.scan()
        assert len(calls) == 1
        assert runtime.receivers.store.status(binding.ref.agent_id, ids[0])["phase"] == "pending"
        await runtime.close()
        assert watcher.done()
        assert runtime._watcher is None
    finally:
        await runtime.close()
        await scheduler.shutdown(grace=0)


async def test_scan_excludes_notified_ids_before_sixteen_message_limit(native_notice):
    import asyncio

    from raven.contracts.mailbox import MailboxInstanceRef

    runtime, binding, scheduler, _, calls, _, ids = native_notice
    store = runtime.receivers.store
    with store.db.connection() as conn:
        row = conn.execute("SELECT * FROM cards WHERE agent_id != ?", (binding.ref.agent_id,)).fetchone()
    sender = MailboxInstanceRef(
        authority_id=store.db.authority_id,
        tenant_id=store.db.tenant_id,
        agent_id=row["agent_id"],
        instance_id=row["instance_id"],
        generation=row["generation"],
    )
    for _ in range(15):
        ids.append(store.send(wire(sender, binding.ref), sender, now=NOW)["message_id"])
    try:
        await runtime.scan()
        await asyncio.wait_for(scheduler._lanes[binding.session_key]._worker, timeout=5)
        assert len(calls) == 1
        newest = store.send(
            wire(sender, binding.ref, message_id="ffffffff-ffff-4fff-bfff-ffffffffffff"), sender, now=NOW
        )["message_id"]
        await runtime.scan()
        await asyncio.wait_for(scheduler._lanes[binding.session_key]._worker, timeout=5)
        assert len(calls) == 2
        assert newest in calls[1].text
        assert not any(old in calls[1].text for old in ids)
    finally:
        await scheduler.shutdown(grace=0)


async def test_native_tools_require_current_session_binding_and_scope(native_notice, monkeypatch):
    from raven.agent.tools.registry import ToolRegistry
    from raven.agent.tools.terminal import bind_terminal_session
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.mailbox_tools import register_mailbox_tools
    from raven.rpc.methods.mailbox import register_mailbox_methods

    runtime, binding, scheduler, _, _, _, ids = native_notice
    monkeypatch.setattr("raven.mailbox.delivery.time.time", lambda: NOW)
    dispatcher = Dispatcher()
    register_mailbox_methods(dispatcher, receivers=runtime.receivers)
    tools = ToolRegistry()
    unregister = register_mailbox_tools(tools, dispatcher)
    poll = tools.get("mailbox_poll")
    try:
        assert "receiver_unauthorized" in await poll.execute(peek=True)
        with bind_terminal_session("other-session"):
            assert "receiver_unauthorized" in await poll.execute(peek=True)
        with bind_terminal_session(binding.session_key):
            text = await poll.execute(peek=True)
            assert text.startswith("[BEGIN UNTRUSTED peer mailbox")
            from raven.security.trust import unwrap_untrusted

            result = json.loads(unwrap_untrusted(text))
            assert [row["message_id"] for row in result["messages"]] == ids
            escaped = await tools.get("mailbox_send").execute(
                envelope=json.loads(wire(binding.ref, binding.ref, scope={**SCOPE, "task_id": "other"}))
            )
            assert "scope_denied" in escaped
            runtime.receivers.revoke(binding.binding_id)
            assert "receiver_unauthorized" in await poll.execute(peek=True)
        assert tools.get("mailbox_commit") is None
    finally:
        unregister()
        assert tools.get("mailbox_poll") is None
        await scheduler.shutdown(grace=0)
