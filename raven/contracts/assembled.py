"""The assembled-context paper: what a context engine hands the loop for one call.

Two carriers, produced by :meth:`ContextEngine.assemble` and read by the loop
and the token strategies alike:

- :class:`AssembledContext` -- the message list for the LLM call, an optional
  system-prompt addition, which session message indices survived assembly,
  and free-form metadata for debugging and telemetry.
- :class:`TokenBudget` -- the per-turn budget an engine sizes the prompt with:
  the context window, what is reserved out of it for the reply, the tool
  schemas and the system prompt, and what that leaves for history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AssembledContext:
    """Output of a ``ContextEngine.assemble()`` call.

    The agent's LLM call uses exactly these messages. Nothing else from
    session history reaches the model directly.
    """

    messages: list[dict[str, Any]]
    system_prompt_addition: str | None = None  # Injected summary / working-state
    include_indices: list[int] | None = None  # Which session msg indices survived
    metadata: dict[str, Any] = field(default_factory=dict)  # Debug / telemetry


@dataclass
class TokenBudget:
    """Token budget breakdown for one turn."""

    context_length: int  # Model's context window
    reserved_output: int  # Reserved for completion
    reserved_tools: int  # Tool schemas + results in prompt
    reserved_system: int  # System prompt overhead
    available_history: int  # What's left for session history + archive injection


__tier__ = "contract"
__all__ = ["AssembledContext", "TokenBudget"]
