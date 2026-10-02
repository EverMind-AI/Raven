"""Scoped receiver enrollment and mailbox isolation behavior."""

import hashlib
import importlib.util
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from raven.agent.registry.identity import IdentityRegistry
from raven.contracts.mailbox import MailboxError
from raven.contracts.terminal import TerminalRecord
from raven.mailbox.delivery import MailboxDelivery
from raven.mailbox.store import MailboxStore
from tests.test_mailbox_store import AUTH, NOW, SCOPE, wire
from tests.test_mailbox_store import mailbox as mailbox


def test_receiver_service_exists():
    assert importlib.util.find_spec("raven.mailbox.receiver") is not None


@pytest.fixture
def receivers(tmp_path):
    from raven.mailbox.receiver import ReceiverService

    store = MailboxStore(tmp_path / "mailbox", authority_id=AUTH)
    other = {"task_id": "other", "workspace_id": "workspace"}
    refs = [
        store.init(
            agent_id=str(uuid4()),
            instance_id=str(uuid4()),
            request_id=str(uuid4()),
            allowed_scopes=[SCOPE, other],
            allowed_kinds=["task.request", "receipt"],
            max_in_flight=4,
        )
        for _ in range(2)
    ]
    record = TerminalRecord(worktree_id="workspace", worktree_path=str(tmp_path), liveness="live", writable=True)
    registry = IdentityRegistry(
        tmp_path / "identity.json", config_rows=lambda: [{"name": "worker", "kind": "cli", "command": "codex"}]
    )
    registry.register("worker", kind_ref="worker", binding=record, task_ref="task")
    host = SimpleNamespace(show=lambda handle: record)
    return ReceiverService(store, registry, host), refs, registry, record, other


def grant(receivers, **kwargs):
    service, refs, *_ = receivers
    return service.grant("worker", refs[1], SCOPE, request_id=str(uuid4()), **kwargs)


def test_enrollment_hashes_secret_and_does_not_mutate_registry(receivers):
    service, _, registry, _, _ = receivers
    generation = registry.show("worker").binding_generation
    issued = grant(receivers)
    binding = service.authenticate(issued["credential"])
    assert binding.scope == SCOPE
    assert registry.show("worker").binding_generation == generation
    assert "credential" not in issued["binding"]
    with service.store.db.connection() as conn:
        saved = conn.execute("SELECT credential_hash FROM receiver_bindings").fetchone()[0]
    assert saved == hashlib.sha256(issued["credential"].encode()).hexdigest()
    assert issued["credential"] not in saved


def test_authentication_and_revocation_fail_closed(receivers):
    service, *_ = receivers
    with pytest.raises(MailboxError, match="receiver_unauthorized"):
        service.authenticate("guessed")
    issued = grant(receivers)
    binding = service.authenticate(issued["credential"])
    service.revoke(binding.binding_id)
    with pytest.raises(MailboxError, match="receiver_revoked"):
        service.current(binding.binding_id)


@pytest.mark.parametrize("replacement", ["card", "registry", "terminal"])
def test_replacement_fences_existing_credential(receivers, replacement):
    service, refs, registry, record, _ = receivers
    issued = grant(receivers)
    if replacement == "card":
        service.store.resume(refs[1], request_id=str(uuid4()))
    elif replacement == "registry":
        registry.register("worker", kind_ref="worker", binding=record, task_ref="task")
    else:
        record.incarnation_id = str(uuid4())
    with pytest.raises(MailboxError, match="instance_fenced|receiver_binding_stale"):
        service.authenticate(issued["credential"])


def test_scope_grant_must_match_registry_task(receivers):
    service, refs, _, _, other = receivers
    with pytest.raises(MailboxError, match="scope_denied"):
        service.grant("worker", refs[1], other, request_id=str(uuid4()))


def test_poll_and_peek_filter_before_artifact_verification(receivers):
    service, refs, _, _, other = receivers
    store = service.store
    corrupt = store.send(wire(*refs, scope=other), refs[0], now=NOW)
    valid = store.send(wire(*refs), refs[0], now=NOW)
    with store.db.connection(write=True) as conn:
        row = conn.execute(
            "SELECT envelope_bytes FROM messages WHERE message_id=?", (corrupt["message_id"],)
        ).fetchone()
        value = json.loads(row[0])
        value["digest"]["value"] = "f" * 64
        conn.execute(
            "UPDATE messages SET envelope_bytes=? WHERE message_id=?",
            (json.dumps(value).encode(), corrupt["message_id"]),
        )
    assert store.peek(refs[1], now=NOW, allowed_scopes=[SCOPE])[0]["message_id"] == valid["message_id"]
    claims = MailboxDelivery(store).poll(refs[1], request_id=str(uuid4()), now=NOW, allowed_scopes=[SCOPE])
    assert [claim.envelope.message_id for claim in claims] == [valid["message_id"]]
    assert store.status(refs[1].agent_id, corrupt["message_id"])["phase"] == "pending"


def test_poll_replay_cannot_change_authorized_scope(receivers):
    service, refs, _, _, other = receivers
    service.store.send(wire(*refs), refs[0], now=NOW)
    request = str(uuid4())
    delivery = MailboxDelivery(service.store)
    delivery.poll(refs[1], request_id=request, now=NOW, allowed_scopes=[SCOPE])
    with pytest.raises(MailboxError, match="request_conflict"):
        delivery.poll(refs[1], request_id=request, now=NOW, allowed_scopes=[other])


def test_native_session_resolution_requires_unique_current_binding(receivers):
    service, refs, registry, *_ = receivers
    registry.register("worker", kind_ref="worker", task_ref="task", session_key="native-session")
    first = grant(receivers)
    binding = service.for_session("native-session")
    assert binding.binding_id == first["binding"]["binding_id"]
    assert binding.terminal_handle is None
    second = grant(receivers)
    with pytest.raises(MailboxError, match="receiver_binding_ambiguous"):
        service.for_session("native-session")
    service.revoke(second["binding"]["binding_id"])
    assert service.for_session("native-session") == binding
    registry.register("worker", kind_ref="worker", task_ref="task", session_key="new-session")
    with pytest.raises(MailboxError, match="receiver_unauthorized"):
        service.for_session("native-session")


def test_read_only_receiver_resolution_preserves_schema_one(mailbox, tmp_path):
    from raven.mailbox.receiver import ReceiverService

    store, *_ = mailbox
    registry = IdentityRegistry(tmp_path / "identity.json", config_rows=lambda: [])
    service = ReceiverService(store, registry, upgrade=False)
    with pytest.raises(MailboxError, match="receiver_capability_unavailable"):
        service.current(str(uuid4()))
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='receiver_bindings'").fetchone() is None


def test_receiver_message_compaction_returns_stable_error(receivers):
    from raven.contracts.mailbox import MailboxResult

    service, refs, *_ = receivers
    binding = service.authenticate(grant(receivers)["credential"])
    sent = service.store.send(wire(*refs, receipt_policy="none"), refs[0], now=NOW)
    delivery = MailboxDelivery(service.store)
    claim = delivery.poll(refs[1], request_id=str(uuid4()), now=NOW)[0]
    delivery.finish(refs[1], claim, MailboxResult(outcome="succeeded", summary="Done", evidence=[]), now=NOW)
    assert delivery.gc(now=NOW + 10, retention_seconds=10)["compacted"] == 1
    with pytest.raises(MailboxError, match="terminal_compacted"):
        service.message(binding, sent["message_id"])
