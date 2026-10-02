"""Verified artifact reads and explicit host-committed task ownership transfers."""

import hashlib
import time

from raven.contracts.mailbox import MailboxError, MailboxInstanceRef
from raven.mailbox import blobs
from raven.mailbox.codec import canonical_bytes
from raven.mailbox.db import canonical_id
from raven.mailbox.delivery import MailboxDelivery


class MailboxHandoff:
    """Keep receiver proposals separate from trusted host ownership operations."""

    def __init__(self, store, *, upgrade=True):
        self.store = store
        self.db = store.db
        if upgrade:
            self.db.upgrade_receivers()

    def _message(self, conn, recipient, message_id, kind, scope, now):
        row = self.store._message(conn, recipient, message_id)
        if row is None:
            raise MailboxError("message_not_found")
        if "envelope_bytes" not in row.keys():
            raise MailboxError("terminal_compacted")
        if row["expires_at"] <= now:
            raise MailboxError("message_expired", message_id=row["message_id"])
        envelope = MailboxDelivery._envelope(row)
        if envelope.kind != kind:
            raise MailboxError("handoff_conflict")
        if scope is not None and envelope.scope.model_dump() != scope:
            raise MailboxError("scope_denied")
        return envelope

    def _offer(self, conn, ref, message_id, scope, now):
        self.store._current(conn, ref)
        offer = self._message(conn, ref.agent_id, message_id, "handoff.offer", scope, now)
        if offer.target_identity.instance_id is not None and canonical_id(
            offer.target_identity.instance_id
        ) != canonical_id(ref.instance_id):
            raise MailboxError("instance_fenced")
        if offer.payload.data["task_ref"] != offer.scope.task_id:
            raise MailboxError("handoff_conflict")
        if canonical_id(offer.payload.data["authority"]["owner"]) != canonical_id(offer.sender_identity.agent_id):
            raise MailboxError("handoff_conflict")
        return offer

    def read_artifact(self, ref, offer_message_id, artifact_hash, *, scope, now=None):
        """Read and hash all declared bytes before recording current-instance coverage."""
        now = int(time.time()) if now is None else now
        with blobs.locked(self.db):
            with self.db.connection() as conn:
                offer = self._offer(conn, ref, offer_message_id, scope, now)
            artifact = next((item for item in offer.artifacts if item.sha256 == artifact_hash), None)
            if artifact is None:
                raise MailboxError("evidence_unavailable")
            try:
                with blobs._open_regular(
                    self.store.root / "blobs" / "sha256" / artifact_hash, "storage_conflict"
                ) as source:
                    content = source.read(artifact.size + 1)
            except OSError:
                raise MailboxError("storage_conflict") from None
            if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact_hash:
                raise MailboxError("storage_conflict")
            with self.db.connection(write=True) as conn:
                self._offer(conn, ref, offer_message_id, scope, now)
                conn.execute(
                    "INSERT OR REPLACE INTO handoff_reads VALUES (?,?,?,?,?,?,?)",
                    (
                        canonical_id(offer.message_id),
                        canonical_id(ref.agent_id),
                        canonical_id(ref.instance_id),
                        ref.generation,
                        artifact_hash,
                        len(content),
                        int(time.time()) if now is None else now,
                    ),
                )
            return content

    def _pair(self, conn, ref, offer_message_id, accept_message_id, scope, now):
        offer = self._offer(conn, ref, offer_message_id, scope, now)
        accept = self._message(conn, offer.sender_identity.agent_id, accept_message_id, "handoff.accept", scope, now)
        data = accept.payload.data
        if (
            canonical_id(accept.sender_identity.agent_id) != canonical_id(ref.agent_id)
            or canonical_id(data["offer_message_id"]) != canonical_id(offer.message_id)
            or canonical_id(accept.sender_identity.instance_id) != canonical_id(ref.instance_id)
            or canonical_id(accept.target_identity.agent_id) != canonical_id(offer.sender_identity.agent_id)
            or (
                accept.target_identity.instance_id is not None
                and canonical_id(accept.target_identity.instance_id) != canonical_id(offer.sender_identity.instance_id)
            )
            or canonical_id(data["handoff_id"]) != canonical_id(offer.payload.data["handoff_id"])
            or accept.in_reply_to is None
            or canonical_id(accept.in_reply_to) != canonical_id(offer.message_id)
            or data["accepted_scope"] != offer.scope.model_dump()
        ):
            raise MailboxError("handoff_conflict")
        required = set(offer.payload.data["required_artifacts"])
        descriptors = {item.sha256: item for item in offer.artifacts}
        coverage = data["read_coverage"]
        hashes = data["read_artifact_hashes"]
        if (
            len(hashes) != len(set(hashes))
            or set(coverage) != set(hashes)
            or not required <= set(hashes)
            or not set(hashes) <= descriptors.keys()
        ):
            raise MailboxError("handoff_unread")
        for sha in hashes:
            expected = {"bytes_read": descriptors[sha].size}
            if coverage[sha] != expected or type(coverage[sha].get("bytes_read")) is not int:
                raise MailboxError("handoff_unread")
            read = conn.execute(
                "SELECT bytes_read FROM handoff_reads WHERE offer_message_id=? AND agent_id=? AND instance_id=? "
                "AND generation=? AND artifact_hash=?",
                (
                    canonical_id(offer.message_id),
                    canonical_id(ref.agent_id),
                    canonical_id(ref.instance_id),
                    ref.generation,
                    sha,
                ),
            ).fetchone()
            if read is None or read[0] != descriptors[sha].size:
                raise MailboxError("handoff_unread")
        return offer, accept

    def propose(self, ref, accept_message_id, *, offer_message_id, scope, now=None):
        """Validate a durable accept message without changing the task owner."""
        now = int(time.time()) if now is None else now
        with blobs.locked(self.db):
            with self.db.connection() as conn:
                offer, accept = self._pair(conn, ref, offer_message_id, accept_message_id, scope, now)
            blobs.verify_artifacts(self.store.root, offer)
        return {
            "status": "PROPOSED",
            "handoff_id": offer.payload.data["handoff_id"],
            "offer_message_id": offer.message_id,
            "accept_message_id": accept.message_id,
            "accepted_scope": accept.payload.data["accepted_scope"],
            "read_coverage": accept.payload.data["read_coverage"],
            "unresolved_items": accept.payload.data["unresolved_items"],
        }

    def task_status(self, *, task_id, workspace_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_authority WHERE task_id=? AND workspace_id=?", (task_id, workspace_id)
            ).fetchone()
            if row is None:
                raise MailboxError("task_not_found")
            return dict(row)

    def create_task(self, *, task_id, workspace_id, owner_agent_id, request_id, now=None):
        """Initialize task authority through the separately authenticated host surface."""
        scope = dict(task_id=task_id, workspace_id=workspace_id)
        if any(type(v) is not str or not v.isascii() or not 1 <= len(v) <= 128 for v in scope.values()):
            raise MailboxError("invalid_scope")
        owner = canonical_id(owner_agent_id)
        inputs = dict(scope, owner_agent_id=owner)
        with self.db.connection(write=True) as conn:
            self.store._card_row(conn, owner)
            replay, input_hash = self.db.replay(conn, owner, request_id, "handoff.create_task", inputs)
            if replay is not None:
                return replay
            if conn.execute(
                "SELECT 1 FROM task_authority WHERE task_id=? AND workspace_id=?", (task_id, workspace_id)
            ).fetchone():
                raise MailboxError("assignment_conflict")
            conn.execute(
                "INSERT INTO task_authority VALUES (?,?,?,1,NULL,NULL,NULL,?)",
                (task_id, workspace_id, owner, int(time.time()) if now is None else now),
            )
            result = dict(
                conn.execute(
                    "SELECT * FROM task_authority WHERE task_id=? AND workspace_id=?", (task_id, workspace_id)
                ).fetchone()
            )
            self.db.save_receipt(conn, owner, request_id, "handoff.create_task", input_hash, result)
            return result

    def _binding(self, conn, binding_id, ref, scope):
        row = conn.execute("SELECT * FROM receiver_bindings WHERE binding_id=?", (canonical_id(binding_id),)).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or (row["agent_id"], row["instance_id"], row["generation"], row["task_id"], row["workspace_id"])
            != (
                canonical_id(ref.agent_id),
                canonical_id(ref.instance_id),
                ref.generation,
                scope["task_id"],
                scope["workspace_id"],
            )
        ):
            raise MailboxError("receiver_fenced")

    def commit(
        self,
        *,
        binding_id,
        revalidate_receiver,
        receiver_ref: MailboxInstanceRef,
        scope,
        offer_message_id,
        accept_message_id,
        expected_owner_agent_id,
        expected_assignment_epoch,
        request_id,
        now=None,
    ):
        """Compare-and-swap ownership only on the separately authenticated host path."""
        ref = receiver_ref
        now = int(time.time()) if now is None else now
        owner = canonical_id(expected_owner_agent_id)
        if type(expected_assignment_epoch) is not int or expected_assignment_epoch < 1:
            raise MailboxError("invalid_epoch")
        inputs = dict(
            binding_id=canonical_id(binding_id),
            receiver_ref=ref.model_dump(),
            scope=scope,
            offer_message_id=canonical_id(offer_message_id),
            accept_message_id=canonical_id(accept_message_id),
            expected_owner_agent_id=owner,
            expected_assignment_epoch=expected_assignment_epoch,
        )
        canonical_bytes(inputs)
        with blobs.locked(self.db):
            with self.db.connection() as conn:
                self.store._current(conn, ref)
                self._binding(conn, binding_id, ref, scope)
                replay, input_hash = self.db.replay(conn, owner, request_id, "handoff.commit", inputs)
                if replay is not None:
                    return replay
                offer, accept = self._pair(conn, ref, offer_message_id, accept_message_id, scope, now)
                if (
                    canonical_id(offer.message_id) != inputs["offer_message_id"]
                    or canonical_id(offer.sender_identity.agent_id) != owner
                    or not offer.payload.data["authority"]["transfer_required"]
                ):
                    raise MailboxError("handoff_conflict")
            blobs.verify_artifacts(self.store.root, offer)
            revalidate_receiver()
            with self.db.connection(write=True) as conn:
                self.store._current(conn, ref)
                self._binding(conn, binding_id, ref, scope)
                replay, input_hash = self.db.replay(conn, owner, request_id, "handoff.commit", inputs)
                if replay is not None:
                    return replay
                self._pair(conn, ref, offer_message_id, accept_message_id, scope, now)
                updated = conn.execute(
                    "UPDATE task_authority SET owner_agent_id=?,assignment_epoch=assignment_epoch+1,"
                    "confirmed_handoff_id=?,offer_message_id=?,accept_message_id=?,updated_at=? "
                    "WHERE task_id=? AND workspace_id=? AND owner_agent_id=? AND assignment_epoch=?",
                    (
                        canonical_id(ref.agent_id),
                        canonical_id(offer.payload.data["handoff_id"]),
                        canonical_id(offer.message_id),
                        canonical_id(accept.message_id),
                        int(time.time()) if now is None else now,
                        scope["task_id"],
                        scope["workspace_id"],
                        owner,
                        expected_assignment_epoch,
                    ),
                )
                if updated.rowcount != 1:
                    raise MailboxError("assignment_conflict")
                result = dict(
                    conn.execute(
                        "SELECT * FROM task_authority WHERE task_id=? AND workspace_id=?",
                        (scope["task_id"], scope["workspace_id"]),
                    ).fetchone()
                )
                self.db.save_receipt(conn, owner, request_id, "handoff.commit", input_hash, result)
                return result
