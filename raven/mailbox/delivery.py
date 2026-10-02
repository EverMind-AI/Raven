"""Bounded claim leases, durable result receipts, and local mailbox recovery."""

import hashlib
import json
import math
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from raven.contracts.mailbox import MailboxClaim, MailboxError, MailboxResult
from raven.mailbox import blobs
from raven.mailbox.codec import canonical_bytes, decode_envelope, encode_envelope, seal_envelope
from raven.mailbox.db import RECEIPT_LIMIT, RESULT_LIMIT, canonical_id

RETENTION_SECONDS = 30 * 86400
RECEIPT_TTL = 7 * 86400


class MailboxDelivery:
    """Consume records owned by one local mailbox authority."""

    def __init__(self, store):
        self.store = store
        self.db = store.db

    @staticmethod
    def _now(now):
        return int(time.time()) if now is None else now

    @staticmethod
    def _lease(seconds):
        if type(seconds) is not int or not 10 <= seconds <= 3600:
            raise MailboxError("invalid_lease")

    def _rows(self, conn, agent_id=None):
        if agent_id is None:
            return conn.execute("SELECT * FROM messages WHERE phase IN ('pending','in_progress')").fetchall()
        return conn.execute(
            "SELECT * FROM messages WHERE authority_id=? AND tenant_id=? AND recipient=? "
            "AND phase IN ('pending','in_progress') ORDER BY created_at,message_id",
            self.store._key(agent_id),
        ).fetchall()

    @staticmethod
    def _envelope(row, event_schemas=None):
        try:
            if event_schemas is None:
                event_schemas = ()
                stored = json.loads(row["envelope_bytes"])
                if type(stored) is dict and stored.get("kind") == "event":
                    event_schemas = [stored["payload"]["schema"]]
            envelope = decode_envelope(row["envelope_bytes"], event_schemas=event_schemas)
            if envelope.digest.value != row["digest"]:
                raise MailboxError("storage_conflict")
            return envelope
        except (MailboxError, ValueError, KeyError, TypeError):
            raise MailboxError("storage_conflict", message_id=row["message_id"]) from None

    def _active(self, conn, ref, claim, now):
        self.store._current(conn, ref)
        row = self.store._message(conn, ref.agent_id, claim.envelope.message_id)
        if row is None:
            raise MailboxError("message_not_found", message_id=claim.envelope.message_id)
        if "envelope_bytes" not in row.keys():
            raise MailboxError("terminal_compacted", message_id=row["message_id"])
        if (
            row["phase"] != "in_progress"
            or row["consumer_instance"] != canonical_id(ref.instance_id)
            or row["consumer_generation"] != ref.generation
            or claim.instance_ref != ref
            or claim.generation != ref.generation
            or claim.attempt != row["attempt"]
            or not secrets.compare_digest(claim.claim_token, row["claim_token"] or "")
            or row["lease_until"] <= now
            or row["expires_at"] <= now
            or claim.envelope.digest.value != row["digest"]
        ):
            raise MailboxError("stale_claim", message_id=row["message_id"])
        return row

    @staticmethod
    def _claim(row, envelope, ref):
        return MailboxClaim(
            envelope=envelope,
            instance_ref=ref,
            claim_token=row["claim_token"],
            generation=ref.generation,
            attempt=row["attempt"],
            lease_until=row["lease_until"],
        )

    def _retry_row(self, conn, row, now, reason, ref=None):
        if row["attempt"] >= 5:
            self._terminal(conn, row, now, "dead_letter", reason=reason, outcome="failed", ref=ref)
            return
        delay = min(60, 2 ** max(0, row["attempt"] - 1)) + secrets.randbelow(1000001) / 1000000
        conn.execute(
            "UPDATE messages SET phase='pending',not_before=?,consumer_instance=NULL,"
            "consumer_generation=NULL,claim_token=NULL,lease_until=NULL,revision=revision+1 "
            "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?",
            (math.ceil(now + delay), *self._key_values(row)),
        )

    @staticmethod
    def _key_values(row):
        return tuple(row[k] for k in ("authority_id", "tenant_id", "recipient", "message_id"))

    def _terminal(
        self, conn, row, now, phase, *, reason=None, outcome=None, ref=None, result=None, evidence_artifacts=None
    ):
        conn.execute(
            "UPDATE messages SET phase=?,terminal_at=?,terminal_reason=?,outcome=?,revision=revision+1 "
            "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?",
            (phase, now, reason, outcome, *self._key_values(row)),
        )
        self._event(
            conn,
            row,
            "terminal",
            now,
            ref=ref,
            reason=reason,
            outcome=outcome,
            result=result,
            evidence_artifacts=evidence_artifacts,
        )

    def _recover(self, conn, now, agent_id=None):
        count = 0
        for row in self._rows(conn, agent_id):
            self._envelope(row)
            card = self.store._card_row(conn, row["recipient"])
            reason = None
            if row["expires_at"] <= now:
                reason = "message_expired"
            elif row["target_instance"] is not None and row["target_instance"] != card["instance_id"]:
                reason = "target_instance_replaced"
            if reason:
                self._terminal(conn, row, now, "dead_letter", reason=reason, outcome="failed")
            elif row["phase"] == "in_progress" and (
                row["consumer_generation"] != card["generation"]
                or row["consumer_instance"] != card["instance_id"]
                or row["lease_until"] <= now
            ):
                self._retry_row(conn, row, now, "attempt_budget_exhausted")
            elif row["phase"] == "pending" and row["attempt"] >= 5:
                self._terminal(conn, row, now, "dead_letter", reason="attempt_budget_exhausted", outcome="failed")
            else:
                continue
            count += 1
        return count

    def recover(self, *, now=None):
        """Recover timestamp and ownership transitions without executing message contents."""
        with self.db.connection(write=True) as conn:
            return self._recover(conn, self._now(now))

    def poll(self, ref, *, request_id, limit=1, lease_seconds=120, now=None, event_schemas=()):
        """Claim a bounded eligible batch once per durable caller request."""
        self._lease(lease_seconds)
        if type(limit) is not int or not 1 <= limit <= 16:
            raise MailboxError("invalid_limit")
        now = self._now(now)
        inputs = dict(
            ref=ref.model_dump(), limit=limit, lease_seconds=lease_seconds, event_schemas=sorted(event_schemas)
        )
        with blobs.locked(self.db):
            with self.db.connection() as conn:
                self.store._current(conn, ref)
                replay, _ = self.db.replay(conn, ref.agent_id, request_id, "poll", inputs)
                if replay is not None:
                    original = [MailboxClaim.model_validate(item) for item in replay["claims"]]
                    rows = []
                    for claim in original:
                        if claim.lease_until <= now:
                            raise MailboxError("stale_claim", message_id=claim.envelope.message_id)
                        rows.append(self._active(conn, ref, claim, now))
                else:
                    rows = [
                        row
                        for row in self._rows(conn, ref.agent_id)
                        if row["expires_at"] > now and row["not_before"] <= now
                    ]
                verified = {}
                for row in rows:
                    envelope = self._envelope(row, event_schemas)
                    blobs.verify_artifacts(self.store.root, envelope)
                    verified[row["message_id"]] = (row["digest"], envelope)
            with self.db.connection(write=True) as conn:
                card = self.store._current(conn, ref)
                replay, input_hash = self.db.replay(conn, ref.agent_id, request_id, "poll", inputs)
                if replay is not None:
                    claims = [MailboxClaim.model_validate(item) for item in replay["claims"]]
                    for claim in claims:
                        if claim.lease_until <= now:
                            raise MailboxError("stale_claim", message_id=claim.envelope.message_id)
                        self._active(conn, ref, claim, now)
                    return claims
                self._recover(conn, now, ref.agent_id)
                inflight = conn.execute(
                    "SELECT count(*) FROM messages WHERE authority_id=? AND tenant_id=? AND recipient=? "
                    "AND phase='in_progress'",
                    self.store._key(ref.agent_id),
                ).fetchone()[0]
                available = min(limit, max(0, card["max_in_flight"] - inflight))
                claims = []
                for row in self._rows(conn, ref.agent_id):
                    if len(claims) >= available:
                        break
                    if row["phase"] != "pending" or row["not_before"] > now:
                        continue
                    prior = verified.get(row["message_id"])
                    if prior is None:
                        continue
                    if prior[0] != row["digest"]:
                        raise MailboxError("storage_conflict", message_id=row["message_id"])
                    envelope = prior[1]
                    if envelope.kind not in json.loads(
                        card["allowed_kinds"]
                    ) or envelope.scope.model_dump() not in json.loads(card["allowed_scopes"]):
                        continue
                    conn.execute(
                        "UPDATE messages SET phase='in_progress',attempt=attempt+1,consumer_instance=?,"
                        "consumer_generation=?,claim_token=?,lease_until=?,revision=revision+1 "
                        "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?",
                        (
                            canonical_id(ref.instance_id),
                            ref.generation,
                            secrets.token_hex(32),
                            min(now + lease_seconds, row["expires_at"]),
                            *self._key_values(row),
                        ),
                    )
                    claimed = self.store._message(conn, ref.agent_id, row["message_id"])
                    claims.append(self._claim(claimed, envelope, ref))
                    self._event(conn, claimed, f"received:{claimed['attempt']}", now, ref=ref)
                self.db.save_receipt(
                    conn,
                    ref.agent_id,
                    request_id,
                    "poll",
                    input_hash,
                    {"claims": [claim.model_dump() for claim in claims]},
                )
                return claims

    def renew(self, ref, claim, *, request_id, lease_seconds=120, now=None):
        """Extend a valid claim without consuming another delivery attempt."""
        self._lease(lease_seconds)
        now = self._now(now)
        inputs = dict(ref=ref.model_dump(), claim=claim.model_dump(), lease_seconds=lease_seconds)
        with self.db.connection(write=True) as conn:
            row = self._active(conn, ref, claim, now)
            replay, input_hash = self.db.replay(conn, ref.agent_id, request_id, "renew", inputs)
            if replay is not None:
                original = MailboxClaim.model_validate(replay)
                if original.lease_until <= now:
                    raise MailboxError("stale_claim", message_id=claim.envelope.message_id)
                return original
            conn.execute(
                "UPDATE messages SET lease_until=?,revision=revision+1 "
                "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?",
                (min(now + lease_seconds, row["expires_at"]), *self._key_values(row)),
            )
            result = self._claim(self.store._message(conn, ref.agent_id, row["message_id"]), claim.envelope, ref)
            self.db.save_receipt(conn, ref.agent_id, request_id, "renew", input_hash, result.model_dump())
            return result

    @staticmethod
    def _final_claim(claim):
        return canonical_bytes(
            dict(
                instance=claim.instance_ref.model_dump(),
                token=claim.claim_token,
                generation=claim.generation,
                attempt=claim.attempt,
            )
        ).decode()

    def _event(
        self, conn, row, event_key, now, *, ref=None, reason=None, outcome=None, result=None, evidence_artifacts=None
    ):
        envelope = self._envelope(row)
        policy = envelope.receipt_policy
        if policy == "none" or (event_key.startswith("received:") and policy != "received_and_terminal"):
            return
        received = event_key.startswith("received:")
        event = dict(
            trace_id=envelope.trace_id,
            scope=envelope.scope.model_dump(),
            target={**envelope.sender_identity.model_dump(), "instance_id": None},
            artifacts=[],
            data=dict(
                for_message_id=envelope.message_id,
                for_digest=envelope.digest.value,
                stage="received" if received else ("rejected" if reason else "processed"),
                attempt=row["attempt"],
                receiver_generation=(
                    ref.generation
                    if ref
                    else row["consumer_generation"] or self.store._card_row(conn, row["recipient"])["generation"]
                ),
                outcome=None if received else outcome,
                reason=reason,
                result=None if result is None else dict(summary=result["summary"], evidence=result["evidence"]),
            ),
        )
        if evidence_artifacts is not None:
            event["artifacts"] = [
                dict(sha256=item.sha256, size=item.size, name=item.sha256[:12], media_type="application/octet-stream")
                for item in evidence_artifacts
            ]
        materialized = self._materialize(event, ref, now) if ref is not None else None
        conn.execute(
            "INSERT OR IGNORE INTO receipt_outbox (authority_id,tenant_id,recipient,message_id,event_key,"
            "event_json,artifact_refs) VALUES (?,?,?,?,?,?,?)",
            (
                *self._key_values(row),
                event_key,
                canonical_bytes(event).decode(),
                canonical_bytes([item["sha256"] for item in event["artifacts"]]).decode(),
            ),
        )
        if materialized is not None:
            self._freeze(conn, self._key_values(row), event_key, materialized, now)

    @staticmethod
    def _materialize(event, ref, now):
        envelope = seal_envelope(
            dict(
                protocol_version="1.0",
                message_id=str(uuid4()),
                trace_id=event["trace_id"],
                in_reply_to=event["data"]["for_message_id"],
                kind="receipt",
                sender_identity={k: v for k, v in ref.model_dump().items() if k != "generation"},
                target_identity=event["target"],
                scope=event["scope"],
                created_at=datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ttl=RECEIPT_TTL,
                receipt_policy="none",
                payload={"content_type": "application/json", "schema": "opena2a.receipt/1", "data": event["data"]},
                artifacts=event["artifacts"],
                extensions={},
            )
        )
        raw = encode_envelope(envelope)
        if len(raw) > RECEIPT_LIMIT:
            raise MailboxError("receipt_too_large")
        return envelope, raw

    @staticmethod
    def _freeze(conn, key, event_key, materialized, now):
        envelope, raw = materialized
        conn.execute(
            "UPDATE receipt_outbox SET receipt_id=?,envelope_bytes=?,digest=?,created_at=?,expires_at=?,status='pending' "
            "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=? AND event_key=? "
            "AND status='unmaterialized'",
            (canonical_id(envelope.message_id), raw, envelope.digest.value, now, now + RECEIPT_TTL, *key, event_key),
        )

    def finish(self, ref, claim, result, *, now=None):
        """Commit a strict result and its receipt intent with the final claim atomically."""
        try:
            normalized = (
                result if isinstance(result, MailboxResult) else MailboxResult.model_validate(result)
            ).model_dump()
            raw = canonical_bytes(normalized)
        except (ValidationError, MailboxError):
            raise MailboxError("invalid_result") from None
        if len(raw) > RESULT_LIMIT:
            raise MailboxError("result_too_large")
        evidence = normalized["evidence"]
        if len(evidence) > 32 or any(
            len(item) != 64 or any(c not in "0123456789abcdef" for c in item) for item in evidence
        ):
            raise MailboxError("invalid_result")
        now = self._now(now)
        final_claim = self._final_claim(claim)
        with blobs.locked(self.db):
            with self.db.connection() as conn:
                self.store._current(conn, ref)
                row = self.store._message(conn, ref.agent_id, claim.envelope.message_id)
                if row is not None and "envelope_bytes" in row.keys() and row["phase"] in ("completed", "dead_letter"):
                    return self._finish_replay(row, ref, claim, raw, final_claim)
                row = self._active(conn, ref, claim, now)
                envelope = self._envelope(row)
                evidence_artifacts = self._evidence(conn, envelope, evidence)
                blobs.verify_artifacts(self.store.root, envelope)
                blobs.verify_artifacts(self.store.root, envelope.model_copy(update={"artifacts": evidence_artifacts}))
            with self.db.connection(write=True) as conn:
                self.store._current(conn, ref)
                row = self.store._message(conn, ref.agent_id, claim.envelope.message_id)
                if row is not None and "envelope_bytes" in row.keys() and row["phase"] in ("completed", "dead_letter"):
                    return self._finish_replay(row, ref, claim, raw, final_claim)
                row = self._active(conn, ref, claim, now)
                conn.execute(
                    "UPDATE messages SET final_claim=?,result_json=?,result_hash=? "
                    "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?",
                    (final_claim, raw.decode(), hashlib.sha256(raw).hexdigest(), *self._key_values(row)),
                )
                self._terminal(
                    conn,
                    row,
                    now,
                    "completed",
                    outcome=normalized["outcome"],
                    ref=ref,
                    result=normalized,
                    evidence_artifacts=evidence_artifacts,
                )
                return self.store._status(self.store._message(conn, ref.agent_id, row["message_id"]))

    def _evidence(self, conn, envelope, hashes):
        descriptors = {item.sha256: item for item in envelope.artifacts}
        missing = set(hashes) - descriptors.keys()
        if missing:
            for row in conn.execute(
                "SELECT envelope_bytes,digest,message_id FROM messages WHERE authority_id=? AND tenant_id=?",
                (self.db.authority_id, self.db.tenant_id),
            ):
                candidate = self._envelope(row)
                if candidate.scope != envelope.scope:
                    continue
                for artifact in candidate.artifacts:
                    if artifact.sha256 in missing:
                        descriptors[artifact.sha256] = artifact
                        missing.remove(artifact.sha256)
                if not missing:
                    break
        if missing:
            raise MailboxError("evidence_unavailable")
        return [descriptors[value] for value in dict.fromkeys(hashes)]

    def _finish_replay(self, row, ref, claim, raw, final_claim):
        if claim.instance_ref != ref or row["final_claim"] != final_claim:
            raise MailboxError("stale_claim", message_id=row["message_id"])
        if row["result_json"] != raw.decode():
            raise MailboxError("ack_conflict", message_id=row["message_id"])
        return self.store._status(row)

    def retry(self, ref, claim, *, reason, now=None):
        """Release a valid attempt with one durable bounded backoff."""
        if type(reason) is not str or not reason:
            raise MailboxError("invalid_reason")
        now = self._now(now)
        with self.db.connection(write=True) as conn:
            row = self._active(conn, ref, claim, now)
            self._retry_row(conn, row, now, reason, ref=ref)
            return self.store._status(self.store._message(conn, ref.agent_id, row["message_id"]))

    def reject(self, ref, claim, *, reason, now=None):
        """Reject an explicitly claimed message without asserting business acceptance."""
        if type(reason) is not str or not reason:
            raise MailboxError("invalid_reason")
        now = self._now(now)
        with self.db.connection(write=True) as conn:
            row = self._active(conn, ref, claim, now)
            self._terminal(conn, row, now, "dead_letter", reason=reason, outcome="failed", ref=ref)
            return self.store._status(self.store._message(conn, ref.agent_id, row["message_id"]))

    def flush_receipts(self, ref, *, now=None):
        """Publish frozen intents through ordinary admission outside source write transactions."""
        now = self._now(now)
        with self.db.connection(write=True) as conn:
            self.store._current(conn, ref)
            rows = conn.execute(
                "SELECT * FROM receipt_outbox WHERE authority_id=? AND tenant_id=? AND recipient=? "
                "AND status IN ('unmaterialized','pending') ORDER BY message_id,event_key",
                self.store._key(ref.agent_id),
            ).fetchall()
            for row in rows:
                if row["status"] == "unmaterialized":
                    self._freeze(
                        conn,
                        self._key_values(row),
                        row["event_key"],
                        self._materialize(json.loads(row["event_json"]), ref, now),
                        now,
                    )
            conn.execute(
                "UPDATE receipt_outbox SET status='delivery_unknown' "
                "WHERE authority_id=? AND tenant_id=? AND recipient=? AND status='pending' AND expires_at<=?",
                (*self.store._key(ref.agent_id), now),
            )
            pending = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM receipt_outbox WHERE authority_id=? AND tenant_id=? AND recipient=? AND status='pending'",
                    self.store._key(ref.agent_id),
                )
            ]
        errors = []
        for intent in pending:
            try:
                self.store.send(
                    intent["envelope_bytes"], ref, now=now, _receipt_event=(intent["message_id"], intent["event_key"])
                )
            except MailboxError as exc:
                if exc.code == "instance_fenced":
                    raise
                errors.append(
                    dict(
                        message_id=intent["message_id"],
                        event_key=intent["event_key"],
                        code=exc.code,
                        retryable=exc.retryable,
                    )
                )
                continue
            with self.db.connection(write=True) as conn:
                self.store._current(conn, ref)
                conn.execute(
                    "UPDATE receipt_outbox SET status='published' WHERE authority_id=? AND tenant_id=? "
                    "AND recipient=? AND message_id=? AND event_key=? AND digest=? AND envelope_bytes=? AND status='pending'",
                    (*self._key_values(intent), intent["event_key"], intent["digest"], intent["envelope_bytes"]),
                )
        with self.db.connection() as conn:
            self.store._current(conn, ref)
            counts = {status: 0 for status in ("published", "pending", "unmaterialized", "delivery_unknown")}
            counts.update(
                dict(
                    conn.execute(
                        "SELECT status,count(*) FROM receipt_outbox WHERE authority_id=? AND tenant_id=? AND recipient=? GROUP BY status",
                        self.store._key(ref.agent_id),
                    )
                )
            )
        return {**counts, "errors": errors}

    def gc(self, *, now=None, retention_seconds=RETENTION_SECONDS):
        """Compact settled terminal records and retain finite deduplication tombstones."""
        if type(retention_seconds) is not int or retention_seconds < 0:
            raise MailboxError("invalid_retention")
        now = self._now(now)
        compacted = 0
        with blobs.locked(self.db):
            with self.db.connection(write=True) as conn:
                deleted = conn.execute("DELETE FROM tombstones WHERE retain_until<=?", (now,)).rowcount
                rows = conn.execute(
                    "SELECT * FROM messages m WHERE phase IN ('completed','dead_letter') AND terminal_at<=? "
                    "AND NOT EXISTS (SELECT 1 FROM receipt_outbox o WHERE o.authority_id=m.authority_id "
                    "AND o.tenant_id=m.tenant_id AND o.recipient=m.recipient AND o.message_id=m.message_id "
                    "AND o.status!='published')",
                    (now - retention_seconds,),
                ).fetchall()
                for row in rows:
                    envelope = self._envelope(row)
                    key = self._key_values(row)
                    outbox = conn.execute(
                        "SELECT event_key,receipt_id,digest,artifact_refs FROM receipt_outbox "
                        "WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=? ORDER BY event_key",
                        key,
                    ).fetchall()
                    references = {item.sha256 for item in envelope.artifacts}
                    if row["result_json"] is not None:
                        references.update(json.loads(row["result_json"])["evidence"])
                    for intent in outbox:
                        references.update(json.loads(intent["artifact_refs"]))
                    retain_until = max(row["expires_at"] + 30, row["terminal_at"] + RETENTION_SECONDS)
                    if retain_until > now:
                        links = [{k: intent[k] for k in ("event_key", "receipt_id", "digest")} for intent in outbox]
                        conn.execute(
                            "INSERT INTO tombstones (authority_id,tenant_id,recipient,message_id,digest,phase,"
                            "result_hash,terminal_at,retain_until,terminal_reason,outcome,artifact_refs,receipt_links) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                *key,
                                row["digest"],
                                row["phase"],
                                row["result_hash"],
                                row["terminal_at"],
                                retain_until,
                                row["terminal_reason"],
                                row["outcome"],
                                canonical_bytes(sorted(references)).decode(),
                                canonical_bytes(links).decode(),
                            ),
                        )
                    conn.execute(
                        "DELETE FROM receipt_outbox WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?",
                        key,
                    )
                    conn.execute(
                        "DELETE FROM messages WHERE authority_id=? AND tenant_id=? AND recipient=? AND message_id=?",
                        key,
                    )
                    compacted += 1
        removed = self.store.gc_blobs(now=now)
        return dict(compacted=compacted, tombstones_deleted=deleted, blobs_deleted=removed)

    def quarantine(self, raw, *, reason, now=None):
        """Retain bounded untrusted bytes locally without interpreting their identities."""
        if type(raw) is not bytes:
            raise MailboxError("invalid_quarantine")
        if len(raw) > 65536:
            raise MailboxError("quarantine_too_large")
        if type(reason) is not str or not reason or not reason.isascii() or len(reason) > 128:
            raise MailboxError("invalid_reason")
        with self.db.connection(write=True) as conn:
            budget = int(conn.execute("SELECT value FROM metadata WHERE key='quarantine_bytes'").fetchone()[0])
            usage = conn.execute(
                "SELECT coalesce(sum(length(raw_bytes)+length(reason)+32),0) FROM quarantine"
            ).fetchone()[0]
            if usage + len(raw) + len(reason) + 32 > budget:
                raise MailboxError("quota_exceeded")
            return conn.execute(
                "INSERT INTO quarantine (raw_bytes,reason,created_at) VALUES (?,?,?)", (raw, reason, self._now(now))
            ).lastrowid
