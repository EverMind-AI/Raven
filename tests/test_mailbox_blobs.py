"""Verified artifact publication and retained-reference collection tests."""

import hashlib
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import encode_envelope, seal_envelope
from raven.mailbox.store import MailboxStore

AUTH = "11111111-1111-4111-8111-111111111111"
SCOPE = {"task_id": "task", "workspace_id": "workspace"}
NOW = 1790899200


@pytest.fixture
def mailbox(tmp_path):
    store = MailboxStore(tmp_path / "a2a", authority_id=AUTH)
    sender = store.init(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["task.request", "receipt"],
    )
    target = store.init(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["task.request", "receipt"],
    )
    return store, sender, target


def wire(sender, target, **changes):
    data = dict(
        protocol_version="1.0",
        message_id=str(uuid4()),
        trace_id=str(uuid4()),
        in_reply_to=None,
        kind="task.request",
        sender_identity={k: v for k, v in sender.model_dump().items() if k != "generation"},
        target_identity={**{k: v for k, v in target.model_dump().items() if k != "generation"}, "instance_id": None},
        scope=SCOPE,
        created_at=datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ttl=60,
        receipt_policy="received_and_terminal",
        payload={
            "content_type": "application/json",
            "schema": "opena2a.task/1",
            "data": {"operation": "review", "arguments": {}, "idempotency_key": "one", "acceptance": []},
        },
        artifacts=[],
        extensions={},
    )
    data.update(changes)
    return encode_envelope(seal_envelope(data))


def artifact(data=b"evidence", **changes):
    value = dict(sha256=hashlib.sha256(data).hexdigest(), size=len(data), media_type="text/plain", name="../display")
    value.update(changes)
    return value


def send_blob(mailbox, tmp_path, **changes):
    store, sender, target = mailbox
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    item = artifact(**changes)
    raw = wire(sender, target, artifacts=[item])
    result = store.send(raw, sender, now=NOW, artifacts={item["sha256"]: source})
    return source, item, raw, result


def test_publish_reuse_display_name_and_lost_source_replay(mailbox, tmp_path):
    store, sender, target = mailbox
    source, item, raw, result = send_blob(mailbox, tmp_path)
    blob = store.root / "blobs" / "sha256" / item["sha256"]
    assert blob.read_bytes() == b"evidence"
    inode = blob.stat().st_ino
    store.send(wire(sender, target, artifacts=[item]), sender, now=NOW)
    assert blob.stat().st_ino == inode
    source.unlink()
    assert store.send(raw, sender, now=NOW + 100, artifacts={item["sha256"]: source})["duplicate"]
    assert store.peek(target, now=NOW)[0]["envelope"]["artifacts"][0]["name"] == "../display"
    assert result["status"] == "stored"


@pytest.mark.parametrize(
    "failure,code",
    [
        ("hash", "artifact_mismatch"),
        ("size", "artifact_mismatch"),
        ("symlink", "unsafe_artifact_path"),
        ("directory", "unsafe_artifact_path"),
    ],
)
def test_failed_import_has_no_record_or_temp(mailbox, tmp_path, failure, code):
    store, sender, target = mailbox
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    item = artifact()
    if failure == "hash":
        source.write_bytes(b"different")
    elif failure == "size":
        item["size"] += 1
    elif failure == "symlink":
        link = tmp_path / "link"
        link.symlink_to(source)
        source = link
    else:
        source = tmp_path
    with pytest.raises(MailboxError, match=code):
        store.send(wire(sender, target, artifacts=[item]), sender, now=NOW, artifacts={item["sha256"]: source})
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert not list(store.root.glob(".blob-import-*"))


def test_read_missing_and_existing_corruption_blocked(mailbox, tmp_path):
    store, sender, target = mailbox
    _, item, _, result = send_blob(mailbox, tmp_path)
    blob = store.root / "blobs" / "sha256" / item["sha256"]
    blob.write_bytes(b"corrupt!")
    with pytest.raises(MailboxError, match="storage_conflict") as caught:
        store.peek(target, now=NOW)
    assert caught.value.message_id == result["message_id"]
    with pytest.raises(MailboxError, match="storage_conflict"):
        store.send(wire(sender, target, artifacts=[item]), sender, now=NOW)
    blob.unlink()
    with pytest.raises(MailboxError, match="storage_conflict"):
        store.peek(target, now=NOW)


def test_quota_counts_temporary_copy_even_for_reuse(mailbox, tmp_path):
    store, sender, target = mailbox
    source, item, _, _ = send_blob(mailbox, tmp_path)
    store.configure_quotas(blob_bytes=item["size"])
    with pytest.raises(MailboxError, match="quota_exceeded"):
        store.send(wire(sender, target, artifacts=[item]), sender, now=NOW, artifacts={item["sha256"]: source})
    assert store.send(wire(sender, target, artifacts=[item]), sender, now=NOW)["status"] == "stored"


def test_publication_failure_never_admits(mailbox, tmp_path, monkeypatch):
    store, sender, target = mailbox

    def fail(*args, **kwargs):
        raise OSError("publication failed")

    monkeypatch.setattr("raven.mailbox.blobs.os.link", fail)
    with pytest.raises(MailboxError, match="storage_error"):
        send_blob(mailbox, tmp_path)
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert not list(store.root.glob(".blob-import-*"))


def test_gc_keeps_live_refs_and_only_removes_old_orphans(mailbox, tmp_path):
    store, _, _ = mailbox
    _, item, _, _ = send_blob(mailbox, tmp_path)
    folder = store.root / "blobs" / "sha256"
    live = folder / item["sha256"]
    old = folder / ("a" * 64)
    young = folder / ("b" * 64)
    old.write_bytes(b"orphan")
    young.write_bytes(b"young")
    os.utime(live, (NOW - 90000,) * 2)
    os.utime(old, (NOW - 90000,) * 2)
    os.utime(young, (NOW,) * 2)
    assert store.gc_blobs(now=NOW) == [old.name]
    assert live.exists() and young.exists()
    assert (store.root / ".mailbox-blobs.lock").exists()


def test_gc_retains_outbox_and_tombstone_refs(mailbox, tmp_path):
    store, _, target = mailbox
    _, _, _, result = send_blob(mailbox, tmp_path)
    folder = store.root / "blobs" / "sha256"
    outbox_hash, tombstone_hash = "c" * 64, "d" * 64
    for digest in (outbox_hash, tombstone_hash):
        path = folder / digest
        path.write_bytes(b"retained")
        os.utime(path, (NOW - 90000,) * 2)
    import json

    with store.db.connection(write=True) as conn:
        conn.execute(
            "INSERT INTO receipt_outbox (authority_id,tenant_id,recipient,message_id,event_key,event_json,artifact_refs) "
            "VALUES (?,?,?,?,?,?,?)",
            (AUTH, "local", target.agent_id, result["message_id"], "terminal", "{}", json.dumps([outbox_hash])),
        )
        conn.execute(
            "INSERT INTO tombstones (authority_id,tenant_id,recipient,message_id,digest,phase,terminal_at,retain_until,artifact_refs) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                AUTH,
                "local",
                target.agent_id,
                str(uuid4()),
                "f" * 64,
                "completed",
                NOW - 1,
                NOW + 100,
                json.dumps([tombstone_hash]),
            ),
        )
    assert store.gc_blobs(now=NOW) == []
    assert all((folder / digest).exists() for digest in (outbox_hash, tombstone_hash))


def test_blob_lock_timeout_and_absent_root_do_not_initialize(mailbox, tmp_path):
    import time

    from raven.utils.portable_lock import file_lock

    store, sender, target = mailbox
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    item = artifact()
    raw = wire(sender, target, artifacts=[item])
    short = MailboxStore(store.root, busy_timeout_ms=30)
    with file_lock(store.root / ".mailbox-blobs.lock"):
        started = time.monotonic()
        with pytest.raises(MailboxError, match="lock_busy"):
            short.send(raw, sender, now=NOW, artifacts={item["sha256"]: source})
        assert time.monotonic() - started < 1
    absent = MailboxStore(tmp_path / "absent", authority_id=AUTH)
    with pytest.raises(MailboxError, match="mailbox_not_initialized"):
        absent.send(raw, sender, now=NOW, artifacts={item["sha256"]: source})
    assert not absent.root.exists()


@pytest.mark.parametrize("change", ["single", "aggregate", "count"])
def test_wire_artifact_limits_remain_codec_errors(mailbox, change):
    _, sender, target = mailbox
    item = artifact()
    if change == "single":
        items = [{**item, "size": 16777217}]
    elif change == "aggregate":
        items = [{**item, "size": 16777216}] * 5
    else:
        items = [item] * 33
    with pytest.raises(MailboxError, match="invalid_envelope"):
        wire(sender, target, artifacts=items)


def test_fsync_and_copy_precede_write_transaction(mailbox, tmp_path, monkeypatch):
    from contextlib import contextmanager

    from raven.mailbox import blobs

    store, _, _ = mailbox
    connection = store.db.connection
    fsync = blobs.os.fsync
    writing = False
    synced = []

    @contextmanager
    def tracked(*, write=False):
        nonlocal writing
        with connection(write=write) as conn:
            writing = write
            try:
                yield conn
            finally:
                writing = False

    def check_sync(fd):
        assert not writing
        synced.append(fd)
        fsync(fd)

    monkeypatch.setattr(store.db, "connection", tracked)
    monkeypatch.setattr(blobs.os, "fsync", check_sync)
    send_blob(mailbox, tmp_path)
    assert len(synced) == 2


def test_gc_never_deletes_import_temps_or_anchor(mailbox, tmp_path):
    store, _, _ = mailbox
    temporary = store.root / ".blob-import-crashed"
    temporary.write_bytes(b"copy")
    os.utime(temporary, (NOW - 90000,) * 2)
    anchor = store.root / ".mailbox-blobs.lock"
    os.utime(anchor, (NOW - 90000,) * 2)
    assert store.gc_blobs(now=NOW) == []
    assert temporary.exists() and anchor.exists()


@pytest.mark.parametrize("failure", ["missing", "directory", "symlink", "corrupt"])
def test_bad_existing_hash_is_not_replaced(mailbox, tmp_path, failure):
    store, sender, target = mailbox
    source, item, _, _ = send_blob(mailbox, tmp_path)
    blob = store.root / "blobs" / "sha256" / item["sha256"]
    blob.unlink()
    if failure == "directory":
        blob.mkdir()
    elif failure == "symlink":
        blob.symlink_to(source)
    elif failure == "corrupt":
        blob.write_bytes(b"corrupt!")
    expected = "artifacts_unavailable" if failure == "missing" else "storage_conflict"
    with pytest.raises(MailboxError, match=expected):
        store.send(wire(sender, target, artifacts=[item]), sender, now=NOW)
    if failure != "missing":
        with pytest.raises(MailboxError, match="storage_conflict"):
            store.send(wire(sender, target, artifacts=[item]), sender, now=NOW, artifacts={item["sha256"]: source})


def test_gc_blocks_damaged_live_envelope_before_deleting_evidence(mailbox, tmp_path):
    import json

    store, _, _ = mailbox
    _, item, raw, _ = send_blob(mailbox, tmp_path)
    blob = store.root / "blobs" / "sha256" / item["sha256"]
    os.utime(blob, (NOW - 90000,) * 2)
    damaged = json.loads(raw)
    damaged["artifacts"] = []
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE messages SET envelope_bytes=?", (json.dumps(damaged).encode(),))
    with pytest.raises(MailboxError, match="storage_conflict"):
        store.gc_blobs(now=NOW)
    assert blob.exists()


def test_reuse_after_failed_publication_sync_requires_directory_sync(mailbox, tmp_path, monkeypatch):
    from raven.mailbox import blobs

    store, sender, target = mailbox
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    item = artifact()
    raw = wire(sender, target, artifacts=[item])
    sync = blobs._sync_directory

    def fail_sync(path):
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(blobs, "_sync_directory", fail_sync)
    with pytest.raises(MailboxError, match="storage_error"):
        store.send(raw, sender, now=NOW, artifacts={item["sha256"]: source})
    blob = store.root / "blobs" / "sha256" / item["sha256"]
    assert blob.exists()
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    source.unlink()
    synced = []

    def successful_sync(path):
        with store.db.connection() as conn:
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        sync(path)
        synced.append(path)

    monkeypatch.setattr(blobs, "_sync_directory", successful_sync)
    assert store.send(raw, sender, now=NOW)["status"] == "stored"
    assert synced == [blob.parent]
