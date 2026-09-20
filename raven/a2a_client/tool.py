"""The single host-agent tool for calling a remote A2A agent.

Withheld from a sub-agent process by name -- see
``raven.agent.subagent.role.WITHHELD_FROM_SUBAGENT``. A credential never
appears in this tool's schema or description: the model passes a URL and
``peers`` decides what authenticates it.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.a2a_client.client import send_message
from raven.config.schema import A2aConfig
from raven.contracts.tool import Tool


class A2aTool(Tool):
    timeout_seconds = 600.0

    def __init__(self, config: A2aConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "a2a_send"

    @property
    def description(self) -> str:
        return (
            "Send a task to an external agent that speaks the A2A protocol, and return its reply. "
            "Takes the URL of the agent's card (usually <origin>/.well-known/agent-card.json). "
            "Use it only for an agent the user has named or that is already configured as a trusted peer."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "card_url": {
                    "type": "string",
                    "description": "URL of the remote agent's A2A agent card.",
                },
                "message": {
                    "type": "string",
                    "description": "The task or question to send to that agent.",
                },
            },
            "required": ["card_url", "message"],
        }

    async def execute(self, **kwargs: Any) -> str:
        card_url = str(kwargs.get("card_url", "")).strip()
        message = str(kwargs.get("message", "")).strip()
        if not card_url or not message:
            return "Error: both card_url and message are required."
        try:
            return await send_message(self._config, card_url, message)
        except Exception as exc:
            logger.opt(exception=True).warning("a2a_send failed for {}", card_url)
            return f"Error: the A2A call failed: {exc}"
