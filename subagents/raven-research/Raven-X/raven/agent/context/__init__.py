"""ContextBuilder — assembles system prompt + history for AgentLoop.

Implementation lives in ``builder.py``.

External callers should keep using:

    from raven.agent.context import ContextBuilder
"""

from raven.agent.context.builder import (
    TOOL_OUTPUT_ELIDED,
    ContextBuilder,
    is_elided_tool_output,
)

__all__ = ["TOOL_OUTPUT_ELIDED", "ContextBuilder", "is_elided_tool_output"]
