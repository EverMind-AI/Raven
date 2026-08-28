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
        # A session may bring stdio servers of its own -- they are connected
        # into a registry that session owns and are visible to its turns alone.
        # http and sse stay false because every server a dispatcher hands a
        # sub-agent arrives as one stdio stanza pointing at a host endpoint,
        # whatever the upstream transport is, and declaring a transport means
        # honouring a definition (and its credentials) inside this process.
        "mcpCapabilities": {"http": False, "sse": False},
        # No auth: nothing to log out of either.
        "auth": {},
        # Extension surface, mirrored by ``ClientCapabilities.ask_user``: the
        # agent can route ask_user through an ``ask_user_request`` update and
        # accept ``_raven/clarify_respond`` -- honoured by AcpMethods once the
        # CLIENT also declares it (both sides must opt in; this key is how the
        # consuming raven knows offering its UI is worthwhile).
        # ``SESSION_MCP_CAPABILITY`` is a promise, not a feature flag: it says
        # the servers a session brings really are connected, and the tools behind
        # them are reachable from that session's turns and from no sibling
        # session on this connection.
        "_meta": {"raven": {"askUser": True}, protocol.SESSION_MCP_CAPABILITY: {}},
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

    Stored as the raw object: the declaration arrives once at the handshake
    and cannot be recovered later, so it is kept rather than read and dropped.
    One thing branches on it today -- ``ask_user`` below arms the question
    broker; fs round-trips still do not (plan §3).
    """

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: dict[str, Any] | None) -> "ClientCapabilities":
        declared = (params or {}).get("clientCapabilities")
        return cls(raw=declared if isinstance(declared, dict) else {})

    @property
    def ask_user(self) -> bool:
        """Whether the client renders ``ask_user_request`` updates and answers
        them via ``_raven/clarify_respond``.

        Declared under ``_meta.raven`` -- the extensibility slot -- so a
        spec-only client's declaration can never collide with it. Absent or
        malformed reads False: the failure mode of a wrong True is a question
        no UI shows, silently answered by the broker's 600s fail-safe.
        """
        meta = self.raw.get("_meta")
        raven = meta.get("raven") if isinstance(meta, dict) else None
        return bool(raven.get("askUser")) if isinstance(raven, dict) else False


__all__ = ["AGENT_NAME", "ClientCapabilities", "agent_capabilities", "initialize_result"]
