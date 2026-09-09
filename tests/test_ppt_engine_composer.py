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


# -- how an effort reaches the model ---------------------------------------------------


class _Routed(_Provider):
    """A provider with the attributes `thinking` reads and the one it copies."""

    def __init__(self, default_model: str, api_base: str = "") -> None:
        super().__init__(("said", None))
        self.default_model = default_model
        self.api_base = api_base
        self.extra_body = {"provider": {"order": ["x"]}}


class _Lazy:
    """The host's lazy proxy, in the three respects that matter here.

    It names its model through an accessor rather than an attribute, it carries no
    `extra_body` of its own, and the provider behind it does not exist -- so
    `unwrapped` is None -- until its first call has built one.
    """

    def __init__(self, inner: _Routed) -> None:
        self._inner = inner
        self.built = False

    def get_default_model(self) -> str:
        return self._inner.default_model

    @property
    def unwrapped(self):
        return self._inner if self.built else None

    def chat_stream(self, *args: Any, **kwargs: Any):
        self.built = True
        return self._inner.chat_stream(*args, **kwargs)


def test_an_effort_behind_openrouter_rides_in_the_body_not_the_parameter() -> None:
    """litellm forwards `reasoning_effort` only for models its table flags as
    reasoning-capable and drops it for the rest, glm-5.3-flash among them: on the live
    deck product every effort the config named reached the model as no setting at
    all. Measured on fifteen pages, the parameter at "low" read a page in 38 to 631s;
    the gateway's own body object at "low" in 13 to 29s."""
    from raven_ppt.tools._composer import thinking

    assert thinking(_Routed("openrouter/z-ai/glm-5.3-flash"), "low") == {"extra_body": {"reasoning": {"effort": "low"}}}
    assert thinking(_Routed("z-ai/glm-5.3-flash", "https://openrouter.ai/api/v1"), "high") == {
        "extra_body": {"reasoning": {"effort": "high"}}
    }
    # This endpoint refuses `enabled: false` ("Reasoning is mandatory"), so off is the floor.
    assert thinking(_Routed("openrouter/z-ai/glm-5.3-flash"), "none") == {
        "extra_body": {"reasoning": {"effort": "low"}}
    }


def test_an_effort_elsewhere_stays_the_parameter_and_no_effort_is_nothing() -> None:
    from raven_ppt.tools._composer import thinking

    assert thinking(_Routed("gpt-5", "https://api.openai.com/v1"), "low") == {"reasoning_effort": "low"}
    assert thinking(_Routed("openrouter/z-ai/glm-5.3-flash"), None) == {}
    assert thinking(_Routed("openrouter/z-ai/glm-5.3-flash"), "") == {}


def test_a_lazily_built_provider_still_names_its_route() -> None:
    """The host hands the engine a proxy that keeps its model private and answers
    `get_default_model()`. Reading only the attribute saw an empty string, so a glm
    behind OpenRouter was not recognised as being behind a gateway and every effort
    this engine set went out as the parameter litellm drops for that model: a live run
    spent 173 seconds on an intake call sized for 14."""
    from raven_ppt.tools._composer import thinking

    lazy = _Lazy(_Routed("openrouter/z-ai/glm-5.3-flash"))
    assert not hasattr(lazy, "default_model")
    assert thinking(lazy, "low") == {"extra_body": {"reasoning": {"effort": "low"}}}


def test_body_extras_ride_on_a_copy_of_the_shared_provider() -> None:
    """The provider is the author's too. Extras for the reader must not change what the
    loop sends, and must keep what the provider already carried."""
    shared = _Routed("openrouter/z-ai/glm-5.3-flash")
    composer = ProviderComposer(provider=shared, extra_body={"reasoning": {"effort": "low"}})

    sending = composer._sending()
    assert sending is not shared
    assert sending.extra_body == {"provider": {"order": ["x"]}, "reasoning": {"effort": "low"}}
    assert shared.extra_body == {"provider": {"order": ["x"]}}
    assert composer._sending() is sending, "the copy is taken once"
    assert ProviderComposer(provider=shared)._sending() is shared, "no extras, no copy"


@pytest.mark.asyncio
async def test_the_extras_reach_a_lazily_built_provider_once_it_is_built() -> None:
    """Where the extras used to be lost. A proxy has no `extra_body` attribute to copy
    until its first call materialises the provider behind it, so a copy taken when the
    tools were assembled found nothing and dropped the effort silently. The copy is
    taken at send time instead, and until one can be taken the call goes out on the
    proxy -- one call at the model's own effort being worth more than no call."""
    inner = _Routed("openrouter/z-ai/glm-5.3-flash")
    lazy = _Lazy(inner)
    composer = ProviderComposer(provider=lazy, extra_body={"reasoning": {"effort": "low"}})

    assert composer._sending() is lazy, "nothing built yet, so nothing to copy"
    await composer.ask("sys", [{"type": "text", "text": "q"}], max_tokens=100)

    sending = composer._sending()
    assert sending is not lazy and sending is not inner
    assert sending.extra_body == {"provider": {"order": ["x"]}, "reasoning": {"effort": "low"}}
    assert inner.extra_body == {"provider": {"order": ["x"]}}, "the author's provider is untouched"
