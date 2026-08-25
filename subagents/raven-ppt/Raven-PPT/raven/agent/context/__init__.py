"""ContextBuilder — assembles system prompt + history for AgentLoop.

Implementation lives in ``builder.py``.

External callers should keep using:

    from raven.agent.context import ContextBuilder
"""

from raven.agent.context.builder import ContextBuilder

__all__ = ["ContextBuilder"]
