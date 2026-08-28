"""The ACP wire layer: newline-delimited JSON-RPC 2.0, plus ACP's own additions.

Framing is one complete JSON object per line, in both directions, verified
against real ACP servers on the client side of this protocol. It is reproduced
here rather than imported because the client direction (``raven.agent.acp``)
is not part of this checkout -- see ``docs/acp-server-design.md`` section 7.

What the agent direction needs beyond plain JSON-RPC:

* an ``id`` that may be a string. The client mints request ids and JSON-RPC
  allows a string, so a client sending ``"id": "req-1"`` is legal and must be
  answered on the same id.
* ``data`` on an error. The ACP codes carry structured detail (which session was
  not found, which field was rejected), and a client showing a person why the
  handshake failed needs more than a sentence.
* the ACP-assigned codes, which are not JSON-RPC's: ``-32000`` auth required,
  ``-32002`` resource not found, ``-32800`` request cancelled.
* version normalisation. ``protocolVersion`` is a required integer, and a client
  that sends ``"1"`` or ``1.0`` is malformed -- but failing the handshake over a
  type is a worse outcome than reading the intent, and the spec's own guidance
  for initialize is to be tolerant. A version that cannot be read at all falls
  back to the latest supported, which is what the spec prescribes for a version
  the agent does not support.
"""

from __future__ import annotations

import json
from typing import Any

SESSION_MCP_CAPABILITY = "raven.mcp.session"
"""Declared under ``agentCapabilities._meta`` when a session may bring its own MCP servers.

The spec has nowhere to say this: stdio servers are the ACP baseline, so
accepting them cannot be advertised, and ``mcpCapabilities`` says nothing about
isolation. A build that answers ``mcpServers`` with ``-32602`` is otherwise
indistinguishable from one that honours it, so a client reading no declaration
has to assume the refusal.
"""

PROTOCOL_VERSION = 1
"""The ACP major version this agent implements.

One, not two: ``schema/v2`` exists upstream but is a draft and no shipping agent
answers ``protocolVersion: 2``. Advertising a version that is not served is the
failure this whole module's tolerance exists to avoid.
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

# The notification a caller sends to retract a request it has stopped waiting
# for. Outbound only: this agent sends it when an ask it made of the client
# times out or is cancelled, so the client can take the form back off the
# screen instead of holding it for the rest of its own budget.
CANCEL_REQUEST_METHOD = "$/cancel_request"

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

Three of the five have no source here and are never sent: ``refusal`` needs a
runtime notice this surface does not raise, and ``max_tokens`` /
``max_turn_requests`` describe limits the agent loop does not report hitting.
Kept as the schema's whole enum so a translator that latched one of them is
caught by a test against this set rather than by a client.
"""


class AcpProtocolError(Exception):
    """The peer sent something that is not a usable JSON-RPC frame."""


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


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """A JSON-RPC error, with ``data`` only when there is something to say.

    Absent rather than null: the schema types ``data`` as optional, and a null
    reads to a client as "there is detail and it is empty" rather than as "there
    is no detail".
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def session_update(session_id: str, update: dict[str, Any]) -> dict[str, Any]:
    """One ``session/update`` notification for a session's stream."""
    return notification("session/update", {"sessionId": session_id, "update": update})


def normalize_protocol_version(raw: Any) -> int:
    """Read a client's ``protocolVersion`` as generously as is still honest.

    Accepted: an integer; a float that is exactly an integer (``1.0``); a string
    of digits (``"1"``). Anything else -- absent, null, a word, a fraction --
    yields :data:`PROTOCOL_VERSION`, because the spec's answer to a version the
    agent does not support is to reply with the version it does support and let
    the client decide whether to disconnect.

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
        # ``isdecimal`` and not ``isdigit``: superscript two satisfies ``isdigit``
        # and makes ``int()`` raise, so the looser check would guard nothing.
        text = raw.strip()
        if text.isdecimal():
            return int(text)
    return PROTOCOL_VERSION


def negotiated_version(requested: Any) -> int:
    """The version to answer ``initialize`` with.

    The client's version when this agent implements it, else this agent's own --
    exactly what the schema's ``protocolVersion`` field documents. An older
    client asking for 0 gets 1 back and can disconnect knowingly.
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
    "SESSION_MCP_CAPABILITY",
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
    "session_update",
]
