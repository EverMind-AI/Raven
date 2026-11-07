"""ContextBuilder — assembles system prompt + history for AgentLoop.

Implementation lives in ``builder.py``.

External callers should keep using:

    from raven.agent.context import ContextBuilder

``silence.py`` holds the silence markers a model ends a turn with, the rules that read them,
and the instruction that teaches the marker to a proactive turn.
"""

from raven.agent.context.builder import ContextBuilder

__all__ = ["ContextBuilder"]
