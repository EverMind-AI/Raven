"""Artifact-backed proposal and separately committed task ownership contracts."""

import hashlib
import sqlite3
from uuid import uuid4

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import decode_envelope
from raven.mailbox.store import MailboxStore
from tests.test_mailbox_store import AUTH, NOW, SCOPE, wire

BINDING = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def handoff(tmp_path):
    from raven.mailbox.handoff import MailboxHandoff

    store = MailboxStore(tmp_path / "mailbox", authority_id=AUTH)
    refs = [
        store.init(
            agent_id=str(uuid4()),
            instance_id=str(uuid4()),
            request_id=str(uuid4()),
            allowed_scopes=[SCOPE],
            allowed_kinds=["handoff.offer", "handoff.accept", "receipt"],
        )
        for _ in range(2)
    ]
    service = MailboxHandoff(store)
    with store.db.connection(write=True) as conn:
        conn.execute(
            "INSERT INTO receiver_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                BINDING,
                refs[1].agent_id,
                refs[1].instance_id,
                refs[1].generation,
                "receiver",
                1,
                SCOPE["task_id"],
                SCOPE["workspace_id"],
                None,
                None,
                "tui:receiver",
                "[]",
                "test-hash",
                None,
                NOW,
            ),
        )
    source = tmp_path / "snapshot"
    source.write_bytes(b"complete authorized snapshot")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    data = dict(
        handoff_id=str(uuid4()),
        mode="offer",
        task_ref=SCOPE["task_id"],
        source_sessions=[],
        objective="Continue authorized task",
        effective_decisions=[],
        rejected_alternatives=[],
        unresolved_items=["Review output"],
        work_state=dict(done=[], next_actions=["Review"]),
        environment=dict(repo_id="repo", commit=None, dirty_patch_sha256=None),
        required_artifacts=[sha],
        acceptance=[],
        authority=dict(owner=refs[0].agent_id, transfer_required=True),
    )
    raw = wire(
        *refs,
        kind="handoff.offer",
        receipt_policy="terminal",
        ttl=3600,
        payload={"content_type": "application/json", "schema": "opena2a.handoff/1", "data": data},
        artifacts=[dict(sha256=sha, size=source.stat().st_size, media_type="text/plain", name="snapshot")],
    )
    sent = store.send(raw, refs[0], now=NOW, artifacts={sha: source})
    return service, store, refs[0], refs[1], decode_envelope(raw), sha, sent


def acceptance(store, sender, receiver, offer, sha, **changes):
    data = dict(
        handoff_id=offer.payload.data["handoff_id"],
        offer_message_id=offer.message_id,
        read_artifact_hashes=[sha],
        read_coverage={sha: {"bytes_read": offer.artifacts[0].size}},
        accepted_scope=SCOPE,
        next_action="Review",
        unresolved_items=["Review output"],
    )
    data.update(changes)
    raw = wire(
        receiver,
        sender,
        kind="handoff.accept",
        receipt_policy="none",
        ttl=3600,
        in_reply_to=offer.message_id,
        payload={"content_type": "application/json", "schema": "opena2a.handoff/1", "data": data},
    )
    return store.send(raw, receiver, now=NOW)["message_id"]


def test_claimed_read_coverage_cannot_propose_without_actual_reads(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    accept_id = acceptance(store, sender, receiver, offer, sha)
    with pytest.raises(MailboxError, match="handoff_unread"):
        service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)


def test_full_read_proposal_and_host_commit_are_distinct_and_idempotent(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    created = service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    assert created["assignment_epoch"] == 1
    content = service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    assert hashlib.sha256(content).hexdigest() == sha
    accept_id = acceptance(store, sender, receiver, offer, sha)
    proposal = service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)
    assert proposal["status"] == "PROPOSED"
    assert proposal["unresolved_items"] == ["Review output"]
    assert service.task_status(**SCOPE)["owner_agent_id"] == sender.agent_id
    request_id = str(uuid4())
    params = dict(
        binding_id=BINDING,
        revalidate_receiver=lambda: None,
        receiver_ref=receiver,
        scope=SCOPE,
        offer_message_id=offer.message_id,
        accept_message_id=accept_id,
        expected_owner_agent_id=sender.agent_id,
        expected_assignment_epoch=1,
        request_id=request_id,
    )
    committed = service.commit(**params, now=NOW)
    assert committed["owner_agent_id"] == receiver.agent_id
    assert committed["assignment_epoch"] == 2
    assert service.commit(**params, now=NOW + 1) == committed
    with pytest.raises(MailboxError, match="assignment_conflict"):
        service.commit(**dict(params, request_id=str(uuid4())), now=NOW + 1)
    with store.db.connection() as conn:
        coverage = conn.execute("SELECT * FROM handoff_reads").fetchone()
        assert coverage["bytes_read"] == len(content)
        assert coverage["generation"] == receiver.generation


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_artifact_blocks_read_and_commit(handoff, damage):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    blob = store.root / "blobs" / "sha256" / sha
    if damage == "missing":
        blob.unlink()
    else:
        blob.write_bytes(b"corrupt")
    with pytest.raises(MailboxError, match="storage_conflict"):
        service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    with pytest.raises(MailboxError, match="storage_conflict"):
        service.commit(
            binding_id=BINDING,
            revalidate_receiver=lambda: None,
            receiver_ref=receiver,
            scope=SCOPE,
            now=NOW,
            offer_message_id=offer.message_id,
            accept_message_id=accept_id,
            expected_owner_agent_id=sender.agent_id,
            expected_assignment_epoch=1,
            request_id=str(uuid4()),
        )
    assert service.task_status(**SCOPE)["assignment_epoch"] == 1


def test_replaced_receiver_cannot_reuse_previous_read_coverage(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    current = store.resume(receiver, request_id=str(uuid4()))
    accept_id = acceptance(store, sender, current, offer, sha)
    with pytest.raises(MailboxError, match="instance_fenced"):
        service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)
    with pytest.raises(MailboxError, match="handoff_unread"):
        service.propose(current, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)


@pytest.mark.parametrize("change", ["handoff_id", "accepted_scope", "offer_message_id", "read_coverage"])
def test_acceptance_requires_exact_offer_scope_and_verified_coverage(handoff, change):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    changes = {
        "handoff_id": str(uuid4()),
        "accepted_scope": dict(SCOPE, task_id="other"),
        "offer_message_id": str(uuid4()),
        "read_coverage": {sha: {"bytes_read": 0}},
    }
    accept_id = acceptance(store, sender, receiver, offer, sha, **{change: changes[change]})
    with pytest.raises(MailboxError):
        service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)


def test_scoped_reader_refuses_other_task_and_undeclared_artifacts(handoff):
    service, _, _, receiver, offer, sha, _ = handoff
    with pytest.raises(MailboxError, match="scope_denied"):
        service.read_artifact(receiver, offer.message_id, sha, scope=dict(SCOPE, task_id="other"), now=NOW)
    with pytest.raises(MailboxError, match="evidence_unavailable"):
        service.read_artifact(receiver, offer.message_id, "0" * 64, scope=SCOPE, now=NOW)


def test_duplicate_host_request_rejects_changed_transfer_inputs(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    request = str(uuid4())
    original = service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=request, now=NOW)
    assert service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=request, now=NOW + 1) == original
    with pytest.raises(MailboxError, match="request_conflict"):
        service.create_task(**dict(SCOPE, task_id="other"), owner_agent_id=sender.agent_id, request_id=request)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    params = dict(
        binding_id=BINDING,
        revalidate_receiver=lambda: None,
        receiver_ref=receiver,
        scope=SCOPE,
        offer_message_id=offer.message_id,
        accept_message_id=accept_id,
        expected_owner_agent_id=sender.agent_id,
        expected_assignment_epoch=1,
        request_id=str(uuid4()),
    )
    service.commit(**params, now=NOW)
    with pytest.raises(MailboxError, match="request_conflict"):
        service.commit(**dict(params, expected_assignment_epoch=2), now=NOW)


def test_processed_offer_receipt_does_not_transfer_task(handoff):
    from raven.mailbox.delivery import MailboxDelivery

    service, store, sender, receiver, offer, _, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    delivery = MailboxDelivery(store)
    claim = delivery.poll(receiver, request_id=str(uuid4()), now=NOW)[0]
    assert claim.envelope.message_id == offer.message_id
    assert (
        delivery.finish(receiver, claim, dict(outcome="succeeded", summary="Read offer", evidence=[]), now=NOW)["phase"]
        == "completed"
    )
    assert delivery.flush_receipts(receiver, now=NOW)["published"] == 1
    receipt = store.peek(sender, now=NOW)[0]["envelope"]
    assert receipt["kind"] == "receipt"
    assert receipt["in_reply_to"] == offer.message_id
    authority = service.task_status(**SCOPE)
    assert authority["owner_agent_id"] == sender.agent_id
    assert authority["assignment_epoch"] == 1
    assert authority["confirmed_handoff_id"] is None


def test_artifact_copy_and_hash_do_not_hold_database_write_transaction(handoff, monkeypatch):
    from raven.mailbox import blobs

    service, store, sender, receiver, offer, sha, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    original = blobs._open_regular
    reads = []

    def checked_open(path, code):
        conn = sqlite3.connect(store.db.path, timeout=0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
        reads.append(path)
        return original(path, code)

    monkeypatch.setattr(blobs, "_open_regular", checked_open)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)
    service.commit(
        binding_id=BINDING,
        revalidate_receiver=lambda: None,
        receiver_ref=receiver,
        scope=SCOPE,
        now=NOW,
        offer_message_id=offer.message_id,
        accept_message_id=accept_id,
        expected_owner_agent_id=sender.agent_id,
        expected_assignment_epoch=1,
        request_id=str(uuid4()),
    )
    assert len(reads) == 3


def test_create_task_validates_scope_and_known_owner(handoff):
    service, _, sender, _, _, _, _ = handoff
    with pytest.raises(MailboxError, match="invalid_scope"):
        service.create_task(
            task_id="", workspace_id=SCOPE["workspace_id"], owner_agent_id=sender.agent_id, request_id=str(uuid4())
        )
    with pytest.raises(MailboxError, match="unknown_agent"):
        service.create_task(**SCOPE, owner_agent_id=str(uuid4()), request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="task_not_found"):
        service.task_status(**SCOPE)


def test_expired_handoff_cannot_first_commit_but_confirmed_replay_survives(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    params = dict(
        binding_id=BINDING,
        revalidate_receiver=lambda: None,
        receiver_ref=receiver,
        scope=SCOPE,
        offer_message_id=offer.message_id,
        accept_message_id=accept_id,
        expected_owner_agent_id=sender.agent_id,
        expected_assignment_epoch=1,
        request_id=str(uuid4()),
    )
    with pytest.raises(MailboxError, match="message_expired"):
        service.commit(**params, now=NOW + 3600)
    confirmed = service.commit(**params, now=NOW + 1)
    assert service.commit(**params, now=NOW + 3600) == confirmed


def test_commit_expected_owner_must_be_offer_source(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=receiver.agent_id, request_id=str(uuid4()), now=NOW)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    with pytest.raises(MailboxError, match="handoff_conflict"):
        service.commit(
            binding_id=BINDING,
            revalidate_receiver=lambda: None,
            receiver_ref=receiver,
            scope=SCOPE,
            offer_message_id=offer.message_id,
            accept_message_id=accept_id,
            expected_owner_agent_id=receiver.agent_id,
            expected_assignment_epoch=1,
            request_id=str(uuid4()),
            now=NOW,
        )
    assert service.task_status(**SCOPE)["assignment_epoch"] == 1


@pytest.mark.parametrize("coverage", [[], None, {"bytes_read": "26"}, {"bytes_read": 26, "extra": 1}])
def test_nested_read_coverage_shape_is_validated(handoff, coverage):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha, read_coverage={sha: coverage})
    with pytest.raises(MailboxError, match="handoff_unread"):
        service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)


def test_host_commit_cannot_substitute_other_receiver_or_scope(handoff):
    service, store, sender, receiver, offer, sha, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    params = dict(
        binding_id=BINDING,
        revalidate_receiver=lambda: None,
        receiver_ref=receiver,
        scope=SCOPE,
        offer_message_id=offer.message_id,
        accept_message_id=accept_id,
        expected_owner_agent_id=sender.agent_id,
        expected_assignment_epoch=1,
        request_id=str(uuid4()),
        now=NOW,
    )
    with pytest.raises(MailboxError, match="receiver_fenced"):
        service.commit(**dict(params, receiver_ref=sender))
    with pytest.raises(MailboxError, match="receiver_fenced"):
        service.commit(**dict(params, scope=dict(SCOPE, workspace_id="other")))
    assert service.task_status(**SCOPE)["assignment_epoch"] == 1


def test_same_accept_uuid_in_other_mailbox_does_not_ambiguate_offer(handoff):
    from raven.mailbox.codec import encode_envelope, seal_envelope

    service, store, sender, receiver, offer, sha, _ = handoff
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    other = store.init(
        agent_id=str(uuid4()), request_id=str(uuid4()), allowed_scopes=[SCOPE], allowed_kinds=["handoff.accept"]
    )
    with store.db.connection() as conn:
        original = decode_envelope(store._message(conn, sender.agent_id, accept_id)["envelope_bytes"])
    data = original.model_dump()
    data["target_identity"] = {**other.model_dump(exclude={"generation"}), "instance_id": None}
    store.send(encode_envelope(seal_envelope(data)), receiver, now=NOW)
    assert (
        service.propose(receiver, accept_id, offer_message_id=offer.message_id, scope=SCOPE, now=NOW)["status"]
        == "PROPOSED"
    )


def test_revoke_during_artifact_verification_fences_host_commit(handoff, monkeypatch):
    from raven.mailbox import blobs

    service, store, sender, receiver, offer, sha, _ = handoff
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    original = blobs.verify_artifacts

    def revoke_after_hash(root, envelope):
        original(root, envelope)
        with store.db.connection(write=True) as conn:
            conn.execute("UPDATE receiver_bindings SET revoked_at=? WHERE binding_id=?", (NOW, BINDING))

    monkeypatch.setattr(blobs, "verify_artifacts", revoke_after_hash)
    with pytest.raises(MailboxError, match="receiver_fenced"):
        service.commit(
            binding_id=BINDING,
            revalidate_receiver=lambda: None,
            receiver_ref=receiver,
            scope=SCOPE,
            offer_message_id=offer.message_id,
            accept_message_id=accept_id,
            expected_owner_agent_id=sender.agent_id,
            expected_assignment_epoch=1,
            request_id=str(uuid4()),
            now=NOW,
        )
    assert service.task_status(**SCOPE)["assignment_epoch"] == 1


@pytest.mark.parametrize("change", ["registry", "incarnation"])
def test_physical_receiver_change_during_hash_fences_commit(handoff, tmp_path, monkeypatch, change):
    from types import SimpleNamespace

    from raven.agent.registry.identity import IdentityRegistry
    from raven.contracts.terminal import TerminalRecord
    from raven.mailbox import blobs
    from raven.mailbox.receiver import ReceiverService

    service, store, sender, receiver, offer, sha, _ = handoff
    record = TerminalRecord(worktree_id=SCOPE["workspace_id"], worktree_path=str(tmp_path), liveness="live")
    registry = IdentityRegistry(
        tmp_path / "receiver-identity.json", config_rows=lambda: [{"name": "worker", "kind": "cli", "command": "codex"}]
    )
    registry.register("worker", kind_ref="worker", binding=record, task_ref=SCOPE["task_id"])
    receivers = ReceiverService(store, registry, SimpleNamespace(show=lambda handle: record))
    binding_id = receivers.grant("worker", receiver, SCOPE, request_id=str(uuid4()))["binding"]["binding_id"]
    service.create_task(**SCOPE, owner_agent_id=sender.agent_id, request_id=str(uuid4()), now=NOW)
    service.read_artifact(receiver, offer.message_id, sha, scope=SCOPE, now=NOW)
    accept_id = acceptance(store, sender, receiver, offer, sha)
    original = blobs.verify_artifacts

    def change_after_hash(root, envelope):
        original(root, envelope)
        if change == "incarnation":
            record.incarnation_id = str(uuid4())
        else:
            registry.register("worker", kind_ref="worker", binding=record, task_ref=SCOPE["task_id"])

    monkeypatch.setattr(blobs, "verify_artifacts", change_after_hash)
    receivers.current(binding_id)
    with pytest.raises(MailboxError, match="receiver_binding_stale"):
        service.commit(
            binding_id=binding_id,
            revalidate_receiver=lambda: receivers.current(binding_id),
            receiver_ref=receiver,
            scope=SCOPE,
            offer_message_id=offer.message_id,
            accept_message_id=accept_id,
            expected_owner_agent_id=sender.agent_id,
            expected_assignment_epoch=1,
            request_id=str(uuid4()),
            now=NOW,
        )
    assert service.task_status(**SCOPE)["owner_agent_id"] == sender.agent_id
    assert service.task_status(**SCOPE)["assignment_epoch"] == 1
