"""Real SQLite and spawned-process recovery of an interrupted notification intent."""

import multiprocessing
from uuid import uuid4

from raven.mailbox.notifications import MailboxNotifications
from raven.mailbox.store import MailboxStore
from tests.test_mailbox_receiver import receivers as receivers
from tests.test_mailbox_receiver_runtime import native_notice as native_notice
from tests.test_mailbox_store import NOW

CTX = multiprocessing.get_context("spawn")


def persist_native_intent(root, binding, request_id, ids, ready, hold):
    ledger = MailboxNotifications(MailboxStore(root))
    ledger.prepare(binding, request_id=request_id, message_ids=ids, now=NOW)
    ledger.begin(request_id, turn_id=request_id, now=NOW)
    ready.set()
    hold.wait(timeout=30)


async def test_process_death_after_durable_intent_never_resubmits(native_notice):
    runtime, binding, scheduler, _, calls, _, ids = native_notice
    request = str(uuid4())
    ready, hold = CTX.Event(), CTX.Event()
    child = CTX.Process(
        target=persist_native_intent,
        args=(runtime.receivers.store.root, binding, request, ids, ready, hold),
    )
    try:
        child.start()
        assert ready.wait(timeout=20)
        child.kill()
        child.join(timeout=5)
        assert child.exitcode == -9
        fresh = MailboxNotifications(MailboxStore(runtime.receivers.store.root))
        assert fresh.status(request)["stage"] == "submitting"
        assert fresh.status(request)["turn_id"] == request
        assert fresh.recover(now=NOW) == 1
        runtime.notifications = fresh
        result = await runtime.notify(binding.binding_id, request_id=request, message_ids=ids)
        assert result["stage"] == "uncertain"
        assert result["detail"] == "host_restarted"
        assert calls == []
        assert len(runtime.receivers.store.peek(binding.ref, limit=2, now=NOW)) == 1
    finally:
        if child.is_alive():
            child.kill()
        child.join(timeout=5)
        child.close()
        await scheduler.shutdown(grace=0)
