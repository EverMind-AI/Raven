"""Rotation and failover across a provider section's several endpoints.

Covers:
- sticky: first endpoint stays "the" endpoint until it fails, cooldown
  transfers to the next, and expiry restores it
- round_robin: the cursor advances by one on every call
- an endpoint-agnostic error (invalid_request) never rotates; auth does
- every endpoint cooling still dispatches (no deadlock)
- chat_stream: transfer before the first delta, no transfer after it,
  no token replay
- _chat_attempt_with_retry composes with the base class's own model-chain
  fallback (chat_with_retry)
- cooldown doubles per failure, capped, and clears on success
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.providers import endpoint_rotor
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse, StreamDelta
from raven.providers.endpoint_rotor import EndpointRotorProvider, RotorState
from raven.providers.endpoints import ResolvedEndpoint


class _StubInner(LLMProvider):
    """Records calls; ``chat()`` pops one scripted response per call and
    ``chat_stream()`` pops one scripted list of deltas/exceptions per call.

    An empty queue defaults to a plain "ok" response/stream so tests that
    don't care about a given endpoint's exact reply don't need to script it.
    """

    def __init__(self, name: str, chat_script: list[Any] | None = None, stream_script: list[list[Any]] | None = None):
        super().__init__(api_key="test")
        self.name = name
        self._chat_script = list(chat_script or [])
        self._stream_script = list(stream_script or [])
        self.chat_calls = 0
        self.stream_calls = 0
        self._CHAT_RETRY_DELAYS = (0, 0, 0)

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.chat_calls += 1
        if self._chat_script:
            return self._chat_script.pop(0)
        return LLMResponse(content=f"ok:{self.name}", finish_reason="stop")

    async def chat_stream(self, messages, tools=None, model=None, **kwargs):
        self.stream_calls += 1
        script = self._stream_script.pop(0) if self._stream_script else [StreamDelta(content=f"ok:{self.name}")]
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item

    def get_default_model(self) -> str:
        return self.name


def _endpoint(label: str) -> ResolvedEndpoint:
    return ResolvedEndpoint(label=label, api_key=f"key-{label}", api_base=None, extra_headers=None)


def _make_rotor(inners: list[_StubInner], strategy: str = "sticky") -> EndpointRotorProvider:
    endpoints = [_endpoint(inner.name) for inner in inners]
    by_label = {e.label: inner for e, inner in zip(endpoints, inners)}
    return EndpointRotorProvider(
        endpoints=endpoints,
        make_inner=lambda e: by_label[e.label],
        default_model="rotor-default",
        strategy=strategy,
    )


_FALLBACK_FATAL = ErrorClassification(category="model_unavailable", should_fallback=True)
_NON_FALLBACK_FATAL = ErrorClassification(category="auth")


class _Clock:
    """Monotonic stand-in a test can advance explicitly."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(endpoint_rotor.time, "monotonic", c)
    return c


async def test_sticky_cools_on_fallback_error_and_recovers_after_expiry(clock):
    e0 = _StubInner("e0", chat_script=[LLMResponse(content="ok0", finish_reason="stop")])
    e1 = _StubInner("e1", chat_script=[LLMResponse(content="ok1", finish_reason="stop")])
    rotor = _make_rotor([e0, e1], strategy="sticky")

    resp = await rotor.chat_with_retry(messages=[], model="m")
    assert resp.content == "ok0"
    assert (e0.chat_calls, e1.chat_calls) == (1, 0)

    e0._chat_script.append(
        LLMResponse(content="e0 unavailable", finish_reason="error", error_classification=_FALLBACK_FATAL)
    )
    resp = await rotor.chat_with_retry(messages=[], model="m")
    assert resp.content == "ok1"
    assert (e0.chat_calls, e1.chat_calls) == (2, 1)

    # e0 is cooling: sticky skips it and goes straight to e1 again, no new e0 attempt.
    e1._chat_script.append(LLMResponse(content="ok1 again", finish_reason="stop"))
    resp = await rotor.chat_with_retry(messages=[], model="m")
    assert resp.content == "ok1 again"
    assert (e0.chat_calls, e1.chat_calls) == (2, 2)

    # Cooldown (30s for a first failure) expires: sticky returns to e0.
    clock.now += 30.0
    e0._chat_script.append(LLMResponse(content="ok0 again", finish_reason="stop"))
    resp = await rotor.chat_with_retry(messages=[], model="m")
    assert resp.content == "ok0 again"
    assert (e0.chat_calls, e1.chat_calls) == (3, 2)


async def test_round_robin_cursor_advances_each_call(clock):
    e0 = _StubInner("e0")
    e1 = _StubInner("e1")
    e2 = _StubInner("e2")
    rotor = _make_rotor([e0, e1, e2], strategy="round_robin")

    order_seen = []
    for _ in range(4):
        resp = await rotor.chat_with_retry(messages=[], model="m")
        order_seen.append(resp.content)

    # Cursor starts at 0 and advances by one on every call, wrapping at 3.
    assert order_seen == ["ok:e0", "ok:e1", "ok:e2", "ok:e0"]
    assert (e0.chat_calls, e1.chat_calls, e2.chat_calls) == (2, 1, 1)


async def test_active_endpoint_label_names_the_next_endpoint_without_rotating(clock):
    """The banner reads this to say which account is answering. Asking is not a
    request, so it must not consume a round-robin slot -- a getter that advanced
    the cursor would skip an endpoint on every render."""
    e0 = _StubInner("e0")
    e1 = _StubInner("e1")
    rotor = _make_rotor([e0, e1], strategy="round_robin")

    assert [rotor.active_endpoint_label for _ in range(3)] == ["e0", "e0", "e0"]

    await rotor.chat_with_retry(messages=[], model="m")

    assert rotor.active_endpoint_label == "e1"


async def test_active_endpoint_label_skips_a_cooling_endpoint(clock):
    """It names where the next request would land, which under sticky is the
    first endpoint that is not cooling -- not simply the first one."""
    e0 = _StubInner(
        "e0",
        chat_script=[
            LLMResponse(content="e0 unavailable", finish_reason="error", error_classification=_FALLBACK_FATAL)
        ],
    )
    e1 = _StubInner("e1")
    rotor = _make_rotor([e0, e1], strategy="sticky")

    assert rotor.active_endpoint_label == "e0"

    await rotor.chat_with_retry(messages=[], model="m")
    assert rotor.active_endpoint_label == "e1"

    clock.now += 30.0
    assert rotor.active_endpoint_label == "e0"


async def test_endpoint_agnostic_error_returns_immediately_without_rotating(clock):
    e0 = _StubInner(
        "e0",
        chat_script=[
            LLMResponse(
                content="400 invalid request",
                finish_reason="error",
                error_classification=ErrorClassification(category="invalid_request"),
            )
        ],
    )
    e1 = _StubInner("e1")
    rotor = _make_rotor([e0, e1], strategy="sticky")

    resp = await rotor.chat_with_retry(messages=[], model="m")

    assert resp.finish_reason == "error"
    assert resp.content == "400 invalid request"
    assert (e0.chat_calls, e1.chat_calls) == (1, 0)
    # A fatal-but-endpoint-agnostic error must not cool the endpoint either --
    # there was nothing wrong with the endpoint, the request was malformed.
    assert rotor._state.is_cooling(0, clock.now) is False


async def test_an_auth_failure_rotates_to_the_next_account(clock):
    """A dead key is exactly the failure a second account exists for.

    auth never falls back across models -- the same key fails for every
    model -- but each endpoint is its own account with its own key, so the
    rotor judges it by its own predicate. Measured on a live OpenRouter
    wire before this test existed: judging by should_fallback alone
    returned the 401 with a healthy endpoint sitting right behind it.
    """
    e0 = _StubInner(
        "e0",
        chat_script=[
            LLMResponse(content="401 unauthorized", finish_reason="error", error_classification=_NON_FALLBACK_FATAL)
        ],
    )
    e1 = _StubInner("e1")
    rotor = _make_rotor([e0, e1], strategy="sticky")

    resp = await rotor.chat_with_retry(messages=[], model="m")

    assert resp.finish_reason != "error"
    assert (e0.chat_calls, e1.chat_calls) == (1, 1)
    assert rotor._state.is_cooling(0, clock.now) is True


async def test_all_endpoints_cooling_still_dispatches_in_order(clock):
    e0 = _StubInner("e0", chat_script=[LLMResponse(content="ok0", finish_reason="stop")])
    e1 = _StubInner("e1")
    rotor = _make_rotor([e0, e1], strategy="sticky")

    rotor._state.mark_failure(0, clock.now)
    rotor._state.mark_failure(1, clock.now)
    assert rotor._healthy_order() == [0, 1]

    resp = await rotor.chat_with_retry(messages=[], model="m")

    assert resp.content == "ok0"
    assert e0.chat_calls == 1


async def test_stream_transfers_on_open_exception_before_any_delta(clock):
    e0 = _StubInner("e0", stream_script=[[RuntimeError("503 service unavailable")]])
    e1 = _StubInner("e1", stream_script=[[StreamDelta(content="hi"), StreamDelta(content=" world")]])
    rotor = _make_rotor([e0, e1], strategy="sticky")

    seen = [d.content async for d in rotor.chat_stream(messages=[])]

    assert seen == ["hi", " world"]
    assert (e0.stream_calls, e1.stream_calls) == (1, 1)
    assert rotor._state.is_cooling(0, clock.now) is True


async def test_stream_transfers_on_error_terminal_first_delta(clock):
    error_delta = StreamDelta(content=None, finish_reason="error", error_classification=_FALLBACK_FATAL)
    e0 = _StubInner("e0", stream_script=[[error_delta]])
    e1 = _StubInner("e1", stream_script=[[StreamDelta(content="ok")]])
    rotor = _make_rotor([e0, e1], strategy="sticky")

    seen = [d.content async for d in rotor.chat_stream(messages=[])]

    assert seen == ["ok"]
    assert (e0.stream_calls, e1.stream_calls) == (1, 1)
    assert rotor._state.is_cooling(0, clock.now) is True


async def test_stream_does_not_transfer_after_a_normal_first_delta(clock):
    e0 = _StubInner("e0", stream_script=[[StreamDelta(content="a"), RuntimeError("mid-stream boom")]])
    e1 = _StubInner("e1", stream_script=[[StreamDelta(content="should-not-be-used")]])
    rotor = _make_rotor([e0, e1], strategy="sticky")

    seen = []
    with pytest.raises(RuntimeError, match="mid-stream boom"):
        async for d in rotor.chat_stream(messages=[]):
            seen.append(d.content)

    # The token before the failure was delivered exactly once, not replayed
    # from a second endpoint, and e1 was never even tried.
    assert seen == ["a"]
    assert e1.stream_calls == 0


async def test_chat_attempt_with_retry_composes_with_base_model_chain_fallback(clock):
    """Both endpoints exhaust on model A (should_fallback) -> the base
    class's own chat_with_retry moves to model B -> the rotor answers B from
    whichever endpoint is tried first (both are cooling by then, so sticky
    ignores cooldown and starts back at e0)."""
    e0 = _StubInner(
        "e0",
        chat_script=[
            LLMResponse(content="a unavailable on e0", finish_reason="error", error_classification=_FALLBACK_FATAL),
            LLMResponse(content="recovered", finish_reason="stop"),
        ],
    )
    e1 = _StubInner(
        "e1",
        chat_script=[
            LLMResponse(content="a unavailable on e1", finish_reason="error", error_classification=_FALLBACK_FATAL),
        ],
    )
    rotor = _make_rotor([e0, e1], strategy="sticky")

    resp = await rotor.chat_with_retry(messages=[], model="A", fallback_models=["B"])

    assert resp.content == "recovered"
    assert (e0.chat_calls, e1.chat_calls) == (2, 1)


async def test_all_endpoints_exhausted_logs_a_warning_naming_each_attempt(clock, caplog):
    """Without this, exhausting every endpoint hands the caller only the last
    endpoint's error -- no way to tell the others were tried and failed too,
    rather than skipped."""
    import logging

    from loguru import logger

    e0 = _StubInner(
        "e0",
        chat_script=[LLMResponse(content="down on e0", finish_reason="error", error_classification=_FALLBACK_FATAL)],
    )
    e1 = _StubInner(
        "e1",
        chat_script=[LLMResponse(content="down on e1", finish_reason="error", error_classification=_FALLBACK_FATAL)],
    )
    rotor = _make_rotor([e0, e1], strategy="sticky")

    # Bridge loguru -> stdlib caplog (loguru doesn't write to logging by default)
    handler_id = logger.add(lambda msg: logging.getLogger("loguru.bridge").warning(msg), level="WARNING")
    try:
        with caplog.at_level(logging.WARNING, logger="loguru.bridge"):
            resp = await rotor.chat_with_retry(messages=[], model="m", fallback_models=[])
    finally:
        logger.remove(handler_id)

    assert resp.content == "down on e1"
    text = "\n".join(rec.message for rec in caplog.records)
    assert "e0" in text
    assert "e1" in text
    assert _FALLBACK_FATAL.category in text


async def test_generation_assigned_after_construction_propagates_to_every_inner(clock):
    """``make_provider`` builds the rotor, then assigns ``provider.generation =
    GenerationSettings(...)`` from config -- see ``raven/cli/_helpers.py``.
    Without push-down each inner keeps the base class's untouched default
    (600s timeout, temperature 0.7, ...), so a configured timeout is silently
    ignored on every actual request."""
    from raven.providers.base import GenerationSettings

    e0 = _StubInner("e0")
    e1 = _StubInner("e1")
    rotor = _make_rotor([e0, e1], strategy="sticky")

    settings = GenerationSettings(temperature=0.1, max_tokens=99, timeout=12.5)
    rotor.generation = settings

    assert e0.generation is settings
    assert e1.generation is settings
    assert e0.generation.timeout == 12.5
    assert e1.generation.timeout == 12.5


def test_emits_unparsed_reasoning_delegates_to_the_first_inner(clock):
    """Same reasoning as ``can_serve``: every endpoint under one rotor is the
    same vendor/section, so the shape of its wire is one answer, not one per
    endpoint."""
    e0 = _StubInner("e0")
    e0.emits_unparsed_reasoning = lambda: True
    e1 = _StubInner("e1")
    e1.emits_unparsed_reasoning = lambda: False
    rotor = _make_rotor([e0, e1], strategy="sticky")

    assert rotor.emits_unparsed_reasoning() is True


async def test_cooldown_doubles_per_failure_capped_and_clears_on_success(clock):
    state = RotorState()

    state.mark_failure(0, clock.now)
    assert state.cooldown_until[0] == pytest.approx(30.0)

    clock.now = 30.0
    state.mark_failure(0, clock.now)
    assert state.cooldown_until[0] == pytest.approx(30.0 + 60.0)

    clock.now = 90.0
    state.mark_failure(0, clock.now)
    assert state.cooldown_until[0] == pytest.approx(90.0 + 120.0)

    # Keep failing until the doubling would exceed the cap; it must clamp.
    clock.now = 500.0
    state.failure_count[0] = 10  # next doubled value (30 * 2**10) is far past the cap
    state.mark_failure(0, clock.now)
    assert state.cooldown_until[0] == pytest.approx(500.0 + 300.0)

    state.mark_success(0)
    assert state.failure_count[0] == 0
    assert state.is_cooling(0, clock.now) is False
