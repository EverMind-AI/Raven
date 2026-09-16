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
    SecurityScheme,
)

from raven import __version__
from raven.config.schema import A2aConfig

CARD_PATH = "/.well-known/agent-card.json"
JSONRPC_BINDING = "JSONRPC"
PROTOCOL_VERSION = "1.0"


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
            "http_auth": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer"),
            )
        },
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

    Sub-agents become skills rather than an `AgentCapabilities.extensions` entry:
    a skill is the protocol's own word for "a thing this agent can be asked to
    do", and a caller that reads skills at all reads them without having to
    understand a raven-specific extension URI first.
    """
    card = build_agent_card(config, base_url=base_url, extended_available=True)
    for agent in agents:
        card.skills.append(
            AgentSkill(
                id=f"subagent:{agent.name}",
                name=agent.name,
                description=agent.description,
                tags=["sub-agent"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        )
    return card
