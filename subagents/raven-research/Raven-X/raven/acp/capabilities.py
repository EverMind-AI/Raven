"""What this agent tells the client it can do, and what the client told us.

Every flag here is false unless something downstream actually honours it, and
each one names what would have to exist for it to flip. An agent that declares
a capability it does not serve produces the worst failure shape in the
protocol: the client routes work through a method that answers with an error,
and the turn stalls on a promise nobody will keep.

Decision A (ACP_INTEGRATION_PLAN.md §6): ``loadSession: true`` plus
``sessionCapabilities: {"resume": {}}``, declared together and only because
both methods exist -- the consuming raven derives statefulness from
``sessionCapabilities.resume`` (key presence) and gates the actual
``session/load`` on ``loadSession``, so dropping either silently makes every
task open a fresh session. ``list`` / ``close`` / ``delete`` / ``fork`` stay
undeclared: each declared key is a method that must then work, and the
consuming raven calls none of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.acp import protocol

AGENT_NAME = "raven-x-research"
"""The ``agentInfo.name`` for the handshake.

Load-bearing beyond identification: the consuming raven picks its ACP dialect
by substring match on this name (``acp_dialects/dialect_for``), so it must
never contain "claude" or "codex" -- either would route our frames through a
third-party dialect. Pinned by a test; do not rename casually.
"""


def _agent_info() -> dict[str, str]:
    # Read rather than hardcoded so it cannot claim a version this build is not.
    from raven import __version__

    return {"name": AGENT_NAME, "version": str(__version__ or "0.0.0")}


def agent_capabilities() -> dict[str, Any]:
    """The ``AgentCapabilities`` object sent in the initialize result."""
    return {
        # A promise kept by AcpMethods._session_load: the transcript is
        # replayed as session/update notifications before the load returns.
        "loadSession": True,
        # The one key the consuming raven reads for statefulness (presence,
        # not truthiness); backed by _session_resume, which is _session_load
        # minus the replay. An empty object is how the schema spells
        # "supported".
        "sessionCapabilities": {"resume": {}},
        # Text is not advertised -- every ACP agent takes text, and
        # promptCapabilities lists only the extras. All false: images and
        # embedded context have no storage/inline path on this agent yet, and
        # the consuming raven sends plain text.
        "promptCapabilities": {
            "image": False,
            "audio": False,
            "embeddedContext": False,
        },
        # MCP is connected once per process from raven's own config; nothing
        # scopes a server to one session. Declaring http/sse would invite
        # exactly the request that has to be refused.
        "mcpCapabilities": {"http": False, "sse": False},
        # No auth: nothing to log out of either.
        "auth": {},
    }


def initialize_result(params: dict[str, Any] | None) -> dict[str, Any]:
    """The full ``InitializeResponse`` for a client's initialize request.

    Tolerant by construction: unknown params are ignored, and a
    ``protocolVersion`` of the wrong type is read for intent. The one thing
    this must not do is fail -- a client that cannot complete initialize has no
    surface to show a person why.

    ``authMethods: []`` is a positive statement: this agent needs no
    authentication, which lets a client proceed straight to ``session/new``.
    """
    requested = (params or {}).get("protocolVersion")
    return {
        "protocolVersion": protocol.negotiated_version(requested),
        "agentCapabilities": agent_capabilities(),
        "authMethods": [],
        "agentInfo": _agent_info(),
    }


@dataclass
class ClientCapabilities:
    """What the client declared it can do.

    Stored as the raw object: nothing branches on it yet (no protocol-level
    questions, no fs round-trips -- plan §3), but the declaration arrives once
    at the handshake and cannot be recovered later, so it is kept rather than
    read and dropped.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: dict[str, Any] | None) -> "ClientCapabilities":
        declared = (params or {}).get("clientCapabilities")
        return cls(raw=declared if isinstance(declared, dict) else {})


__all__ = ["AGENT_NAME", "ClientCapabilities", "agent_capabilities", "initialize_result"]
