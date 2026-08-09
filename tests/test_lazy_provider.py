"""Unit tests for ``LazyProvider`` -- defers the real (litellm-importing) build."""

import asyncio
import threading
import time

import pytest

from raven.providers.base import GenerationSettings
from raven.providers.lazy import LazyProvider


class _FakeProvider:
    def __init__(self, *, emits_unparsed_reasoning: bool = False, active_endpoint_label: str | None = None) -> None:
        self.generation = GenerationSettings()
        self._emits_unparsed_reasoning = emits_unparsed_reasoning
        if active_endpoint_label is not None:
            self.active_endpoint_label = active_endpoint_label

    def get_default_model(self) -> str:
        return "built-model"

    def emits_unparsed_reasoning(self) -> bool:
        return self._emits_unparsed_reasoning

    async def chat(self, *args, **kwargs) -> str:
        return "chat"

    async def chat_stream(self, *args, **kwargs):
        yield "delta"

    async def chat_with_retry(self, *args, **kwargs) -> str:
        return "retry"


def _lazy(calls: list) -> LazyProvider:
    def factory() -> _FakeProvider:
        calls.append(1)
        return _FakeProvider()

    return LazyProvider(factory, default_model="cfg-model", generation=GenerationSettings(temperature=0.5))


def test_construction_and_config_reads_do_not_build() -> None:
    calls: list = []
    lp = _lazy(calls)

    assert calls == []  # constructing did not call the factory
    assert lp.get_default_model() == "cfg-model"  # from config, not the built provider
    assert lp.generation.temperature == 0.5  # from config
    assert calls == []  # neither read triggered a build


def test_first_chat_builds_and_memoizes() -> None:
    calls: list = []
    lp = _lazy(calls)

    assert asyncio.run(lp.chat([])) == "chat"
    assert calls == [1]
    assert asyncio.run(lp.chat([])) == "chat"
    assert calls == [1]  # not rebuilt


def test_chat_stream_and_retry_delegate() -> None:
    calls: list = []
    lp = _lazy(calls)

    async def _drain():
        return [d async for d in lp.chat_stream([])]

    assert asyncio.run(_drain()) == ["delta"]
    assert asyncio.run(lp.chat_with_retry([])) == "retry"
    assert calls == [1]  # one build shared across both


def test_built_is_thread_safe() -> None:
    calls: list = []

    def factory() -> _FakeProvider:
        time.sleep(0.05)  # widen the race window
        calls.append(1)
        return _FakeProvider()

    lp = LazyProvider(factory, "cfg-model", GenerationSettings())
    threads = [threading.Thread(target=lp._built) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == [1]  # built exactly once despite concurrent access


def test_prewarm_builds_in_background() -> None:
    built = threading.Event()

    def factory() -> _FakeProvider:
        built.set()
        return _FakeProvider()

    lp = LazyProvider(factory, "cfg-model", GenerationSettings())
    lp.prewarm()

    assert built.wait(timeout=2.0), "prewarm did not build the provider in the background"


def test_emits_unparsed_reasoning_defaults_false_before_materialization() -> None:
    """The stream collation that asks this only runs after a call, and the
    first call is what builds the inner provider -- before that there is
    nothing to normalize anyway, so the answer must be False without ever
    invoking the factory."""
    calls: list = []

    def factory() -> _FakeProvider:
        calls.append(1)
        return _FakeProvider(emits_unparsed_reasoning=True)

    lp = LazyProvider(factory, "cfg-model", GenerationSettings())

    assert lp.emits_unparsed_reasoning() is False
    assert calls == []  # asking did not build the provider


def test_emits_unparsed_reasoning_forwards_after_materialization() -> None:
    """Once built, the answer is the real provider's -- not the pre-build
    default -- so the TUI's think-tag normalization (which reads this) keeps
    working on the primary path through ``make_lazy_provider``."""

    def factory() -> _FakeProvider:
        return _FakeProvider(emits_unparsed_reasoning=True)

    lp = LazyProvider(factory, "cfg-model", GenerationSettings())
    asyncio.run(lp.chat([]))  # materializes _provider

    assert lp.emits_unparsed_reasoning() is True


def test_active_endpoint_label_is_the_initial_label_before_materialization() -> None:
    lp = LazyProvider(
        lambda: _FakeProvider(),
        "cfg-model",
        GenerationSettings(),
        initial_endpoint_label="first",
    )

    assert lp.active_endpoint_label == "first"


def test_active_endpoint_label_is_none_when_no_initial_label_was_given() -> None:
    lp = LazyProvider(lambda: _FakeProvider(), "cfg-model", GenerationSettings())

    assert lp.active_endpoint_label is None


def test_active_endpoint_label_forwards_to_the_real_provider_after_materialization() -> None:
    """After the first call, the rotor behind the real provider may have
    rotated -- the footer must reflect that, not stay pinned to the initial
    label forever."""

    def factory() -> _FakeProvider:
        return _FakeProvider(active_endpoint_label="second")

    lp = LazyProvider(factory, "cfg-model", GenerationSettings(), initial_endpoint_label="first")
    asyncio.run(lp.chat([]))

    assert lp.active_endpoint_label == "second"


def test_prewarm_swallows_build_error() -> None:
    def factory():
        raise RuntimeError("boom")

    lp = LazyProvider(factory, "cfg-model", GenerationSettings())
    lp.prewarm()  # must not raise
    time.sleep(0.05)
    # the error surfaces on a real call instead
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(lp.chat([]))
