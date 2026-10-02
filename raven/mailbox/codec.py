"""Strict OpenA2A 1.0 decoding and OA-C14N-1 integrity for local transport.

Decode validates immutable wire data only. Root identity, TTL admission, quotas,
artifact availability, and operation authorization belong to the caller.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime
from typing import Any, Collection
from uuid import UUID

from pydantic import ValidationError

from raven.contracts.mailbox import MailboxEnvelope, MailboxError

MAX_ENVELOPE_BYTES = 65536
MAX_DEPTH = 32
MAX_INT = (1 << 53) - 1
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def _check_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH or (type(value) in (list, dict) and depth >= MAX_DEPTH):
        raise MailboxError("too_deep")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_INT:
            raise MailboxError("integer_out_of_range")
        return
    if type(value) is str:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise MailboxError("isolated_surrogate")
        return
    if type(value) is list:
        for item in value:
            _check_value(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise MailboxError("non_ascii_key")
            _check_value(item, depth + 1)
        return
    raise MailboxError("unsupported_json_type")


def canonical_bytes(value: Any) -> bytes:
    """Return OA-C14N-1 bytes after validating its restricted JSON profile."""
    _check_value(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def envelope_digest(envelope: dict | MailboxEnvelope) -> str:
    """Hash every wire field except the top-level digest and signature."""
    value = envelope.model_dump() if isinstance(envelope, MailboxEnvelope) else envelope
    _check_value(value)
    unsigned = {key: item for key, item in value.items() if key not in {"digest", "signature"}}
    return hashlib.sha256(canonical_bytes(unsigned)).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MailboxError("duplicate_key")
        result[key] = value
    return result


def _parse_integer(value: str) -> int:
    if len(value.lstrip("-")) > 16:
        raise MailboxError("integer_out_of_range")
    integer = int(value)
    if abs(integer) > MAX_INT:
        raise MailboxError("integer_out_of_range")
    return integer


def _reject_number(value: str) -> None:
    raise MailboxError("float_or_nonfinite_not_allowed")


def _check_wire_depth(text: str) -> None:
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise MailboxError("too_deep")
        elif char in "]}":
            depth -= 1


def _uuid(value: Any) -> None:
    if type(value) is not str or not _UUID.fullmatch(value):
        raise MailboxError("invalid_envelope")
    try:
        UUID(value)
    except ValueError:
        raise MailboxError("invalid_envelope") from None


def _shape(data: Any, fields: dict[str, type], nullable: Collection[str] = (), *, exact: bool = False) -> None:
    if type(data) is not dict or not fields.keys() <= data.keys() or (exact and data.keys() != fields.keys()):
        raise MailboxError("invalid_envelope")
    for key, kind in fields.items():
        if data[key] is None and key in nullable:
            continue
        if type(data[key]) is not kind:
            raise MailboxError("invalid_envelope")


def _strings(value: list, *, hashes: bool = False) -> None:
    if any(type(item) is not str or (hashes and not _HEX.fullmatch(item)) for item in value):
        raise MailboxError("invalid_envelope")


def _payload(envelope: MailboxEnvelope, event_schemas: Collection[str]) -> None:
    kind = envelope.kind
    schema = envelope.payload.schema_id
    data = envelope.payload.data
    expected = {
        "task.request": "opena2a.task/1",
        "task.result": "opena2a.result/1",
        "receipt": "opena2a.receipt/1",
        "handoff.offer": "opena2a.handoff/1",
        "handoff.accept": "opena2a.handoff/1",
    }
    if kind == "event":
        if schema not in event_schemas:
            raise MailboxError("unsupported_payload_schema")
        return
    if schema != expected[kind]:
        raise MailboxError("unsupported_payload_schema")
    if kind == "task.request":
        _shape(data, {"operation": str, "arguments": dict, "idempotency_key": str, "acceptance": list})
        if not data["operation"] or not 1 <= len(data["idempotency_key"]) <= 128:
            raise MailboxError("invalid_envelope")
        _strings(data["acceptance"])
    elif kind == "task.result":
        _shape(data, {"request_message_id": str, "outcome": str, "summary": str, "evidence": list})
        _uuid(data["request_message_id"])
        if data["outcome"] not in {"succeeded", "failed", "blocked"}:
            raise MailboxError("invalid_envelope")
        _strings(data["evidence"], hashes=True)
    elif kind == "receipt":
        _shape(
            data,
            {
                "for_message_id": str,
                "for_digest": str,
                "stage": str,
                "attempt": int,
                "receiver_generation": int,
                "outcome": str,
                "reason": str,
                "result": dict,
            },
            {"outcome", "reason", "result"},
        )
        _uuid(data["for_message_id"])
        if not _HEX.fullmatch(data["for_digest"]) or data["attempt"] < 0 or data["receiver_generation"] < 1:
            raise MailboxError("invalid_envelope")
        if (
            envelope.receipt_policy != "none"
            or envelope.in_reply_to != data["for_message_id"]
            or envelope.target_identity.instance_id is not None
        ):
            raise MailboxError("invalid_envelope")
        stage = data["stage"]
        if stage == "received":
            if data["outcome"] is not None or data["result"] is not None:
                raise MailboxError("invalid_envelope")
        elif stage == "processed":
            if data["outcome"] not in {"succeeded", "failed", "blocked"}:
                raise MailboxError("invalid_envelope")
        elif stage == "rejected":
            if data["outcome"] != "failed" or not data["reason"]:
                raise MailboxError("invalid_envelope")
        else:
            raise MailboxError("invalid_envelope")
        if data["result"] is not None:
            _shape(data["result"], {"summary": str, "evidence": list})
            _strings(data["result"]["evidence"], hashes=True)
    elif kind == "handoff.offer":
        _shape(
            data,
            {
                "handoff_id": str,
                "mode": str,
                "task_ref": str,
                "source_sessions": list,
                "objective": str,
                "effective_decisions": list,
                "rejected_alternatives": list,
                "unresolved_items": list,
                "work_state": dict,
                "environment": dict,
                "required_artifacts": list,
                "acceptance": list,
                "authority": dict,
            },
        )
        _uuid(data["handoff_id"])
        if data["mode"] != "offer":
            raise MailboxError("invalid_envelope")
        _shape(data["work_state"], {"done": list, "next_actions": list})
        _strings(data["work_state"]["done"])
        _strings(data["work_state"]["next_actions"])
        _shape(
            data["environment"],
            {"repo_id": str, "commit": str, "dirty_patch_sha256": str},
            {"commit", "dirty_patch_sha256"},
        )
        patch = data["environment"]["dirty_patch_sha256"]
        if patch is not None and not _HEX.fullmatch(patch):
            raise MailboxError("invalid_envelope")
        _shape(data["authority"], {"owner": str, "transfer_required": bool})
        artifact_hashes = {artifact.sha256 for artifact in envelope.artifacts}
        for session in data["source_sessions"]:
            _shape(
                session,
                {
                    "provider": str,
                    "session_id": str,
                    "artifact_sha256": str,
                    "source_snapshot_bytes": int,
                    "coverage": dict,
                },
            )
            if session["artifact_sha256"] not in artifact_hashes or session["source_snapshot_bytes"] < 0:
                raise MailboxError("invalid_envelope")
            _shape(session["coverage"], {"selected_steps": list, "complete": bool})
            if any(type(step) is not int or step < 0 for step in session["coverage"]["selected_steps"]):
                raise MailboxError("invalid_envelope")
        for decision in data["effective_decisions"]:
            _shape(
                decision,
                {
                    "decision_id": str,
                    "branch_id": str,
                    "status": str,
                    "text": str,
                    "scope": str,
                    "source_refs": list,
                    "confirmed_by": str,
                    "supersedes": list,
                },
            )
            if decision["status"] != "confirmed":
                raise MailboxError("invalid_envelope")
            _strings(decision["source_refs"])
            _strings(decision["supersedes"])
        for field in ("rejected_alternatives", "unresolved_items", "acceptance"):
            _strings(data[field])
        _strings(data["required_artifacts"], hashes=True)
        if not set(data["required_artifacts"]) <= artifact_hashes:
            raise MailboxError("invalid_envelope")
    else:
        _shape(
            data,
            {
                "handoff_id": str,
                "offer_message_id": str,
                "read_artifact_hashes": list,
                "read_coverage": dict,
                "accepted_scope": dict,
                "next_action": str,
                "unresolved_items": list,
            },
        )
        _uuid(data["handoff_id"])
        _uuid(data["offer_message_id"])
        _strings(data["read_artifact_hashes"], hashes=True)
        _strings(data["unresolved_items"])
        _shape(data["accepted_scope"], {"task_id": str, "workspace_id": str}, exact=True)
        for value in data["accepted_scope"].values():
            if not value.isascii() or not 1 <= len(value) <= 128:
                raise MailboxError("invalid_envelope")


def _validate(value: Any, event_schemas: Collection[str]) -> MailboxEnvelope:
    if type(value) is not dict:
        raise MailboxError("invalid_envelope")
    if "protocol_version" in value and value["protocol_version"] != "1.0":
        raise MailboxError("unsupported_version")
    try:
        envelope = MailboxEnvelope.model_validate(value)
    except ValidationError:
        raise MailboxError("invalid_envelope") from None
    for identity in (envelope.sender_identity, envelope.target_identity):
        for identifier in (identity.authority_id, identity.agent_id):
            _uuid(identifier)
        if identity.instance_id is not None:
            _uuid(identity.instance_id)
    if envelope.sender_identity.instance_id is None:
        raise MailboxError("invalid_envelope")
    for identifier in (envelope.message_id, envelope.trace_id):
        _uuid(identifier)
    if envelope.in_reply_to is not None:
        _uuid(envelope.in_reply_to)
    for scope in (envelope.scope.task_id, envelope.scope.workspace_id):
        if not scope.isascii():
            raise MailboxError("invalid_envelope")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", envelope.created_at):
        raise MailboxError("invalid_envelope")
    try:
        datetime.strptime(envelope.created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise MailboxError("invalid_envelope") from None
    if sum(artifact.size for artifact in envelope.artifacts) > 67108864:
        raise MailboxError("invalid_envelope")
    _payload(envelope, event_schemas)
    if not hmac.compare_digest(envelope.digest.value, envelope_digest(value)):
        raise MailboxError("digest_mismatch")
    if envelope.signature is not None:
        raise MailboxError("unsupported_signature_profile")
    return envelope


def decode_envelope(raw: bytes, *, event_schemas: Collection[str] = ()) -> MailboxEnvelope:
    """Decode bounded UTF-8 JSON and verify strict schema, digest, and local profile.

    Event schema names must be explicitly registered by the caller. This transport
    validates their JSON profile; the receiving application validates event data.
    """
    if type(raw) is not bytes:
        raise MailboxError("invalid_json")
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise MailboxError("envelope_too_large")
    try:
        text = raw.decode("utf-8")
        _check_wire_depth(text)
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise MailboxError("invalid_json") from None
    _check_value(value)
    return _validate(value, event_schemas)


def encode_envelope(envelope: MailboxEnvelope | dict, *, event_schemas: Collection[str] = ()) -> bytes:
    """Return canonical complete wire bytes, validating before publication."""
    value = envelope.model_dump() if isinstance(envelope, MailboxEnvelope) else envelope
    wire = canonical_bytes(value)
    decode_envelope(wire, event_schemas=event_schemas)
    return wire


def seal_envelope(unsigned: dict, *, event_schemas: Collection[str] = ()) -> MailboxEnvelope:
    """Create a new local envelope without changing its supplied identity or time."""
    value = {key: item for key, item in unsigned.items() if key not in {"digest", "signature"}}
    value["digest"] = {"algorithm": "sha256", "canonicalization": "oa-c14n-1", "value": envelope_digest(value)}
    value["signature"] = None
    return decode_envelope(canonical_bytes(value), event_schemas=event_schemas)
