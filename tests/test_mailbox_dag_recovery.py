"""Strict authority takeover preserves known evidence and fences unknown execution."""

from dataclasses import replace
from uuid import uuid4

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.dag import CheckObservation, OwnerObservation
from tests.test_mailbox_dag_runner import admitted_candidate, prepare_attempt, with_policy
from tests.test_mailbox_dag_runner import strict_root as strict_root
from tests.test_mailbox_store import mailbox as mailbox


def takeover(ledger, fence, root_fixture):
    _, _, owner, old = root_fixture
    new = replace(old, executor_id=str(uuid4()), pid=old.pid + 1)
    observation = OwnerObservation(old.executor_id, old.pid, old.linux_boot_id, old.process_start_ticks, "dead")
    return ledger.acquire_owner(
        fence.root_id,
        task_owner_ref=owner,
        executor=new,
        expected_owner_epoch=fence.owner_epoch,
        request_id=str(uuid4()),
        replace=True,
        previous_owner=observation,
    )


def test_takeover_resumes_admitted_review_and_first_check_but_fences_old_owner(strict_root):
    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    prepared = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    new = takeover(ledger, fence, strict_root)
    view = ledger.recovery_view(new)
    assert view.attempts[0].state == "review" and view.attempts[0].check_runs[0].state == "prepared"
    assert view.attempts[0].execution_owner_epoch == fence.owner_epoch
    with pytest.raises(MailboxError, match="owner_conflict"):
        ledger.mark_check_submitting(fence, prepared.check_run_id, request_id=str(uuid4()))
    check = ledger.mark_check_submitting(new, prepared.check_run_id, request_id=str(uuid4()))
    assert check.submit_now and check.execution_owner_epoch == new.owner_epoch
    ledger.record_check_result(
        new,
        check.check_run_id,
        CheckObservation(0, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
    )
    assert ledger.decide(new, attempt.attempt_id, request_id=str(uuid4())).decision == "accepted"


def test_takeover_marks_lost_launch_and_submitted_check_unknown_no_resubmit(strict_root):
    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    new = takeover(ledger, fence, strict_root)
    view = ledger.recovery_view(new)
    assert view.attempts[0].state == "unknown"
    assert not ledger.mark_submitting(new, intent.dispatch_id, request_id=str(uuid4())).submit_now


def test_subjective_resolve_cannot_forge_objective_unknown_or_stale_decision(strict_root):
    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    assert callable(getattr(ledger, "resolve", None)), "typed Human resolution is missing"
    with pytest.raises(MailboxError, match="resolution_conflict"):
        ledger.resolve(
            fence,
            attempt.attempt_id,
            expected_owner_epoch=fence.owner_epoch,
            resolution={
                "action": "subjective_approve",
                "snapshot_fingerprint": snapshot.fingerprint,
                "decision_note": "approved",
            },
            request_id=str(uuid4()),
        )
    with pytest.raises(MailboxError, match="owner_conflict"):
        ledger.resolve(
            fence,
            attempt.attempt_id,
            expected_owner_epoch=0,
            resolution={"action": "abandon", "decision_note": "stop"},
            request_id=str(uuid4()),
        )


def test_strict_baseline_and_check_refs_survive_existing_gc(strict_root):
    import os

    from raven.mailbox.blobs import collect

    ledger, root, _, _ = strict_root
    folder = ledger.store.root / "blobs" / "sha256"
    folder.mkdir(parents=True, exist_ok=True)
    pinned = root.verification_plan.baseline.baseline_sha256
    orphan = "f" * 64
    for digest in (pinned, orphan):
        path = folder / digest
        path.write_bytes(b"evidence")
        os.utime(path, (1, 1))
    assert collect(ledger.db, now=100000) == [orphan]
    assert (folder / pinned).exists()


def test_default_three_repairs_survives_reopening_and_never_resets(strict_root):
    from raven.mailbox.dag import StrictDagLedger

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    for ordinal in range(4):
        key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
        ledger.record_result(fence, attempt.attempt_id, key)
        check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
        ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
        ledger.record_check_result(
            fence,
            check.check_run_id,
            CheckObservation(1, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
        )
        decided = ledger.decide(fence, attempt.attempt_id, request_id=str(uuid4()))
        assert decided.attempt.repair_count == min(ordinal + 1, 3)
        ledger = StrictDagLedger(ledger.store)
        if ordinal < 3:
            assert decided.decision == "repair_scheduled"
            intent = decided.intents[0]
            ledger.mark_submitting(fence, intent.dispatch_id, request_id=str(uuid4()))
            attempt = next(a for a in ledger.recovery_view(fence).attempts if a.attempt_id == intent.attempt_id)
        else:
            assert decided.decision == "human" and decided.reason == "repair_budget_exhausted"
            assert not ledger.recovery_view(fence).prepared_intents


def test_explicit_replan_preserves_root_budget_and_persists_new_run_intent(strict_root):
    from dataclasses import asdict

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    ledger.record_unknown(fence, attempt.attempt_id, reason="response_lost", request_id=str(uuid4()))
    root = ledger.recovery_view(fence).root
    resolution = {
        "action": "replan",
        "successor_graph": root.graph,
        "successor_verification_plan": asdict(root.verification_plan),
        "logical_node_mapping": {"A": "A", "B": "B"},
        "decision_note": "explicit successor after lost execution",
    }
    import json

    resolution = json.loads(json.dumps(resolution))
    request = str(uuid4())
    result = ledger.resolve(
        fence, attempt.attempt_id, expected_owner_epoch=fence.owner_epoch, resolution=resolution, request_id=request
    )
    assert result.decision == "replay" and len(result.intents) == 1
    successor = result.intents[0]
    assert successor.successor_kind == "replan" and successor.run_id != root.run_id
    view = ledger.recovery_view(fence)
    assert view.root.root_id == root.root_id and view.root.max_auto_repairs == 3
    assert view.root.run_id == successor.run_id
    replay = ledger.resolve(
        fence, attempt.attempt_id, expected_owner_epoch=fence.owner_epoch, resolution=resolution, request_id=request
    )
    assert replay.intents == result.intents
    assert ledger.root_for_run({"task_id": "task", "workspace_id": "workspace"}, root.run_id).root_id == root.root_id


def test_all_accepted_nodes_complete_root_and_subjective_note_is_durable(strict_root):
    import json
    from dataclasses import replace

    ledger, root, _, _ = strict_root
    policy = replace(root.verification_plan.nodes[0], subjective_review=True)
    with ledger.db.connection(write=True) as conn:
        ledger._save_root(conn, with_policy(root, policy))
    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    for ordinal in range(2):
        check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
        ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
        ledger.record_check_result(
            fence,
            check.check_run_id,
            CheckObservation(0, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
        )
        request = str(uuid4())
        result = ledger.decide(fence, attempt.attempt_id, request_id=request)
        if ordinal == 0:
            assert result.decision == "human"
            assert ledger.recovery_view(fence).root.state == "human"
            request = str(uuid4())
            resolution = {
                "action": "subjective_approve",
                "snapshot_fingerprint": snapshot.fingerprint,
                "decision_note": "Layout approved",
            }
            result = ledger.resolve(
                fence,
                attempt.attempt_id,
                expected_owner_epoch=fence.owner_epoch,
                resolution=resolution,
                request_id=request,
            )
            with ledger.db.connection() as conn:
                saved = json.loads(
                    conn.execute("SELECT result FROM mutation_receipts WHERE request_id=?", (request,)).fetchone()[0]
                )
                assert saved["resolution"] == resolution
            intent = result.intents[0]
            ledger.mark_submitting(fence, intent.dispatch_id, request_id=str(uuid4()))
            attempt = next(a for a in ledger.recovery_view(fence).attempts if a.attempt_id == intent.attempt_id)
            key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
            ledger.record_result(fence, attempt.attempt_id, key)
    assert ledger.recovery_view(fence).root.state == "accepted"


def test_accepted_attempt_cannot_be_abandoned_after_children_released(strict_root):
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
    ledger.decide(fence, attempt.attempt_id, request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="resolution_conflict"):
        ledger.resolve(
            fence,
            attempt.attempt_id,
            expected_owner_epoch=fence.owner_epoch,
            resolution={"action": "abandon", "decision_note": "late"},
            request_id=str(uuid4()),
        )
    assert ledger.recovery_view(fence).attempts[0].state == "accepted"


def test_replan_keeps_historical_plan_pins_and_rejects_old_callbacks(strict_root):
    import json
    from dataclasses import asdict, replace

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt, admit=False)
    ledger.record_unknown(fence, attempt.attempt_id, reason="response_lost", request_id=str(uuid4()))
    old = ledger.recovery_view(fence).root
    policies = (
        replace(old.verification_plan.nodes[0], logical_node_id="C"),
        replace(old.verification_plan.nodes[1], logical_node_id="D", depends_on=("C",)),
    )
    baseline = replace(old.verification_plan.baseline, baseline_sha256="f" * 64, revision_commit="c" * 40)
    plan = replace(old.verification_plan, revision_commit=baseline.revision_commit, baseline=baseline, nodes=policies)
    resolution = json.loads(
        json.dumps(
            {
                "action": "replan",
                "successor_graph": {"nodes": [{"id": "C"}, {"id": "D"}]},
                "successor_verification_plan": asdict(plan),
                "logical_node_mapping": {"A": "C", "B": "D"},
                "decision_note": "changed scope",
            }
        )
    )
    ledger.resolve(
        fence,
        attempt.attempt_id,
        expected_owner_epoch=fence.owner_epoch,
        resolution=resolution,
        request_id=str(uuid4()),
    )
    historical = ledger.root_for_run({"task_id": "task", "workspace_id": "workspace"}, old.run_id)
    assert historical.plan_hash == old.plan_hash and historical.graph_hash == old.graph_hash
    assert historical.verification_plan == old.verification_plan
    with ledger.db.connection() as conn:
        pins = json.loads(
            conn.execute("SELECT artifact_refs FROM strict_roots WHERE root_id=?", (old.root_id,)).fetchone()[0]
        )
        assert old.verification_plan.baseline.baseline_sha256 in pins and baseline.baseline_sha256 in pins
    raw = next(
        a for a in ledger.recovery_view(fence).attempts if a.attempt_id == attempt.attempt_id
    ).staged_envelope_bytes
    with pytest.raises(MailboxError, match="attempt_conflict"):
        ledger.stage_result(fence, attempt.attempt_id, raw, snapshot)


def test_snapshot_changed_after_durable_exit_zero_blocks_acceptance(strict_root):
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
    blocked = ledger.record_unknown(fence, attempt.attempt_id, reason="snapshot_changed", request_id=str(uuid4()))
    assert blocked.human_reason == "snapshot_changed"
    result = ledger.decide(fence, attempt.attempt_id, request_id=str(uuid4()))
    assert result.decision == "human" and not result.intents and result.attempt.state != "accepted"


def test_saved_graph_and_plan_hash_corruption_is_visible(strict_root):
    import json

    ledger, root, _, _ = strict_root
    with ledger.db.connection(write=True) as conn:
        saved = json.loads(
            conn.execute("SELECT record_json FROM strict_roots WHERE root_id=?", (root.root_id,)).fetchone()[0]
        )
        saved["verification_plan"]["nodes"][0]["checks"][0]["argv"] = ["/tmp/replaced-check"]
        conn.execute("UPDATE strict_roots SET record_json=? WHERE root_id=?", (json.dumps(saved), root.root_id))
    with pytest.raises(MailboxError, match="storage_conflict"):
        ledger.status(root.root_id, scope={"task_id": "task", "workspace_id": "workspace"})


def test_same_executor_new_stale_owner_request_is_not_replay(strict_root):
    ledger, root, owner, executor = strict_root
    ledger.acquire_owner(
        root.root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=str(uuid4())
    )
    with pytest.raises(MailboxError, match="owner_conflict"):
        ledger.acquire_owner(
            root.root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=str(uuid4())
        )


def test_review_report_is_consumed_and_retained_through_r1_gc(strict_root):
    from raven.mailbox.dag import StrictDagLedger
    from raven.mailbox.delivery import MailboxDelivery

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    assert ledger.store.status(key.recipient_agent_id, key.message_id)["phase"] == "completed"
    MailboxDelivery(ledger.store).gc(now=2000000000, retention_seconds=0)
    reopened = StrictDagLedger(ledger.store)
    assert reopened.record_result(fence, attempt.attempt_id, key).state == "review"


def process_identity():
    import os
    from pathlib import Path

    from raven.mailbox.dag import ExecutorIdentity

    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    return ExecutorIdentity(
        str(uuid4()), os.getpid(), Path("/proc/sys/kernel/random/boot_id").read_text().strip(), int(fields[19])
    )


def commit_window_process(root_path, root_id, owner_data, window, connection):
    import signal

    from raven.contracts.mailbox import MailboxInstanceRef
    from raven.mailbox.dag import StrictDagLedger
    from raven.mailbox.store import MailboxStore

    owner = MailboxInstanceRef(**owner_data)
    ledger = StrictDagLedger(MailboxStore(root_path))
    root = ledger.status(root_id, scope={"task_id": "task", "workspace_id": "workspace"}).root
    executor = process_identity()
    fence = ledger.acquire_owner(
        root_id, task_owner_ref=owner, executor=executor, expected_owner_epoch=0, request_id=str(uuid4())
    )
    intent, attempt = ledger.claim_node(fence, "A", run_id=root.run_id, request_id=str(uuid4()))
    if window != "prepared":
        ledger.mark_submitting(fence, intent.dispatch_id, request_id=str(uuid4()))
    if window in ("review", "check_submitting", "check_result", "accepted", "repair"):
        key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
        ledger.record_result(fence, attempt.attempt_id, key)
        if window != "review":
            check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
            ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
            if window != "check_submitting":
                ledger.record_check_result(
                    fence,
                    check.check_run_id,
                    CheckObservation(
                        1 if window == "repair" else 0,
                        None,
                        snapshot.fingerprint,
                        snapshot.fingerprint,
                        "1" * 64,
                        "2" * 64,
                        1000,
                        1100,
                    ),
                )
                if window in ("accepted", "repair"):
                    ledger.decide(fence, attempt.attempt_id, request_id=str(uuid4()))
    connection.send({"executor": executor, "attempt_id": attempt.attempt_id, "result_id": intent.result_message_id})
    signal.pause()


def recover_window_process(root_path, root_id, owner_data, old, connection):
    from pathlib import Path

    from raven.contracts.mailbox import MailboxInstanceRef
    from raven.mailbox.dag import StrictDagLedger
    from raven.mailbox.store import MailboxStore

    assert not Path("/proc").joinpath(str(old.pid)).exists()
    ledger = StrictDagLedger(MailboxStore(root_path))
    owner = MailboxInstanceRef(**owner_data)
    observation = OwnerObservation(old.executor_id, old.pid, old.linux_boot_id, old.process_start_ticks, "dead")
    fence = ledger.acquire_owner(
        root_id,
        task_owner_ref=owner,
        executor=process_identity(),
        expected_owner_epoch=1,
        request_id=str(uuid4()),
        replace=True,
        previous_owner=observation,
    )
    view = ledger.recovery_view(fence)
    connection.send(
        {
            "owner_epoch": fence.owner_epoch,
            "attempts": [
                (
                    a.attempt_id,
                    a.state,
                    a.repair_count,
                    [c.state for c in a.check_runs],
                    a.result.message_id if a.result else None,
                )
                for a in view.attempts
            ],
            "intents": [(i.attempt_id, i.successor_kind) for i in view.prepared_intents],
        }
    )


@pytest.mark.parametrize(
    "window", ["prepared", "submitting", "review", "check_submitting", "check_result", "accepted", "repair"]
)
def test_sigkill_committed_windows_reconcile_in_fresh_process(strict_root, window):
    import multiprocessing
    import signal

    ledger, root, owner, _ = strict_root
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(
        target=commit_window_process, args=(ledger.store.root, root.root_id, owner.model_dump(), window, child)
    )
    worker.start()
    try:
        assert parent.poll(30), "commit window child failed to report"
        committed = parent.recv()
        worker.kill()
        worker.join(10)
        assert worker.exitcode == -signal.SIGKILL
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(10)
    parent2, child2 = context.Pipe()
    restarted = context.Process(
        target=recover_window_process,
        args=(ledger.store.root, root.root_id, owner.model_dump(), committed["executor"], child2),
    )
    restarted.start()
    try:
        assert parent2.poll(30), "recovery child failed to report"
        recovered = parent2.recv()
        restarted.join(10)
        assert restarted.exitcode == 0
    finally:
        if restarted.is_alive():
            restarted.kill()
            restarted.join(10)
    assert recovered["owner_epoch"] == 2
    first = recovered["attempts"][0]
    assert first[0] == committed["attempt_id"]
    assert (
        first[1]
        == {
            "prepared": "prepared",
            "submitting": "unknown",
            "review": "review",
            "check_submitting": "review",
            "check_result": "review",
            "accepted": "accepted",
            "repair": "failed",
        }[window]
    )
    if window == "check_submitting":
        assert first[3] == ["unknown"] and not recovered["intents"]
    if window in ("review", "check_submitting", "check_result", "accepted", "repair"):
        assert first[4] == committed["result_id"]
    if window == "repair":
        assert first[2] == 1 and len(recovered["intents"]) == 1 and recovered["intents"][0][1] == "repair"
    elif window == "accepted":
        assert len(recovered["intents"]) == 1 and recovered["intents"][0][1] == "node"
    elif window == "prepared":
        assert len(recovered["intents"]) == 1
    else:
        assert not recovered["intents"]


def test_create_wire_replay_precedes_moving_revision_and_executable_resolution(strict_root):
    from dataclasses import replace

    ledger, root, owner, _ = strict_root
    wire = {
        "history_root": root.history_root + "-wire",
        "revision_selector": {"repo_id": "workspace", "ref": "refs/heads/main"},
        "node_policies": [{"check": "saved argv"}],
    }
    request = str(uuid4())
    assert callable(getattr(ledger, "replay_create", None)), "readonly create receipt lookup missing"
    assert (
        ledger.replay_create(
            task_owner_ref=owner,
            scope={"task_id": "task", "workspace_id": "workspace"},
            request_id=request,
            wire_inputs=wire,
        )
        is None
    )
    created = ledger.create_root(
        task_owner_ref=owner,
        scope={"task_id": "task", "workspace_id": "workspace"},
        history_root=wire["history_root"],
        session_key="session",
        graph=root.graph,
        verification_plan=root.verification_plan,
        request_id=request,
        wire_inputs=wire,
    )
    moved = replace(root.verification_plan, revision_commit="f" * 40)
    replay = ledger.create_root(
        task_owner_ref=owner,
        scope={"task_id": "task", "workspace_id": "workspace"},
        history_root=wire["history_root"],
        session_key="session",
        graph=root.graph,
        verification_plan=moved,
        request_id=request,
        wire_inputs=wire,
    )
    assert replay == created
    assert (
        ledger.replay_create(
            task_owner_ref=owner,
            scope={"task_id": "task", "workspace_id": "workspace"},
            request_id=request,
            wire_inputs=wire,
        )
        == created
    )
    with pytest.raises(MailboxError, match="request_conflict"):
        ledger.replay_create(
            task_owner_ref=owner,
            scope={"task_id": "task", "workspace_id": "workspace"},
            request_id=request,
            wire_inputs={**wire, "history_root": "changed"},
        )


def test_replan_wire_replay_precedes_successor_plan_resolution(strict_root):
    import json
    from dataclasses import asdict

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    ledger.record_unknown(fence, attempt.attempt_id, reason="response_lost", request_id=str(uuid4()))
    root = ledger.recovery_view(fence).root
    sealed = json.loads(
        json.dumps(
            {
                "action": "replan",
                "successor_graph": root.graph,
                "successor_verification_plan": asdict(root.verification_plan),
                "logical_node_mapping": {"A": "A", "B": "B"},
                "decision_note": "explicit successor",
            }
        )
    )
    wire = {
        **sealed,
        "successor_verification_plan": {"revision_selector": {"repo_id": "workspace", "ref": "refs/heads/main"}},
    }
    request = str(uuid4())
    assert (
        ledger.replay_resolve(
            fence, attempt.attempt_id, expected_owner_epoch=fence.owner_epoch, request_id=request, wire_resolution=wire
        )
        is None
    )
    committed = ledger.resolve(
        fence,
        attempt.attempt_id,
        expected_owner_epoch=fence.owner_epoch,
        resolution=sealed,
        request_id=request,
        wire_resolution=wire,
    )
    replay = ledger.replay_resolve(
        fence, attempt.attempt_id, expected_owner_epoch=fence.owner_epoch, request_id=request, wire_resolution=wire
    )
    assert replay.intents == committed.intents
    with pytest.raises(MailboxError, match="request_conflict"):
        ledger.replay_resolve(
            fence,
            attempt.attempt_id,
            expected_owner_epoch=fence.owner_epoch,
            request_id=request,
            wire_resolution={**wire, "decision_note": "changed"},
        )


def test_takeover_replays_exact_staged_return_admission_without_worker_execution(strict_root):
    from tests.test_mailbox_store import NOW

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt, admit=False)
    new = takeover(ledger, fence, strict_root)
    staged = ledger.recovery_view(new).attempts[0]
    ledger.store.send(staged.staged_envelope_bytes, intent.sender_ref, now=NOW)
    adopted = ledger.record_result(new, attempt.attempt_id, key)
    assert adopted.state == "review" and adopted.execution_owner_epoch == fence.owner_epoch
    assert not ledger.recovery_view(new).prepared_intents
    with pytest.raises(MailboxError, match="owner_conflict"):
        ledger.record_result(fence, attempt.attempt_id, key)


def test_staged_old_sender_cannot_be_rewritten_after_card_replacement(strict_root):
    from tests.test_mailbox_store import NOW

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt, admit=False)
    raw = ledger.recovery_view(fence).attempts[0].staged_envelope_bytes
    ledger.store.resume(intent.sender_ref, instance_id=str(uuid4()), request_id=str(uuid4()))
    with pytest.raises(MailboxError, match="instance_fenced"):
        ledger.store.send(raw, intent.sender_ref, now=NOW)
    with ledger.db.connection() as conn:
        assert ledger.store._message(conn, key.recipient_agent_id, key.message_id) is None


def test_replan_fences_prior_run_check_callbacks_even_same_executor_epoch(strict_root):
    import json
    from dataclasses import asdict

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    ledger.record_result(fence, attempt.attempt_id, key)
    check = ledger.prepare_check(fence, attempt.attempt_id, "test", request_id=str(uuid4()), now_ms=1000)
    ledger.mark_check_submitting(fence, check.check_run_id, request_id=str(uuid4()))
    root = ledger.recovery_view(fence).root
    resolution = json.loads(
        json.dumps(
            {
                "action": "replan",
                "successor_graph": root.graph,
                "successor_verification_plan": asdict(root.verification_plan),
                "logical_node_mapping": {"A": "A", "B": "B"},
                "decision_note": "authorized isolated successor",
            }
        )
    )
    ledger.resolve(
        fence,
        attempt.attempt_id,
        expected_owner_epoch=fence.owner_epoch,
        resolution=resolution,
        request_id=str(uuid4()),
    )
    for operation in (
        lambda: ledger.record_check_started(fence, check.check_run_id, strict_root[3]),
        lambda: ledger.record_check_result(
            fence,
            check.check_run_id,
            CheckObservation(0, None, snapshot.fingerprint, snapshot.fingerprint, "1" * 64, "2" * 64, 1000, 1100),
        ),
        lambda: ledger.record_unknown(
            fence, check_run_id=check.check_run_id, reason="response_lost", request_id=str(uuid4())
        ),
    ):
        with pytest.raises(MailboxError, match="attempt_conflict"):
            operation()


def test_staged_admitted_report_is_retained_before_result_attachment(strict_root):
    from raven.mailbox.delivery import MailboxDelivery
    from tests.test_mailbox_store import NOW

    ledger, fence, intent, attempt = prepare_attempt(strict_root)
    key, snapshot = admitted_candidate(ledger, fence, intent, attempt)
    delivery = MailboxDelivery(ledger.store)
    claim = delivery.poll(intent.recipient_ref, request_id=str(uuid4()), now=NOW)[0]
    delivery.finish(
        intent.recipient_ref, claim, {"outcome": "succeeded", "summary": "report processed", "evidence": []}, now=NOW
    )
    result = delivery.gc(now=2000000000, retention_seconds=0)
    assert result["compacted"] == 0
    assert ledger.record_result(fence, attempt.attempt_id, key).state == "review"
