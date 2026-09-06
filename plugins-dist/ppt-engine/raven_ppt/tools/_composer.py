"""Asking a model one question, on an empty context.

A per-page call runs on the same model unless another is configured, because the
isolation that matters is the empty context rather than a different set of
weights: a page's copy is written better against that page's claim than as one
twentieth of a reply.

Streaming rather than a single request, and that is not a preference. A page block
runs to hundreds of tokens and a whole prelude to thousands, so a reply takes
minutes; a non-streaming request leaves the connection idle for the duration and
the gateways in front of these endpoints cut it at around six. Streaming also
turns a truncated reply into something observable -- the terminal delta says
`length` -- which is what lets the retry below double the budget instead of
guessing.
"""

from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass
from typing import Any

# How an effort reaches a model behind OpenRouter: in the body, as the gateway's own
# `reasoning` object, and not as the `reasoning_effort` parameter. litellm forwards
# that parameter only for models its own table flags as reasoning-capable and drops
# it for the rest -- openrouter/z-ai/glm-5.3-flash among them -- so on the live deck
# product every effort the config named reached the model as no setting at all, and a
# GLM thinks at its default. Measured on the same fifteen pages: the parameter at
# "low" read a page in 38 to 631 seconds and 8000 output tokens (indistinguishable
# from "medium"), the body's `effort: low` in 13 to 29 seconds and 460. `enabled:
# false` is also the gateway's spelling, but this endpoint answers it with "Reasoning
# is mandatory for this endpoint and cannot be disabled", so "none" is sent as the
# lowest effort rather than as off. The host sends the same object for qwen behind
# OpenRouter (raven.providers.capabilities). Only for that gateway: an OpenAI endpoint
# refuses a body field it does not know.
NO_THINKING = "none"


def thinking(provider: Any, effort: str | None) -> dict[str, Any]:
    """`ProviderComposer` keywords that carry ``effort`` to ``provider`` as it understands it.

    On an OpenRouter-routed provider the effort rides in the request body; everywhere
    else it is the ``reasoning_effort`` parameter, which the provider honours or drops
    as it does today.
    """
    if not effort:
        return {}
    if _behind_openrouter(provider):
        return {"extra_body": {"reasoning": {"effort": "low" if effort == NO_THINKING else effort}}}
    return {"reasoning_effort": effort}


def _behind_openrouter(provider: Any) -> bool:
    said = f"{_model_of(provider)} {getattr(provider, 'api_base', '') or ''}".lower()
    return "openrouter" in said


def _model_of(provider: Any) -> str:
    """The model this provider sends to by default, however this one spells it.

    Read through the accessor as well as the attribute, because the host hands the
    engine a lazy proxy that keeps its model private and answers `get_default_model()`.
    Reading only the attribute saw an empty string, so the route was never recognised
    as a gateway and every effort this engine set went out as `reasoning_effort` -- the
    one spelling the note above says litellm drops for these models.
    """
    named = getattr(provider, "default_model", None)
    if named:
        return str(named)
    getter = getattr(provider, "get_default_model", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:  # noqa: BLE001 -- a provider that cannot name its model is not one behind a gateway
        return ""


@dataclass
class ProviderComposer:
    """One question, one answer, one retry."""

    provider: Any
    model: str | None = None
    temperature: float = 0.2
    reasoning_effort: str | None = None
    #: Merged into the request body of every call this composer makes -- see
    #: `thinking`. The provider is shared with the author's loop, so the extras ride
    #: on a shallow copy of it rather than on the instance everyone else calls, and the
    #: copy is taken at the first call rather than here -- see `_sending`.
    extra_body: dict[str, Any] | None = None
    #: Whether a reply the model cut for length is asked again at twice the budget.
    #: Right for a page's copy, whose length is the page's; wrong for a reading, where
    #: the budget is already the whole call and the second attempt costs what the
    #: first did -- a 20-page run paid that on every page a reasoning model ran long on.
    retry_on_length: bool = True
    spent: dict[str, int] = None  # type: ignore[assignment]
    failure: str = ""
    """Why the *last* `ask` came back empty, when it was the transport rather than
    the model. Read by a caller whose reply-parsing error would otherwise blame the
    model for a gateway that answered 503: a live run against one that did exactly
    that reported "the reply did not parse" five times over.

    Only readable by a caller that asks one question at a time. A failure belongs
    to one call and this field holds one value, so several concurrent asks on one
    composer leave whichever finished last -- take the failure from
    `ask_with_failure` instead."""

    def __post_init__(self) -> None:
        if self.spent is None:
            self.spent = {"input": 0, "output": 0}
        # Not a field: the copy of the provider that carries `extra_body` is this
        # instance's own working state, and `dataclasses.replace` must not carry one
        # composer's sender onto another's provider.
        self._sender: Any = None

    def _sending(self) -> Any:
        """The provider this call goes on: the host's, plus this composer's body extras.

        The extras ride on the provider instance because `chat_stream` has no parameter
        for them, and on a copy because the instance is shared with the author's loop.
        The copy has to be taken from the provider that will actually send, and the host
        hands the engine a lazy proxy: it grows no `extra_body` attribute until its own
        first call materialises the provider behind it, so a copy taken when the tools
        were assembled found nothing to copy and dropped the extras without a word.
        Measured on a live run: a reader effort of "low" and the intake call's own low
        effort both reached a glm behind OpenRouter as no setting at all, and the intake
        took 173 seconds where the same call at the gateway's low effort takes 14.

        Memoized once it succeeds. Until then every call retries it and sends on the
        proxy meanwhile, which is the same request without the extras -- right, because
        one call at the model's own effort is worth more than no call.
        """
        if self._sender is not None:
            return self._sender
        if not self.extra_body:
            return self.provider
        target = getattr(self.provider, "unwrapped", None) or self.provider
        if not hasattr(target, "extra_body"):
            return self.provider
        own = copy.copy(target)
        own.extra_body = {**(getattr(target, "extra_body", None) or {}), **self.extra_body}
        self._sender = own
        return own

    async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str:
        """The reply text, or "" when two attempts produced nothing usable.

        An empty string rather than an exception: one unusable reply costs its own
        page and must not cost the round. The caller reports it as that page's
        defect and carries on with the others.
        """
        text, _ = await self.ask_with_failure(system, parts, max_tokens=max_tokens)
        return text

    async def ask_with_failure(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> tuple[str, str]:
        """The reply, and why the transport gave nothing back, as one result.

        Returned by the call that produced it rather than only published on
        `self.failure` for the caller to fetch afterwards. A caller running several
        asks concurrently on one composer fetches whichever finished last: a figure
        whose reply was merely malformed was reported to the author as a figure the
        gateway never answered, sending them to fix a transport that was fine.
        """
        text, reason, failure = await self._once(system, parts, max_tokens)
        # `reason != "length"` and not just `text.strip()`: a reply the provider cut
        # off is a non-empty string, so testing only for emptiness returned the
        # truncated half and left the doubled-budget retry below unreachable in the
        # one case it was written for. Measured on a live run whose intake failed
        # three times in a row -- "unterminated string at character 310 of 311",
        # then 1244 of 1295, then 1588 of 1588 -- each time handing back a partial
        # object the caller could only report as unparseable, and each time costing
        # the author a fresh call at about eighty seconds. Reasoning models are why
        # the cut lands in different places: their thinking is spent from the same
        # budget as the answer, so what is left for the JSON varies per call.
        if text.strip() and (reason != "length" or not self.retry_on_length):
            self.failure = ""
            return text, ""
        # One retry. Doubled budget when the reply was cut off mid-sentence,
        # because the same budget produces the same truncation; the same budget
        # otherwise, because the failure was transport rather than length.
        budget = max_tokens * 2 if reason == "length" else max_tokens
        retried, _, retried_failure = await self._once(system, parts, budget)
        failure = retried_failure or failure
        self.failure = failure
        # The partial stands if the retry brought back nothing at all: it is no
        # worse than the empty string, and the caller's two error paths -- "the
        # transport failed" and "the reply did not parse" -- read the same either
        # way.
        return retried or text, failure

    async def _once(self, system: str, parts: list[dict[str, Any]], max_tokens: int) -> tuple[str, str | None, str]:
        """The reply, the provider's finish reason, and this attempt's own failure."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": parts}]
        collected: list[str] = []
        finish: str | None = None
        try:
            stream = self._sending().chat_stream(
                messages,
                model=self.model,
                max_tokens=max_tokens,
                temperature=self.temperature,
                **({"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}),
            )
            async with contextlib.aclosing(stream) as deltas:
                async for delta in deltas:
                    if delta.content:
                        collected.append(delta.content)
                    if delta.finish_reason:
                        finish = delta.finish_reason
                    if delta.usage:
                        self._record(delta.usage)
        except Exception as exc:  # noqa: BLE001 -- one page's failure, not the round's
            # Whatever arrived before the failure is discarded rather than parsed:
            # half a JSON object is a defect report, not an edit. The reason is
            # kept, because "the gateway was unavailable" and "the model wrote
            # something unparseable" call for different next moves.
            return "", finish, f"{type(exc).__name__}: {exc}"
        return "".join(collected), finish, ""

    def _record(self, usage: dict[str, Any]) -> None:
        """Tokens spent by this pass, as a delta rather than a lifetime total.

        The predecessor read the provider's running totals, so the number it
        reported for one call was everything the process had ever spent.
        """
        for key, names in (
            ("input", ("prompt_tokens", "input_tokens")),
            ("output", ("completion_tokens", "output_tokens")),
        ):
            for name in names:
                value = usage.get(name)
                if isinstance(value, int):
                    self.spent[key] += value
                    break
