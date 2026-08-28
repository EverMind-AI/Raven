"""What this agent tells the client it can do, and what the client told us.

Both halves matter and only one of them is usually written. An agent that
declares a capability it does not serve produces the worst failure shape in the
protocol: the client routes work through a method that answers with an error and
the turn stalls on a promise nobody will keep. Worse, it is silent in the other
direction too -- a client reads the declaration and disables its own tools
accordingly.

So every flag here is false unless something downstream honours it, and each one
names what would have to exist for it to flip. The inbound half is kept rather
than read and dropped, because whether a question can be asked at all is a
decision that cannot be made at the point of asking if the declaration was
thrown away at the handshake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.acp import protocol

# A sentinel for "the key was absent", distinguishable from a key present and
# null -- which the schema treats as absent too, but only after this check reads
# it as present. Both end up meaning unsupported; the sentinel is what keeps
# ``{}`` meaning supported.
_MISSING = object()


def _agent_info() -> dict[str, str]:
    """Name and version, read from the package rather than hardcoded, so it
    cannot claim a version this build is not."""
    from raven import __version__

    return {"name": "raven-ppt", "version": str(__version__ or "0.0.0")}


def agent_capabilities() -> dict[str, Any]:
    """The ``AgentCapabilities`` object sent in the initialize result."""
    return {
        # The transcript is on disk -- ``SessionManager`` appends every turn to
        # ``sessions/{channel}/{chat_id}.jsonl`` under the session's own job
        # directory -- so ``session/load`` replays it as ``session/update``
        # notifications and the reopened session answers with its history intact.
        "loadSession": True,
        "promptCapabilities": {
            # No image path on the prompt side: a deck is built from documents,
            # and an inbound screenshot has nowhere to go that the material
            # staging does not already cover by absolute path.
            "image": False,
            "audio": False,
            # An embedded text resource is inlined into the prompt. A binary one
            # is named rather than decoded, which is a degradation and not a
            # failure -- see AcpMethods._embedded_resource.
            "embeddedContext": True,
        },
        # A session may bring stdio servers of its own -- they are connected
        # into a registry that session owns and are visible to its turns alone.
        # http and sse stay false because every server a dispatcher hands a
        # sub-agent arrives as one stdio stanza pointing at a host endpoint,
        # whatever the upstream transport is, and declaring a transport means
        # honouring a definition (and its credentials) inside this process.
        "mcpCapabilities": {"http": False, "sse": False},
        # An empty object is how the schema spells "supported". ``resume`` travels
        # with ``loadSession`` above and cannot be dropped on its own: the
        # consuming raven reads ``sessionCapabilities.resume`` for
        # ``AcpAgentBackend.is_stateful`` and will not look up a stored session id
        # without it, then gates the ``session/load`` call itself on
        # ``loadSession`` -- so either one missing silently makes every task a new
        # session however well the other half works. ``list`` and ``delete`` stay
        # absent because each declared key is a method that must then work, and
        # neither is served here.
        "sessionCapabilities": {"close": {}, "resume": {}},
        # No auth: authMethods is empty, so there is nothing to log out of
        # either. Declaring auth.logout would put a method on the wire whose only
        # honest answer is that there was no session to end.
        "auth": {},
        # ``SESSION_MCP_CAPABILITY`` is a promise, not a feature flag: it says
        # the servers a session brings really are connected, and the tools behind
        # them are reachable from that session's turns and from no sibling
        # session on this connection.
        "_meta": {protocol.SESSION_MCP_CAPABILITY: {}},
    }


def initialize_result(params: dict[str, Any] | None) -> dict[str, Any]:
    """The full ``InitializeResponse`` for a client's initialize request.

    Tolerant by construction: unknown params are ignored rather than rejected,
    and a ``protocolVersion`` of the wrong type is read for intent. The one thing
    this must not do is fail -- a client that cannot complete initialize has no
    way to show a person why, because every surface for saying so is on the far
    side of the handshake.

    ``authMethods: []`` is a positive statement, not an omission: it says this
    agent needs no authentication, which is what lets a client proceed straight
    to ``session/new``.
    """
    return {
        "protocolVersion": protocol.negotiated_version((params or {}).get("protocolVersion")),
        "agentCapabilities": agent_capabilities(),
        "authMethods": [],
        "agentInfo": _agent_info(),
    }


@dataclass
class ClientCapabilities:
    """What the client declared it can do, as this agent will consult it.

    Stored as the raw object plus the handful of readings that depend on it, so a
    reader can see which flags are load-bearing. Everything defaults to "not
    declared", which is also what an absent or malformed ``clientCapabilities``
    yields -- a client that sends garbage is treated as one that can do nothing,
    which is safe in the only direction that matters.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: dict[str, Any] | None) -> "ClientCapabilities":
        declared = (params or {}).get("clientCapabilities")
        return cls(raw=declared if isinstance(declared, dict) else {})

    def _section(self, name: str) -> dict[str, Any]:
        section = self.raw.get(name)
        return section if isinstance(section, dict) else {}

    @property
    def elicitation_form(self) -> bool:
        """Whether ``elicitation/create`` in form mode could be used.

        Load-bearing: ``raven.acp.questions`` reads it to pick between the route
        that can carry a typed answer and the one that cannot. A client that
        declares form support is asked through ``elicitation/create``; one that
        does not gets ``session/request_permission`` when the question has choices
        to offer, and otherwise sees the question as an agent message with the
        tool taking its default.

        Presence, not truthiness: the schema says ``{}`` explicitly advertises
        form support, so a client declaring it with no options would read as no
        support under a truthy check.
        """
        declared = self._section("elicitation").get("form", _MISSING)
        return declared is not _MISSING and declared is not None

    @property
    def reads_files(self) -> bool:
        """Whether the client offers ``fs/read_text_file``. Not called -- this
        agent reads through its own tools, and the unsaved-buffer divergence that
        makes the client's copy more accurate is also what makes it inconsistent
        with what the agent then writes."""
        return bool(self._section("fs").get("readTextFile"))

    @property
    def writes_files(self) -> bool:
        """Whether the client offers ``fs/write_text_file``. Not called; see
        :attr:`reads_files`."""
        return bool(self._section("fs").get("writeTextFile"))


__all__ = ["ClientCapabilities", "agent_capabilities", "initialize_result"]
