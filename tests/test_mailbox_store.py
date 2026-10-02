"""Durable identity and offline admission tests for the local mailbox."""

import json
import sqlite3
import time
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


def test_init_replays_original_reference_and_fixes_root(tmp_path):
    store = MailboxStore(tmp_path / "root", authority_id=AUTH)
    params = dict(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["task.request"],
    )
    original = store.init(**params)
    assert store.init(**params) == original
    assert original.generation == 1
    assert store.card(original.agent_id).limits.max_in_flight == 1
    with pytest.raises(MailboxError, match="root_identity_mismatch"):
        MailboxStore(store.root, authority_id=str(uuid4())).card(original.agent_id)


def test_resume_replay_is_exact_and_fences_old_sender(mailbox):
    store, sender, target = mailbox
    request = str(uuid4())
    instance = str(uuid4())
    new = store.resume(sender, instance_id=instance, request_id=request)
    assert new.generation == 2
    assert store.resume(sender, instance_id=instance, request_id=request) == new
    with pytest.raises(MailboxError, match="instance_fenced"):
        store.send(wire(sender, target), sender, now=NOW)
    with pytest.raises(MailboxError, match="request_conflict"):
        store.resume(sender, instance_id=str(uuid4()), request_id=request)


def test_offline_send_duplicate_and_read_only_peek(mailbox):
    store, sender, target = mailbox
    raw = wire(sender, target)
    result = store.send(raw, sender, now=NOW)
    assert result["phase"] == "pending"
    replay = store.send(raw, sender, now=NOW + 100)
    assert replay == {**result, "status": "pending", "duplicate": True}
    assert store.status(target.agent_id, result["message_id"]) == {
        key: value for key, value in result.items() if key not in {"status", "duplicate", "durability"}
    }
    first = store.peek(target, now=NOW)
    assert first == store.peek(target, now=NOW)
    assert first[0]["envelope"] == json.loads(raw)
    assert "claim_token" not in json.dumps(first)
    assert first[0]["attempt"] == 0
    altered = json.loads(raw)
    altered["extensions"] = {"changed": True}
    with pytest.raises(MailboxError, match="id_conflict"):
        store.send(encode_envelope(seal_envelope(altered)), sender, now=NOW)
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


@pytest.mark.parametrize(
    "change,error",
    [
        ({"scope": {"task_id": "other", "workspace_id": "workspace"}}, "scope_denied"),
        ({"created_at": "2026-10-02T00:00:31Z"}, "created_in_future"),
        ({"created_at": "2026-10-01T23:59:00Z"}, "message_expired"),
    ],
)
def test_new_message_admission_rejects(mailbox, change, error):
    store, sender, target = mailbox
    with pytest.raises(MailboxError, match=error):
        store.send(wire(sender, target, **change), sender, now=NOW)


def test_tenant_sender_and_pinned_target_refusal(mailbox):
    store, sender, target = mailbox
    data = json.loads(wire(sender, target))
    data["target_identity"]["tenant_id"] = "other"
    with pytest.raises(MailboxError, match="root_identity_mismatch"):
        store.send(encode_envelope(seal_envelope(data)), sender, now=NOW)
    data = json.loads(wire(sender, target))
    data["sender_identity"]["instance_id"] = str(uuid4())
    with pytest.raises(MailboxError, match="sender_mismatch"):
        store.send(encode_envelope(seal_envelope(data)), sender, now=NOW)
    data = json.loads(wire(sender, target))
    data["target_identity"]["instance_id"] = str(uuid4())
    with pytest.raises(MailboxError, match="target_instance_mismatch"):
        store.send(encode_envelope(seal_envelope(data)), sender, now=NOW)


def test_quota_reserves_receipts_and_duplicate_survives_full(mailbox):
    store, sender, target = mailbox
    store.configure_quotas(record_limit=1, record_bytes=262144)
    raw = wire(sender, target)
    result = store.send(raw, sender, now=NOW)
    assert store.send(raw, sender, now=NOW) == {**result, "status": "pending", "duplicate": True}
    with pytest.raises(MailboxError, match="quota_exceeded"):
        store.send(wire(sender, target), sender, now=NOW)
    with store.db.connection() as conn:
        row = conn.execute("SELECT reserved_bytes FROM messages").fetchone()
        assert row[0] >= len(raw) + 6 * 16384 + 8192


def test_missing_inspect_corrupt_and_new_schema_refused(tmp_path):
    store = MailboxStore(tmp_path / "missing", authority_id=AUTH)
    with pytest.raises(MailboxError, match="mailbox_not_initialized"):
        store.card(str(uuid4()))
    assert not store.root.exists()
    store.root.mkdir(mode=0o700)
    store.db.path.write_bytes(b"broken")
    store.db.path.chmod(0o600)
    with pytest.raises(MailboxError, match="storage_error"):
        store.card(str(uuid4()))
    store.db.path.unlink()
    store.init(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["task.request"],
    )
    with sqlite3.connect(store.db.path) as conn:
        conn.execute("PRAGMA user_version=2")
    with pytest.raises(MailboxError, match="unsupported_schema"):
        store.card(str(uuid4()))


def test_busy_is_bounded(mailbox):
    store, sender, target = mailbox
    short = MailboxStore(store.root, authority_id=AUTH, busy_timeout_ms=30)
    with sqlite3.connect(store.db.path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(MailboxError, match="storage_busy"):
            short.send(wire(sender, target), sender, now=NOW)
        assert time.monotonic() - started < 1


def test_symlink_and_permissions_refused(tmp_path):
    root = tmp_path / "real"
    root.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(root)
    store = MailboxStore(link, authority_id=AUTH)
    with pytest.raises(MailboxError, match="unsafe_storage_path"):
        store.init(
            agent_id=str(uuid4()),
            instance_id=str(uuid4()),
            request_id=str(uuid4()),
            allowed_scopes=[SCOPE],
            allowed_kinds=["task.request"],
        )


def test_uuid_semantic_keys_do_not_duplicate(mailbox):
    store, sender, target = mailbox
    raw = wire(sender, target)
    original = store.send(raw, sender, now=NOW)
    assert store.status(target.agent_id.upper(), original["message_id"].upper()) == {
        key: value for key, value in original.items() if key not in {"status", "duplicate", "durability"}
    }
    data = json.loads(raw)
    data["message_id"] = data["message_id"].upper()
    with pytest.raises(MailboxError, match="id_conflict"):
        store.send(encode_envelope(seal_envelope(data)), sender, now=NOW)


def test_artifacts_not_claimed_available(mailbox):
    store, sender, target = mailbox
    with pytest.raises(MailboxError, match="artifacts_unavailable"):
        store.send(
            wire(
                sender, target, artifacts=[{"sha256": "a" * 64, "size": 1, "media_type": "text/plain", "name": "input"}]
            ),
            sender,
            now=NOW,
        )


def test_root_can_be_reopened_without_duplicate_authority(mailbox):
    store, sender, _ = mailbox
    opened = MailboxStore(store.root)
    assert opened.card(sender.agent_id) == store.card(sender.agent_id)
    with pytest.raises(MailboxError, match="root_identity_mismatch"):
        MailboxStore(store.root, tenant_id="other").card(sender.agent_id)


def test_admin_resume_uses_stable_agent_and_old_ref_replay(mailbox):
    store, sender, _ = mailbox
    request = str(uuid4())
    instance = str(uuid4())
    new = store.resume(sender.agent_id, instance_id=instance, request_id=request)
    assert store.resume(sender, instance_id=instance, request_id=request) == new
    third = store.resume(sender, instance_id=str(uuid4()), request_id=str(uuid4()))
    assert third.generation == 3
    assert sender.generation == 1
    assert store.resume(sender, instance_id=instance, request_id=request) == new
    with pytest.raises(MailboxError, match="instance_fenced"):
        store.peek(new, now=NOW)


def test_duplicate_enrollment_new_request_does_not_replace_identity(mailbox):
    store, sender, _ = mailbox
    ref = store.init(
        agent_id=sender.agent_id.upper(),
        instance_id=sender.instance_id.upper(),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["receipt", "task.request"],
    )
    assert ref == sender
    with pytest.raises(MailboxError, match="agent_already_enrolled"):
        store.init(
            agent_id=sender.agent_id,
            instance_id=str(uuid4()),
            request_id=str(uuid4()),
            allowed_scopes=[SCOPE],
            allowed_kinds=["task.request"],
        )


def test_kind_policy_denied_without_business_executor(mailbox):
    store, sender, target = mailbox
    with pytest.raises(MailboxError, match="kind_denied"):
        store.send(
            wire(
                sender,
                target,
                kind="task.result",
                payload={
                    "content_type": "application/json",
                    "schema": "opena2a.result/1",
                    "data": {
                        "request_message_id": str(uuid4()),
                        "outcome": "succeeded",
                        "summary": "Done",
                        "evidence": [],
                    },
                },
            ),
            sender,
            now=NOW,
        )


def test_future_boundary_and_ttl_boundary(mailbox):
    store, sender, target = mailbox
    raw = wire(sender, target, created_at="2026-10-02T00:00:30Z")
    assert store.send(raw, sender, now=NOW)["phase"] == "pending"
    with pytest.raises(MailboxError, match="message_expired"):
        store.send(wire(sender, target), sender, now=NOW + 60)


def test_tombstone_replay_preserves_terminal(mailbox):
    store, sender, target = mailbox
    raw = wire(sender, target)
    result = store.send(raw, sender, now=NOW)
    with store.db.connection(write=True) as conn:
        conn.execute(
            "INSERT INTO tombstones (authority_id,tenant_id,recipient,message_id,digest,phase,"
            "result_hash,terminal_at,retain_until) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                AUTH,
                "local",
                target.agent_id,
                result["message_id"],
                result["digest"],
                "completed",
                "b" * 64,
                NOW + 1,
                NOW + 2592000,
            ),
        )
        conn.execute("DELETE FROM messages")
    replay = store.send(raw, sender, now=NOW + 100)
    assert replay["phase"] == "completed"
    assert replay["result_hash"] == "b" * 64
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_peek_detects_corrupt_wire_without_exposing_token(mailbox):
    store, sender, target = mailbox
    result = store.send(wire(sender, target), sender, now=NOW)
    with store.db.connection(write=True) as conn:
        conn.execute("UPDATE messages SET envelope_bytes=?,claim_token=?", (b"{}", "secret-token"))
    with pytest.raises(MailboxError, match="storage_conflict") as err:
        store.peek(target, now=NOW)
    assert err.value.message_id == result["message_id"]
    assert "secret-token" not in str(err.value)
    assert "secret-token" not in json.dumps(store.status(target.agent_id, result["message_id"]))


def test_sqlite_settings_and_constraints(mailbox):
    store, _, target = mailbox
    with store.db.connection(write=True) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO receipt_outbox (authority_id,tenant_id,recipient,message_id,event_key,event_json) "
                "VALUES (?,?,?,?,?,?)",
                (AUTH, "local", target.agent_id, str(uuid4()), "terminal", "{}"),
            )


@pytest.mark.parametrize("target_type", ["symlink", "directory", "public_db", "public_root"])
def test_unsafe_existing_storage_refused(mailbox, tmp_path, target_type):
    store, sender, _ = mailbox
    if target_type == "symlink":
        other = tmp_path / "other.sqlite3"
        store.db.path.rename(other)
        store.db.path.symlink_to(other)
    elif target_type == "directory":
        store.db.path.unlink()
        store.db.path.mkdir()
    elif target_type == "public_db":
        store.db.path.chmod(0o644)
    else:
        store.root.chmod(0o755)
    with pytest.raises(MailboxError, match="unsafe_storage_path"):
        store.card(sender.agent_id)


def test_byte_quota_reserves_entire_message_budget(mailbox):
    store, sender, target = mailbox
    store.configure_quotas(record_bytes=262143)
    with pytest.raises(MailboxError, match="quota_exceeded"):
        store.send(wire(sender, target), sender, now=NOW)
    store.configure_quotas(record_bytes=262144)
    assert store.send(wire(sender, target), sender, now=NOW)["phase"] == "pending"


@pytest.mark.parametrize("committed,error", [(False, "storage_error"), (True, "commit_unknown")])
def test_commit_boundary_reports_known_rollback_or_unknown(mailbox, monkeypatch, committed, error):
    store, sender, target = mailbox
    raw = wire(sender, target)
    opener = store.db._open

    class CommitFailure:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            if committed:
                self.connection.commit()
            raise sqlite3.OperationalError("injected commit response failure")

    monkeypatch.setattr(store.db, "_open", lambda: CommitFailure(opener()))
    with pytest.raises(MailboxError, match=error):
        store.send(raw, sender, now=NOW)
    monkeypatch.setattr(store.db, "_open", opener)
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == int(committed)
    if committed:
        assert store.send(raw, sender, now=NOW)["phase"] == "pending"


def test_init_permission_failure_is_structured(tmp_path, monkeypatch):
    store = MailboxStore(tmp_path / "root", authority_id=AUTH)
    original = __import__("os").open

    def deny(path, *args, **kwargs):
        if str(path).endswith(".sqlite3"):
            raise PermissionError("injected access failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("raven.mailbox.db.os.open", deny)
    with pytest.raises(MailboxError, match="storage_permission"):
        store.init(
            agent_id=str(uuid4()),
            instance_id=str(uuid4()),
            request_id=str(uuid4()),
            allowed_scopes=[SCOPE],
            allowed_kinds=["task.request"],
        )


def test_full_disk_before_commit_rolls_back(mailbox, monkeypatch):
    store, sender, target = mailbox
    opener = store.db._open

    class DiskFull:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO messages"):
                error = sqlite3.OperationalError("injected full disk")
                error.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise error
            return self.connection.execute(sql, *args)

    monkeypatch.setattr(store.db, "_open", lambda: DiskFull(opener()))
    with pytest.raises(MailboxError, match="storage_full"):
        store.send(wire(sender, target), sender, now=NOW)
    monkeypatch.setattr(store.db, "_open", opener)
    with store.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_claim_state_requires_nonnull_generation(mailbox):
    store, sender, target = mailbox
    store.send(wire(sender, target), sender, now=NOW)
    with store.db.connection(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE messages SET phase='in_progress',attempt=1,consumer_instance=?,"
                "consumer_generation=NULL,claim_token='secret',lease_until=?",
                (target.instance_id, NOW + 10),
            )


def test_root_authority_cannot_be_mutated(mailbox):
    store, _, _ = mailbox
    with store.db.connection(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE metadata SET value=? WHERE key='authority_id'", (str(uuid4()),))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM metadata WHERE key='tenant_id'")


def test_concurrent_first_init_returns_one_identity(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "a2a"
    params = dict(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["task.request"],
    )
    code = (
        "import json,sys; from raven.mailbox.store import MailboxStore; "
        "store=MailboxStore(sys.argv[1]); "
        "print(store.init(**json.loads(sys.argv[2])).model_dump_json())"
    )
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(root), json.dumps(params)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = []
    try:
        for child in children:
            output, error = child.communicate(timeout=15)
            assert child.returncode == 0, error
            results.append(json.loads(output))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    assert all(result == results[0] for result in results)
    with MailboxStore(root).db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM cards").fetchone()[0] == 1


def test_crash_before_bootstrap_publication_leaves_no_final_db(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "a2a"
    code = (
        "import os,sys; from raven.mailbox.store import MailboxStore; "
        "from raven.mailbox import db; "
        "db.os.link=lambda *a,**k: os._exit(41); "
        "MailboxStore(sys.argv[1]).db.initialize()"
    )
    child = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, timeout=15)
    assert child.returncode == 41, child.stderr
    assert not (root / "mailbox.sqlite3").exists()
    store = MailboxStore(root)
    ref = store.init(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["task.request"],
    )
    assert store.card(ref.agent_id).generation == 1


def test_bootstrap_lock_is_bounded(tmp_path):
    from raven.utils.portable_lock import file_lock

    root = tmp_path / "a2a"
    root.mkdir(mode=0o700)
    anchor = root / ".mailbox-init.lock"
    anchor.touch(mode=0o600)
    with file_lock(anchor):
        start = time.monotonic()
        with pytest.raises(MailboxError, match="lock_busy"):
            MailboxStore(root, busy_timeout_ms=30).db.initialize()
        assert time.monotonic() - start < 1
    assert anchor.exists()


def test_receipt_size_and_event_count_constraints(mailbox):
    store, sender, target = mailbox
    result = store.send(wire(sender, target), sender, now=NOW)
    prefix = (AUTH, "local", target.agent_id, result["message_id"])
    with store.db.connection(write=True) as conn:
        for attempt in range(1, 6):
            conn.execute(
                "INSERT INTO receipt_outbox (authority_id,tenant_id,recipient,message_id,event_key,"
                "envelope_bytes,event_json) VALUES (?,?,?,?,?,?,?)",
                (*prefix, f"received:{attempt}", b"x" * 16384, "{}"),
            )
        conn.execute(
            "INSERT INTO receipt_outbox (authority_id,tenant_id,recipient,message_id,event_key,event_json) "
            "VALUES (?,?,?,?,?,?)",
            (*prefix, "terminal", "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE receipt_outbox SET envelope_bytes=? WHERE event_key='terminal'", (b"x" * 16385,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO receipt_outbox (authority_id,tenant_id,recipient,message_id,event_key,event_json) "
                "VALUES (?,?,?,?,?,?)",
                (*prefix, "received:6", "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO receipt_outbox (authority_id,tenant_id,recipient,message_id,event_key,event_json) "
                "VALUES (?,?,?,?,?,?)",
                (*prefix, "received:1", "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE messages SET reserved_bytes=262145")


def test_generated_instance_replays_after_lost_output(tmp_path):
    store = MailboxStore(tmp_path / "root", authority_id=AUTH)
    params = dict(
        agent_id=str(uuid4()), request_id=str(uuid4()), allowed_scopes=[SCOPE], allowed_kinds=["task.request"]
    )
    enrolled = store.init(**params)
    assert store.init(**params) == enrolled
    request = str(uuid4())
    resumed = store.resume(enrolled.agent_id, request_id=request)
    assert resumed.generation == 2
    assert store.resume(enrolled.agent_id, request_id=request) == resumed
    assert store.card(enrolled.agent_id).generation == 2


@pytest.mark.parametrize(
    "failure,error",
    [
        ("statement", "storage_error"),
        ("business", "storage_error"),
        ("permission", "storage_error"),
        ("os", "storage_error"),
        ("commit", "commit_unknown"),
    ],
)
def test_rollback_failure_stays_structured_and_preserves_commit_boundary(mailbox, monkeypatch, failure, error):
    store, _, _ = mailbox
    opener = store.db._open
    commits = []
    closed = []

    class RollbackFailure:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql == "SELECT injected_failure":
                exc = sqlite3.OperationalError("injected statement I/O failure")
                exc.sqlite_errorcode = sqlite3.SQLITE_IOERR
                raise exc
            return self.connection.execute(sql, *args)

        def rollback(self):
            raise sqlite3.OperationalError("injected rollback I/O failure")

        def commit(self):
            commits.append(True)
            raise sqlite3.OperationalError("injected commit I/O failure")

        def close(self):
            closed.append(True)
            self.connection.close()

    monkeypatch.setattr(store.db, "_open", lambda: RollbackFailure(opener()))
    with pytest.raises(MailboxError) as caught:
        with store.db.connection(write=True) as conn:
            if failure == "statement":
                conn.execute("SELECT injected_failure")
            elif failure == "business":
                raise MailboxError("scope_denied")
            elif failure == "permission":
                raise PermissionError("injected permission failure")
            elif failure == "os":
                raise OSError("injected I/O failure")
    assert caught.value.code == error
    assert bool(commits) == (failure == "commit")
    assert closed == [True]


def test_ordinary_receipt_admission_enforces_raw_size_after_replay(mailbox, monkeypatch):
    store, sender, target = mailbox
    original = json.loads(wire(sender, target))
    data = {
        "for_message_id": original["message_id"],
        "for_digest": original["digest"]["value"],
        "stage": "rejected",
        "attempt": 1,
        "receiver_generation": 1,
        "outcome": "failed",
        "reason": "x" * 20000,
        "result": None,
    }
    raw = wire(
        sender,
        target,
        kind="receipt",
        in_reply_to=original["message_id"],
        receipt_policy="none",
        payload={"content_type": "application/json", "schema": "opena2a.receipt/1", "data": data},
    )
    assert len(raw) > 16384
    with pytest.raises(MailboxError, match="receipt_too_large"):
        store.send(raw, sender, now=NOW)
    monkeypatch.setattr("raven.mailbox.store.RECEIPT_LIMIT", 65536)
    historical = store.send(raw, sender, now=NOW)
    monkeypatch.setattr("raven.mailbox.store.RECEIPT_LIMIT", 16384)
    assert store.send(raw, sender, now=NOW + 100)["message_id"] == historical["message_id"]
    data["reason"] = "rejected"
    small = wire(
        sender,
        target,
        kind="receipt",
        in_reply_to=original["message_id"],
        receipt_policy="none",
        payload={"content_type": "application/json", "schema": "opena2a.receipt/1", "data": data},
    )
    stored = store.send(small, sender, now=NOW)
    assert store.send(small, sender, now=NOW + 100)["message_id"] == stored["message_id"]


def test_receiver_schema_upgrade_preserves_messages_and_poll_replay(mailbox):
    from raven.mailbox.delivery import MailboxDelivery

    store, sender, target = mailbox
    stored = store.send(wire(sender, target), sender, now=NOW)
    delivery = MailboxDelivery(store)
    request_id = str(uuid4())
    claims = delivery.poll(target, request_id=request_id, now=NOW)
    with store.db.connection() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM messages")]
        receipts = [tuple(row) for row in conn.execute("SELECT * FROM mutation_receipts")]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    store.db.upgrade_receivers()
    store.db.upgrade_receivers()
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert [tuple(row) for row in conn.execute("SELECT * FROM messages")] == before
        assert [tuple(row) for row in conn.execute("SELECT * FROM mutation_receipts")] == receipts
        assert {"receiver_bindings", "notifications", "handoff_reads", "task_authority"} <= {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert delivery.poll(target, request_id=request_id, now=NOW) == claims
    assert store.status(target.agent_id, stored["message_id"])["phase"] == "in_progress"


def test_receiver_schema_upgrade_rolls_back_partial_ddl(mailbox, monkeypatch):
    from raven.mailbox import db

    store, sender, target = mailbox
    store.send(wire(sender, target), sender, now=NOW)
    monkeypatch.setattr(db, "_RECEIVER_SCHEMA", (*db._RECEIVER_SCHEMA[:1], "INVALID SQL"))
    with pytest.raises(MailboxError, match="storage_error"):
        store.db.upgrade_receivers()
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='receiver_bindings'").fetchone() is None
    assert len(store.peek(target, now=NOW)) == 1


def test_receiver_schema_upgrade_refuses_future_version(mailbox):
    store, _, _ = mailbox
    with store.db.connection(write=True) as conn:
        conn.execute("PRAGMA user_version=3")
    with pytest.raises(MailboxError, match="unsupported_schema"):
        store.db.upgrade_receivers()
    with sqlite3.connect(store.db.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='receiver_bindings'").fetchone() is None
