"""Durable exact-batch notification intent and uncertainty checks."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.notifications import MailboxNotifications
from tests.test_mailbox_store import NOW, SCOPE, wire
from tests.test_mailbox_store import mailbox as mailbox


@pytest.fixture
def notices(mailbox):
    store, sender, ref = mailbox
    store.db.upgrade_receivers()
    binding_id = str(uuid4())
    binding = SimpleNamespace(binding_id=binding_id, ref=ref, scope=SCOPE, terminal_incarnation=str(uuid4()))
    with store.db.connection(write=True) as conn:
        conn.execute(
            "INSERT INTO receiver_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding_id,
                ref.agent_id,
                ref.instance_id,
                ref.generation,
                "worker",
                1,
                SCOPE["task_id"],
                SCOPE["workspace_id"],
                "term",
                binding.terminal_incarnation,
                None,
                "[]",
                "not-a-credential",
                None,
                NOW,
            ),
        )
    ids = [store.send(wire(sender, ref), sender, now=NOW)["message_id"] for _ in range(2)]
    return MailboxNotifications(store), binding, ids


def test_exact_notification_replay_and_changed_batch_conflict(notices):
    notifications, binding, ids = notices
    request = str(uuid4())
    first = notifications.prepare(binding, request_id=request, message_ids=[ids[0]], now=NOW)
    assert notifications.prepare(binding, request_id=request, message_ids=[ids[0]], now=NOW + 1) == first
    with pytest.raises(MailboxError, match="request_conflict"):
        notifications.prepare(binding, request_id=request, message_ids=[ids[1]], now=NOW)
    assert first["message_ids"] == [ids[0]]
    assert "envelope" not in json.dumps(first)
    with notifications.store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


def test_submission_is_durable_and_never_reentered_when_unknown(notices):
    notifications, binding, ids = notices
    request = str(uuid4())
    notifications.prepare(binding, request_id=request, message_ids=ids, now=NOW)
    assert notifications.begin(request, now=NOW)
    assert not notifications.begin(request, now=NOW)
    restarted = MailboxNotifications(notifications.store)
    assert restarted.recover(now=NOW + 1) == 1
    assert restarted.status(request)["stage"] == "uncertain"
    assert not restarted.begin(request, now=NOW + 1)
    assert len(notifications.store.peek(binding.ref, limit=2, now=NOW + 1)) == 2


def test_correlated_evidence_is_distinct_from_processing(notices):
    notifications, binding, ids = notices
    request = str(uuid4())
    notifications.prepare(binding, request_id=request, message_ids=ids, now=NOW)
    assert notifications.begin(request, now=NOW)
    notifications.record(request, "input_accepted", bytes_written=25, now=NOW)
    notifications.record(request, "turn_started", turn_id="actual-turn", now=NOW)
    assert notifications.status(request)["stage"] == "turn_started"
    assert notifications.status(request)["turn_id"] == "actual-turn"
    assert all(notifications.store.status(binding.ref.agent_id, item)["phase"] == "pending" for item in ids)
    assert not notifications.begin(request, now=NOW)


def test_preflight_block_can_retry_without_prior_submission(notices):
    notifications, binding, ids = notices
    request = str(uuid4())
    notifications.prepare(binding, request_id=request, message_ids=ids, now=NOW)
    notifications.record(request, "blocked", detail="permission", now=NOW)
    assert notifications.begin(request, now=NOW + 1)
    notifications.record(request, "uncertain", detail="response_lost", now=NOW + 1)
    assert not notifications.begin(request, now=NOW + 2)
    with pytest.raises(MailboxError, match="notification_transition"):
        notifications.record(request, "blocked", now=NOW + 2)


def test_batch_and_binding_scope_are_checked(notices):
    notifications, binding, ids = notices
    with pytest.raises(MailboxError, match="invalid_notification"):
        notifications.prepare(binding, request_id=str(uuid4()), message_ids=[ids[0], ids[0]], now=NOW)
    changed = SimpleNamespace(**{**vars(binding), "scope": {"task_id": "other", "workspace_id": "workspace"}})
    with pytest.raises(MailboxError, match="scope_denied"):
        notifications.prepare(changed, request_id=str(uuid4()), message_ids=ids, now=NOW)
    with notifications.store.db.connection(write=True) as conn:
        conn.execute("UPDATE receiver_bindings SET revoked_at=?", (NOW,))
    with pytest.raises(MailboxError, match="receiver_fenced"):
        notifications.prepare(binding, request_id=str(uuid4()), message_ids=ids, now=NOW)
