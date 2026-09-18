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

``judge`` is the other half of "what the agent does next": the registry asks
it once per call a response proposed, before that call is dispatched. The
default answers with whatever this dispatch's Charter asked for, which is
nothing at all on a turn that carried none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from raven.contracts.harness import ActionModule, ActionRequest

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMResponse


class DefaultAction:
    """Dispatch one model call: streaming when a sink is attached, else retrying."""

    def judge(
        self,
        name: str,
        params: Mapping[str, Any],
        prior: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> list[str]:
        """Why this dispatch's Charter refuses this call, or an empty list.

        Imported in the call, not for style: ``raven.agent.subagent`` pulls its
        manager and every backend on package import, so naming the charter at
        the top of this file makes importing the harness import all of that.

        Nothing is caught here on purpose. ``charter.judge`` already guards the
        two things that can genuinely fail -- a judge whose source the gate
        refused is dropped, and one that raises at run time has said nothing --
        and what is left is rule evaluation over strings, which raises only
        when this package has a defect. Catching that would turn every such
        defect into checks that quietly stop applying, which is the failure a
        refusal exists to prevent.
        """
        from raven.agent.subagent.charter import judge as charter_judge

        return charter_judge(name, params, prior)

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


def bind(action: DefaultAction) -> ActionModule:
    """Admit a built Action role, naming a missing member at assembly rather
    than as an AttributeError inside somebody's turn.

    The same guard the Memory role gets, and for a sharper reason: the tool
    registry catches whatever ``judge`` raises and answers with no opinion, so
    a role missing that method would not fail loudly -- it would quietly let
    every call a dispatch's Charter refuses through.
    """
    if not isinstance(action, ActionModule):
        raise TypeError(f"{type(action).__name__} cannot serve as the Action role: it must provide decide and judge")
    return action


__all__ = ["DefaultAction", "bind"]
