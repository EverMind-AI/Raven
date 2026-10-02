"""Behavioral lease, result, receipt, and retention tests for local delivery."""

import hashlib
import json
import os
from uuid import uuid4

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import canonical_bytes, encode_envelope, seal_envelope
from raven.mailbox.delivery import MailboxDelivery
from raven.mailbox.store import MailboxStore
from tests.test_mailbox_store import AUTH, NOW, wire
from tests.test_mailbox_store import mailbox as mailbox


def test_poll_replays_without_claiming_second_message(mailbox):
    store, sender, target = mailbox
    first = store.send(wire(sender, target), sender, now=NOW)
    second = store.send(wire(sender, target), sender, now=NOW)
    delivery = MailboxDelivery(store)
    request = str(uuid4())
    claims = delivery.poll(target, request_id=request, now=NOW)
    assert len(claims) == 1
    assert claims[0].envelope.message_id == min(first["message_id"], second["message_id"])
    assert claims[0].attempt == 1
    assert len(claims[0].claim_token) == 64
    assert claims[0].lease_until == NOW + 60
    assert delivery.poll(target, request_id=request, now=NOW + 1) == claims
    assert delivery.poll(target, request_id=str(uuid4()), now=NOW + 1) == []
    with pytest.raises(MailboxError, match="stale_claim"):
        delivery.poll(target, request_id=request, now=NOW + 60)


@pytest.mark.parametrize("seconds", [9, 3601, True])
def test_lease_bounds(mailbox, seconds):
    store, _, target = mailbox
    with pytest.raises(MailboxError, match="invalid_lease"):
        MailboxDelivery(store).poll(target, request_id=str(uuid4()), lease_seconds=seconds, now=NOW)


def test_expiry_recovery_fences_old_claim_and_renew_replay(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, ttl=3600), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), lease_seconds=10, now=NOW)[0]
    request = str(uuid4())
    renewed = delivery.renew(target, claim, request_id=request, lease_seconds=10, now=NOW + 1)
    assert renewed.lease_until == NOW + 11
    assert delivery.renew(target, claim, request_id=request, lease_seconds=10, now=NOW + 2) == renewed
    delivery.recover(now=NOW + 11)
    replacement = delivery.poll(target, request_id=str(uuid4()), now=NOW + 14)[0]
    assert replacement.attempt == 2
    assert replacement.claim_token != claim.claim_token
    with pytest.raises(MailboxError, match="stale_claim"):
        delivery.renew(target, renewed, request_id=str(uuid4()), now=NOW + 14)
    new = store.resume(target, request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="instance_fenced"):
        delivery.poll(target, request_id=str(uuid4()), now=NOW + 14)
    delivery.recover(now=NOW + 14)
    assert delivery.poll(new, request_id=str(uuid4()), now=NOW + 18)[0].attempt == 3


def result(summary="Done", **changes):
    return dict(outcome="succeeded", summary=summary, evidence=[], **changes)


@pytest.mark.parametrize("policy,events", [("none", 0), ("terminal", 1), ("received_and_terminal", 2)])
def test_finish_atomic_events_and_expired_exact_replay(mailbox, policy, events):
    store, sender, target = mailbox
    sent = store.send(wire(sender, target, receipt_policy=policy), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    status = delivery.finish(target, claim, result(), now=NOW + 1)
    assert status["phase"] == "completed"
    assert status["outcome"] == "succeeded"
    assert len(status["result_hash"]) == 64
    assert delivery.finish(target, claim, result(), now=NOW + 100) == status
    with pytest.raises(MailboxError, match="ack_conflict"):
        delivery.finish(target, claim, result("Changed"), now=NOW + 100)
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM receipt_outbox").fetchone()[0] == events
    assert delivery.flush_receipts(target, now=NOW + 2)["published"] == events
    receipts = store.peek(sender, limit=16, now=NOW + 2)
    assert len(receipts) == events
    for entry in receipts:
        assert entry["envelope"]["in_reply_to"] == sent["message_id"]
        assert entry["envelope"]["receipt_policy"] == "none"
        assert entry["envelope"]["target_identity"]["instance_id"] is None
    replaced = store.resume(target, request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="instance_fenced"):
        delivery.finish(target, claim, result(), now=NOW + 100)
    with pytest.raises(MailboxError, match="stale_claim"):
        delivery.finish(replaced, claim, result(), now=NOW + 100)


def test_result_and_receipt_size_failure_preserve_valid_claim(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    with pytest.raises(MailboxError, match="result_too_large"):
        delivery.finish(target, claim, result("x" * 8192), now=NOW + 1)
    with pytest.raises(MailboxError, match="receipt_too_large"):
        delivery.reject(target, claim, reason="x" * 16384, now=NOW + 1)
    assert store.status(target.agent_id, claim.envelope.message_id)["phase"] == "in_progress"
    assert delivery.finish(target, claim, result(), now=NOW + 1)["phase"] == "completed"


def test_retry_backoff_once_and_no_sixth_claim(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, ttl=3600), sender, now=NOW)
    delivery = MailboxDelivery(store)
    clock = NOW
    for attempt in range(1, 6):
        claim = delivery.poll(target, request_id=str(uuid4()), lease_seconds=10, now=clock)[0]
        assert claim.attempt == attempt
        status = delivery.retry(target, claim, reason="temporarily_busy", now=clock)
        if attempt == 5:
            assert status["phase"] == "dead_letter"
            break
        with store.db.connection() as conn:
            not_before = conn.execute("SELECT not_before FROM messages").fetchone()[0]
        assert clock + 2 ** (attempt - 1) <= not_before <= clock + 2 ** (attempt - 1) + 1
        delivery.recover(now=clock)
        with store.db.connection() as conn:
            assert conn.execute("SELECT not_before FROM messages").fetchone()[0] == not_before
        assert delivery.poll(target, request_id=str(uuid4()), now=not_before - 1) == []
        clock = not_before
    assert delivery.poll(target, request_id=str(uuid4()), now=clock + 100) == []
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM receipt_outbox").fetchone()[0] == 6


def test_finish_rejects_unverified_evidence_and_unsupported_outcome(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    with pytest.raises(MailboxError, match="invalid_result"):
        delivery.finish(target, claim, dict(outcome="accepted", summary="", evidence=[]), now=NOW)
    with pytest.raises(MailboxError, match="evidence_unavailable"):
        delivery.finish(target, claim, dict(outcome="succeeded", summary="", evidence=["a" * 64]), now=NOW)


def test_outbox_crash_replays_frozen_historical_receipt_only(mailbox, monkeypatch):
    store, sender, target = mailbox
    store.send(wire(sender, target, receipt_policy="terminal"), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    delivery.finish(target, claim, result(), now=NOW)
    real_send = store.send

    def response_lost(*args, **kwargs):
        real_send(*args, **kwargs)
        raise MailboxError("commit_unknown", retryable=True)

    monkeypatch.setattr(store, "send", response_lost)
    assert delivery.flush_receipts(target, now=NOW)["pending"] == 1
    with store.db.connection() as conn:
        original = dict(conn.execute("SELECT * FROM receipt_outbox").fetchone())
    new = store.resume(target, request_id=str(uuid4()))
    monkeypatch.setattr(store, "send", real_send)
    with pytest.raises(MailboxError, match="instance_fenced"):
        real_send(original["envelope_bytes"], target, now=NOW)
    assert delivery.flush_receipts(new, now=NOW + 1)["published"] == 1
    with store.db.connection() as conn:
        frozen = dict(conn.execute("SELECT * FROM receipt_outbox").fetchone())
        assert frozen["envelope_bytes"] == original["envelope_bytes"]
        assert conn.execute("SELECT count(*) FROM messages WHERE recipient=?", (sender.agent_id,)).fetchone()[0] == 1
    forged = json.loads(original["envelope_bytes"])
    forged["message_id"] = str(uuid4())
    with pytest.raises(MailboxError, match="sender_mismatch"):
        real_send(encode_envelope(seal_envelope(forged)), new, now=NOW)


def test_recovery_materializes_only_current_owner_and_expired_receipt_stays_frozen(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, ttl=10, receipt_policy="terminal"), sender, now=NOW)
    delivery = MailboxDelivery(store)
    assert delivery.recover(now=NOW + 10) == 1
    with store.db.connection() as conn:
        row = conn.execute("SELECT * FROM receipt_outbox").fetchone()
        assert row["status"] == "unmaterialized"
        assert row["envelope_bytes"] is None
    new = store.resume(target, request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="instance_fenced"):
        delivery.flush_receipts(target, now=NOW + 10)
    store.configure_quotas(record_limit=0)
    assert delivery.flush_receipts(new, now=NOW + 10)["pending"] == 1
    with store.db.connection() as conn:
        frozen = dict(conn.execute("SELECT * FROM receipt_outbox").fetchone())
    expired = delivery.flush_receipts(new, now=NOW + 10 + 604800)
    assert expired["delivery_unknown"] == 1
    store.configure_quotas(record_limit=100)
    assert delivery.flush_receipts(new, now=NOW + 11 + 604800)["delivery_unknown"] == 1
    with store.db.connection() as conn:
        row = conn.execute("SELECT * FROM receipt_outbox").fetchone()
        assert row["receipt_id"] == frozen["receipt_id"]
        assert row["envelope_bytes"] == frozen["envelope_bytes"]
    assert delivery.gc(now=NOW + 40 * 86400)["compacted"] == 0


def test_gc_preserves_terminal_dedup_receipt_links_and_old_ack(mailbox):
    store, sender, target = mailbox
    raw = wire(sender, target, receipt_policy="terminal")
    sent = store.send(raw, sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    finished = delivery.finish(target, claim, result(), now=NOW)
    assert delivery.gc(now=NOW + 10, retention_seconds=10)["compacted"] == 0
    delivery.flush_receipts(target, now=NOW + 10)
    assert delivery.gc(now=NOW + 10, retention_seconds=10)["compacted"] == 1
    assert store.send(raw, sender, now=NOW + 100)["result_hash"] == finished["result_hash"]
    with pytest.raises(MailboxError, match="terminal_compacted"):
        delivery.finish(target, claim, result(), now=NOW + 100)
    with store.db.connection() as conn:
        tombstone = conn.execute("SELECT * FROM tombstones").fetchone()
        assert json.loads(tombstone["receipt_links"])[0]["event_key"] == "terminal"
        assert tombstone["digest"] == sent["digest"]
    assert delivery.gc(now=NOW + 30 * 86400 + 1)["tombstones_deleted"] == 1
    with pytest.raises(MailboxError, match="message_expired"):
        store.send(raw, sender, now=NOW + 30 * 86400 + 1)


def test_recovery_precedence_and_corrupt_wire_visibility(mailbox):
    store, sender, target = mailbox
    delivery = MailboxDelivery(store)
    pinned = target.model_dump(exclude={"generation"})
    sent = store.send(wire(sender, target, target_identity=pinned), sender, now=NOW)
    delivery.poll(target, request_id=str(uuid4()), now=NOW)
    store.resume(target, request_id=str(uuid4()))
    delivery.recover(now=NOW + 60)
    assert store.status(target.agent_id, sent["message_id"])["terminal_reason"] == "message_expired"
    live = store.send(wire(sender, target, ttl=3600), sender, now=NOW)
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE messages SET envelope_bytes=? WHERE message_id=?", (b"{}", live["message_id"]))
    with pytest.raises(MailboxError, match="storage_conflict"):
        delivery.recover(now=NOW + 100)


def test_quarantine_limits_raw_metadata_quota_and_no_receipt(mailbox):
    store, _, _ = mailbox
    delivery = MailboxDelivery(store)
    assert delivery.quarantine(b"not JSON", reason="invalid_json", now=NOW) == 1
    with pytest.raises(MailboxError, match="quarantine_too_large"):
        delivery.quarantine(b"x" * 65537, reason="invalid_json", now=NOW)
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE metadata SET value='65536' WHERE key='quarantine_bytes'")
    with pytest.raises(MailboxError, match="quota_exceeded"):
        delivery.quarantine(b"x" * 65536, reason="invalid_json", now=NOW)
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM receipt_outbox").fetchone()[0] == 0


def test_limit_current_policy_and_fifth_lease_failure(mailbox):
    store, sender, target = mailbox
    delivery = MailboxDelivery(store)
    with pytest.raises(MailboxError, match="invalid_limit"):
        delivery.poll(target, request_id=str(uuid4()), limit=17, now=NOW)
    store.send(wire(sender, target, ttl=3600), sender, now=NOW)
    clock = NOW
    for attempt in range(1, 6):
        claim = delivery.poll(target, request_id=str(uuid4()), lease_seconds=10, now=clock)[0]
        assert claim.attempt == attempt
        delivery.recover(now=clock + 10)
        clock += 10 + min(60, 2 ** (attempt - 1)) + 1
    assert delivery.poll(target, request_id=str(uuid4()), now=clock) == []
    assert store.status(target.agent_id, claim.envelope.message_id)["phase"] == "dead_letter"


def test_replay_expired_response_never_returns_a_fresh_lease(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, ttl=3600), sender, now=NOW)
    delivery = MailboxDelivery(store)
    poll_request = str(uuid4())
    claim = delivery.poll(target, request_id=poll_request, lease_seconds=10, now=NOW)[0]
    request = str(uuid4())
    delivery.renew(target, claim, request_id=request, lease_seconds=10, now=NOW + 1)
    delivery.renew(target, claim, request_id=str(uuid4()), lease_seconds=120, now=NOW + 2)
    with pytest.raises(MailboxError, match="stale_claim"):
        delivery.poll(target, request_id=poll_request, lease_seconds=10, now=NOW + 11)
    with pytest.raises(MailboxError, match="stale_claim"):
        delivery.renew(target, claim, request_id=request, lease_seconds=10, now=NOW + 11)


def test_artifact_evidence_retained_through_receipts_compaction_and_expiry(mailbox, tmp_path):
    store, sender, target = mailbox
    data = b"verified evidence"
    sha = hashlib.sha256(data).hexdigest()
    source = tmp_path / "evidence"
    source.write_bytes(data)
    descriptor = dict(sha256=sha, size=len(data), media_type="text/plain", name="evidence")
    raw = wire(sender, target, receipt_policy="terminal", artifacts=[descriptor])
    store.send(raw, sender, now=NOW, artifacts={sha: source})
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    completed = dict(outcome="blocked", summary="Needs review", evidence=[sha])
    assert delivery.finish(target, claim, completed, now=NOW)["outcome"] == "blocked"
    blob = store.root / "blobs" / "sha256" / sha
    os.utime(blob, (NOW - 90000, NOW - 90000))
    assert store.gc_blobs(now=NOW) == []
    delivery.flush_receipts(target, now=NOW)
    receipt = store.peek(sender, now=NOW)[0]["envelope"]
    assert receipt["artifacts"] == [dict(descriptor, name=sha[:12], media_type="application/octet-stream")]
    assert receipt["payload"]["data"]["result"]["evidence"] == [sha]
    delivery.gc(now=NOW + 10, retention_seconds=10)
    assert store.gc_blobs(now=NOW + 100) == []
    delivery.recover(now=NOW + 604801)
    delivery.gc(now=NOW + 30 * 86400 + 604802, retention_seconds=0)
    assert not blob.exists()


def test_current_policy_max_in_flight_order_and_ttl_cap(mailbox):
    store, sender, target = mailbox
    delivery = MailboxDelivery(store)
    ids = [store.send(wire(sender, target, ttl=3600), sender, now=NOW)["message_id"] for _ in range(3)]
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE cards SET max_in_flight=2 WHERE agent_id=?", (target.agent_id,))
    claims = delivery.poll(target, request_id=str(uuid4()), limit=16, lease_seconds=3600, now=NOW)
    assert [item.envelope.message_id for item in claims] == sorted(ids)[:2]
    assert all(item.lease_until == NOW + 3600 for item in claims)
    assert delivery.poll(target, request_id=str(uuid4()), limit=16, now=NOW) == []
    delivery.finish(target, claims[0], result(), now=NOW)
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE cards SET allowed_kinds=? WHERE agent_id=?", ('["receipt"]', target.agent_id))
    assert delivery.poll(target, request_id=str(uuid4()), now=NOW) == []
    with store.db.connection(write=True) as conn:
        conn.execute(
            "UPDATE cards SET allowed_kinds=?,allowed_scopes=? WHERE agent_id=?",
            ('["task.request"]', '[{"task_id":"other","workspace_id":"workspace"}]', target.agent_id),
        )
    assert delivery.poll(target, request_id=str(uuid4()), now=NOW) == []


def test_finish_fencing_and_request_conflicts(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, ttl=3600), sender, now=NOW)
    delivery = MailboxDelivery(store)
    request = str(uuid4())
    claim = delivery.poll(target, request_id=request, lease_seconds=10, now=NOW)[0]
    with pytest.raises(MailboxError, match="request_conflict"):
        delivery.poll(target, request_id=request, lease_seconds=11, now=NOW)
    with pytest.raises(MailboxError, match="stale_claim"):
        delivery.finish(target, claim.model_copy(update={"claim_token": "0" * 64}), result(), now=NOW)
    delivery.recover(now=NOW + 10)
    new = delivery.poll(target, request_id=str(uuid4()), now=NOW + 12)[0]
    with pytest.raises(MailboxError, match="stale_claim"):
        delivery.finish(target, claim, result(), now=NOW + 12)
    assert delivery.finish(target, new, result(), now=NOW + 12)["phase"] == "completed"


def test_publication_holds_no_source_write_and_historical_exception_is_exact(mailbox, monkeypatch):
    store, sender, target = mailbox
    store.send(wire(sender, target, receipt_policy="terminal"), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    delivery.finish(target, claim, result(), now=NOW)
    real_send = store.send

    def checked_send(raw, ref, **kwargs):
        with store.db.connection(write=True) as conn:
            assert conn.execute("SELECT count(*) FROM receipt_outbox").fetchone()[0] == 1
        return real_send(raw, ref, **kwargs)

    monkeypatch.setattr(store, "send", checked_send)
    assert delivery.flush_receipts(target, now=NOW)["published"] == 1
    with store.db.connection() as conn:
        intent = conn.execute("SELECT * FROM receipt_outbox").fetchone()
        forged = json.loads(intent["envelope_bytes"])
        forged["extensions"] = {"forged": True}
    current = store.resume(target, request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="instance_fenced"):
        real_send(
            encode_envelope(seal_envelope(forged)),
            current,
            now=NOW,
            _receipt_event=(claim.envelope.message_id, "terminal"),
        )


def test_empty_quarantine_still_consumes_bounded_metadata_budget(mailbox):
    store, _, _ = mailbox
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE metadata SET value='64' WHERE key='quarantine_bytes'")
    delivery = MailboxDelivery(store)
    delivery.quarantine(b"", reason="invalid", now=NOW)
    with pytest.raises(MailboxError, match="quota_exceeded"):
        delivery.quarantine(b"", reason="invalid", now=NOW)


def test_finish_can_reference_new_same_scope_published_output(mailbox, tmp_path):
    store, sender, target = mailbox
    store.send(wire(sender, target, ttl=3600, receipt_policy="terminal"), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    data = b"new output evidence"
    sha = hashlib.sha256(data).hexdigest()
    source = tmp_path / "output"
    source.write_bytes(data)
    descriptor = dict(sha256=sha, size=len(data), media_type="text/plain", name="output")
    with store.db.connection(write=True) as conn:
        conn.execute(
            "UPDATE cards SET allowed_kinds=? WHERE agent_id=?", ('["task.result","receipt"]', sender.agent_id)
        )
    output = wire(
        target,
        sender,
        kind="task.result",
        ttl=60,
        receipt_policy="none",
        artifacts=[descriptor],
        payload={
            "content_type": "application/json",
            "schema": "opena2a.result/1",
            "data": dict(
                request_message_id=claim.envelope.message_id, outcome="succeeded", summary="Output", evidence=[sha]
            ),
        },
    )
    store.send(output, target, now=NOW, artifacts={sha: source})
    completed = dict(outcome="succeeded", summary="Built output", evidence=[sha])
    assert delivery.finish(target, claim, completed, now=NOW)["phase"] == "completed"
    with store.db.connection() as conn:
        intent = conn.execute("SELECT * FROM receipt_outbox").fetchone()
        assert json.loads(intent["envelope_bytes"])["artifacts"] == [
            dict(descriptor, name=sha[:12], media_type="application/octet-stream")
        ]
    blob = store.root / "blobs" / "sha256" / sha
    os.utime(blob, (NOW - 90000, NOW - 90000))
    delivery.recover(now=NOW + 60)
    delivery.gc(now=NOW + 60, retention_seconds=0)
    assert blob.exists()
    assert store.gc_blobs(now=NOW + 100) == []


def test_result_and_terminal_roll_back_when_outbox_insert_fails(mailbox, monkeypatch):
    import sqlite3

    store, sender, target = mailbox
    store.send(wire(sender, target, receipt_policy="terminal"), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    opener = store.db._open

    class FullOutbox:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql.startswith("INSERT OR IGNORE INTO receipt_outbox"):
                error = sqlite3.OperationalError("injected full outbox")
                error.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise error
            return self.connection.execute(sql, *args)

    monkeypatch.setattr(store.db, "_open", lambda: FullOutbox(opener()))
    with pytest.raises(MailboxError, match="storage_full"):
        delivery.finish(target, claim, result(), now=NOW)
    monkeypatch.setattr(store.db, "_open", opener)
    with store.db.connection() as conn:
        row = conn.execute("SELECT * FROM messages").fetchone()
        assert row["phase"] == "in_progress"
        assert row["result_hash"] is None
        assert row["result_json"] is None
        assert conn.execute("SELECT count(*) FROM receipt_outbox").fetchone()[0] == 0
    assert delivery.finish(target, claim, result(), now=NOW)["phase"] == "completed"


def test_receipt_admission_is_not_a_global_envelope_limit(mailbox):
    store, sender, target = mailbox
    raw = wire(sender, target, extensions={"large": "x" * 20000}, receipt_policy="none")
    assert len(raw) > 16384
    store.send(raw, sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    assert delivery.finish(target, claim, result(), now=NOW)["phase"] == "completed"


def test_registered_event_schema_required_at_claim_boundary(mailbox):
    store, sender, target = mailbox
    data = json.loads(wire(sender, target, receipt_policy="none"))
    data["kind"] = "event"
    data["payload"] = {"content_type": "application/json", "schema": "local.event/1", "data": {"value": 1}}
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE cards SET allowed_kinds=? WHERE agent_id=?", ('["event"]', target.agent_id))
    raw = encode_envelope(seal_envelope(data, event_schemas=["local.event/1"]), event_schemas=["local.event/1"])
    store.send(raw, sender, event_schemas=["local.event/1"], now=NOW)
    delivery = MailboxDelivery(store)
    with pytest.raises(MailboxError, match="storage_conflict"):
        delivery.poll(target, request_id=str(uuid4()), now=NOW)
    claim = delivery.poll(target, request_id=str(uuid4()), event_schemas=["local.event/1"], now=NOW)[0]
    assert delivery.finish(target, claim, result(), now=NOW)["phase"] == "completed"


def test_legal_rich_input_metadata_does_not_prevent_claim_or_rejection(mailbox, tmp_path):
    store, sender, target = mailbox
    content = b"input"
    sha = hashlib.sha256(content).hexdigest()
    source = tmp_path / "rich-input"
    source.write_bytes(content)
    descriptors = [
        dict(sha256=sha, size=len(content), name="\u4e00" * 128, media_type="\u4e00" * 128) for _ in range(32)
    ]
    sent = store.send(wire(sender, target, artifacts=descriptors), sender, now=NOW, artifacts={sha: source})
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    assert claim.envelope.message_id == sent["message_id"]
    assert delivery.reject(target, claim, reason="rejected", now=NOW)["phase"] == "dead_letter"
    with store.db.connection() as conn:
        for row in conn.execute("SELECT envelope_bytes FROM receipt_outbox"):
            assert json.loads(row[0])["artifacts"] == []
    assert (store.root / "blobs" / "sha256" / sha).exists()


def test_maximal_result_evidence_uses_bounded_receipt_display_metadata(tmp_path):
    scope = dict(task_id="t" * 128, workspace_id="w" * 128)
    store = MailboxStore(tmp_path / "max-metadata", authority_id=AUTH, tenant_id="t" * 63)
    sender, target = [
        store.init(
            agent_id=str(uuid4()),
            instance_id=str(uuid4()),
            request_id=str(uuid4()),
            allowed_scopes=[scope],
            allowed_kinds=["task.request", "receipt"],
        )
        for _ in range(2)
    ]
    descriptors = []
    sources = {}
    for index in range(32):
        content = str(index).encode()
        sha = hashlib.sha256(content).hexdigest()
        path = tmp_path / str(index)
        path.write_bytes(content)
        sources[sha] = path
        descriptors.append(dict(sha256=sha, size=len(content), name="\u4e00" * 128, media_type="\u4e00" * 128))
    store.send(
        wire(sender, target, receipt_policy="received_and_terminal", artifacts=descriptors, scope=scope),
        sender,
        now=NOW,
        artifacts=sources,
    )
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=str(uuid4()), now=NOW)[0]
    completed = dict(outcome="succeeded", summary="", evidence=list(sources))
    completed["summary"] = "x" * (8192 - len(canonical_bytes(completed)))
    assert len(canonical_bytes(completed)) == 8192
    assert delivery.finish(target, claim, completed, now=NOW)["phase"] == "completed"
    with store.db.connection() as conn:
        rows = conn.execute("SELECT envelope_bytes FROM receipt_outbox ORDER BY event_key").fetchall()
        assert len(rows) == 2
        processed = json.loads(rows[1][0])
        max_size_digit_growth = 32 * (len(str(16777216)) - 1)
        assert len(rows[1][0]) + max_size_digit_growth <= 16384
        assert [(item["sha256"], item["size"]) for item in processed["artifacts"]] == [
            (item["sha256"], item["size"]) for item in descriptors
        ]
        assert all(
            len(item["name"]) == 12 and item["media_type"] == "application/octet-stream"
            for item in processed["artifacts"]
        )
    assert delivery.flush_receipts(target, now=NOW)["published"] == 2
