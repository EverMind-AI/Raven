"""Wire framing and error types for ACP over stdio.

Framing is newline-delimited JSON-RPC 2.0: one complete JSON object per line, in
both directions. Verified against a real ``hermes acp`` server, which answers a
single-line ``initialize`` request with a single-line result.

The error types exist so callers can tell apart failures that mean different
things. A remote error is the agent saying no; a protocol error is the agent
saying something raven cannot parse; a connection error is the agent not being
there at all. Registration reports these as ``needs_auth`` / ``unreachable``
differently, and a dispatch retries none of them the same way.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
"""The ACP protocol version raven advertises in ``initialize``.

Measured: ``hermes acp`` (adapter 0.17.0) answers ``protocolVersion: 1``. An
agent replying with a different number is not rejected here -- the number is
recorded in the snapshot so a mismatch is visible to the operator rather than
fatal at connect time.
"""

CLIENT_CAPABILITIES: dict[str, Any] = {
    # Declared false because raven does not yet serve these back. Advertising a
    # capability it cannot honour is worse than not having it: the agent would
    # route file access through raven and stall on a method that answers with an
    # error. Flipping either to true is the approval work, not this layer's.
    "fs": {"readTextFile": False, "writeTextFile": False},
}

METHOD_NOT_FOUND = -32601
"""JSON-RPC's own code, used to answer an agent-initiated request raven does not
implement. An explicit error keeps the agent moving; silence would hang it."""


class AcpError(Exception):
    """Base for every ACP transport failure."""


class AcpConnectionError(AcpError):
    """The agent process could not be started, or the connection died."""


class AcpTimeoutError(AcpError):
    """A request went unanswered within its budget."""


class AcpProtocolError(AcpError):
    """The agent sent something that is not a usable JSON-RPC frame."""


class AcpRemoteError(AcpError):
    """The agent answered a request with a JSON-RPC error object."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{method} failed: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message
        self.data = data


def encode(frame: dict[str, Any]) -> bytes:
    """Serialise one frame for the wire.

    ``ensure_ascii=False`` so a non-ASCII prompt is not inflated into escapes,
    and no embedded newline can appear because ``json.dumps`` escapes them --
    which is what makes line framing safe.
    """
    return (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: str) -> dict[str, Any]:
    """Parse one wire line, raising :class:`AcpProtocolError` on anything else."""
    try:
        frame = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AcpProtocolError(f"not JSON: {line[:200]!r}") from exc
    if not isinstance(frame, dict):
        raise AcpProtocolError(f"not a JSON object: {line[:200]!r}")
    return frame


def request(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def initialize_params() -> dict[str, Any]:
    """Exactly the two params measured to be accepted, and nothing else.

    A third ``clientInfo`` field was tried and rejected: ``hermes acp`` answered
    ``-32602 Invalid params``. Since nothing here needs to identify raven to the
    agent, the fix is to not send it rather than to guess at its shape -- an
    optional-looking extra that hard-fails the handshake is the worst kind of
    protocol guess.
    """
    return {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": CLIENT_CAPABILITIES}


__all__ = [
    "CLIENT_CAPABILITIES",
    "METHOD_NOT_FOUND",
    "PROTOCOL_VERSION",
    "AcpConnectionError",
    "AcpError",
    "AcpProtocolError",
    "AcpRemoteError",
    "AcpTimeoutError",
    "decode",
    "encode",
    "error_response",
    "initialize_params",
    "notification",
    "request",
    "result_response",
]
