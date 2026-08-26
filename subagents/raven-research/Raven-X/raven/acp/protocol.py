"""The ACP wire layer: newline-delimited JSON-RPC 2.0 framing and codes.

Ported from the main raven repo's ``raven/acp/protocol.py`` merged with the
framing helpers it imported from the client direction (``raven/agent/acp/
protocol.py``); Raven-X has no client direction, so both halves live here.

Two agent-direction widenings over plain JSON-RPC helpers:

* an ``id`` may be a string. The client mints request ids and JSON-RPC allows
  strings; a client sending ``"id": "req-1"`` is legal and must be answered on
  the same id.
* ``data`` on an error. ACP error codes carry structured detail (which session
  was not found, which field was rejected).
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
"""The ACP major version this agent implements.

One, not two: ``schema/v2`` is a draft upstream and no shipping agent answers
``protocolVersion: 2``. Advertising a version we do not serve is the failure
mode version negotiation exists to prevent.
"""

# JSON-RPC's own codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# ACP's additions, from the specification's error table.
AUTH_REQUIRED = -32000
RESOURCE_NOT_FOUND = -32002
REQUEST_CANCELLED = -32800

STOP_REASONS = frozenset(
    {
        "end_turn",
        "cancelled",
        "refusal",
        "max_tokens",
        "max_turn_requests",
    }
)
"""Every value ``PromptResponse.stopReason`` may take (the schema's enum).

Raven-X sends only ``end_turn`` and ``cancelled``; the rest are kept so a
schema check catches a translator latching a reason with no source here.
"""


class AcpProtocolError(Exception):
    """A wire line that is not a usable JSON-RPC frame."""


def encode(frame: dict[str, Any]) -> bytes:
    """Serialise one frame for the wire.

    ``ensure_ascii=False`` so a non-ASCII prompt is not inflated into escapes;
    ``json.dumps`` escapes embedded newlines, which is what makes line framing
    safe.
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


def request(request_id: int | str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(
    request_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """A JSON-RPC error, with ``data`` only when there is something to say.

    Absent rather than null: the schema types ``data`` as optional, and a null
    reads to a client as "there is detail and it is empty".
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def normalize_protocol_version(raw: Any) -> int:
    """Read a client's ``protocolVersion`` as generously as is still honest.

    Accepted: an integer; a float that is exactly an integer (``1.0``); a
    string of decimal digits (``"1"``). Anything else yields
    :data:`PROTOCOL_VERSION`: the spec's answer to a version the agent cannot
    read is to reply with the version it does support and let the client decide
    whether to disconnect.

    ``bool`` is rejected even though it is an ``int`` subclass: ``True``
    normalising to version 1 would be a coincidence, not a reading of intent.
    ``isdecimal`` and not ``isdigit``: ``isdigit`` accepts characters
    (superscript two) that make ``int()`` raise.
    """
    if isinstance(raw, bool):
        return PROTOCOL_VERSION
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdecimal():
            return int(text)
    return PROTOCOL_VERSION


def negotiated_version(requested: Any) -> int:
    """The version to answer ``initialize`` with: the client's when this agent
    implements it, else this agent's own -- exactly what the schema's
    ``protocolVersion`` field documents."""
    version = normalize_protocol_version(requested)
    return version if version == PROTOCOL_VERSION else PROTOCOL_VERSION


__all__ = [
    "AUTH_REQUIRED",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "REQUEST_CANCELLED",
    "RESOURCE_NOT_FOUND",
    "STOP_REASONS",
    "AcpProtocolError",
    "decode",
    "encode",
    "error_response",
    "negotiated_version",
    "normalize_protocol_version",
    "notification",
    "request",
    "result_response",
]
