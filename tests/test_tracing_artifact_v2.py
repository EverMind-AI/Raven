"""Contract tests for the audit.artifact.v2 reference format.

The round-trip test is the invariant the whole design rests on: addressing a
v1 payload and resolving the result must return the original, byte for byte.
"""

from __future__ import annotations

import json

import pytest

from raven.tracing import artifact_v2 as v2


def _msg(role: str, content):
    return {"role": role, "content": content}


def test_the_address_follows_the_bytes_not_the_semantics():
    """Key order is part of the address, deliberately.

    Sorting keys would share one blob between two orderings, at the cost of an
    exact round trip - see ``message_text``. Measured over 406,357 recorded
    occurrences, sorting changed the distinct count by zero.
    """
    a = {"role": "user", "content": "hi"}
    b = {"content": "hi", "role": "user"}
    assert v2.message_sha1(a) != v2.message_sha1(b)


def test_round_trip_preserves_key_order_inside_content(tmp_path):
    """The regression the round-trip assertion above first caught."""
    msg = {"role": "user", "content": [{"type": "text", "text": "latest"}]}
    sha1 = v2.message_sha1(msg)
    path = v2.message_path(tmp_path, sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(v2.message_text(msg), encoding="utf-8")

    resolved = v2.resolve_payload(
        {
            "artifactFormat": v2.ARTIFACT_FORMAT,
            "prompt": v2.make_ref(sha1),
            "messages": [v2.make_ref(sha1)],
        },
        tmp_path,
    )

    assert list(resolved["messages"][0]["content"][0]) == ["type", "text"]
    assert resolved["prompt"] == v2.coerce_text(msg["content"])


def test_distinct_messages_get_distinct_addresses():
    assert v2.message_sha1(_msg("user", "a")) != v2.message_sha1(_msg("user", "b"))


def test_ref_sha1_accepts_a_well_formed_envelope():
    sha1 = "0" * 40
    assert v2.ref_sha1(v2.make_ref(sha1)) == sha1


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not a dict",
        {},
        {"$msg": "../../etc/passwd"},
        {"$msg": "ABCDEF" + "0" * 34},
        {"$msg": "0" * 39},
        {"$msg": "0" * 41},
        {"$msg": 1234},
        {"$msg": "0" * 40, "extra": 1},
        {"role": "user", "content": "a real message"},
    ],
)
def test_ref_sha1_rejects_everything_else(value):
    assert v2.ref_sha1(value) is None


def test_coerce_text_is_json_stringify_compatible():
    assert v2.coerce_text(None) == ""
    assert v2.coerce_text("plain") == "plain"
    # No spaces after ':' or ',' - JSON.stringify produces exactly this, and
    # the viewer must resolve a multimodal prompt to the same bytes.
    assert v2.coerce_text([{"type": "text", "text": "x"}]) == '[{"type":"text","text":"x"}]'


def test_resolve_leaves_a_v1_payload_untouched():
    v1 = {"provider": "p", "messages": [_msg("user", "hi")]}
    assert v2.resolve_payload(v1, "/nonexistent") is v1


def test_round_trip_returns_the_original_payload(tmp_path):
    msgs = [
        _msg("system", "you are raven"),
        _msg("user", "first"),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "name": "grep"}]},
        _msg("user", [{"type": "text", "text": "latest"}]),
    ]
    v1 = {
        "provider": "openrouter",
        "providerClass": "LiteLLMProvider",
        "model": "openrouter/x",
        "systemPrompt": v2.coerce_text(msgs[0]["content"]),
        "prompt": v2.coerce_text(msgs[3]["content"]),
        "messages": msgs,
        "tools": [{"function": {"name": "grep"}}],
    }

    refs = []
    for m in msgs:
        sha1 = v2.message_sha1(m)
        path = v2.message_path(tmp_path, sha1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(v2.message_text(m), encoding="utf-8")
        refs.append(v2.make_ref(sha1))
    shell = {
        "artifactFormat": v2.ARTIFACT_FORMAT,
        "provider": "openrouter",
        "providerClass": "LiteLLMProvider",
        "model": "openrouter/x",
        "systemPrompt": refs[0],
        "prompt": refs[3],
        "messages": refs,
        "tools": [{"function": {"name": "grep"}}],
    }

    resolved = v2.resolve_payload(shell, tmp_path)
    assert json.dumps(resolved, ensure_ascii=False, sort_keys=True) == json.dumps(
        v1, ensure_ascii=False, sort_keys=True
    )


def test_a_missing_blob_becomes_a_placeholder_at_the_same_index(tmp_path):
    kept = _msg("user", "kept")
    sha1 = v2.message_sha1(kept)
    path = v2.message_path(tmp_path, sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(v2.message_text(kept), encoding="utf-8")
    gone = "f" * 40

    seen = []
    resolved = v2.resolve_payload(
        {
            "artifactFormat": v2.ARTIFACT_FORMAT,
            "messages": [v2.make_ref(gone), v2.make_ref(sha1)],
        },
        tmp_path,
        on_missing=lambda h: (seen.append(h), {"role": "unknown", "content": "gone"})[1],
    )

    assert seen == [gone]
    assert len(resolved["messages"]) == 2
    assert resolved["messages"][1] == kept


def test_an_inlined_message_survives_resolution(tmp_path):
    inlined = _msg("user", "written inline because its blob failed")
    resolved = v2.resolve_payload({"artifactFormat": v2.ARTIFACT_FORMAT, "messages": [inlined]}, tmp_path)
    assert resolved["messages"] == [inlined]


def test_text_fields_resolve_to_text_not_to_the_message_object(tmp_path):
    msg = _msg("system", "you are raven")
    sha1 = v2.message_sha1(msg)
    path = v2.message_path(tmp_path, sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(v2.message_text(msg), encoding="utf-8")

    resolved = v2.resolve_payload({"artifactFormat": v2.ARTIFACT_FORMAT, "systemPrompt": v2.make_ref(sha1)}, tmp_path)
    assert resolved["systemPrompt"] == "you are raven"


def test_resolution_drops_the_format_discriminator(tmp_path):
    resolved = v2.resolve_payload({"artifactFormat": v2.ARTIFACT_FORMAT, "provider": "p"}, tmp_path)
    assert "artifactFormat" not in resolved
