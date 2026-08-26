"""Wire-layer contract: framing helpers and version negotiation."""

import pytest

from raven.acp import protocol


def test_encode_is_one_line_utf8():
    frame = {"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {"text": "你好\n再见"}}
    raw = protocol.encode(frame)
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1  # embedded newlines are escaped, framing is safe
    assert "你好" in raw.decode("utf-8")  # ensure_ascii=False
    assert protocol.decode(raw.decode("utf-8").rstrip("\n")) == frame


def test_decode_rejects_non_json_and_non_object():
    with pytest.raises(protocol.AcpProtocolError):
        protocol.decode("not json")
    with pytest.raises(protocol.AcpProtocolError):
        protocol.decode("[1, 2]")


def test_request_accepts_string_ids():
    frame = protocol.request("req-1", "initialize", {"a": 1})
    assert frame["id"] == "req-1"
    assert frame["params"] == {"a": 1}
    assert "params" not in protocol.request(2, "x")


def test_error_response_data_absent_not_null():
    assert "data" not in protocol.error_response(1, -32600, "bad")["error"]
    assert protocol.error_response(1, -32600, "bad", {"k": "v"})["error"]["data"] == {"k": "v"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1, 1),
        (2, 2),
        (1.0, 1),
        ("1", 1),
        (" 1 ", 1),
        (True, protocol.PROTOCOL_VERSION),  # bool is not a version
        (1.5, protocol.PROTOCOL_VERSION),
        ("one", protocol.PROTOCOL_VERSION),
        (None, protocol.PROTOCOL_VERSION),
        ("²", protocol.PROTOCOL_VERSION),  # isdigit-but-not-decimal must not reach int()
    ],
)
def test_normalize_protocol_version(raw, expected):
    assert protocol.normalize_protocol_version(raw) == expected


def test_negotiated_version_answers_own_version_for_unsupported():
    assert protocol.negotiated_version(protocol.PROTOCOL_VERSION) == protocol.PROTOCOL_VERSION
    assert protocol.negotiated_version(0) == protocol.PROTOCOL_VERSION
    assert protocol.negotiated_version(99) == protocol.PROTOCOL_VERSION


def test_stop_reasons_is_the_schema_enum():
    assert protocol.STOP_REASONS == {"end_turn", "cancelled", "refusal", "max_tokens", "max_turn_requests"}
