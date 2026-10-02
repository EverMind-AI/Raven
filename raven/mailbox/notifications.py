"""Durable mailbox pointers and one-shot submission evidence, separate from claims."""

import hashlib
import json
import time

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import canonical_bytes
from raven.mailbox.db import canonical_id
from raven.mailbox.delivery import MailboxDelivery

_TRANSITIONS = {
    "prepared": {"blocked", "submitting"},
    "blocked": {"blocked", "submitting"},
    "submitting": {"uncertain", "input_accepted", "turn_started"},
    "uncertain": {"uncertain", "input_accepted", "turn_started"},
    "input_accepted": {"input_accepted", "turn_started"},
    "turn_started": {"turn_started"},
}


class MailboxNotifications:
    """Persist an exact batch before I/O; uncertain attempts never become eligible again."""

    def __init__(self, store):
        self.store = store
        self.db = store.db

    @staticmethod
    def _now(now):
        return int(time.time()) if now is None else now

    @staticmethod
    def _public(row):
        return {**dict(row), "message_ids": json.loads(row["message_ids"])}

    def _row(self, conn, request_id):
        row = conn.execute("SELECT * FROM notifications WHERE request_id=?", (canonical_id(request_id),)).fetchone()
        if row is None:
            raise MailboxError("notification_not_found")
        return row

    def prepare(self, binding, *, request_id, message_ids, now=None):
        """Bind a host-validated receiver and exact message IDs without claiming work."""
        request_id = canonical_id(request_id)
        if type(message_ids) is not list or not 1 <= len(message_ids) <= 16:
            raise MailboxError("invalid_notification")
        ids = [canonical_id(value) for value in message_ids]
        if len(set(ids)) != len(ids):
            raise MailboxError("invalid_notification")
        inputs = {
            "binding_id": binding.binding_id,
            "instance": binding.ref.model_dump(),
            "scope": binding.scope,
            "message_ids": ids,
            "terminal_incarnation": binding.terminal_incarnation,
        }
        input_hash = hashlib.sha256(canonical_bytes(inputs)).hexdigest()
        now = self._now(now)
        with self.db.connection(write=True) as conn:
            self.store._current(conn, binding.ref)
            owner = conn.execute("SELECT * FROM receiver_bindings WHERE binding_id=?", (binding.binding_id,)).fetchone()
            if (
                owner is None
                or owner["revoked_at"] is not None
                or (owner["agent_id"], owner["instance_id"], owner["generation"], owner["terminal_incarnation"])
                != (binding.ref.agent_id, binding.ref.instance_id, binding.ref.generation, binding.terminal_incarnation)
            ):
                raise MailboxError("receiver_fenced")
            if (owner["task_id"], owner["workspace_id"]) != (binding.scope["task_id"], binding.scope["workspace_id"]):
                raise MailboxError("scope_denied")
            previous = conn.execute("SELECT * FROM notifications WHERE request_id=?", (request_id,)).fetchone()
            if previous is not None:
                if previous["input_hash"] != input_hash:
                    raise MailboxError("request_conflict")
                return self._public(previous)
            for message_id in ids:
                row = self.store._message(conn, binding.ref.agent_id, message_id)
                if row is None or "envelope_bytes" not in row.keys():
                    raise MailboxError("message_not_found")
                envelope = MailboxDelivery._envelope(row)
                if envelope.scope.model_dump() != binding.scope:
                    raise MailboxError("scope_denied")
                if row["phase"] != "pending" or row["expires_at"] <= now or row["not_before"] > now:
                    raise MailboxError("message_not_eligible")
                if row["target_instance"] is not None and row["target_instance"] != binding.ref.instance_id:
                    raise MailboxError("instance_fenced")
            conn.execute(
                "INSERT INTO notifications (request_id,binding_id,message_ids,input_hash,stage,"
                "terminal_incarnation,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    binding.binding_id,
                    canonical_bytes(ids).decode(),
                    input_hash,
                    "prepared",
                    binding.terminal_incarnation,
                    now,
                    now,
                ),
            )
            return self._public(self._row(conn, request_id))

    def begin(self, request_id, *, turn_id=None, now=None):
        """Commit the submission intent once, before a terminal write or scheduler call."""
        turn_id = canonical_id(turn_id) if turn_id is not None else None
        with self.db.connection(write=True) as conn:
            self._row(conn, request_id)
            return bool(
                conn.execute(
                    "UPDATE notifications SET stage='submitting',detail=NULL,turn_id=coalesce(?,turn_id),updated_at=? "
                    "WHERE request_id=? AND stage IN ('prepared','blocked')",
                    (turn_id, self._now(now), canonical_id(request_id)),
                ).rowcount
            )

    def record(self, request_id, stage, *, bytes_written=0, turn_id=None, detail=None, now=None):
        """Apply trusted correlated input/turn evidence, never a message processing result."""
        if stage not in _TRANSITIONS or type(bytes_written) is not int or bytes_written < 0:
            raise MailboxError("invalid_notification")
        with self.db.connection(write=True) as conn:
            row = self._row(conn, request_id)
            if stage not in _TRANSITIONS[row["stage"]]:
                raise MailboxError("notification_transition")
            conn.execute(
                "UPDATE notifications SET stage=?,bytes_written=max(bytes_written,?),"
                "turn_id=coalesce(?,turn_id),detail=?,updated_at=? WHERE request_id=?",
                (stage, bytes_written, turn_id, detail, self._now(now), canonical_id(request_id)),
            )
            return self._public(self._row(conn, request_id))

    def status(self, request_id):
        with self.db.connection() as conn:
            return self._public(self._row(conn, request_id))

    def recover(self, *, now=None):
        """After host restart, retain interrupted submissions as uncertain without resending."""
        with self.db.connection(write=True) as conn:
            return conn.execute(
                "UPDATE notifications SET stage='uncertain',detail='host_restarted',updated_at=? "
                "WHERE stage='submitting'",
                (self._now(now),),
            ).rowcount
