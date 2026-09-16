"""The A2A agent card this host publishes.

Every capability is answered by what this build actually does. An optimistic
card is worse than a narrow one: a caller that believes an advertised
capability fails at the call instead of choosing another path.
"""

from __future__ import annotations

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


def build_agent_card(config: A2aConfig, *, base_url: str) -> AgentCard:
    """The card served at :data:`CARD_PATH` for a server mounted at `base_url`."""
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
            extended_agent_card=False,
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
