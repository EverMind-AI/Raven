"""The ACP wire layer as the agent direction needs it.

Framing is not re-implemented: :mod:`raven.agent.acp.protocol` already encodes
and decodes newline-delimited JSON-RPC, byte for byte the same as
``raven/rpc/server.py`` does, and has been run against real agents. It is
imported rather than copied, and rather than moved -- moving it would touch 78
tests belonging to the client direction for no user-visible gain.

What is added here is what only the agent direction needs:

* an ``id`` that may be a string. The client mints request ids, and JSON-RPC
  allows a string; the client-direction helper types it ``int`` because raven
  was the one minting. A client sending ``"id": "req-1"`` is legal and must be
  answered on the same id.
* ``data`` on an error. The ACP error codes carry structured detail (which
  session was not found, which field was rejected), and a client showing a
  person why the handshake failed needs more than a sentence.
* the ACP-assigned codes, which are not JSON-RPC's: ``-32000`` auth required,
  ``-32002`` resource not found, ``-32800`` request cancelled.
* version normalisation. ``protocolVersion`` is a required integer, and a client
  that sends ``"1"`` or ``1.0`` is malformed -- but failing the handshake over a
  type is a worse outcome than reading the intent, and the spec's own guidance
  for initialize is to be tolerant. A version we cannot read at all falls back
  to the latest we support, which is the same answer the spec prescribes for a
  version we do not support.
"""

from __future__ import annotations

from typing import Any

from raven.agent.acp.protocol import (
    AcpProtocolError,
    decode,
    encode,
    notification,
    result_response,
)

PROTOCOL_VERSION = 1
"""The ACP major version this agent implements.

One, not two: ``schema/v2`` exists upstream but is a draft, no shipping agent
answers ``protocolVersion: 2``, and the official guidance is to gate a v2 path
behind both version negotiation and a feature flag. Advertising a version we do
not serve is the failure mode that guidance exists to prevent.
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
"""Every value ``PromptResponse.stopReason`` may take.

Two of the five have no source in raven and are therefore never sent:
``max_tokens`` and ``max_turn_requests`` describe limits the agent loop does not
report hitting. Kept in the set because it is the *schema's* enum, and a
translator that latched one of them would be caught by the schema check rather
than by this constant.
"""


def request(request_id: int | str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """An outbound request frame.

    Widened from the client direction's ``int``: this side both answers ids it
    did not mint and mints its own, and a single helper for both keeps one
    encoder in the process.
    """
    frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def error_response(
    request_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """A JSON-RPC error, with ``data`` only when there is something to say.

    Absent rather than null: the schema types ``data`` as optional, and a null
    reads to a client as "there is detail and it is empty" rather than as "there
    is no detail".
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def normalize_protocol_version(raw: Any) -> int:
    """Read a client's ``protocolVersion`` as generously as is still honest.

    Accepted: an integer; a float that is exactly an integer (``1.0``); a string
    of digits (``"1"``). Anything else -- absent, null, a word, a fraction --
    yields :data:`PROTOCOL_VERSION`, because the spec's answer to a version the
    agent does not support is to reply with the version it does support and let
    the client decide whether to disconnect. Erroring instead would deny the
    client the information it needs to make that decision.

    ``bool`` is rejected even though it is an ``int`` subclass: ``True`` would
    normalise to version 1 by accident, which is a coincidence rather than a
    reading of intent.
    """
    if isinstance(raw, bool):
        return PROTOCOL_VERSION
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        # ``isdecimal`` and not ``isdigit``, measured: ``"\u00b2"`` (superscript two)
        # satisfies ``isdigit`` and makes ``int()`` raise, so the looser check
        # would guard nothing. ``isdecimal`` accepts exactly the alphabets
        # ``int()`` parses -- which includes non-ASCII decimal digits, so
        # ``"\u0661\u0662"`` reads as 12. That is the tolerance this function is
        # for, not a hole in it.
        if text.isdecimal():
            return int(text)
    return PROTOCOL_VERSION


def negotiated_version(requested: Any) -> int:
    """The version to answer ``initialize`` with.

    The client's version when this agent implements it, else this agent's own --
    exactly what the schema's ``protocolVersion`` field documents. An older
    client asking for 0 gets 0 back only if we served it; we do not, so it gets
    1 and can disconnect knowingly.
    """
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
