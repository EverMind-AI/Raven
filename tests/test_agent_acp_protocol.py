"""The ACP wire framing, in both directions.

Small functions, and the reason they are pinned is that both directions share
them: a frame that round-trips through one serialiser and not the other is the
kind of bug that only shows up against somebody else's implementation, by which
point the evidence is a remote parse error with no local trace.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.acp import protocol


def test_a_frame_is_one_line_and_the_newline_is_part_of_it() -> None:
    """The framing IS the newline: a reader splits on it, so a writer that omits
    it produces two frames glued together and a reader that adds a second one
    produces an empty frame between them."""
    out = protocol.encode({"jsonrpc": "2.0", "id": 1, "method": "initialize"})

    assert out.endswith(b"\n")
    assert out.count(b"\n") == 1
    assert protocol.decode(out.decode().rstrip("\n"))["method"] == "initialize"


def test_non_ascii_survives_the_round_trip() -> None:
    """Escaping non-ASCII would still be legal JSON, which is exactly why this is
    pinned: the two directions have to agree, and a mismatch shows up as a
    mangled path or message rather than as a parse failure."""
    frame = {"jsonrpc": "2.0", "id": 1, "params": {"path": "/tmp/测试.txt"}}

    assert protocol.decode(protocol.encode(frame).decode())["params"]["path"] == "/tmp/测试.txt"


@pytest.mark.parametrize("line", ["", "   ", "not json", "{", '{"jsonrpc": "2.0"', "[1, 2]", '"a string"'])
def test_anything_that_is_not_a_json_object_is_a_protocol_error(line: str) -> None:
    """One error type for every unusable line, because the caller's answer is the
    same for all of them: this peer is not speaking the protocol. A bare
    ValueError or a TypeError from indexing would each need their own handler."""
    with pytest.raises(protocol.AcpProtocolError):
        protocol.decode(line)


def test_the_error_types_separate_answers_a_caller_gives_differently() -> None:
    """A remote error is the agent saying no, a protocol error is the agent saying
    something unparseable, and a connection error is the agent not being there.
    A caller reports the first two and retries none of them the same way, so they
    cannot collapse into one class."""
    assert issubclass(protocol.AcpConnectionError, protocol.AcpError)
    assert issubclass(protocol.AcpProtocolError, protocol.AcpError)
    assert issubclass(protocol.AcpRemoteError, protocol.AcpError)
    assert issubclass(protocol.AcpTimeoutError, protocol.AcpError)

    err = protocol.AcpRemoteError("session/new", -32602, "Invalid params", {"field": "cwd"})
    assert err.method == "session/new"
    assert err.code == -32602
    assert err.data == {"field": "cwd"}
    assert "session/new" in str(err) and "-32602" in str(err)


def test_a_request_carries_params_only_when_there_are_some() -> None:
    """Absent rather than null: a peer that validates its params against a schema
    can reject an explicit null for a method whose params are optional."""
    assert protocol.request(1, "session/cancel") == {"jsonrpc": "2.0", "id": 1, "method": "session/cancel"}
    assert protocol.request(2, "m", {"a": 1})["params"] == {"a": 1}


def test_a_notification_carries_no_id_at_all() -> None:
    """The one structural difference that decides whether a peer answers: an id
    means a reply is expected, and a notification with one makes the peer wait
    for an answer nobody will send."""
    note = protocol.notification("session/update", {"sessionId": "s"})

    assert "id" not in note
    assert note["method"] == "session/update"
    assert protocol.notification("m") == {"jsonrpc": "2.0", "method": "m"}


def test_a_response_is_a_result_or_an_error_and_never_both() -> None:
    ok = protocol.result_response(1, {"sessionId": "s"})
    bad = protocol.error_response(1, -32601, "Method not found")

    assert "error" not in ok and ok["result"] == {"sessionId": "s"}
    assert "result" not in bad and bad["error"] == {"code": -32601, "message": "Method not found"}
    # The id has to survive verbatim, including a string one: a peer pairs its
    # pending call by identity, so coercing the type strands the call.
    assert protocol.error_response("stub-2", -1, "x")["id"] == "stub-2"


def test_the_handshake_params_carry_only_what_was_measured_to_be_accepted() -> None:
    """A third field was tried against a real agent and answered with -32602. The
    fix was to stop sending it rather than to guess at its shape, so the set is
    pinned here: an optional-looking extra that hard-fails a handshake is the
    worst kind of protocol guess."""
    params = protocol.initialize_params()

    assert set(params) == {"protocolVersion", "clientCapabilities"}
    assert params["protocolVersion"] == protocol.PROTOCOL_VERSION
    # Serialisable as it stands: this goes straight into a frame.
    json.dumps(params)
