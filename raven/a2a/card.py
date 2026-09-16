"""The two A2A agent cards this host publishes.

Every capability is answered by what this build actually does. An optimistic
card is worse than a narrow one: a caller that believes an advertised
capability fails at the call instead of choosing another path.

There are two because the protocol splits one description across two
authentication states. The public card is fetched by an unauthenticated GET and
has to be: a caller reads it to learn which scheme to authenticate with, so
requiring the credential first would be circular. The extended card is a
``GetExtendedAgentCard`` RPC, which rides the same bearer check as every other
method, and is therefore the only one of the two that may name what this host
can actually do.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from raven import __version__
from raven.config.agent_names import is_builtin_agent_name
from raven.config.schema import A2aConfig

CARD_PATH = "/.well-known/agent-card.json"
JSONRPC_BINDING = "JSONRPC"
PROTOCOL_VERSION = "1.0"

AUTH_SCHEME = "http_auth"
"""The name `security_schemes` defines and `security_requirements` selects."""

SCHEME_NO_SCOPES = StringList()
"""The scope list for a scheme that has none.

`SecurityRequirement.schemes` maps a scheme name to the scopes a caller needs
under it. Bearer-token access here is all-or-nothing, so the list is empty --
which is the protocol's way of saying "this scheme, no scopes", and is not the
same as declaring no requirement at all.
"""

SUBAGENT_EXTENSION_URI = "https://raven.evermind.ai/a2a/extensions/sub-agents/v1"
"""Identifies the roster carried in `AgentCapabilities.extensions`.

`AgentExtension` is the protocol's own extension point, and the only place a
conformant card may carry a payload the spec does not define: `AgentCard` is a
closed set of fourteen protobuf fields, so a custom top-level key is rejected by
a strict parser and silently dropped by a lenient one. A reader that does not
know this URI ignores the entry -- which is why `required` is false -- and one
that does knows the shape of `params` without having to guess it from a skill
list.

Versioned in the path so a later change of shape is a new URI rather than a
redefinition of this one: a peer keying off the URI has no other way to tell
which shape it is being handed.
"""


def build_agent_card(config: A2aConfig, *, base_url: str, extended_available: bool = False) -> AgentCard:
    """The public card served at :data:`CARD_PATH` for a server mounted at `base_url`.

    `extended_available` is what the caller can actually get, not what the build
    can in principle serve: it is true only when this process was given a roster
    to derive the extended card from. Advertising it otherwise would send a
    caller to a method that answers ``ExtendedAgentCardNotConfiguredError``.
    """
    return AgentCard(
        name="Raven",
        description=(
            "Raven is a host agent that manages and orchestrates all sub-agents on this device "
            "to perform complex tasks."
        ),
        # This agent's own version, not the protocol's -- they are adjacent fields
        # here and a literal "1.0" in both read as one repeated value.
        version=__version__,
        supported_interfaces=[
            AgentInterface(
                url=base_url,
                protocol_binding=JSONRPC_BINDING,
                protocol_version=PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extended_agent_card=extended_available,
        ),
        security_schemes={
            AUTH_SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer"),
            )
        },
        # Both fields, because they answer different questions and this face
        # enforces the answer to the second. `security_schemes` defines what
        # `http_auth` means; `security_requirements` says a caller must satisfy
        # it. Declaring only the first tells a peer that reads the card honestly
        # that no authentication is needed -- it then calls without a credential
        # and is refused, having been told nothing that would have prevented it.
        security_requirements=[SecurityRequirement(schemes={AUTH_SCHEME: SCHEME_NO_SCOPES})],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="general",
                name="General assistance",
                description=(
                    "Answer a question, research a topic, write or edit a document, or carry out a "
                    "multi-step task and report what was done."
                ),
                tags=["general", "research", "writing"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )


class RosterEntry(Protocol):
    """The two fields the extended card reads off a sub-agent.

    Structural rather than the concrete ``AgentMeta``: the card needs a name and
    a sentence, and typing it that way keeps this surface from importing the
    sub-agent package for a shape it only reads.
    """

    name: str
    description: str


def build_extended_agent_card(config: A2aConfig, *, base_url: str, agents: Sequence[RosterEntry]) -> AgentCard:
    """The authenticated card, naming the sub-agents this host can dispatch to.

    This is where the derive-once rule applies. The public card cannot carry it
    -- it answers before authentication, so a derived inventory there would hand
    the host's installed agents to anyone who can reach the port -- but the
    caller of this one has already presented the bearer token the public card
    told it to use, and is by definition an operator-admitted peer.

    The roster rides `capabilities.extensions`, not `skills`. A skill is the
    protocol's word for something this agent can be *asked to do*, and a peer
    cannot ask for `Raven-Code` -- it can only send a message to this host,
    which then decides what to dispatch. Listing each sub-agent as a skill
    therefore advertises call targets that do not exist. What the peer can
    actually ask for is the orchestration itself, which is one skill; which
    agents back it is reference data, and `AgentExtension` is where the protocol
    puts data it does not define (see :data:`SUBAGENT_EXTENSION_URI`).

    The package's own seed row is dropped from that data. It is this host's
    in-process loop -- the agent this card already describes, and the one a peer
    reaches by sending a message to the interface above -- so naming it would
    offer a second route to the agent the caller is already talking to, under a
    second name. `is_builtin_agent_name` rather than a comparison against the
    name: the seed answers to a legacy spelling too, and may be redeclared over
    ACP without ceasing to be the host's generic agent.

    A host whose roster is empty gets neither the skill nor the extension. The
    file's rule is that a card answers what the build actually does, and a
    gateway whose loop has not started yet has nothing to orchestrate.
    """
    card = build_agent_card(config, base_url=base_url, extended_available=True)
    roster = [agent for agent in agents if not is_builtin_agent_name(agent.name)]
    if not roster:
        return card
    card.skills.append(
        AgentSkill(
            id="subagent-orchestration",
            name="Sub-agent orchestration",
            description=(
                "Break a task across the sub-agents installed on this device, dispatch each step to "
                "the one that fits, and report the result. State the outcome you need rather than an "
                "agent to run: which agents exist is this host's own business, and it sequences them."
            ),
            tags=["delegation", "orchestration", "sub-agents"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        )
    )
    # Built in place rather than from a constructed `Struct`: `params` is a
    # `google.protobuf.Struct`, whose class is generated at import time and has
    # no stub, so naming the type is an unresolved import to the type checker
    # while the field itself is declared by `AgentExtension` and resolves fine.
    extension = card.capabilities.extensions.add(
        uri=SUBAGENT_EXTENSION_URI,
        description="The sub-agents this host can dispatch work to.",
        required=False,
    )
    extension.params.update({"agents": [{"name": agent.name, "description": agent.description} for agent in roster]})
    return card
