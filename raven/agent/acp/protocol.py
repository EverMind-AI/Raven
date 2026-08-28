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
    # Form only. `url` elicitation is for out-of-band OAuth, payment and
    # credential collection, so advertising it would let a sub-agent send the
    # user to an arbitrary URL to enter them. The two are independently
    # advertisable, so omitting one is a supported subset rather than a
    # half-honoured capability.
    "elicitation": {"form": {}},
    # Extension surface, not spec: Raven-X routes ask_user through an
    # `ask_user_request` session update answered by `_raven/clarify_respond`
    # rather than through elicitation, and arms that route only when the CLIENT
    # declares it here. Declared under `_meta` so a spec-only agent cannot
    # collide with it, and honoured by `raven/agent/acp/ask_user.py` -- a True
    # raven did not serve would be a question put to nobody, answered by the
    # agent's own 600s fail-safe.
    "_meta": {"raven": {"askUser": True}},
}

METHOD_NOT_FOUND = -32601
"""JSON-RPC's own code, used to answer an agent-initiated request raven does not
implement. An explicit error keeps the agent moving; silence would hang it."""

CANCEL_REQUEST_METHOD = "$/cancel_request"
"""An agent taking back a request whose answer it will no longer read.

Protocol-level and explicitly optional -- a receiver MAY act on it -- so it
carries no id of its own and nothing answers it. Worth acting on all the same:
the requests it retracts here are the ones raven puts in front of a person, and
the retraction is the only signal that the run behind the question has stopped
listening."""


# -- the extensions a raven serves over ACP -----------------------------------
#
# Schema 1.20.0 has no steering method: a prompt is the only way text reaches an
# agent, and two prompts on one session are refused because ``session/update``
# carries no request correlation. So mid-turn steering has to be an extension,
# and the schema's own convention for one is an underscore-prefixed method name
# -- the single namespace a conformant client will not mistake for a standard
# method it should have implemented.
#
# Paired: the method is announced in ``agentCapabilities._meta`` under
# ``STEER_CAPABILITY`` and is invisible to a client that did not read the
# declaration, which is the point. Both directions live here -- the agent
# direction serves the method, the client direction calls it -- so a typo cannot
# make one side silently disagree with the other.
STEER_METHOD = "_raven/session/steer"
STEER_CAPABILITY = "raven.steer"

# The other one, and a promise rather than a feature flag: a raven that declares
# this says that ``mcpServers`` on ``session/new`` / ``session/load`` /
# ``session/resume`` is really connected, and that the tools behind it are
# visible to that session's turns alone -- neither the definitions nor the tools
# outlive the session, and no sibling session on the same connection can reach
# them.
#
# It has to be an extension because the spec has no field for either half.
# stdio servers are the ACP baseline, so accepting them is not advertisable at
# all, and ``mcpCapabilities`` carries nothing about isolation -- measured,
# claude-agent-acp and opencode report the same ``{http, sse}`` object while only
# claude isolates. So a raven that answers the field with ``-32602`` is
# indistinguishable on the wire from one that honours it, and that gap is the
# only thing this closes. It is not a gate on handing any agent a bridge: a
# bridge stanza is an ordinary stdio server, and its per-session lifetime is the
# host's socket lifetime, which needs no agreement from the far side.
SESSION_MCP_CAPABILITY = "raven.mcp.session"


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
    "CANCEL_REQUEST_METHOD",
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
