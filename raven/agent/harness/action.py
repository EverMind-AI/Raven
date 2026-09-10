"""The default Action strategy: the loop's own single-call dispatch.

Two paths, chosen the way the loop chose them: a turn with a delta sink
streams through the loop's own ``_llm_call_stream`` (which fans tokens and
reasoning out to the turn's sinks, and is therefore the shell's to own and
this module's to call), and a turn without one takes the provider's retry
ladder with its fallback chain. Nothing else moves: the request arrives with
messages, tools and model already rewritten by the token strategies, and the
response goes back for the shell's recoveries to inspect.

``decide`` is one *decision*, not one HTTP request. This default spends
exactly one call, but the contract lets a replacement spend several -- a
best-of-n or a critic pass -- and return the one the loop sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.contracts.harness import ActionRequest

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMResponse


class DefaultAction:
    """Dispatch one model call: streaming when a sink is attached, else retrying."""

    async def decide(self, request: ActionRequest) -> "LLMResponse":
        if request.on_token_delta is not None or request.on_reasoning_delta is not None:
            return await request.stream_call(
                messages=request.messages,
                tools=request.tools,
                model=request.model,
                on_token_delta=request.on_token_delta,
                on_reasoning_delta=request.on_reasoning_delta,
                **request.generation_overrides,
            )
        return await request.provider.chat_with_retry(
            messages=request.messages,
            tools=request.tools,
            model=request.model,
            fallback_models=request.fallback_models,
            **request.generation_overrides,
        )


__all__ = ["DefaultAction"]
