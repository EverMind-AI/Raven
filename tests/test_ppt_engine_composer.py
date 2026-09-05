"""The one-question composer, and the retry that was written but unreachable.

Everything the ppt route asks a second model -- reading the task, captioning a
figure -- goes through `ProviderComposer.ask`, and it had no tests: the branch
that doubles the budget for a reply the provider cut off could not be reached at
all, because a truncated reply is a non-empty string and emptiness was the only
thing tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from raven_ppt.tools._composer import ProviderComposer


@dataclass
class _Delta:
    content: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


class _Provider:
    """Answers a scripted (text, finish_reason) per call and records the budgets."""

    def __init__(self, *answers: tuple[str, str | None], raise_on: int | None = None) -> None:
        self.answers = list(answers)
        self.budgets: list[int] = []
        self.raise_on = raise_on

    def chat_stream(self, _messages, *, model=None, max_tokens: int, temperature=None, **_kw):
        self.budgets.append(max_tokens)
        index = len(self.budgets) - 1
        text, finish = self.answers[index] if index < len(self.answers) else ("", None)
        should_raise = self.raise_on == index

        async def stream():
            if text:
                yield _Delta(content=text)
            yield _Delta(finish_reason=finish, usage={"prompt_tokens": 10, "completion_tokens": 20})
            if should_raise:
                raise RuntimeError("gateway said 503")

        return stream()


def _ask(provider, budget: int = 4000):
    return ProviderComposer(provider=provider).ask("sys", [{"type": "text", "text": "q"}], max_tokens=budget)


@pytest.mark.asyncio
async def test_a_reply_the_provider_cut_off_is_retried_with_twice_the_budget() -> None:
    """The bug. A live run's intake failed three times in a row on this -- the JSON
    came back unterminated at character 310 of 311, then 1244 of 1295, then 1588 of
    1588 -- and each failure cost a fresh call, because the partial object was
    returned instead of being retried for."""
    provider = _Provider(('{"topic": "half of a', "length"), ('{"topic": "all of it"}', "stop"))

    reply = await _ask(provider, budget=4000)

    assert reply == '{"topic": "all of it"}'
    assert provider.budgets == [4000, 8000], "the second call gets twice the budget"


@pytest.mark.asyncio
async def test_a_complete_reply_costs_one_call() -> None:
    """The retry must not fire on a reply that finished: it doubles the cost of every
    intake and every caption."""
    provider = _Provider(('{"topic": "done"}', "stop"))

    reply = await _ask(provider)

    assert reply == '{"topic": "done"}'
    assert provider.budgets == [4000]


@pytest.mark.asyncio
async def test_an_empty_reply_is_retried_at_the_same_budget() -> None:
    """A transport that answered nothing is not a length problem, and doubling the
    budget for it buys nothing."""
    provider = _Provider(("", None), ('{"topic": "second time"}', "stop"))

    reply = await _ask(provider)

    assert reply == '{"topic": "second time"}'
    assert provider.budgets == [4000, 4000]


@pytest.mark.asyncio
async def test_the_partial_stands_when_the_retry_brings_back_nothing() -> None:
    """No worse than the empty string it used to return, and the caller's two error
    paths read the same either way."""
    provider = _Provider(('{"topic": "half of a', "length"), ("", None))

    reply = await _ask(provider)

    assert reply == '{"topic": "half of a'
    assert provider.budgets == [4000, 8000]


@pytest.mark.asyncio
async def test_the_tokens_of_both_attempts_are_counted() -> None:
    """The spend is what a caller reports, and a retried call spends twice."""
    provider = _Provider(('{"a": 1', "length"), ('{"a": 1}', "stop"))
    composer = ProviderComposer(provider=provider)

    await composer.ask("sys", [{"type": "text", "text": "q"}], max_tokens=4000)

    assert composer.spent == {"input": 20, "output": 40}
