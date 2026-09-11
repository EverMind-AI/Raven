"""The default Memory strategy: the window this turn's model calls see.

The context engine stays the thing that assembles -- it holds the Curator's
working state and each segment builder's provider binding, and the builders
were sized against this generation's context window, so a generation's window
*is* its engine and replacing it is a new generation. What this module adds is
the seat: the loop asks the Memory role rather than reaching past it, and the
two turn-side decisions that were never the shell's move here with it.

``candidate_messages`` turns on ``owns_compaction``, which is this role's own
property: an engine that archives for itself wants the whole append-only log
so it can decide what to evict, one that does not wants the post-consolidation
slice so the view matches what the consolidator left behind.

``token_budget`` sizes the prompt against the model's own ceiling, which is the
window's arithmetic by definition -- and the five things it reads (provider,
model, window, tool definitions, identity builder) are the same five the engine
itself was constructed with, so this is the logic returning to the role that
owns it rather than new coupling. All five are read through callables because
all five move under a live ``/model`` switch or a hot config apply, and a value
captured at assembly would answer for the retired one.

Still with the shell on purpose: the assembly orchestration around ``assemble``
(TurnContext construction, the degraded-segment stash, the injected-skill
bookkeeping the loop reads back afterwards). Half of that is the window's and
half is the shell's, so it is not a move this step can make honestly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from raven.contracts.assembled import TokenBudget
from raven.contracts.harness import MemoryModule
from raven.providers.base import send_max_tokens
from raven.utils.tokens import estimate_prompt_tokens

if TYPE_CHECKING:
    from raven.contracts.assembled import AssembledContext
    from raven.contracts.context import ContextEngine, TurnContext


class DefaultMemory:
    """This generation's context engine, plus the window's own turn decisions."""

    def __init__(
        self,
        engine: "ContextEngine",
        *,
        provider: Callable[[], Any],
        model: Callable[[], str],
        context_window_tokens: Callable[[], int],
        tool_definitions: Callable[[], list[dict[str, Any]]],
        system_prompt: Callable[[list[Any] | None], str],
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._model = model
        self._window = context_window_tokens
        self._tool_definitions = tool_definitions
        self._system_prompt = system_prompt

    @property
    def owns_compaction(self) -> bool:
        return self._engine.owns_compaction

    def candidate_messages(self, session: Any) -> list[dict[str, Any]]:
        if self._engine.owns_compaction:
            return list(session.messages)
        return session.get_history(max_messages=0)

    def token_budget(self, selected_skills: list[Any] | None = None) -> TokenBudget:
        provider = self._provider()
        window = self._window()
        # allow_fetch=False for the reason construction passes it: this runs per
        # turn on the loop's own thread and only needs a number to reserve, not
        # the one a request will carry. The fallback under-reserves at worst.
        ceiling = send_max_tokens(
            getattr(provider, "generation", None),
            # The id a request goes out under, so the reservation matches the
            # ceiling that request will carry rather than the stored name's.
            getattr(provider, "wire_model_id", lambda m: m)(self._model()),
            allow_fetch=False,
        )
        # The whole ceiling, not a share of it. Requests no longer name a
        # ceiling, so the one that applies is the model's own. Reserving less
        # hands out a prompt the reply cannot coexist with: measured on this
        # repo's default model, a share leaves the prompt 150000 of a 200000
        # window against a reply allowed 64000, and the sum is refused at
        # request time -- and the emergency shrink only elides tool bodies, so
        # a history grown on conversation gets no retry from that refusal.
        reserved_output = min(ceiling, window)
        tool_tokens = estimate_prompt_tokens([], self._tool_definitions())
        system_tokens = estimate_prompt_tokens([{"role": "system", "content": self._system_prompt(selected_skills)}])
        return TokenBudget(
            context_length=window,
            reserved_output=reserved_output,
            reserved_tools=tool_tokens,
            reserved_system=system_tokens,
            available_history=max(0, window - reserved_output - tool_tokens - system_tokens),
        )

    async def assemble(
        self,
        session_key: str,
        session_messages: list[dict[str, Any]],
        budget: TokenBudget,
        *,
        turn: "TurnContext",
    ) -> "AssembledContext":
        return await self._engine.assemble(session_key, session_messages, budget, turn=turn)

    async def after_turn(self, session_key: str, outcome: dict[str, Any]) -> None:
        await self._engine.after_turn(session_key, outcome)


def bind(memory: DefaultMemory) -> MemoryModule:
    """Admit a built Memory role, naming a missing member at assembly rather
    than as an AttributeError inside somebody's turn."""
    if not isinstance(memory, MemoryModule):
        raise TypeError(
            f"{type(memory).__name__} cannot serve as the Memory role: it must provide "
            "owns_compaction, candidate_messages, token_budget, assemble and after_turn"
        )
    return memory


__all__ = ["DefaultMemory", "bind"]
