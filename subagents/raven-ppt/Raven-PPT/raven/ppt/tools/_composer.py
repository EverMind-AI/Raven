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
    spent: dict[str, int] = None  # type: ignore[assignment]
    failure: str = ""
    """Why the last `ask` came back empty, when it was the transport rather than
    the model. Read by a caller whose reply-parsing error would otherwise blame the
    model for a gateway that answered 503: a live run against one that did exactly
    that reported "the reply did not parse" five times over."""

    def __post_init__(self) -> None:
        if self.spent is None:
            self.spent = {"input": 0, "output": 0}

    async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str:
        """The reply text, or "" when two attempts produced nothing usable.

        An empty string rather than an exception: one unusable reply costs its own
        page and must not cost the round. The caller reports it as that page's
        defect and carries on with the others.
        """
        self.failure = ""
        text, reason = await self._once(system, parts, max_tokens)
        if text.strip():
            return text
        # One retry. Doubled budget when the reply was cut off mid-sentence,
        # because the same budget produces the same truncation; the same budget
        # otherwise, because the failure was transport rather than length.
        budget = max_tokens * 2 if reason == "length" else max_tokens
        text, _ = await self._once(system, parts, budget)
        return text

    async def _once(self, system: str, parts: list[dict[str, Any]], max_tokens: int) -> tuple[str, str | None]:
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
            self.failure = f"{type(exc).__name__}: {exc}"
            return "", finish
        return "".join(collected), finish

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
