"""What this agent tells the client it can do, and what the client told us.

Both halves matter and only one of them is usually written. An agent that
declares a capability it does not serve produces the worst failure shape in the
protocol: the client routes work through a method that answers with an error,
and the turn stalls on a promise nobody will keep. So every flag here is false
unless something downstream actually honours it, and each one names what would
have to exist for it to flip.

The inbound half is kept rather than read and dropped. ``ask_user`` has no tool
call to attach, and ``RequestPermissionRequest.toolCall`` is required, so the
routing decision for a bare question depends on whether the client declared
``elicitation`` -- a decision that cannot be made at the point of asking if the
declaration was thrown away at the handshake.
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
    """Name and version, read from the installed distribution.

    Read rather than hardcoded so it cannot claim a version this build is not.
    A source checkout with no distribution metadata reports ``0.0.0`` instead of
    failing the handshake over a cosmetic field.
    """
    from raven import __version__

    return {"name": "raven", "version": str(__version__ or "0.0.0")}


def agent_capabilities() -> dict[str, Any]:
    """The ``AgentCapabilities`` object sent in the initialize result."""
    return {
        # The transcript is replayed as ``session/update`` notifications during
        # the load, so a resumed session is drawn by the same client code as a
        # live one. Declared true only because that replay exists -- the flag is
        # a promise, and a client that reopens a session on a false promise shows
        # an empty history for a conversation that has one.
        "loadSession": True,
        "promptCapabilities": {
            # Images are turned into files and handed to the turn as media
            # paths, which is the same road every channel's attachments take.
            "image": True,
            # No audio path exists on the prompt side at all.
            "audio": False,
            # An embedded text resource is inlined into the prompt. A binary one
            # is named rather than decoded, which is a degradation and not a
            # failure -- see updates/prompt content handling.
            "embeddedContext": True,
        },
        # Per-session MCP servers are refused explicitly rather than declared:
        # the agent loop connects MCP once per process, lazily, and there is no
        # mechanism for a session to bring its own. Declaring http/sse here
        # would invite exactly the request that has to be refused.
        "mcpCapabilities": {"http": False, "sse": False},
        # ``list`` only, and an empty object is how the schema spells "supported"
        # for it. resume / close / delete / additionalDirectories stay undeclared:
        # all four are stable, all four are objects rather than booleans, Zed uses
        # none of them, and each one declared is a method that must then work. See
        # the compatibility matrix.
        "sessionCapabilities": {"list": {}},
        # No auth: authMethods is empty, so there is nothing to log out of
        # either. Declaring auth.logout would put a method on the wire whose
        # only honest answer is that there was no session to end.
        "auth": {},
    }


def initialize_result(params: dict[str, Any] | None) -> dict[str, Any]:
    """The full ``InitializeResponse`` for a client's initialize request.

    Tolerant by construction: unknown params are ignored rather than rejected,
    and a ``protocolVersion`` of the wrong type is read for intent (see
    :func:`raven.acp.protocol.negotiated_version`). The one thing this must not
    do is fail -- a client that cannot complete initialize has no way to show a
    person why, because every surface for saying so is on the far side of the
    handshake.

    ``authMethods: []`` is a positive statement, not an omission: it says this
    agent needs no authentication, which is what lets a client proceed straight
    to ``session/new``.
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
    """What the client declared it can do, as this agent will consult it.

    Stored as the raw object plus the handful of decisions that depend on it, so
    a reader can see which flags are load-bearing without reading the whole
    protocol. Everything defaults to "not declared", which is also what an
    absent or malformed ``clientCapabilities`` yields -- a client that sends
    garbage gets treated as a client that can do nothing, which is safe in the
    only direction that matters.
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
    def elicitation(self) -> bool:
        """Whether the client declared the ``elicitation`` group at all.

        The whole reason the inbound half is stored. Without it, ``ask_user``
        has to be squeezed into ``session/request_permission`` with a
        synthesised tool call, because that request requires one.
        """
        return self.raw.get("elicitation") is not None

    @property
    def elicitation_form(self) -> bool:
        """Whether ``elicitation/create`` in form mode may be used.

        The narrower flag, and the one the question routing actually branches on.
        A client can declare the group and support only ``url`` mode, which is for
        sending somebody to a web page -- useless for asking a question. Reading
        the group as sufficient would route every question into a mode the client
        never claimed.

        Presence, not truthiness: the schema says ``{}`` explicitly advertises
        form support, so a client declaring it with no options would read as no
        support under a truthy check.
        """
        declared = self._section("elicitation").get("form", _MISSING)
        return declared is not _MISSING and declared is not None

    @property
    def reads_files(self) -> bool:
        """Whether the client offers ``fs/read_text_file``.

        Not called today -- raven reads through its own tools, and the
        unsaved-buffer divergence that makes the client's copy more accurate is
        also what makes it inconsistent with what the agent then edits. Recorded
        so the compatibility matrix can state it as a choice rather than as an
        omission.
        """
        return bool(self._section("fs").get("readTextFile"))

    @property
    def writes_files(self) -> bool:
        """Whether the client offers ``fs/write_text_file``. Not called; see
        :attr:`reads_files`."""
        return bool(self._section("fs").get("writeTextFile"))

    @property
    def has_terminal(self) -> bool:
        """Whether the client offers the ``terminal/*`` group. Not called: raven
        runs commands through its own sandbox executor, which is where the
        approval and the process-group kill live."""
        return self.raw.get("terminal") is not None


__all__ = ["ClientCapabilities", "agent_capabilities", "initialize_result"]
