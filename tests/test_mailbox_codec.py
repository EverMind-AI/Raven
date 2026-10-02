"""Wire conformance and admission tests for the trusted-local mailbox codec."""

import hashlib
import json

import pytest

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import canonical_bytes, decode_envelope, encode_envelope, envelope_digest, seal_envelope


@pytest.fixture
def request_data():
    identity = {
        "authority_id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "local",
        "agent_id": "22222222-2222-4222-8222-222222222222",
        "instance_id": "33333333-3333-4333-8333-333333333333",
    }
    return {
        "protocol_version": "1.0",
        "message_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "trace_id": "99999999-9999-4999-8999-999999999999",
        "in_reply_to": None,
        "kind": "task.request",
        "sender_identity": identity,
        "target_identity": {**identity, "instance_id": None},
        "scope": {"task_id": "TASK-42", "workspace_id": "review"},
        "created_at": "2026-09-26T00:00:00Z",
        "ttl": 86400,
        "receipt_policy": "terminal",
        "payload": {
            "content_type": "application/json",
            "schema": "opena2a.task/1",
            "data": {
                "operation": "code.review",
                "arguments": {"revision": "abc123"},
                "idempotency_key": "review-1",
                "acceptance": ["Report problems with evidence"],
            },
        },
        "artifacts": [],
        "extensions": {},
    }


@pytest.mark.parametrize(
    "value,wire,digest",
    [
        ({}, b"{}", "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
        (
            {"b": "\u4e2d\U0001f600\n", "a": 1},
            b'{"a":1,"b":"\\u4e2d\\ud83d\\ude00\\n"}',
            "0b2ab6506e9371e287218fa98be46f5acd85e839f4eb92bdd7577d05bbb2b30f",
        ),
        (
            {"z": 0, "t": True, "n": None},
            b'{"n":null,"t":true,"z":0}',
            "84e7410a4d7961619608c0508fe64b630ecf1842f1780e719c4465ace06a31c7",
        ),
    ],
)
def test_golden_vectors(value, wire, digest):
    assert canonical_bytes(value) == wire
    assert hashlib.sha256(wire).hexdigest() == digest


def test_complete_request_round_trip_without_executor(request_data):
    envelope = seal_envelope(request_data)
    assert decode_envelope(encode_envelope(envelope)) == envelope
    assert envelope.payload.data["operation"] == "code.review"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":1.0}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":9007199254740992}',
        b'{"x":-9007199254740992}',
        b'{"\\u4e2d":1}',
        b'{"x":"\\ud800"}',
        b"[" * 33 + b"0" + b"]" * 33,
        b" " * 65537,
    ],
)
def test_rejects_invalid_json_profile(raw):
    with pytest.raises(MailboxError):
        decode_envelope(raw)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("unexpected", 1, "invalid_envelope"),
        ("protocol_version", "1.1", "unsupported_version"),
        ("signature", {}, "unsupported_signature_profile"),
        ("ttl", True, "invalid_envelope"),
        ("created_at", "2026-02-30T00:00:00Z", "invalid_envelope"),
    ],
)
def test_rejects_invalid_envelope(request_data, field, value, code):
    wire = seal_envelope(request_data).model_dump()
    wire[field] = value
    wire["digest"]["value"] = envelope_digest(wire)
    with pytest.raises(MailboxError) as error:
        decode_envelope(json.dumps(wire).encode())
    assert error.value.code == code


def test_digest_mismatch(request_data):
    wire = seal_envelope(request_data).model_dump()
    wire["scope"]["task_id"] = "OTHER"
    with pytest.raises(MailboxError) as error:
        decode_envelope(json.dumps(wire).encode())
    assert error.value.code == "digest_mismatch"


def test_digest_covers_all_unsigned_fields(request_data):
    first = envelope_digest(request_data)
    request_data["signature"] = None
    request_data["digest"] = {"value": "ignored"}
    assert envelope_digest(request_data) == first
    request_data["extensions"] = {"note": "changed"}
    assert envelope_digest(request_data) != first


def test_no_unicode_normalization():
    assert canonical_bytes({"x": "\u00e9"}) != canonical_bytes({"x": "e\u0301"})


@pytest.mark.parametrize("path", ["identity", "scope", "payload", "artifact", "digest"])
def test_nested_unknown_fields_rejected(request_data, path):
    wire = seal_envelope(request_data).model_dump()
    objects = {
        "identity": wire["sender_identity"],
        "scope": wire["scope"],
        "payload": wire["payload"],
        "digest": wire["digest"],
    }
    if path == "artifact":
        wire["artifacts"] = [{"sha256": "0" * 64, "size": 0, "media_type": "text/plain", "name": "report", "extra": 1}]
    else:
        objects[path]["extra"] = 1
    wire["digest"]["value"] = envelope_digest(wire)
    with pytest.raises(MailboxError):
        decode_envelope(json.dumps(wire).encode())


@pytest.mark.parametrize(
    "kind,schema,data",
    [
        (
            "task.result",
            "opena2a.result/1",
            {
                "request_message_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "outcome": "succeeded",
                "summary": "Reviewed",
                "evidence": [],
            },
        ),
        (
            "handoff.accept",
            "opena2a.handoff/1",
            {
                "handoff_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "offer_message_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "read_artifact_hashes": [],
                "read_coverage": {"complete": False},
                "accepted_scope": {"task_id": "TASK-42", "workspace_id": "review"},
                "next_action": "Review changes",
                "unresolved_items": [],
            },
        ),
        (
            "handoff.offer",
            "opena2a.handoff/1",
            {
                "handoff_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "mode": "offer",
                "task_ref": "TASK-42",
                "source_sessions": [],
                "objective": "Review changes",
                "effective_decisions": [],
                "rejected_alternatives": [],
                "unresolved_items": [],
                "work_state": {"done": [], "next_actions": ["Review"]},
                "environment": {"repo_id": "review", "commit": None, "dirty_patch_sha256": None},
                "required_artifacts": [],
                "acceptance": ["Report evidence"],
                "authority": {"owner": "task-owner", "transfer_required": True},
            },
        ),
    ],
)
def test_application_kinds_round_trip(request_data, kind, schema, data):
    request_data["kind"] = kind
    request_data["payload"] = {"content_type": "application/json", "schema": schema, "data": data}
    envelope = seal_envelope(request_data)
    assert decode_envelope(encode_envelope(envelope)).payload.data == data
    request_data["payload"]["data"]["unknown"] = True
    extended = seal_envelope(request_data)
    assert decode_envelope(encode_envelope(extended)).payload.data["unknown"] is True


@pytest.mark.parametrize(
    "stage,outcome,reason,result",
    [
        ("received", None, None, None),
        ("processed", "succeeded", None, {"summary": "Reviewed", "evidence": []}),
        ("rejected", "failed", "unsupported_operation", None),
    ],
)
def test_receipt_stage_contract(request_data, stage, outcome, reason, result):
    request_data["kind"] = "receipt"
    request_data["receipt_policy"] = "none"
    request_data["in_reply_to"] = request_data["message_id"]
    request_data["payload"] = {
        "content_type": "application/json",
        "schema": "opena2a.receipt/1",
        "data": {
            "for_message_id": request_data["message_id"],
            "for_digest": "0" * 64,
            "stage": stage,
            "attempt": 1,
            "receiver_generation": 1,
            "outcome": outcome,
            "reason": reason,
            "result": result,
        },
    }
    envelope = seal_envelope(request_data)
    assert decode_envelope(encode_envelope(envelope)).payload.data["stage"] == stage
    request_data["receipt_policy"] = "terminal"
    with pytest.raises(MailboxError):
        seal_envelope(request_data)


def test_event_requires_explicit_registered_schema(request_data):
    request_data["kind"] = "event"
    request_data["payload"] = {
        "content_type": "application/json",
        "schema": "example.event/1",
        "data": {"arbitrary": {"extension": True}},
    }
    with pytest.raises(MailboxError) as error:
        seal_envelope(request_data)
    assert error.value.code == "unsupported_payload_schema"
    envelope = seal_envelope(request_data, event_schemas={"example.event/1"})
    assert (
        decode_envelope(encode_envelope(envelope, event_schemas={"example.event/1"}), event_schemas={"example.event/1"})
        == envelope
    )


@pytest.mark.parametrize("raw", [b"\xff", b"\xef\xbb\xbf{}", b'{"x":1e999}', b'{"x":1,"\\u0078":2}'])
def test_invalid_encoding_and_escaped_duplicate_keys(raw):
    with pytest.raises(MailboxError):
        decode_envelope(raw)


def test_strings_do_not_count_as_nesting(request_data):
    request_data["extensions"] = {"brackets": "[" * 100 + '\\"' + "]" * 100}
    assert seal_envelope(request_data).extensions == request_data["extensions"]


def test_safe_integer_boundaries_and_control_escapes():
    assert canonical_bytes([9007199254740991, -9007199254740991]) == b"[9007199254740991,-9007199254740991]"
    assert canonical_bytes("\x00\b\f\n\r\t\x7f/") == b'"\\u0000\\b\\f\\n\\r\\t\\u007f/"'


@pytest.mark.parametrize("mutation", ["missing", "null_sender", "scope", "uuid", "artifact_size", "ttl", "acceptance"])
def test_additional_field_constraints(request_data, mutation):
    if mutation == "missing":
        del request_data["extensions"]
    elif mutation == "null_sender":
        request_data["sender_identity"]["instance_id"] = None
    elif mutation == "scope":
        request_data["scope"]["task_id"] = "\u00e9"
    elif mutation == "uuid":
        request_data["trace_id"] = "invalid"
    elif mutation == "artifact_size":
        request_data["artifacts"] = [
            {"sha256": "0" * 64, "size": 16777217, "media_type": "text/plain", "name": "report"}
        ]
    elif mutation == "ttl":
        request_data["ttl"] = 604801
    else:
        request_data["payload"]["data"]["acceptance"] = [1]
    with pytest.raises(MailboxError):
        seal_envelope(request_data)


@pytest.mark.parametrize("container", [list, dict])
def test_empty_container_depth_boundary(container):
    value = container()
    for _ in range(31):
        value = [value] if container is list else {"x": value}
    canonical_bytes(value)
    value = [value] if container is list else {"x": value}
    with pytest.raises(MailboxError) as error:
        canonical_bytes(value)
    assert error.value.code == "too_deep"


def test_wire_byte_boundary(request_data):
    wire = encode_envelope(seal_envelope(request_data))
    bounded = wire + b" " * (65536 - len(wire))
    assert decode_envelope(bounded).message_id == request_data["message_id"]
    with pytest.raises(MailboxError) as error:
        decode_envelope(bounded + b" ")
    assert error.value.code == "envelope_too_large"


def test_extremely_long_integer_is_protocol_error():
    with pytest.raises(MailboxError) as error:
        decode_envelope(b'{"x":' + b"9" * 5000 + b"}")
    assert error.value.code == "integer_out_of_range"


def test_task_deadline_application_extension_round_trip(request_data):
    request_data["payload"]["data"]["task_deadline"] = "2026-10-03T00:00:00Z"
    request_data["payload"]["data"]["metadata"] = {"revision": 1}
    envelope = seal_envelope(request_data)
    assert decode_envelope(encode_envelope(envelope)).payload.data == request_data["payload"]["data"]


def test_handoff_decision_edges_round_trip(request_data):
    request_data["kind"] = "handoff.offer"
    request_data["payload"] = {
        "content_type": "application/json",
        "schema": "opena2a.handoff/1",
        "data": {
            "handoff_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "mode": "offer",
            "task_ref": "TASK-42",
            "source_sessions": [],
            "objective": "Review changes",
            "effective_decisions": [
                {
                    "decision_id": "d1",
                    "branch_id": "transport",
                    "status": "confirmed",
                    "text": "Use the local mailbox",
                    "scope": "review",
                    "source_refs": ["human:1"],
                    "confirmed_by": "human",
                    "supersedes": [],
                    "depends_on": ["d0"],
                    "derived_from": ["proposal0"],
                }
            ],
            "rejected_alternatives": [],
            "unresolved_items": [],
            "work_state": {"done": [], "next_actions": [], "checkpoint": "review"},
            "environment": {"repo_id": "review", "commit": None, "dirty_patch_sha256": None},
            "required_artifacts": [],
            "acceptance": [],
            "authority": {"owner": "task-owner", "transfer_required": True},
        },
    }
    envelope = seal_envelope(request_data)
    assert decode_envelope(encode_envelope(envelope)).payload.data == request_data["payload"]["data"]


@pytest.mark.parametrize("value", [1.5, {"\u00e9": 1}, "\ud800", 9007199254740992])
def test_application_extensions_still_obey_json_profile(request_data, value):
    request_data["payload"]["data"]["extension"] = value
    with pytest.raises(MailboxError):
        seal_envelope(request_data)
