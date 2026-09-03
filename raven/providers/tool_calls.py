"""OpenAI-style ``tool_call`` payloads: how a tool call is written into the
assistant message that goes back upstream on the next turn."""

from __future__ import annotations

import json
from typing import Any

from raven.contracts.llm_provider import ToolCallRequest


def openai_tool_call(call: ToolCallRequest) -> dict[str, Any]:
    """Serialize ``call`` to an OpenAI-style tool_call payload."""
    tool_call: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.arguments, ensure_ascii=False),
        },
    }
    if call.provider_specific_fields:
        tool_call["provider_specific_fields"] = call.provider_specific_fields
    if call.function_provider_specific_fields:
        tool_call["function"]["provider_specific_fields"] = call.function_provider_specific_fields
    return tool_call
