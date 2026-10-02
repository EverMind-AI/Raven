"""Real spawned-process contention and durable mailbox crash-boundary checks."""

import hashlib
import json
import multiprocessing
import signal
import sqlite3
import time
from contextlib import contextmanager
from uuid import UUID

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.delivery import MailboxDelivery
from raven.mailbox.store import MailboxStore
from raven.utils.portable_lock import file_lock
from tests.test_mailbox_store import NOW, wire
from tests.test_mailbox_store import mailbox as mailbox

CTX = multiprocessing.get_context("spawn")


def request(index):
    return str(UUID(int=index + 1, version=4))


def worker(root, operation, ref, payload, gate, output):
    store = MailboxStore(root)
    delivery = MailboxDelivery(store)
    gate.wait(timeout=30)
    if operation == "send":
        value = store.send(payload, ref, now=NOW)
    else:
        value = [c.model_dump(mode="json") for c in delivery.poll(ref, request_id=payload, now=NOW)]
    output.put(value)


def run_workers(root, operation, ref, payloads):
    gate = CTX.Barrier(len(payloads))
    output = CTX.Queue()
    children = [CTX.Process(target=worker, args=(root, operation, ref, p, gate, output)) for p in payloads]
    try:
        for child in children:
            child.start()
        values = [output.get(timeout=40) for _ in children]
        for child in children:
            child.join(timeout=10)
            assert child.exitcode == 0
        return values
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)
        output.close()
        output.join_thread()


def hold_lock(root, kind, ready, release):
    if kind == "sqlite":
        conn = sqlite3.connect(root / "mailbox.sqlite3")
        try:
            conn.execute("BEGIN IMMEDIATE")
            ready.set()
            assert release.wait(timeout=30)
        finally:
            conn.rollback()
            conn.close()
    else:
        with file_lock(root / ".mailbox-blobs.lock"):
            ready.set()
            assert release.wait(timeout=30)


@contextmanager
def held(root, kind):
    ready, release = CTX.Event(), CTX.Event()
    child = CTX.Process(target=hold_lock, args=(root, kind, ready, release))
    child.start()
    try:
        assert ready.wait(timeout=20)
        yield
    finally:
        release.set()
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        assert child.exitcode == 0


def crash_child(root, boundary, target, claim, ready, release):
    store = MailboxStore(root)
    delivery = MailboxDelivery(store)
    if boundary == "poll":
        original = store.db.connection

        @contextmanager
        def committed(*, write=False):
            with original(write=write) as conn:
                yield conn
            if write:
                ready.set()
                assert release.wait(timeout=30)

        store.db.connection = committed
        delivery.poll(target, request_id=request(100), now=NOW)
    elif boundary == "finish":
        delivery.finish(target, claim, dict(outcome="succeeded", summary="Done", evidence=[]), now=NOW)
        ready.set()
        assert release.wait(timeout=30)
    else:
        original = store.send

        def sent(raw, ref, **kwargs):
            value = original(raw, ref, **kwargs)
            if json.loads(raw)["kind"] == "receipt":
                ready.set()
                assert release.wait(timeout=30)
            return value

        store.send = sent
        delivery.flush_receipts(target, now=NOW + 1)


def kill_at(root, boundary, target, claim=None):
    ready, release = CTX.Event(), CTX.Event()
    child = CTX.Process(target=crash_child, args=(root, boundary, target, claim, ready, release))
    child.start()
    try:
        assert ready.wait(timeout=20)
        child.kill()
        child.join(timeout=5)
        assert child.exitcode == -signal.SIGKILL
    finally:
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)


def test_twenty_independent_senders_replay_one_insert(mailbox, tmp_path):
    store, sender, target = mailbox
    raw = wire(sender, target, message_id=request(200), trace_id=request(201), receipt_policy="none")
    envelope_file = tmp_path / "envelope.json"
    envelope_file.write_bytes(raw)
    values = run_workers(store.root, "send", sender, [raw] * 20)
    assert sum(not item["duplicate"] for item in values) == 1
    assert len({(v["message_id"], v["digest"], v["phase"]) for v in values}) == 1
    assert envelope_file.read_bytes() == raw
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_simultaneous_polls_respect_single_card_slot(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, receipt_policy="none"), sender, now=NOW)
    values = run_workers(store.root, "poll", target, [request(i) for i in range(8)])
    claims = [claim for group in values for claim in group]
    assert len(claims) == 1
    assert claims[0]["attempt"] == 1
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages WHERE phase='in_progress'").fetchone()[0] == 1
        assert conn.execute("SELECT attempt FROM messages").fetchone()[0] == 1
    store.send(wire(sender, target, receipt_policy="none"), sender, now=NOW)
    assert MailboxDelivery(store).poll(target, request_id=request(99), now=NOW) == []


def test_external_sqlite_write_lock_times_out_but_reads_work(mailbox):
    store, sender, target = mailbox
    raw = wire(sender, target, receipt_policy="none")
    with held(store.root, "sqlite"):
        assert store.peek(target, now=NOW) == []
        started = time.monotonic()
        with pytest.raises(MailboxError, match="storage_busy"):
            store.send(raw, sender, now=NOW)
        assert 1.8 <= time.monotonic() - started < 8
    assert store.send(raw, sender, now=NOW)["duplicate"] is False


def test_external_blob_anchor_bounds_import_and_gc(mailbox, tmp_path):
    store, sender, target = mailbox
    source = tmp_path / "artifact"
    source.write_bytes(b"verified")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    raw = wire(sender, target, artifacts=[dict(sha256=sha, size=8, media_type="text/plain", name="input")])
    with held(store.root, "blob"):
        for action in (
            lambda: store.send(raw, sender, now=NOW, artifacts={sha: source}),
            lambda: store.gc_blobs(now=NOW),
        ):
            started = time.monotonic()
            with pytest.raises(MailboxError, match="lock_busy"):
                action()
            assert 1.8 <= time.monotonic() - started < 8
    store.send(raw, sender, now=NOW, artifacts={sha: source})


def test_poll_commit_survives_kill_before_response(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, receipt_policy="none"), sender, now=NOW)
    store.send(wire(sender, target, receipt_policy="none"), sender, now=NOW)
    kill_at(store.root, "poll", target)
    reopened = MailboxStore(store.root)
    replay = MailboxDelivery(reopened).poll(target, request_id=request(100), now=NOW + 1)
    assert len(replay) == 1
    with reopened.db.connection() as conn:
        rows = conn.execute("SELECT message_id,attempt FROM messages ORDER BY attempt DESC").fetchall()
        assert [r["attempt"] for r in rows] == [1, 0]
        assert rows[0]["message_id"] == replay[0].envelope.message_id
        assert conn.execute("SELECT count(*) FROM mutation_receipts WHERE method='poll'").fetchone()[0] == 1
    assert MailboxDelivery(reopened).poll(target, request_id=request(100), now=NOW + 2) == replay
    with pytest.raises(MailboxError, match="stale_claim"):
        MailboxDelivery(reopened).poll(target, request_id=request(100), now=NOW + 121)
    with reopened.db.connection() as conn:
        assert sorted(r[0] for r in conn.execute("SELECT attempt FROM messages")) == [0, 1]


@pytest.mark.parametrize("boundary", ["finish", "receipt"])
def test_terminal_and_receipt_publication_survive_sigkill(mailbox, boundary):
    store, sender, target = mailbox
    store.send(wire(sender, target, receipt_policy="terminal"), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=request(100), now=NOW)[0]
    if boundary == "receipt":
        delivery.finish(target, claim, dict(outcome="succeeded", summary="Done", evidence=[]), now=NOW)
    kill_at(store.root, boundary, target, claim)
    reopened = MailboxStore(store.root)
    with reopened.db.connection() as conn:
        message = dict(
            conn.execute("SELECT * FROM messages WHERE message_id=?", (claim.envelope.message_id,)).fetchone()
        )
        frozen = dict(conn.execute("SELECT * FROM receipt_outbox").fetchone())
        assert message["phase"] == "completed"
        assert message["result_hash"]
        assert json.loads(message["result_json"])["summary"] == "Done"
        assert frozen["status"] != "published"
        target_rows = conn.execute("SELECT * FROM messages WHERE recipient=?", (sender.agent_id,)).fetchall()
        assert len(target_rows) == (boundary == "receipt")
    assert MailboxDelivery(reopened).flush_receipts(target, now=NOW + 2)["published"] == 1
    with reopened.db.connection() as conn:
        row = dict(conn.execute("SELECT * FROM receipt_outbox").fetchone())
        receipt = conn.execute("SELECT * FROM messages WHERE recipient=?", (sender.agent_id,)).fetchall()
        assert len(receipt) == 1
        assert row["status"] == "published"
        assert row["receipt_id"] == frozen["receipt_id"] == receipt[0]["message_id"]
        assert row["digest"] == frozen["digest"] == receipt[0]["digest"]
        assert row["envelope_bytes"] == frozen["envelope_bytes"] == receipt[0]["envelope_bytes"]
        assert (
            conn.execute(
                "SELECT result_hash FROM messages WHERE message_id=?", (claim.envelope.message_id,)
            ).fetchone()[0]
            == message["result_hash"]
        )


def test_lowered_metadata_quota_blocks_admission_preserves_finish(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target, receipt_policy="terminal"), sender, now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(target, request_id=request(100), now=NOW)[0]
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE metadata SET value='0' WHERE key='record_limit'")
    with pytest.raises(MailboxError, match="quota_exceeded"):
        store.send(wire(sender, target), sender, now=NOW)
    assert (
        delivery.finish(target, claim, dict(outcome="succeeded", summary="Done", evidence=[]), now=NOW)["phase"]
        == "completed"
    )
