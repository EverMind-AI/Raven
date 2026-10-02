"""Trusted-local Cards and offline message admission over one SQLite authority."""

import json
import re
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection
from uuid import uuid4

from pydantic import ValidationError

from raven.contracts.mailbox import MailboxCard, MailboxError, MailboxInstanceRef
from raven.home import raven_home
from raven.mailbox import blobs
from raven.mailbox.codec import canonical_bytes, decode_envelope
from raven.mailbox.db import MESSAGE_BUDGET, RECEIPT_LIMIT, Database, canonical_id

KINDS = {"task.request", "task.result", "handoff.offer", "handoff.accept", "receipt", "event"}


class MailboxStore:
    """Manage local identity and immutable delivery records without a runtime service."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        authority_id: str | None = None,
        tenant_id: str | None = None,
        busy_timeout_ms: int = 2000,
    ):
        if tenant_id is not None and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", tenant_id):
            raise MailboxError("invalid_identity")
        self.db = Database(
            root if root is not None else raven_home() / "a2a",
            authority_id=authority_id,
            tenant_id=tenant_id,
            busy_timeout_ms=busy_timeout_ms,
        )
        self.root = self.db.root

    def _key(self, agent_id: str) -> tuple:
        return self.db.authority_id, self.db.tenant_id, canonical_id(agent_id)

    def _card_row(self, conn, agent_id: str):
        row = conn.execute(
            "SELECT * FROM cards WHERE authority_id=? AND tenant_id=? AND agent_id=?", self._key(agent_id)
        ).fetchone()
        if row is None:
            raise MailboxError("unknown_agent")
        return row

    def _identity(self, identity) -> None:
        if canonical_id(identity.authority_id) != self.db.authority_id or identity.tenant_id != self.db.tenant_id:
            raise MailboxError("root_identity_mismatch")

    def _current(self, conn, ref: MailboxInstanceRef):
        self._identity(ref)
        row = self._card_row(conn, ref.agent_id)
        if row["instance_id"] != canonical_id(ref.instance_id) or row["generation"] != ref.generation:
            raise MailboxError("instance_fenced")
        return row

    def init(
        self,
        *,
        agent_id: str,
        instance_id: str | None = None,
        request_id: str,
        allowed_scopes: Collection[dict],
        allowed_kinds: Collection[str],
        display_name: str = "",
        brand: str = "",
        capabilities: Collection[dict] = (),
        max_in_flight: int = 1,
    ) -> MailboxInstanceRef:
        """Enroll one explicit stable identity; exact request replay recovers its reference."""
        agent_id, request_id = canonical_id(agent_id), canonical_id(request_id)
        instance_id = canonical_id(instance_id) if instance_id is not None else None
        scopes = list(allowed_scopes)
        kinds = sorted(set(allowed_kinds))
        if (
            not kinds
            or not set(kinds) <= KINDS
            or not scopes
            or any(
                type(scope) is not dict
                or set(scope) != {"task_id", "workspace_id"}
                or any(
                    type(value) is not str or not value.isascii() or not 1 <= len(value) <= 128
                    for value in scope.values()
                )
                for scope in scopes
            )
        ):
            raise MailboxError("invalid_policy")
        if type(max_in_flight) is not int or max_in_flight < 1:
            raise MailboxError("invalid_policy")
        inputs = dict(
            agent_id=agent_id,
            instance_id=instance_id,
            allowed_scopes=scopes,
            allowed_kinds=kinds,
            display_name=display_name,
            brand=brand,
            capabilities=list(capabilities),
            max_in_flight=max_in_flight,
        )
        canonical_bytes(inputs)
        self.db.initialize()
        blobs.initialize(self.db)
        with self.db.connection(write=True) as conn:
            replay, input_hash = self.db.replay(conn, agent_id, request_id, "init", inputs)
            if replay is not None:
                return MailboxInstanceRef.model_validate(replay)
            instance_id = instance_id or str(uuid4())
            try:
                card = MailboxCard(
                    card_version="1.0",
                    authority_id=self.db.authority_id,
                    tenant_id=self.db.tenant_id,
                    agent_id=agent_id,
                    display_name=display_name,
                    brand=brand,
                    instance_id=instance_id,
                    generation=1,
                    protocol_versions=["1.0"],
                    capabilities=list(capabilities),
                    limits={"max_envelope_bytes": 65536, "max_in_flight": max_in_flight},
                )
            except ValidationError:
                raise MailboxError("invalid_card") from None
            existing = conn.execute(
                "SELECT * FROM cards WHERE authority_id=? AND tenant_id=? AND agent_id=?", self._key(agent_id)
            ).fetchone()
            if existing is not None:
                if (
                    existing["card_json"] != canonical_bytes(card.model_dump()).decode()
                    or existing["allowed_scopes"] != canonical_bytes(scopes).decode()
                    or existing["allowed_kinds"] != canonical_bytes(kinds).decode()
                ):
                    raise MailboxError("agent_already_enrolled")
            else:
                conn.execute(
                    "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        *self._key(agent_id),
                        instance_id,
                        1,
                        canonical_bytes(card.model_dump()).decode(),
                        canonical_bytes(scopes).decode(),
                        canonical_bytes(kinds).decode(),
                        max_in_flight,
                    ),
                )
            result = MailboxInstanceRef(
                authority_id=self.db.authority_id,
                tenant_id=self.db.tenant_id,
                agent_id=agent_id,
                instance_id=instance_id,
                generation=1,
            )
            self.db.save_receipt(conn, agent_id, request_id, "init", input_hash, result.model_dump())
        return result

    def resume(
        self, agent: MailboxInstanceRef | str, *, instance_id: str | None = None, request_id: str
    ) -> MailboxInstanceRef:
        """Trusted local administration replaces an instance once per durable request."""
        agent_id = canonical_id(agent.agent_id if isinstance(agent, MailboxInstanceRef) else agent)
        request_id = canonical_id(request_id)
        instance_id = canonical_id(instance_id) if instance_id is not None else None
        inputs = {"agent_id": agent_id, "instance_id": instance_id}
        with self.db.connection(write=True) as conn:
            if isinstance(agent, MailboxInstanceRef):
                self._identity(agent)
            replay, input_hash = self.db.replay(conn, agent_id, request_id, "resume", inputs)
            if replay is not None:
                return MailboxInstanceRef.model_validate(replay)
            row = self._card_row(conn, agent_id)
            instance_id = instance_id or str(uuid4())
            if row["instance_id"] == instance_id:
                raise MailboxError("instance_reuse")
            result = MailboxInstanceRef(
                authority_id=self.db.authority_id,
                tenant_id=self.db.tenant_id,
                agent_id=agent_id,
                instance_id=instance_id,
                generation=row["generation"] + 1,
            )
            card = json.loads(row["card_json"])
            card.update(instance_id=instance_id, generation=result.generation)
            conn.execute(
                "UPDATE cards SET instance_id=?,generation=?,card_json=? "
                "WHERE authority_id=? AND tenant_id=? AND agent_id=?",
                (instance_id, result.generation, canonical_bytes(card).decode(), *self._key(agent_id)),
            )
            self.db.save_receipt(conn, agent_id, request_id, "resume", input_hash, result.model_dump())
        return result

    def card(self, agent_id: str) -> MailboxCard:
        with self.db.connection() as conn:
            row = self._card_row(conn, agent_id)
            try:
                return MailboxCard.model_validate_json(row["card_json"])
            except ValidationError:
                raise MailboxError("storage_error") from None

    def configure_quotas(
        self, *, record_limit: int | None = None, record_bytes: int | None = None, blob_bytes: int | None = None
    ) -> None:
        """Explicit trusted-local management changes admission budgets only."""
        changes = {key: value for key, value in locals().items() if key != "self" and value is not None}
        if any(type(value) is not int or value < 0 for value in changes.values()):
            raise MailboxError("invalid_quota")
        with self.db.connection(write=True) as conn:
            conn.executemany(
                "UPDATE metadata SET value=? WHERE key=?", [(str(value), key) for key, value in changes.items()]
            )

    def _message(self, conn, agent_id: str, message_id: str):
        key = (*self._key(agent_id), canonical_id(message_id))
        row = conn.execute(
            "SELECT * FROM messages WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?", key
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM tombstones WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?", key
            ).fetchone()
        return row

    @staticmethod
    def _status(row) -> dict:
        return {key: row[key] for key in ("message_id", "digest", "phase", "result_hash", "terminal_reason", "outcome")}

    def _send_replay(self, conn, envelope, sender_ref, raw=None, receipt_event=None):
        self._identity(envelope.sender_identity)
        self._identity(envelope.target_identity)
        self._current(conn, sender_ref)
        sender = envelope.sender_identity
        historical = False
        if receipt_event is not None:
            original_id, event_key = receipt_event
            intent = conn.execute(
                "SELECT envelope_bytes,digest,receipt_id FROM receipt_outbox "
                "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=? AND event_key=?",
                (*self._key(sender_ref.agent_id), canonical_id(original_id), event_key),
            ).fetchone()
            historical = (
                intent is not None
                and intent["envelope_bytes"] == raw
                and intent["digest"] == envelope.digest.value
                and intent["receipt_id"] == canonical_id(envelope.message_id)
                and envelope.kind == "receipt"
                and canonical_id(sender.agent_id) == canonical_id(sender_ref.agent_id)
            )
            if not historical:
                raise MailboxError("instance_fenced")
        if not historical and (
            canonical_id(sender.agent_id) != canonical_id(sender_ref.agent_id)
            or canonical_id(sender.instance_id) != canonical_id(sender_ref.instance_id)
        ):
            raise MailboxError("sender_mismatch")
        row = self._message(conn, envelope.target_identity.agent_id, envelope.message_id)
        if row is not None:
            if row["digest"] != envelope.digest.value:
                raise MailboxError("id_conflict")
            return {**self._status(row), "status": row["phase"], "duplicate": True, "durability": "sqlite-local-v1"}
        return None

    def send(
        self,
        raw: bytes,
        sender_ref: MailboxInstanceRef,
        *,
        now: int | None = None,
        event_schemas: Collection[str] = (),
        artifacts: dict[str, Path] | None = None,
        _receipt_event: tuple[str, str] | None = None,
    ) -> dict:
        """Persist validated immutable bytes before returning their stored status."""
        envelope = decode_envelope(raw, event_schemas=event_schemas)
        now = int(time.time()) if now is None else now
        with blobs.locked(self.db) if envelope.artifacts else nullcontext():
            if envelope.artifacts:
                with self.db.connection() as conn:
                    replay = self._send_replay(conn, envelope, sender_ref, raw, _receipt_event)
                    if replay is not None:
                        return replay
                blobs.import_artifacts(self.db, envelope, artifacts or {})
            with self.db.connection(write=True) as conn:
                replay = self._send_replay(conn, envelope, sender_ref, raw, _receipt_event)
                if replay is not None:
                    return replay
                if envelope.kind == "receipt" and len(raw) > RECEIPT_LIMIT:
                    raise MailboxError("receipt_too_large")
                sender = envelope.sender_identity
                target = self._card_row(conn, envelope.target_identity.agent_id)
                pinned = envelope.target_identity.instance_id
                if pinned is not None and canonical_id(pinned) != target["instance_id"]:
                    raise MailboxError("target_instance_mismatch")
                if envelope.kind not in json.loads(target["allowed_kinds"]):
                    raise MailboxError("kind_denied")
                if envelope.scope.model_dump() not in json.loads(target["allowed_scopes"]):
                    raise MailboxError("scope_denied")
                created = int(
                    datetime.strptime(envelope.created_at, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
                expires = created + envelope.ttl
                if created > now + 30:
                    raise MailboxError("created_in_future")
                if expires <= now:
                    raise MailboxError("message_expired")
                metadata = dict(conn.execute("SELECT key,value FROM metadata"))
                usage = conn.execute(
                    "SELECT count(*),coalesce(sum(reserved_bytes),0) FROM messages "
                    "WHERE authority_id=? AND tenant_id=? AND recipient=?",
                    self._key(target["agent_id"]),
                ).fetchone()
                if usage[0] >= int(metadata["record_limit"]) or usage[1] + MESSAGE_BUDGET > int(
                    metadata["record_bytes"]
                ):
                    raise MailboxError("quota_exceeded")
                conn.execute(
                    "INSERT INTO messages (authority_id,tenant_id,recipient,message_id,sender_agent,target_instance,"
                    "envelope_bytes,digest,created_at,expires_at,reserved_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        *self._key(target["agent_id"]),
                        canonical_id(envelope.message_id),
                        canonical_id(sender.agent_id),
                        canonical_id(pinned) if pinned is not None else None,
                        raw,
                        envelope.digest.value,
                        created,
                        expires,
                        MESSAGE_BUDGET,
                    ),
                )
                result = {
                    **self._status(self._message(conn, target["agent_id"], envelope.message_id)),
                    "status": "stored",
                    "duplicate": False,
                    "durability": "sqlite-local-v1",
                }
            return result

    def status(self, agent_id: str, message_id: str) -> dict:
        with self.db.connection() as conn:
            row = self._message(conn, agent_id, message_id)
            if row is None:
                raise MailboxError("message_not_found")
            return self._status(row)

    def peek(
        self,
        instance_ref: MailboxInstanceRef,
        *,
        limit: int = 1,
        now: int | None = None,
        event_schemas: Collection[str] = (),
    ) -> list[dict]:
        """Inspect eligible immutable envelopes without consuming or exposing claims."""
        if type(limit) is not int or not 1 <= limit <= 16:
            raise MailboxError("invalid_limit")
        now = int(time.time()) if now is None else now
        with self.db.connection() as conn:
            card = self._current(conn, instance_ref)
            rows = conn.execute(
                "SELECT * FROM messages WHERE authority_id=? AND tenant_id=? AND recipient=? "
                "AND phase='pending' AND not_before<=? AND expires_at>? "
                "AND (target_instance IS NULL OR target_instance=?) ORDER BY created_at,message_id",
                (*self._key(instance_ref.agent_id), now, now, canonical_id(instance_ref.instance_id)),
            )
            result = []
            for row in rows:
                try:
                    envelope = decode_envelope(row["envelope_bytes"], event_schemas=event_schemas)
                except MailboxError:
                    raise MailboxError("storage_conflict", message_id=row["message_id"]) from None
                if envelope.digest.value != row["digest"]:
                    raise MailboxError("storage_conflict", message_id=row["message_id"])
                blobs.verify_artifacts(self.root, envelope)
                if envelope.kind not in json.loads(
                    card["allowed_kinds"]
                ) or envelope.scope.model_dump() not in json.loads(card["allowed_scopes"]):
                    continue
                result.append(
                    {
                        **self._status(row),
                        "envelope": envelope.model_dump(),
                        "revision": row["revision"],
                        "attempt": row["attempt"],
                    }
                )
                if len(result) == limit:
                    break
            return result

    def gc_blobs(self, *, now: int | None = None) -> list[str]:
        """Collect old unreferenced artifacts under the publication lock."""
        return blobs.collect(self.db, now=int(time.time()) if now is None else now)
