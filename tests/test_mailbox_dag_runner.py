"""Strict DAG authority migration and durable transition tests."""

import pytest

from raven.contracts.mailbox import MailboxError
from tests.test_mailbox_store import mailbox as mailbox


def test_explicit_strict_upgrade_preserves_r1_r2_and_is_idempotent(mailbox):
    store, sender, _ = mailbox
    assert callable(getattr(store.db, "upgrade_strict", None)), "explicit schema3 migration is missing"
    store.db.upgrade_strict()
    store.db.upgrade_receivers()
    store.db.upgrade_strict()
    assert store.card(sender.agent_id).instance_id == sender.instance_id
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"strict_roots", "strict_nodes", "strict_attempts", "strict_dispatch_outbox"} <= tables
        assert conn.execute("SELECT count(*) FROM cards").fetchone()[0] == 2


def test_partial_strict_schema_is_rejected_without_repair(mailbox):
    store, _, _ = mailbox
    assert callable(getattr(store.db, "upgrade_strict", None)), "explicit schema3 migration is missing"
    store.db.upgrade_strict()
    with store.db.connection(write=True) as conn:
        conn.execute("DROP TABLE strict_dispatch_outbox")
    with pytest.raises(MailboxError, match="unsupported_schema"):
        with store.db.connection():
            pass


def test_ordinary_read_does_not_upgrade_receiver_schema(mailbox):
    store, sender, _ = mailbox
    store.card(sender.agent_id)
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    store.db.upgrade_receivers()
    store.card(sender.agent_id)
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_strict_ledger_exports_frozen_runtime_records():
    import importlib.util

    assert importlib.util.find_spec("raven.mailbox.dag") is not None, "strict authority module is missing"
    from raven.mailbox import dag

    for name in (
        "StrictDagLedger",
        "ExecutorIdentity",
        "OwnerObservation",
        "RootFence",
        "ArtifactEntry",
        "SnapshotManifest",
        "CheckSpec",
        "NodePolicy",
        "VerificationPlan",
        "RootRecord",
        "DispatchIntent",
        "AttemptRecord",
        "AdmittedResultKey",
        "CheckRunRecord",
        "CheckObservation",
        "TransitionResult",
        "RecoveryRecord",
    ):
        assert hasattr(dag, name), name


@pytest.fixture
def strict_root(mailbox, tmp_path):
    from uuid import uuid4

    from raven.mailbox.dag import (
        CheckSpec,
        ExecutorIdentity,
        NodePolicy,
        SnapshotManifest,
        StrictDagLedger,
        VerificationPlan,
    )
    from raven.mailbox.handoff import MailboxHandoff
    from tests.test_mailbox_store import SCOPE

    store, owner, _ = mailbox
    owner = store.init(
        agent_id=str(uuid4()),
        instance_id=str(uuid4()),
        request_id=str(uuid4()),
        allowed_scopes=[SCOPE],
        allowed_kinds=["task.request", "task.result", "receipt"],
    )
    ledger = StrictDagLedger(store, upgrade=True)
    MailboxHandoff(store).create_task(**SCOPE, owner_agent_id=owner.agent_id, request_id=str(uuid4()))
    baseline = SnapshotManifest("a" * 40, "b" * 40, "c" * 64, (), "d" * 64)
    check = CheckSpec("test", ("/usr/bin/true",), "e" * 64, ".", 10, repairable_exit_codes=(1,))
    policies = (
        NodePolicy("A", (), ("output.txt",), ("check.py",), (check,)),
        NodePolicy("B", ("A",), ("next.txt",), ("check.py",), (check,)),
    )
    plan = VerificationPlan(baseline.revision_commit, baseline.revision_tree, baseline, policies)
    assert callable(getattr(ledger, "create_root", None)), "strict root authority is missing"
    root = ledger.create_root(
        task_owner_ref=owner,
        scope=SCOPE,
        history_root=str(tmp_path / "new-history"),
        session_key="session",
        graph={"nodes": [{"id": "A"}, {"id": "B"}]},
        verification_plan=plan,
        request_id=str(uuid4()),
    )
    executor = ExecutorIdentity(str(uuid4()), 123, "boot", 456)
    return ledger, root, owner, executor


def test_unique_executor_and_dispatch_cas(strict_root):
    from dataclasses import replace
    from uuid import uuid4

    ledger, root, owner, executor = strict_root
    request = str(uuid4())
    fence = ledger.acquire_owner(
        root.root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=request
    )
    assert (
        ledger.acquire_owner(
            root.root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=request
        )
        == fence
    )
    with pytest.raises(MailboxError, match="owner_conflict"):
        ledger.acquire_owner(
            root.root_id,
            task_owner_ref=owner,
            executor=replace(executor, executor_id=str(uuid4())),
            expected_owner_epoch=1,
            request_id=str(uuid4()),
        )
    with pytest.raises(MailboxError, match="dependencies_not_accepted"):
        ledger.claim_node(fence, "B", run_id=root.run_id, request_id=str(uuid4()))
    intent, attempt = ledger.claim_node(fence, "A", run_id=root.run_id, request_id=str(uuid4()))
    submit = str(uuid4())
    assert ledger.mark_submitting(fence, intent.dispatch_id, request_id=submit).submit_now
    assert not ledger.mark_submitting(fence, intent.dispatch_id, request_id=submit).submit_now
    assert attempt.ordinal == 1
    assert ledger.root_for_run({"task_id": "task", "workspace_id": "workspace"}, root.run_id).root_id == root.root_id
    with pytest.raises(MailboxError, match="scope_denied"):
        ledger.status(root.root_id, scope={"task_id": "wrong", "workspace_id": "workspace"})


def with_policy(root, policy):
    import hashlib
    import json
    from dataclasses import asdict, replace

    from raven.mailbox.codec import canonical_bytes

    plan = replace(root.verification_plan, nodes=(policy, root.verification_plan.nodes[1]))
    return replace(
        root,
        verification_plan=plan,
        plan_hash=hashlib.sha256(canonical_bytes(json.loads(json.dumps(asdict(plan))))).hexdigest(),
    )


def admitted_candidate(ledger, fence, intent, attempt, *, admit=True):
    import json

    from raven.mailbox.codec import encode_envelope, seal_envelope
    from raven.mailbox.dag import AdmittedResultKey
    from tests.test_mailbox_store import NOW, wire

    raw = wire(
        intent.sender_ref,
        intent.recipient_ref,
        message_id=intent.result_message_id,
        in_reply_to=intent.request_message_id,
        kind="task.result",
        receipt_policy="none",
        payload={
            "content_type": "application/json",
            "schema": "opena2a.result/1",
            "data": {
                "request_message_id": intent.request_message_id,
                "outcome": "succeeded",
                "summary": "candidate",
                "evidence": [],
                "strict_root_id": fence.root_id,
                "logical_node_id": attempt.logical_node_id,
                "attempt_id": attempt.attempt_id,
                "task_id": intent.task_id,
                "backend": "raven-loop",
            },
        },
    )
    raw = encode_envelope(seal_envelope(json.loads(raw)))
    snapshot = ledger.status(
        fence.root_id, scope={"task_id": "task", "workspace_id": "workspace"}
    ).root.verification_plan.baseline
    ledger.stage_result(fence, attempt.attempt_id, raw, snapshot)
    if admit:
        ledger.store.send(raw, intent.sender_ref, now=NOW)
    return AdmittedResultKey(
        intent.recipient_ref.agent_id, intent.result_message_id, json.loads(raw)["digest"]["value"]
    ), snapshot


def prepare_attempt(strict_root):
    from uuid import uuid4

    ledger, root, owner, executor = strict_root
    fence = ledger.acquire_owner(
        root.root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=str(uuid4())
    )
    intent, attempt = ledger.claim_node(fence, "A", run_id=root.run_id, request_id=str(uuid4()))
    ledger.mark_submitting(fence, intent.dispatch_id, request_id=str(uuid4()))
    return ledger, fence, intent, attempt


def test_result_requires_admitted_row_and_acceptance_releases_child_atomically(strict_root):
    from uuid import uuid4

    from raven.mailbox.dag import CheckObservation

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    assert callable(getattr(ledger, "stage_result", None)), "durable candidate staging is missing"
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt, admit=False)
    with pytest.raises(MailboxError, match="result_not_admitted"):
        ledger.record_result(fence, attempt.attempt_id, key)
    from tests.test_mailbox_store import NOW

    raw = (
        ledger.status(fence.root_id, scope={"task_id": "task", "workspace_id": "workspace"})
        .attempts[0]
        .staged_envelope_bytes
    )
    ledger.store.send(raw, intent.sender_ref, now=NOW)
    assert ledger.record_result(fence, attempt.attempt_id, key).state == "review"
    check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    request = str(uuid4())
    assert ledger.mark_check_submitting(fence, check.check_run_id, request_id=request).submit_now
    assert not ledger.mark_check_submitting(fence, check.check_run_id, request_id=request).submit_now
    observed = CheckObservation(0, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100)
    ledger.record_check_result(fence, check.check_run_id, observed)
    decision = str(uuid4())
    accepted = ledger.decide(fence, attempt.attempt_id, request_id=decision)
    assert accepted.decision == "accepted"
    assert ledger.store.status(key.recipient_agent_id, key.message_id)["phase"] == "completed"
    assert not ledger.store.peek(fence.task_owner_ref, now=1790899200)
    assert len(accepted.intents) == 1 and accepted.intents[0].logical_node_id == "B"
    replay = ledger.decide(fence, attempt.attempt_id, request_id=decision)
    assert replay.intents == accepted.intents
    assert (
        ledger.status(fence.root_id, scope={"task_id": "task", "workspace_id": "workspace"}).attempts[0].repair_count
        == 0
    )


def test_known_failure_spends_budget_once_and_unknown_never_repairs(strict_root):
    from uuid import uuid4

    from raven.mailbox.dag import CheckObservation

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    assert callable(getattr(ledger, "stage_result", None)), "durable candidate staging is missing"
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
    ledger.record_check_result(
        fence,
        check.check_run_id,
        CheckObservation(1, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
    )
    request = str(uuid4())
    failed = ledger.decide(fence, attempt.attempt_id, request_id=request)
    assert failed.decision == "repair_scheduled" and failed.attempt.repair_count == 1
    assert ledger.decide(fence, attempt.attempt_id, request_id=request).attempt.repair_count == 1
    repair = failed.intents[0]
    ledger.mark_submitting(fence, repair.dispatch_id, request_id=str(uuid4()))
    unknown = ledger.record_unknown(fence, repair.attempt_id, reason="response_lost", request_id=str(uuid4()))
    assert unknown.state == "unknown" and unknown.repair_count == 1
    view = ledger.recovery_view(fence)
    assert not view.prepared_intents


def test_invalid_check_and_unchecked_node_policy_refused(strict_root):
    from dataclasses import replace
    from uuid import uuid4

    ledger, root, owner, _ = strict_root
    kwargs = dict(
        task_owner_ref=owner,
        scope={"task_id": "task", "workspace_id": "workspace"},
        history_root=root.history_root + "-bad",
        session_key="session",
        graph=root.graph,
        request_id=str(uuid4()),
    )
    for bad in (
        replace(root.verification_plan.nodes[0], checks=(), subjective_review=False),
        replace(
            root.verification_plan.nodes[0],
            checks=(replace(root.verification_plan.nodes[0].checks[0], argv=("relative",)),),
        ),
        replace(root.verification_plan.nodes[0], output_paths=("check.py",)),
    ):
        with pytest.raises(MailboxError, match="invalid_verification_plan"):
            ledger.create_root(**kwargs, verification_plan=replace(root.verification_plan, nodes=(bad,)))


def test_atomic_accept_rolls_back_when_successor_intent_cannot_publish(strict_root):
    from uuid import uuid4

    from raven.mailbox.dag import CheckObservation

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
    ledger.record_check_result(
        fence,
        check.check_run_id,
        CheckObservation(0, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
    )
    with ledger.db.connection(write=True) as conn:
        conn.execute(
            "CREATE TRIGGER fail_strict_publish BEFORE INSERT ON strict_dispatch_outbox BEGIN SELECT RAISE(ABORT,'publish failed'); END"
        )
    request = str(uuid4())
    with pytest.raises(MailboxError, match="storage_error"):
        ledger.decide(fence, attempt.attempt_id, request_id=request)
    view = ledger.recovery_view(fence)
    assert view.attempts[0].state == "review" and len(view.attempts) == 1
    with ledger.db.connection(write=True) as conn:
        conn.execute("DROP TRIGGER fail_strict_publish")
    assert ledger.decide(fence, attempt.attempt_id, request_id=request).decision == "accepted"


def test_strict_migration_failure_rolls_back_all_added_tables(mailbox, monkeypatch):
    from raven.mailbox import db

    store, _, _ = mailbox
    monkeypatch.setattr(db, "_STRICT_SCHEMA", (*db._STRICT_SCHEMA, db._STRICT_SCHEMA[0]))
    with pytest.raises(MailboxError, match="storage_error"):
        store.db.upgrade_strict()
    with store.db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "strict_roots" not in tables and "receiver_bindings" not in tables


def test_forged_admitted_identity_and_mutated_snapshot_are_refused(strict_root):
    from dataclasses import replace

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    with pytest.raises(MailboxError, match="result_conflict"):
        ledger.record_result(fence, attempt.attempt_id, replace(key, digest="f" * 64))
    raw = ledger.recovery_view(fence).attempts[0].staged_envelope_bytes
    with pytest.raises(MailboxError, match="result_conflict"):
        ledger.stage_result(fence, attempt.attempt_id, raw, replace(snapshot, fingerprint="f" * 64))


def test_unknown_check_cannot_be_submitted_twice_or_declared_success(strict_root):
    from uuid import uuid4

    from raven.mailbox.dag import CheckObservation

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
    unknown = ledger.record_check_result(
        fence,
        check.check_run_id,
        CheckObservation(None, "timeout", snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 12000),
    )
    assert unknown.state == "unknown"
    assert not ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4())).submit_now
    decided = ledger.decide(fence, attempt.attempt_id, request_id=str(uuid4()))
    assert decided.decision == "human" and not decided.intents and decided.attempt.repair_count == 0


def test_known_unlisted_nonzero_is_failed_not_objective_unknown(strict_root):
    from uuid import uuid4

    from raven.mailbox.dag import CheckObservation

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
    ledger.record_check_result(
        fence,
        check.check_run_id,
        CheckObservation(17, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
    )
    result = ledger.decide(fence, attempt.attempt_id, request_id=str(uuid4()))
    assert result.decision == "failed" and result.attempt.state == "failed" and result.attempt.repair_count == 0


def test_check_policy_default_does_not_invent_repairable_exit():
    from raven.mailbox.dag import CheckSpec

    assert CheckSpec("check", ("/usr/bin/true",), "e" * 64, ".", 10).repairable_exit_codes == ()


def test_subjective_flag_does_not_gate_known_objective_repair(strict_root):
    from dataclasses import replace
    from uuid import uuid4

    from raven.mailbox.dag import CheckObservation

    ledger, root, _, _ = strict_root
    policy = replace(root.verification_plan.nodes[0], subjective_review=True)
    with ledger.db.connection(write=True) as conn:
        ledger._save_root(conn, with_policy(root, policy))
    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
    ledger.record_check_result(
        fence,
        check.check_run_id,
        CheckObservation(1, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
    )
    with pytest.raises(MailboxError, match="resolution_conflict"):
        ledger.resolve(
            fence,
            attempt.attempt_id,
            expected_owner_epoch=fence.owner_epoch,
            resolution={
                "action": "subjective_approve",
                "snapshot_fingerprint": snapshot.fingerprint,
                "decision_note": "layout",
            },
            request_id=str(uuid4()),
        )
    assert ledger.recovery_view(fence).attempts[0].repair_count == 0
    result = ledger.decide(fence, attempt.attempt_id, request_id=str(uuid4()))
    assert result.decision == "repair_scheduled" and result.attempt.repair_count == 1


@pytest.mark.parametrize(
    "column,value,error",
    [("allowed_kinds", '["task.request"]', "kind_denied"), ("allowed_scopes", "[]", "scope_denied")],
)
def test_strict_create_requires_actual_owner_card_result_scope(strict_root, column, value, error):
    from uuid import uuid4

    ledger, root, owner, _ = strict_root
    with ledger.db.connection(write=True) as conn:
        conn.execute(f"UPDATE cards SET {column}=? WHERE agent_id=?", (value, owner.agent_id))
    with pytest.raises(MailboxError, match=error):
        ledger.create_root(
            task_owner_ref=owner,
            scope={"task_id": "task", "workspace_id": "workspace"},
            history_root=root.history_root + "-new",
            session_key="session",
            graph=root.graph,
            verification_plan=root.verification_plan,
            request_id=str(uuid4()),
        )


def test_explicit_recover_adopts_current_card_for_first_launch(strict_root):
    from uuid import uuid4

    ledger, root, owner, executor = strict_root
    fence = ledger.acquire_owner(
        root.root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=str(uuid4())
    )
    intent, attempt = ledger.claim_node(fence, "A", run_id=root.run_id, request_id=str(uuid4()))
    current = ledger.store.resume(owner, request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="owner_conflict"):
        ledger.acquire_owner(
            root.root_id, task_owner_ref=current, executor=executor, expected_owner_epoch=1, request_id=str(uuid4())
        )
    adopted = ledger.acquire_owner(
        root.root_id,
        task_owner_ref=current,
        executor=executor,
        expected_owner_epoch=1,
        request_id=str(uuid4()),
        adopt_current_ref=True,
    )
    assert adopted.owner_epoch == 2
    view = ledger.recovery_view(adopted)
    prepared = view.prepared_intents[0]
    assert (prepared.dispatch_id, prepared.attempt_id, prepared.result_message_id) == (
        intent.dispatch_id,
        attempt.attempt_id,
        intent.result_message_id,
    )
    assert prepared.sender_ref == prepared.recipient_ref == current
    assert view.attempts[0].execution_owner_epoch == 2
    assert view.attempts[0].repair_count == 0
    assert ledger.mark_submitting(adopted, prepared.dispatch_id, request_id=str(uuid4())).submit_now
    with pytest.raises(MailboxError):
        ledger.recovery_view(fence)


@pytest.mark.parametrize("admit", [False, True])
def test_card_recovery_preserves_original_candidate_provenance(strict_root, admit):
    from uuid import uuid4

    ledger, root, owner, executor = strict_root
    fence = ledger.acquire_owner(
        root.root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=str(uuid4())
    )
    intent, attempt = ledger.claim_node(fence, "A", run_id=root.run_id, request_id=str(uuid4()))
    ledger.mark_submitting(fence, intent.dispatch_id, request_id=str(uuid4()))
    key, _ = admitted_candidate(ledger, fence, intent, attempt, admit=admit)
    if admit:
        ledger.record_result(fence, attempt.attempt_id, key)
    before = ledger.recovery_view(fence).attempts[0]
    current = ledger.store.resume(owner, request_id=str(uuid4()))
    request = str(uuid4())
    wire = {"root_id": root.root_id, "expected_owner_epoch": 1, "replace_owner": False}
    adopted = ledger.acquire_owner(
        root.root_id,
        task_owner_ref=current,
        executor=executor,
        expected_owner_epoch=1,
        request_id=request,
        adopt_current_ref=True,
        wire_inputs=wire,
    )
    after = ledger.recovery_view(adopted).attempts[0]
    assert after.staged_envelope_bytes == before.staged_envelope_bytes
    assert after.execution_owner_epoch == 1
    assert after.result == before.result
    with ledger.db.connection() as conn:
        assert ledger._attempt_intent(conn, attempt.attempt_id).sender_ref == owner
    assert (
        ledger.replay_owner(
            root.root_id, task_owner_ref=current, executor=executor, request_id=request, wire_inputs=wire
        )
        == adopted
    )
    with pytest.raises(MailboxError, match="request_conflict"):
        ledger.replay_owner(
            root.root_id,
            task_owner_ref=current,
            executor=executor,
            request_id=request,
            wire_inputs={**wire, "expected_owner_epoch": 2},
        )
    if not admit:
        with pytest.raises(MailboxError, match="instance_fenced"):
            ledger.store.send(before.staged_envelope_bytes, owner)
        assert after.state == "unknown"
    else:
        assert after.state == "review"
