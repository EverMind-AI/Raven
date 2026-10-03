"""Durable strict DAG claims, admitted evidence, verification and successor authority."""

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from dataclasses import replace as replace_record
from graphlib import CycleError, TopologicalSorter
from pathlib import PurePosixPath
from uuid import uuid4

from raven.contracts.mailbox import MailboxError, MailboxInstanceRef
from raven.mailbox.codec import canonical_bytes, decode_envelope
from raven.mailbox.db import canonical_id
from raven.mailbox.delivery import MailboxDelivery


@dataclass(frozen=True)
class ExecutorIdentity:
    executor_id: str
    pid: int
    linux_boot_id: str
    process_start_ticks: int


@dataclass(frozen=True)
class OwnerObservation:
    executor_id: str
    pid: int
    linux_boot_id: str
    process_start_ticks: int
    state: str


@dataclass(frozen=True)
class RootFence:
    root_id: str
    task_owner_ref: MailboxInstanceRef
    executor_id: str
    owner_epoch: int
    assignment_epoch: int


@dataclass(frozen=True)
class ArtifactEntry:
    path: str
    sha256: str
    size: int
    media_type: str = "application/octet-stream"


@dataclass(frozen=True)
class SnapshotManifest:
    revision_commit: str
    revision_tree: str
    baseline_sha256: str
    entries: tuple[ArtifactEntry, ...]
    fingerprint: str


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    argv: tuple[str, ...]
    executable_sha256: str
    cwd: str
    timeout_seconds: int
    repairable_exit_codes: tuple[int, ...] = ()
    nonrepairable_exit_codes: tuple[int, ...] = ()


@dataclass(frozen=True)
class NodePolicy:
    logical_node_id: str
    depends_on: tuple[str, ...]
    output_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    checks: tuple[CheckSpec, ...]
    subjective_review: bool = False


@dataclass(frozen=True)
class VerificationPlan:
    revision_commit: str
    revision_tree: str
    baseline: SnapshotManifest
    nodes: tuple[NodePolicy, ...]


@dataclass(frozen=True)
class AdmittedResultKey:
    recipient_agent_id: str
    message_id: str
    digest: str


@dataclass(frozen=True)
class CheckObservation:
    exit_code: int | None
    error: str | None
    before_fingerprint: str
    after_fingerprint: str
    stdout_sha256: str
    stderr_sha256: str
    started_at_ms: int
    ended_at_ms: int


@dataclass(frozen=True)
class CheckRunRecord:
    check_run_id: str
    attempt_id: str
    check_id: str
    state: str
    deadline_ms: int
    snapshot_fingerprint: str
    execution_owner_epoch: int
    process_identity: ExecutorIdentity | None = None
    observation: CheckObservation | None = None
    submit_now: bool = False


@dataclass(frozen=True)
class RootRecord:
    root_id: str
    history_root: str
    session_key: str
    task_id: str
    workspace_id: str
    run_id: str
    task_owner_ref: MailboxInstanceRef
    assignment_epoch: int
    executor: ExecutorIdentity | None
    owner_epoch: int
    max_auto_repairs: int
    graph_hash: str
    plan_hash: str
    graph: dict
    verification_plan: VerificationPlan
    state: str


@dataclass(frozen=True)
class DispatchIntent:
    dispatch_id: str
    root_id: str
    logical_node_id: str
    attempt_id: str
    run_id: str
    task_id: str
    request_message_id: str
    result_message_id: str
    sender_ref: MailboxInstanceRef
    recipient_ref: MailboxInstanceRef
    input_hash: str
    input_snapshot: SnapshotManifest | None
    state: str
    successor_kind: str = "node"
    submit_now: bool = False


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    root_id: str
    logical_node_id: str
    run_id: str
    ordinal: int
    execution_owner_epoch: int
    decision_owner_epoch: int
    assignment_epoch: int
    state: str
    staged_envelope_bytes: bytes | None = None
    result: AdmittedResultKey | None = None
    snapshot: SnapshotManifest | None = None
    check_runs: tuple[CheckRunRecord, ...] = ()
    repair_count: int = 0
    human_reason: str | None = None


@dataclass(frozen=True)
class TransitionResult:
    attempt: AttemptRecord
    intents: tuple[DispatchIntent, ...]
    decision: str
    reason: str | None = None


@dataclass(frozen=True)
class RecoveryRecord:
    root: RootRecord
    attempts: tuple[AttemptRecord, ...]
    prepared_intents: tuple[DispatchIntent, ...]
    unknown_attempts: tuple[str, ...]


def _canonical(value):
    return canonical_bytes(json.loads(json.dumps(value)))


def _validate_plan(plan):
    ids = {n.logical_node_id for n in plan.nodes}
    if not ids or len(ids) != len(plan.nodes):
        raise MailboxError("invalid_verification_plan")
    try:
        tuple(TopologicalSorter({n.logical_node_id: n.depends_on for n in plan.nodes}).static_order())
    except CycleError:
        raise MailboxError("invalid_verification_plan") from None
    for node in plan.nodes:
        if not node.checks and not node.subjective_review or not set(node.depends_on) <= ids:
            raise MailboxError("invalid_verification_plan")
        if len({c.check_id for c in node.checks}) != len(node.checks):
            raise MailboxError("invalid_verification_plan")
        paths = (*node.output_paths, *node.protected_paths)
        if any(not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts for path in paths):
            raise MailboxError("invalid_verification_plan")
        if any(
            a == b or a.startswith(b + "/") or b.startswith(a + "/")
            for a in node.output_paths
            for b in node.protected_paths
        ):
            raise MailboxError("invalid_verification_plan")
        for check in node.checks:
            if (
                not check.argv
                or not PurePosixPath(check.argv[0]).is_absolute()
                or not re.fullmatch("[0-9a-f]{64}", check.executable_sha256)
                or type(check.timeout_seconds) is not int
                or check.timeout_seconds <= 0
                or PurePosixPath(check.cwd).is_absolute()
                or ".." in PurePosixPath(check.cwd).parts
                or 0 in (*check.repairable_exit_codes, *check.nonrepairable_exit_codes)
                or set(check.repairable_exit_codes) & set(check.nonrepairable_exit_codes)
            ):
                raise MailboxError("invalid_verification_plan")


def _json(record):
    data = asdict(record)
    for key in ("task_owner_ref", "sender_ref", "recipient_ref"):
        if key in data:
            data[key] = data[key].model_dump()
    data.pop("staged_envelope_bytes", None)
    data.pop("submit_now", None)
    if "check_runs" in data:
        for check in data["check_runs"]:
            check.pop("submit_now", None)
    return _canonical(data).decode()


def _snapshot(data):
    if data is None:
        return None
    return SnapshotManifest(**{**data, "entries": tuple(ArtifactEntry(**x) for x in data["entries"])})


def _plan(data):
    return VerificationPlan(
        data["revision_commit"],
        data["revision_tree"],
        _snapshot(data["baseline"]),
        tuple(
            NodePolicy(
                **{
                    **n,
                    "depends_on": tuple(n["depends_on"]),
                    "output_paths": tuple(n["output_paths"]),
                    "protected_paths": tuple(n["protected_paths"]),
                    "checks": tuple(
                        CheckSpec(
                            **{
                                **c,
                                "argv": tuple(c["argv"]),
                                "repairable_exit_codes": tuple(c["repairable_exit_codes"]),
                                "nonrepairable_exit_codes": tuple(c["nonrepairable_exit_codes"]),
                            }
                        )
                        for c in n["checks"]
                    ),
                }
            )
            for n in data["nodes"]
        ),
    )


def _root(data):
    return RootRecord(
        **{
            **data,
            "task_owner_ref": MailboxInstanceRef(**data["task_owner_ref"]),
            "executor": ExecutorIdentity(**data["executor"]) if data["executor"] else None,
            "verification_plan": _plan(data["verification_plan"]),
        }
    )


def _check(data):
    return CheckRunRecord(
        **{
            **data,
            "process_identity": ExecutorIdentity(**data["process_identity"]) if data["process_identity"] else None,
            "observation": CheckObservation(**data["observation"]) if data["observation"] else None,
        }
    )


def _attempt(data, candidate=None):
    return AttemptRecord(
        **{
            **data,
            "staged_envelope_bytes": candidate,
            "result": AdmittedResultKey(**data["result"]) if data["result"] else None,
            "snapshot": _snapshot(data["snapshot"]),
            "check_runs": tuple(_check(x) for x in data["check_runs"]),
        }
    )


def _intent(data):
    return DispatchIntent(
        **{
            **data,
            "sender_ref": MailboxInstanceRef(**data["sender_ref"]),
            "recipient_ref": MailboxInstanceRef(**data["recipient_ref"]),
            "input_snapshot": _snapshot(data["input_snapshot"]),
        }
    )


class StrictDagLedger:
    """Keep business execution and durable acceptance separate from mailbox delivery."""

    def __init__(self, store, *, upgrade=False):
        self.store = store
        self.db = store.db
        if upgrade:
            self.db.upgrade_strict()

    def _require_schema(self, conn):
        if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            raise MailboxError("receiver_capability_unavailable")

    def _load_root(self, conn, root_id):
        self._require_schema(conn)
        row = conn.execute("SELECT record_json FROM strict_roots WHERE root_id=?", (canonical_id(root_id),)).fetchone()
        if row is None:
            raise MailboxError("strict_root_not_found")
        root = _root(json.loads(row[0]))
        if (
            root.graph_hash != hashlib.sha256(_canonical(root.graph)).hexdigest()
            or root.plan_hash != hashlib.sha256(_canonical(asdict(root.verification_plan))).hexdigest()
        ):
            raise MailboxError("storage_conflict")
        return root

    def _save_root(self, conn, root):
        conn.execute("UPDATE strict_roots SET record_json=? WHERE root_id=?", (_json(root), root.root_id))

    def _authority(self, conn, ref, scope):
        card = self.store._current(conn, ref)
        if scope not in json.loads(card["allowed_scopes"]):
            raise MailboxError("scope_denied")
        if "task.result" not in json.loads(card["allowed_kinds"]):
            raise MailboxError("kind_denied")
        row = conn.execute(
            "SELECT * FROM task_authority WHERE task_id=? AND workspace_id=?", (scope["task_id"], scope["workspace_id"])
        ).fetchone()
        if row is None or row["owner_agent_id"] != canonical_id(ref.agent_id):
            raise MailboxError("owner_conflict")
        return row["assignment_epoch"]

    def _fence(self, conn, fence):
        root = self._load_root(conn, fence.root_id)
        epoch = self._authority(
            conn, fence.task_owner_ref, {"task_id": root.task_id, "workspace_id": root.workspace_id}
        )
        if (
            root.executor is None
            or root.executor.executor_id != fence.executor_id
            or root.owner_epoch != fence.owner_epoch
            or epoch != fence.assignment_epoch
            or root.assignment_epoch != epoch
            or root.task_owner_ref != fence.task_owner_ref
        ):
            raise MailboxError("owner_conflict")
        return root

    def _load_attempt(self, conn, attempt_id):
        row = conn.execute(
            "SELECT record_json,candidate_bytes FROM strict_attempts WHERE attempt_id=?", (canonical_id(attempt_id),)
        ).fetchone()
        if row is None:
            raise MailboxError("attempt_not_found")
        return _attempt(json.loads(row[0]), row[1])

    def _save_attempt(self, conn, attempt):
        conn.execute(
            "UPDATE strict_attempts SET record_json=?,candidate_bytes=? WHERE attempt_id=?",
            (_json(attempt), attempt.staged_envelope_bytes, attempt.attempt_id),
        )

    def _load_intent(self, conn, dispatch_id):
        row = conn.execute(
            "SELECT record_json FROM strict_dispatch_outbox WHERE dispatch_id=?", (canonical_id(dispatch_id),)
        ).fetchone()
        if row is None:
            raise MailboxError("dispatch_not_found")
        return _intent(json.loads(row[0]))

    def _save_intent(self, conn, intent):
        conn.execute(
            "UPDATE strict_dispatch_outbox SET record_json=? WHERE dispatch_id=?", (_json(intent), intent.dispatch_id)
        )

    def create_root(
        self,
        *,
        task_owner_ref,
        scope,
        history_root,
        session_key,
        graph,
        verification_plan,
        request_id,
        max_auto_repairs=3,
        wire_inputs=None,
    ):
        inputs = {
            "scope": scope,
            "history_root": history_root,
            "session_key": session_key,
            "graph": graph,
            "verification_plan": asdict(verification_plan),
            "max_auto_repairs": max_auto_repairs,
        }
        inputs = wire_inputs if wire_inputs is not None else inputs
        with self.db.connection(write=True) as conn:
            epoch = self._authority(conn, task_owner_ref, scope)
            replay, digest = self.db.replay(
                conn, task_owner_ref.agent_id, request_id, "dag.create", json.loads(_canonical(inputs))
            )
            if replay:
                return _root(replay["record"])
            if type(max_auto_repairs) is not int or max_auto_repairs < 0:
                raise MailboxError("invalid_repair_budget")
            _validate_plan(verification_plan)
            nodes = verification_plan.nodes
            if (
                not nodes
                or len({n.logical_node_id for n in nodes}) != len(nodes)
                or any(not n.checks and not n.subjective_review for n in nodes)
            ):
                raise MailboxError("invalid_verification_plan")

            if conn.execute(
                "SELECT 1 FROM strict_roots WHERE json_extract(record_json,'$.history_root')=?", (history_root,)
            ).fetchone():
                raise MailboxError("history_root_conflict")
            root = RootRecord(
                str(uuid4()),
                history_root,
                session_key,
                scope["task_id"],
                scope["workspace_id"],
                str(uuid4()),
                task_owner_ref,
                epoch,
                None,
                0,
                max_auto_repairs,
                hashlib.sha256(_canonical(graph)).hexdigest(),
                hashlib.sha256(_canonical(asdict(verification_plan))).hexdigest(),
                graph,
                verification_plan,
                "prepared",
            )
            conn.execute(
                "INSERT INTO strict_roots VALUES (?,?,?,?,?,?,?)",
                (
                    root.root_id,
                    root.run_id,
                    root.task_id,
                    root.workspace_id,
                    _json(root),
                    _canonical([verification_plan.baseline.baseline_sha256]).decode(),
                    "{}",
                ),
            )
            for n in nodes:
                conn.execute(
                    "INSERT INTO strict_nodes VALUES (?,?,?)",
                    (
                        root.root_id,
                        n.logical_node_id,
                        _canonical({"repair_count": 0, "attempt_id": None, "accepted_snapshot": None}).decode(),
                    ),
                )
            self.db.save_receipt(
                conn, task_owner_ref.agent_id, request_id, "dag.create", digest, {"record": json.loads(_json(root))}
            )
            return root

    def replay_owner(self, root_id, *, task_owner_ref, executor, request_id, wire_inputs):
        with self.db.connection() as conn:
            root = self._load_root(conn, root_id)
            self._authority(conn, task_owner_ref, {"task_id": root.task_id, "workspace_id": root.workspace_id})
            replay, _ = self.db.replay(
                conn, task_owner_ref.agent_id, request_id, "dag.owner", json.loads(_canonical(wire_inputs))
            )
            if not replay:
                return None
            fence = RootFence(**{**replay, "task_owner_ref": MailboxInstanceRef(**replay["task_owner_ref"])})
            self._fence(conn, fence)
            if root.executor != executor or root.task_owner_ref != task_owner_ref:
                raise MailboxError("owner_conflict")
            return fence

    def acquire_owner(
        self,
        root_id,
        *,
        task_owner_ref,
        executor,
        expected_owner_epoch,
        request_id,
        replace=False,
        previous_owner=None,
        adopt_current_ref=False,
        wire_inputs=None,
    ):
        inputs = {
            "root_id": root_id,
            "executor": asdict(executor),
            "expected_owner_epoch": expected_owner_epoch,
            "replace": replace,
            "previous_owner": asdict(previous_owner) if previous_owner else None,
            "adopt_current_ref": adopt_current_ref,
        }
        inputs = wire_inputs if wire_inputs is not None else inputs
        with self.db.connection(write=True) as conn:
            root = self._load_root(conn, root_id)
            epoch = self._authority(conn, task_owner_ref, {"task_id": root.task_id, "workspace_id": root.workspace_id})
            replay, digest = self.db.replay(
                conn, task_owner_ref.agent_id, request_id, "dag.owner", json.loads(_canonical(inputs))
            )
            if replay:
                fence = RootFence(**{**replay, "task_owner_ref": MailboxInstanceRef(**replay["task_owner_ref"])})
                self._fence(conn, fence)
                return fence
            if root.owner_epoch != expected_owner_epoch:
                raise MailboxError("owner_conflict")
            ref_changed = root.task_owner_ref != task_owner_ref
            if root.assignment_epoch != epoch or (
                ref_changed
                and (
                    not adopt_current_ref
                    or root.task_owner_ref.agent_id != task_owner_ref.agent_id
                    or root.task_owner_ref.authority_id != task_owner_ref.authority_id
                    or root.task_owner_ref.tenant_id != task_owner_ref.tenant_id
                )
            ):
                raise MailboxError("owner_conflict")
            if root.executor != executor or ref_changed:
                if (
                    root.executor != executor
                    and root.executor
                    and (
                        not replace
                        or previous_owner is None
                        or previous_owner.state == "live"
                        or (
                            previous_owner.executor_id,
                            previous_owner.pid,
                            previous_owner.linux_boot_id,
                            previous_owner.process_start_ticks,
                        )
                        != (
                            root.executor.executor_id,
                            root.executor.pid,
                            root.executor.linux_boot_id,
                            root.executor.process_start_ticks,
                        )
                    )
                ):
                    raise MailboxError("owner_conflict")
                root = replace_record(
                    root,
                    task_owner_ref=task_owner_ref,
                    executor=executor,
                    owner_epoch=root.owner_epoch + 1,
                    state="running",
                )
                for row in conn.execute(
                    "SELECT attempt_id FROM strict_attempts WHERE root_id=?", (root.root_id,)
                ).fetchall():
                    attempt = self._load_attempt(conn, row[0])
                    checks = tuple(
                        replace_record(c, state="unknown")
                        if c.state in ("submitting", "running") and c.observation is None
                        else c
                        for c in attempt.check_runs
                    )
                    state = (
                        "unknown"
                        if attempt.state in ("submitting", "started") and attempt.result is None
                        else attempt.state
                    )
                    self._save_attempt(
                        conn,
                        replace_record(
                            attempt,
                            state=state,
                            check_runs=checks,
                            decision_owner_epoch=root.owner_epoch,
                            execution_owner_epoch=root.owner_epoch
                            if state == "prepared"
                            else attempt.execution_owner_epoch,
                        ),
                    )
                    if state == "prepared" and ref_changed:
                        intent = self._attempt_intent(conn, attempt.attempt_id)
                        if intent.state == "prepared" and attempt.staged_envelope_bytes is None:
                            self._save_intent(
                                conn, replace_record(intent, sender_ref=task_owner_ref, recipient_ref=task_owner_ref)
                            )
                    if state == "unknown":
                        self._save_intent(
                            conn, replace_record(self._attempt_intent(conn, attempt.attempt_id), state="unknown")
                        )
                self._save_root(conn, root)
                self._refresh_state(conn, root)
            if root.task_owner_ref != task_owner_ref or root.assignment_epoch != epoch:
                raise MailboxError("owner_conflict")
            fence = RootFence(root.root_id, task_owner_ref, executor.executor_id, root.owner_epoch, epoch)
            self.db.save_receipt(
                conn,
                task_owner_ref.agent_id,
                request_id,
                "dag.owner",
                digest,
                {**asdict(fence), "task_owner_ref": task_owner_ref.model_dump()},
            )
            return fence

    def _claim(self, conn, fence, root, logical_node_id, run_id, kind="node"):
        node = conn.execute(
            "SELECT record_json FROM strict_nodes WHERE root_id=? AND logical_node_id=?",
            (root.root_id, logical_node_id),
        ).fetchone()
        if node is None:
            raise MailboxError("node_not_found")
        data = json.loads(node[0])
        if data["attempt_id"]:
            raise MailboxError("attempt_conflict")
        policy = next(n for n in root.verification_plan.nodes if n.logical_node_id == logical_node_id)
        snapshots = []
        for dep in policy.depends_on:
            saved = conn.execute(
                "SELECT record_json FROM strict_nodes WHERE root_id=? AND logical_node_id=?", (root.root_id, dep)
            ).fetchone()
            if saved is None or json.loads(saved[0])["accepted_snapshot"] is None:
                raise MailboxError("dependencies_not_accepted")
            snapshots.append(json.loads(saved[0])["accepted_snapshot"])
        attempt_id = str(uuid4())
        ordinal = (
            conn.execute(
                "SELECT count(*) FROM strict_attempts WHERE root_id=? AND logical_node_id=?",
                (root.root_id, logical_node_id),
            ).fetchone()[0]
            + 1
        )
        attempt = AttemptRecord(
            attempt_id,
            root.root_id,
            logical_node_id,
            run_id,
            ordinal,
            fence.owner_epoch,
            fence.owner_epoch,
            fence.assignment_epoch,
            "prepared",
            repair_count=data["repair_count"],
        )
        intent = DispatchIntent(
            str(uuid4()),
            root.root_id,
            logical_node_id,
            attempt_id,
            run_id,
            attempt_id,
            str(uuid4()),
            str(uuid4()),
            root.task_owner_ref,
            root.task_owner_ref,
            hashlib.sha256(_canonical(snapshots)).hexdigest(),
            root.verification_plan.baseline,
            "prepared",
            kind,
        )
        conn.execute(
            "INSERT INTO strict_attempts VALUES (?,?,?,?,?,?,?)",
            (attempt_id, root.root_id, logical_node_id, run_id, _json(attempt), None, "[]"),
        )
        conn.execute(
            "INSERT INTO strict_dispatch_outbox VALUES (?,?,?,?,?)",
            (intent.dispatch_id, root.root_id, attempt_id, _json(intent), "[]"),
        )
        data["attempt_id"] = attempt_id
        conn.execute(
            "UPDATE strict_nodes SET record_json=? WHERE root_id=? AND logical_node_id=?",
            (_canonical(data).decode(), root.root_id, logical_node_id),
        )
        return intent, attempt

    def claim_node(self, fence, logical_node_id, *, run_id, request_id):
        inputs = {
            "root_id": fence.root_id,
            "logical_node_id": logical_node_id,
            "run_id": run_id,
            "owner_epoch": fence.owner_epoch,
        }
        with self.db.connection(write=True) as conn:
            root = self._fence(conn, fence)
            replay, digest = self.db.replay(
                conn, fence.task_owner_ref.agent_id, request_id, "dag.claim", json.loads(_canonical(inputs))
            )
            if replay:
                return self._load_intent(conn, replay["dispatch_id"]), self._load_attempt(conn, replay["attempt_id"])
            intent, attempt = self._claim(conn, fence, root, logical_node_id, run_id)
            self.db.save_receipt(
                conn,
                fence.task_owner_ref.agent_id,
                request_id,
                "dag.claim",
                digest,
                {"dispatch_id": intent.dispatch_id, "attempt_id": attempt.attempt_id},
            )
            return intent, attempt

    def mark_submitting(self, fence, dispatch_id, *, request_id):
        canonical_id(request_id)
        with self.db.connection(write=True) as conn:
            self._fence(conn, fence)
            intent = self._load_intent(conn, dispatch_id)
            attempt = self._load_attempt(conn, intent.attempt_id)
            if intent.root_id != fence.root_id:
                raise MailboxError("owner_conflict")
            if intent.state != "prepared":
                return intent
            if attempt.execution_owner_epoch != fence.owner_epoch:
                raise MailboxError("owner_conflict")
            intent = replace_record(intent, state="submitting")
            self._save_intent(conn, intent)
            self._save_attempt(conn, replace_record(attempt, state="submitting"))
            return replace_record(intent, submit_now=True)

    def _view(self, conn, root):
        attempts = tuple(
            self._load_attempt(conn, r[0])
            for r in conn.execute(
                "SELECT attempt_id FROM strict_attempts WHERE root_id=? ORDER BY rowid", (root.root_id,)
            )
        )
        intents = tuple(
            _intent(json.loads(r[0]))
            for r in conn.execute("SELECT record_json FROM strict_dispatch_outbox WHERE root_id=?", (root.root_id,))
        )
        return RecoveryRecord(
            root,
            attempts,
            tuple(i for i in intents if i.state == "prepared"),
            tuple(a.attempt_id for a in attempts if a.state == "unknown"),
        )

    def recovery_view(self, fence):
        with self.db.connection() as conn:
            return self._view(conn, self._fence(conn, fence))

    def status(self, root_id, *, scope):
        with self.db.connection() as conn:
            root = self._load_root(conn, root_id)
            if scope != {"task_id": root.task_id, "workspace_id": root.workspace_id}:
                raise MailboxError("scope_denied")
            return self._view(conn, root)

    def root_for_run(self, scope, run_id):
        with self.db.connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT root_id FROM strict_roots WHERE run_id=? UNION SELECT root_id FROM strict_attempts WHERE run_id=?",
                (run_id, run_id),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT root_id FROM strict_roots,json_each(strict_roots.history_json) WHERE json_each.key=?",
                    (run_id,),
                ).fetchone()
            if row is None:
                return None
            root = self._load_root(conn, row[0])
            if root.run_id != run_id:
                versions = json.loads(
                    conn.execute("SELECT history_json FROM strict_roots WHERE root_id=?", (root.root_id,)).fetchone()[0]
                )
                if run_id in versions:
                    root = _root(versions[run_id])
                    if (
                        root.graph_hash != hashlib.sha256(_canonical(root.graph)).hexdigest()
                        or root.plan_hash != hashlib.sha256(_canonical(asdict(root.verification_plan))).hexdigest()
                    ):
                        raise MailboxError("storage_conflict")
            if scope != {"task_id": root.task_id, "workspace_id": root.workspace_id}:
                raise MailboxError("scope_denied")
            return root

    def _execution(self, conn, fence, attempt_id, *, check=False, historical=False):
        root = self._fence(conn, fence)
        attempt = self._load_attempt(conn, attempt_id)
        if attempt.root_id != root.root_id or attempt.assignment_epoch != fence.assignment_epoch:
            raise MailboxError("owner_conflict")
        if not historical and attempt.run_id != root.run_id:
            raise MailboxError("attempt_conflict")
        if not check and attempt.execution_owner_epoch != fence.owner_epoch:
            raise MailboxError("owner_conflict")
        return root, attempt

    def _attempt_intent(self, conn, attempt_id):
        row = conn.execute(
            "SELECT record_json FROM strict_dispatch_outbox WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        return _intent(json.loads(row[0]))

    def _candidate(self, raw, intent, attempt, root):
        envelope = decode_envelope(raw)
        data = envelope.payload.data
        if (
            envelope.kind != "task.result"
            or canonical_id(envelope.message_id) != intent.result_message_id
            or envelope.in_reply_to != intent.request_message_id
            or data.get("request_message_id") != intent.request_message_id
            or data.get("strict_root_id") != root.root_id
            or data.get("logical_node_id") != attempt.logical_node_id
            or data.get("attempt_id") != attempt.attempt_id
            or data.get("task_id") != intent.task_id
            or data.get("backend") != "raven-loop"
            or envelope.scope.model_dump() != {"task_id": root.task_id, "workspace_id": root.workspace_id}
            or canonical_id(envelope.sender_identity.agent_id) != intent.sender_ref.agent_id
            or canonical_id(envelope.sender_identity.instance_id) != intent.sender_ref.instance_id
            or canonical_id(envelope.target_identity.agent_id) != intent.recipient_ref.agent_id
            or envelope.target_identity.instance_id not in (None, intent.recipient_ref.instance_id)
        ):
            raise MailboxError("result_conflict")
        return envelope

    def record_started(self, fence, attempt_id, actual_task_id, correlation=None):
        with self.db.connection(write=True) as conn:
            _, attempt = self._execution(conn, fence, attempt_id)
            if actual_task_id != attempt.attempt_id or attempt.state not in ("submitting", "started"):
                raise MailboxError("attempt_conflict")
            attempt = replace_record(attempt, state="started")
            self._save_attempt(conn, attempt)
            intent = self._attempt_intent(conn, attempt_id)
            self._save_intent(conn, replace_record(intent, state="started"))
            return attempt

    def stage_result(self, fence, attempt_id, raw, snapshot):
        with self.db.connection(write=True) as conn:
            root, attempt = self._execution(conn, fence, attempt_id)
            intent = self._attempt_intent(conn, attempt_id)
            envelope = self._candidate(raw, intent, attempt, root)
            if (
                snapshot.revision_commit != root.verification_plan.revision_commit
                or snapshot.revision_tree != root.verification_plan.revision_tree
                or snapshot.baseline_sha256 != root.verification_plan.baseline.baseline_sha256
            ):
                raise MailboxError("snapshot_conflict")
            if attempt.staged_envelope_bytes:
                if attempt.staged_envelope_bytes != raw or attempt.snapshot != snapshot:
                    raise MailboxError("result_conflict")
                return attempt
            if attempt.state not in ("submitting", "started"):
                raise MailboxError("attempt_conflict")
            attempt = replace_record(attempt, staged_envelope_bytes=raw, snapshot=snapshot)
            self._save_attempt(conn, attempt)
            conn.execute(
                "UPDATE strict_attempts SET artifact_refs=? WHERE attempt_id=?",
                (_canonical([snapshot.baseline_sha256, *[a.sha256 for a in envelope.artifacts]]).decode(), attempt_id),
            )
            return attempt

    def _admitted(self, conn, root, attempt, key):
        intent = self._attempt_intent(conn, attempt.attempt_id)
        if key.recipient_agent_id != intent.recipient_ref.agent_id or key.message_id != intent.result_message_id:
            raise MailboxError("result_conflict")
        row = self.store._message(conn, key.recipient_agent_id, key.message_id)
        if row is None:
            raise MailboxError("result_not_admitted")
        if (
            "envelope_bytes" not in row.keys()
            or row["envelope_bytes"] != attempt.staged_envelope_bytes
            or row["digest"] != key.digest
        ):
            raise MailboxError("result_conflict")
        self._candidate(row["envelope_bytes"], intent, attempt, root)
        return row

    def record_result(self, fence, attempt_id, key):
        with self.db.connection(write=True) as conn:
            root, attempt = self._execution(conn, fence, attempt_id, check=True)
            self._admitted(conn, root, attempt, key)
            if attempt.result is not None:
                if attempt.result != key:
                    raise MailboxError("result_conflict")
                return attempt
            if attempt.state not in ("submitting", "started", "review", "unknown"):
                raise MailboxError("attempt_conflict")
            attempt = replace_record(attempt, result=key, state="review", human_reason=None)
            self._save_attempt(conn, attempt)
            report = self._admitted(conn, root, attempt, key)
            if report["phase"] in ("pending", "in_progress"):
                MailboxDelivery(self.store)._terminal(
                    conn,
                    report,
                    int(time.time()),
                    "completed",
                    reason="strict_result_consumed",
                    outcome="succeeded",
                    ref=fence.task_owner_ref,
                )
            return attempt

    def _check_attempt(self, conn, fence, check_run_id):
        root = self._fence(conn, fence)
        for row in conn.execute("SELECT attempt_id FROM strict_attempts WHERE root_id=?", (fence.root_id,)):
            attempt = self._load_attempt(conn, row[0])
            for check in attempt.check_runs:
                if check.check_run_id == check_run_id:
                    if attempt.run_id != root.run_id:
                        raise MailboxError("attempt_conflict")
                    return attempt, check
        raise MailboxError("check_not_found")

    def _save_check(self, conn, attempt, check):
        attempt = replace_record(
            attempt, check_runs=tuple(check if c.check_run_id == check.check_run_id else c for c in attempt.check_runs)
        )
        self._save_attempt(conn, attempt)
        return check

    def prepare_check(self, fence, attempt_id, check_id, *, request_id, now_ms):
        canonical_id(request_id)
        with self.db.connection(write=True) as conn:
            root, attempt = self._execution(conn, fence, attempt_id, check=True)
            if attempt.state != "review" or attempt.result is None:
                raise MailboxError("attempt_conflict")
            self._admitted(conn, root, attempt, attempt.result)
            existing = next((c for c in attempt.check_runs if c.check_id == check_id), None)
            if existing:
                return existing
            policy = next(n for n in root.verification_plan.nodes if n.logical_node_id == attempt.logical_node_id)
            spec = next((c for c in policy.checks if c.check_id == check_id), None)
            if spec is None:
                raise MailboxError("check_not_found")
            check = CheckRunRecord(
                str(uuid4()),
                attempt_id,
                check_id,
                "prepared",
                now_ms + spec.timeout_seconds * 1000,
                attempt.snapshot.fingerprint,
                fence.owner_epoch,
            )
            self._save_attempt(conn, replace_record(attempt, check_runs=(*attempt.check_runs, check)))
            return check

    def mark_check_submitting(self, fence, check_run_id, *, request_id):
        canonical_id(request_id)
        with self.db.connection(write=True) as conn:
            attempt, check = self._check_attempt(conn, fence, check_run_id)
            if check.state != "prepared":
                return check
            check = replace_record(check, state="submitting", execution_owner_epoch=fence.owner_epoch)
            self._save_check(conn, attempt, check)
            return replace_record(check, submit_now=True)

    def record_check_started(self, fence, check_run_id, process_identity):
        with self.db.connection(write=True) as conn:
            attempt, check = self._check_attempt(conn, fence, check_run_id)
            if check.execution_owner_epoch != fence.owner_epoch or check.state != "submitting":
                raise MailboxError("owner_conflict")
            return self._save_check(
                conn, attempt, replace_record(check, state="running", process_identity=process_identity)
            )

    def record_check_result(self, fence, check_run_id, observation):
        with self.db.connection(write=True) as conn:
            attempt, check = self._check_attempt(conn, fence, check_run_id)
            if check.execution_owner_epoch != fence.owner_epoch:
                raise MailboxError("owner_conflict")
            if check.observation is not None:
                if check.observation != observation:
                    raise MailboxError("check_conflict")
                return check
            if check.state not in ("submitting", "running"):
                raise MailboxError("check_conflict")
            known = (
                observation.error is None
                and type(observation.exit_code) is int
                and observation.before_fingerprint == check.snapshot_fingerprint == observation.after_fingerprint
                and observation.ended_at_ms <= check.deadline_ms
            )
            check = replace_record(check, observation=observation, state="result" if known else "unknown")
            self._save_check(conn, attempt, check)
            conn.execute(
                "UPDATE strict_attempts SET artifact_refs=? WHERE attempt_id=?",
                (
                    _canonical(
                        sorted(
                            set(
                                json.loads(
                                    conn.execute(
                                        "SELECT artifact_refs FROM strict_attempts WHERE attempt_id=?",
                                        (attempt.attempt_id,),
                                    ).fetchone()[0]
                                )
                            )
                            | {observation.stdout_sha256, observation.stderr_sha256}
                        )
                    ).decode(),
                    attempt.attempt_id,
                ),
            )
            return check

    def _refresh_state(self, conn, root):
        nodes = [
            json.loads(r[0])
            for r in conn.execute("SELECT record_json FROM strict_nodes WHERE root_id=?", (root.root_id,))
        ]
        current = [self._load_attempt(conn, n["attempt_id"]) for n in nodes if n["attempt_id"]]
        if all(n["accepted_snapshot"] is not None for n in nodes):
            state = "accepted"
        elif any(a.state == "failed" for a in current):
            state = "failed"
        elif any(a.state == "unknown" or a.human_reason for a in current):
            state = "human"
        else:
            state = "running"
        self._save_root(conn, replace_record(root, state=state))

    def _transition(self, conn, root, fence, attempt, request_id, subjective=False):
        policy = next(n for n in root.verification_plan.nodes if n.logical_node_id == attempt.logical_node_id)
        if attempt.result is None or attempt.snapshot is None:
            raise MailboxError("result_not_admitted")
        self._admitted(conn, root, attempt, attempt.result)
        if attempt.state in ("accepted", "failed"):
            return TransitionResult(attempt, (), "replay")
        checks = {c.check_id: c for c in attempt.check_runs}
        reason = (
            attempt.human_reason
            if attempt.human_reason not in (None, "subjective_review", "repair_budget_exhausted")
            else None
        )
        failure = False
        nonrepairable = False
        for spec in policy.checks:
            check = checks.get(spec.check_id)
            if check is None or check.state != "result" or check.observation is None:
                reason = "verification_unknown"
                break
            code = check.observation.exit_code
            if code:
                if code in spec.repairable_exit_codes:
                    failure = True
                elif code in spec.nonrepairable_exit_codes:
                    nonrepairable = True
                else:
                    nonrepairable = True
        intents = []
        node = json.loads(
            conn.execute(
                "SELECT record_json FROM strict_nodes WHERE root_id=? AND logical_node_id=?",
                (root.root_id, attempt.logical_node_id),
            ).fetchone()[0]
        )
        if reason:
            attempt = replace_record(attempt, human_reason=reason)
            decision = "human"
        elif nonrepairable:
            attempt = replace_record(attempt, state="failed")
            decision = "failed"
        elif failure:
            if node["repair_count"] >= root.max_auto_repairs:
                attempt = replace_record(attempt, human_reason="repair_budget_exhausted")
                decision, reason = "human", "repair_budget_exhausted"
            else:
                node["repair_count"] += 1
                node["attempt_id"] = None
                attempt = replace_record(attempt, state="failed", repair_count=node["repair_count"])
                conn.execute(
                    "UPDATE strict_nodes SET record_json=? WHERE root_id=? AND logical_node_id=?",
                    (_canonical(node).decode(), root.root_id, attempt.logical_node_id),
                )
                intents.append(self._claim(conn, fence, root, attempt.logical_node_id, attempt.run_id, "repair")[0])
                decision = "repair_scheduled"
        elif policy.subjective_review and not subjective:
            reason = "subjective_review"
            attempt = replace_record(attempt, human_reason=reason)
            decision = "human"
        else:
            attempt = replace_record(attempt, state="accepted", human_reason=None)
            node["accepted_snapshot"] = asdict(attempt.snapshot)
            conn.execute(
                "UPDATE strict_nodes SET record_json=? WHERE root_id=? AND logical_node_id=?",
                (_canonical(node).decode(), root.root_id, attempt.logical_node_id),
            )
            for child in root.verification_plan.nodes:
                if attempt.logical_node_id not in child.depends_on:
                    continue
                saved = json.loads(
                    conn.execute(
                        "SELECT record_json FROM strict_nodes WHERE root_id=? AND logical_node_id=?",
                        (root.root_id, child.logical_node_id),
                    ).fetchone()[0]
                )
                if saved["attempt_id"]:
                    continue
                if all(
                    json.loads(
                        conn.execute(
                            "SELECT record_json FROM strict_nodes WHERE root_id=? AND logical_node_id=?",
                            (root.root_id, d),
                        ).fetchone()[0]
                    )["accepted_snapshot"]
                    is not None
                    for d in child.depends_on
                ):
                    intents.append(self._claim(conn, fence, root, child.logical_node_id, root.run_id)[0])
            decision = "accepted"
        self._save_attempt(conn, attempt)
        if decision != "human":
            report = self._admitted(conn, root, attempt, attempt.result)
            if report["phase"] in ("pending", "in_progress"):
                MailboxDelivery(self.store)._terminal(
                    conn,
                    report,
                    int(time.time()),
                    "completed",
                    reason="strict_result_consumed",
                    outcome="succeeded",
                    ref=fence.task_owner_ref,
                )
            self._save_intent(conn, replace_record(self._attempt_intent(conn, attempt.attempt_id), state="settled"))
        self._refresh_state(conn, root)
        return TransitionResult(attempt, tuple(intents), decision, reason)

    def decide(self, fence, attempt_id, *, request_id):
        inputs = {"root_id": fence.root_id, "attempt_id": attempt_id, "owner_epoch": fence.owner_epoch}
        with self.db.connection(write=True) as conn:
            root, attempt = self._execution(conn, fence, attempt_id, check=True, historical=True)
            replay, digest = self.db.replay(conn, fence.task_owner_ref.agent_id, request_id, "dag.decide", inputs)
            if replay:
                return TransitionResult(
                    self._load_attempt(conn, replay["attempt_id"]),
                    tuple(self._load_intent(conn, x) for x in replay["dispatch_ids"]),
                    replay["decision"],
                    replay["reason"],
                )
            if attempt.run_id != root.run_id:
                raise MailboxError("attempt_conflict")
            result = self._transition(conn, root, fence, attempt, request_id)
            self.db.save_receipt(
                conn,
                fence.task_owner_ref.agent_id,
                request_id,
                "dag.decide",
                digest,
                {
                    "attempt_id": attempt_id,
                    "dispatch_ids": [i.dispatch_id for i in result.intents],
                    "decision": result.decision,
                    "reason": result.reason,
                },
            )
            return result

    def record_unknown(self, fence, attempt_id=None, *, check_run_id=None, reason, request_id):
        canonical_id(request_id)
        with self.db.connection(write=True) as conn:
            self._fence(conn, fence)
            if check_run_id:
                attempt, check = self._check_attempt(conn, fence, check_run_id)
                if check.observation is not None:
                    return check
                return self._save_check(conn, attempt, replace_record(check, state="unknown"))
            root, attempt = self._execution(conn, fence, attempt_id, check=True)
            if attempt.state in ("accepted", "failed"):
                return attempt
            if attempt.result is not None:
                attempt = replace_record(attempt, human_reason=reason)
                self._save_attempt(conn, attempt)
                self._refresh_state(conn, root)
                return attempt
            attempt = replace_record(attempt, state="unknown", human_reason=reason)
            self._save_attempt(conn, attempt)
            self._save_intent(conn, replace_record(self._attempt_intent(conn, attempt_id), state="unknown"))
            self._refresh_state(conn, root)
            return attempt

    def resolve(self, fence, attempt_id, *, expected_owner_epoch, resolution, request_id, wire_resolution=None):
        resolution = json.loads(_canonical(resolution))
        if expected_owner_epoch != fence.owner_epoch:
            raise MailboxError("owner_conflict")
        inputs = {
            "root_id": fence.root_id,
            "attempt_id": attempt_id,
            "owner_epoch": expected_owner_epoch,
            "resolution": wire_resolution if wire_resolution is not None else resolution,
        }
        with self.db.connection(write=True) as conn:
            root, attempt = self._execution(conn, fence, attempt_id, check=True, historical=True)
            replay, digest = self.db.replay(conn, fence.task_owner_ref.agent_id, request_id, "dag.resolve", inputs)
            if replay:
                return TransitionResult(
                    self._load_attempt(conn, attempt_id),
                    tuple(self._load_intent(conn, x) for x in replay["dispatch_ids"]),
                    replay["decision"],
                    replay["reason"],
                )
            if attempt.run_id != root.run_id:
                raise MailboxError("resolution_conflict")
            action = resolution.get("action")
            if attempt.state == "accepted" or (attempt.state == "failed" and action != "replan"):
                raise MailboxError("resolution_conflict")
            if action == "subjective_approve":
                policy = next(n for n in root.verification_plan.nodes if n.logical_node_id == attempt.logical_node_id)
                if (
                    not policy.subjective_review
                    or attempt.snapshot is None
                    or resolution.get("snapshot_fingerprint") != attempt.snapshot.fingerprint
                ):
                    raise MailboxError("resolution_conflict")
                checks = {c.check_id: c for c in attempt.check_runs}
                if any(
                    c.check_id not in checks
                    or checks[c.check_id].state != "result"
                    or checks[c.check_id].observation.exit_code != 0
                    for c in policy.checks
                ):
                    raise MailboxError("resolution_conflict")
                result = self._transition(conn, root, fence, attempt, request_id, subjective=True)
                if result.decision == "human":
                    raise MailboxError("resolution_conflict")
            elif action == "reconcile_execution":
                key = AdmittedResultKey(**resolution["result_key"])
                self._admitted(conn, root, attempt, key)
                if attempt.state not in ("unknown", "review"):
                    raise MailboxError("resolution_conflict")
                attempt = replace_record(
                    attempt, state="review", result=key, decision_owner_epoch=fence.owner_epoch, human_reason=None
                )
                self._save_attempt(conn, attempt)
                result = TransitionResult(attempt, (), "replay")
            elif action == "reconcile_verification":
                check = next((c for c in attempt.check_runs if c.check_run_id == resolution.get("check_run_id")), None)
                if check is None or check.observation is None:
                    raise MailboxError("resolution_conflict")
                result = self._transition(conn, root, fence, attempt, request_id)
            elif action == "replan":
                if attempt.state not in ("unknown", "failed", "review"):
                    raise MailboxError("resolution_conflict")
                plan = _plan(resolution["successor_verification_plan"])
                _validate_plan(plan)
                graph = resolution["successor_graph"]
                mapping = resolution["logical_node_mapping"]
                old_nodes = {
                    r["logical_node_id"]: json.loads(r["record_json"])
                    for r in conn.execute(
                        "SELECT logical_node_id,record_json FROM strict_nodes WHERE root_id=?", (root.root_id,)
                    )
                }
                new_ids = {n.logical_node_id for n in plan.nodes}
                if (
                    set(mapping) != set(old_nodes)
                    or len(set(mapping.values())) != len(mapping)
                    or not set(mapping.values()) <= new_ids
                ):
                    raise MailboxError("resolution_conflict")
                budgets = {mapping[key]: value["repair_count"] for key, value in old_nodes.items()}
                saved = conn.execute(
                    "SELECT history_json,artifact_refs FROM strict_roots WHERE root_id=?", (root.root_id,)
                ).fetchone()
                versions = json.loads(saved[0])
                versions[root.run_id] = json.loads(_json(root))
                pins = set(json.loads(saved[1])) | {plan.baseline.baseline_sha256}
                root = replace_record(
                    root,
                    run_id=str(uuid4()),
                    graph=graph,
                    verification_plan=plan,
                    graph_hash=hashlib.sha256(_canonical(graph)).hexdigest(),
                    plan_hash=hashlib.sha256(_canonical(asdict(plan))).hexdigest(),
                    state="running",
                )
                conn.execute(
                    "UPDATE strict_roots SET run_id=?,record_json=?,artifact_refs=?,history_json=? WHERE root_id=?",
                    (
                        root.run_id,
                        _json(root),
                        _canonical(sorted(pins)).decode(),
                        _canonical(versions).decode(),
                        root.root_id,
                    ),
                )
                conn.execute("DELETE FROM strict_nodes WHERE root_id=?", (root.root_id,))
                for policy in plan.nodes:
                    conn.execute(
                        "INSERT INTO strict_nodes VALUES (?,?,?)",
                        (
                            root.root_id,
                            policy.logical_node_id,
                            _canonical(
                                {
                                    "repair_count": budgets.get(policy.logical_node_id, 0),
                                    "attempt_id": None,
                                    "accepted_snapshot": None,
                                }
                            ).decode(),
                        ),
                    )
                for row in conn.execute(
                    "SELECT attempt_id FROM strict_attempts WHERE root_id=?", (root.root_id,)
                ).fetchall():
                    old = self._load_attempt(conn, row[0])
                    if old.state not in ("accepted", "failed"):
                        self._save_attempt(conn, replace_record(old, state="failed", human_reason="explicit_replan"))
                        self._save_intent(
                            conn, replace_record(self._attempt_intent(conn, old.attempt_id), state="settled")
                        )
                intents = tuple(
                    self._claim(conn, fence, root, policy.logical_node_id, root.run_id, "replan")[0]
                    for policy in plan.nodes
                    if not policy.depends_on
                )
                attempt = self._load_attempt(conn, attempt_id)
                result = TransitionResult(attempt, intents, "replay", "explicit_replan")
            elif action == "abandon":
                attempt = replace_record(attempt, state="failed", human_reason=resolution.get("decision_note"))
                self._save_attempt(conn, attempt)
                self._save_intent(conn, replace_record(self._attempt_intent(conn, attempt_id), state="settled"))
                result = TransitionResult(attempt, (), "failed", attempt.human_reason)
            else:
                raise MailboxError("resolution_conflict")
            self._refresh_state(conn, root)
            self.db.save_receipt(
                conn,
                fence.task_owner_ref.agent_id,
                request_id,
                "dag.resolve",
                digest,
                {
                    "dispatch_ids": [i.dispatch_id for i in result.intents],
                    "decision": result.decision,
                    "reason": result.reason,
                    "resolution": resolution,
                },
            )
            return result

    def replay_create(self, *, task_owner_ref, scope, request_id, wire_inputs):
        with self.db.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
                return None
            self._authority(conn, task_owner_ref, scope)
            replay, _ = self.db.replay(conn, task_owner_ref.agent_id, request_id, "dag.create", wire_inputs)
            return _root(replay["record"]) if replay else None

    def replay_resolve(self, fence, attempt_id, *, expected_owner_epoch, request_id, wire_resolution):
        if expected_owner_epoch != fence.owner_epoch:
            raise MailboxError("owner_conflict")
        inputs = {
            "root_id": fence.root_id,
            "attempt_id": attempt_id,
            "owner_epoch": expected_owner_epoch,
            "resolution": wire_resolution,
        }
        with self.db.connection() as conn:
            self._execution(conn, fence, attempt_id, check=True, historical=True)
            replay, _ = self.db.replay(conn, fence.task_owner_ref.agent_id, request_id, "dag.resolve", inputs)
            if replay is None:
                return None
            return TransitionResult(
                self._load_attempt(conn, attempt_id),
                tuple(self._load_intent(conn, x) for x in replay["dispatch_ids"]),
                replay["decision"],
                replay["reason"],
            )
