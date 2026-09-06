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
from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderComposer:
    """One question, one answer, one retry."""

    provider: Any
    model: str | None = None
    temperature: float = 0.2
    reasoning_effort: str | None = None
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
            stream = self.provider.chat_stream(
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
