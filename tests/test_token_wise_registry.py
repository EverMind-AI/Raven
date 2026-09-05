"""Tests for raven.token_wise.registry.StrategyRegistry."""

from __future__ import annotations

import pytest

from raven.token_wise.base import TokenStrategy, UsageSnapshot
from raven.token_wise.registry import StrategyRegistry


class _RecordingBefore(TokenStrategy):
    """Strategy that tags every message it sees so order can be verified."""

    def __init__(self, tag: str):
        self._tag = tag

    @property
    def name(self) -> str:
        return self._tag

    async def before_llm_call(self, messages, tools, model):
        # Append a sentinel message so we can read execution order from the chain.
        messages = messages + [{"role": "system", "content": f"tag:{self._tag}"}]
        return messages, tools, model


class _RecordingAfter(TokenStrategy):
    """Strategy that records every after_llm_call invocation."""

    def __init__(self, tag: str, sink: list[str]):
        self._tag = tag
        self._sink = sink

    @property
    def name(self) -> str:
        return self._tag

    async def after_llm_call(self, response, usage):
        self._sink.append(self._tag)


class _BoomBefore(TokenStrategy):
    name = "boom_before"

    async def before_llm_call(self, messages, tools, model):
        raise RuntimeError("intentional before failure")


class _BoomAfter(TokenStrategy):
    name = "boom_after"

    async def after_llm_call(self, response, usage):
        raise RuntimeError("intentional after failure")


def _snap() -> UsageSnapshot:
    return UsageSnapshot(model="m")


async def test_empty_registry_is_pass_through():
    reg = StrategyRegistry([])
    msgs, tools, model = await reg.before_llm_call([{"role": "user", "content": "hi"}], None, "m")
    assert msgs == [{"role": "user", "content": "hi"}]
    assert tools is None
    assert model == "m"
    await reg.after_llm_call({}, _snap())  # must not raise


async def test_before_hooks_run_in_registration_order():
    """Each strategy sees the output of all prior strategies."""
    reg = StrategyRegistry([_RecordingBefore("a"), _RecordingBefore("b"), _RecordingBefore("c")])
    msgs, _, _ = await reg.before_llm_call([], None, "m")
    tags = [m["content"] for m in msgs]
    assert tags == ["tag:a", "tag:b", "tag:c"]


async def test_after_hooks_run_in_registration_order():
    sink: list[str] = []
    reg = StrategyRegistry(
        [
            _RecordingAfter("first", sink),
            _RecordingAfter("second", sink),
            _RecordingAfter("third", sink),
        ]
    )
    await reg.after_llm_call({}, _snap())
    assert sink == ["first", "second", "third"]


async def test_before_hook_failure_propagates():
    """A bad pre-process must NOT be silently swallowed."""
    reg = StrategyRegistry([_BoomBefore()])
    with pytest.raises(RuntimeError, match="intentional before failure"):
        await reg.before_llm_call([], None, "m")


async def test_after_hook_failure_is_swallowed_other_strategies_still_run():
    """A failing telemetry hook must not abort downstream hooks or the loop."""
    sink: list[str] = []
    reg = StrategyRegistry(
        [
            _RecordingAfter("before_boom", sink),
            _BoomAfter(),
            _RecordingAfter("after_boom", sink),
        ]
    )
    # Should NOT raise.
    await reg.after_llm_call({}, _snap())
    # Both surrounding hooks still fired.
    assert sink == ["before_boom", "after_boom"]


def test_get_returns_strategy_by_name():
    a = _RecordingAfter("alpha", [])
    b = _RecordingAfter("beta", [])
    reg = StrategyRegistry([a, b])
    assert reg.get("alpha") is a
    assert reg.get("beta") is b
    assert reg.get("missing") is None


def test_register_appends():
    reg = StrategyRegistry([])
    a = _RecordingAfter("only", [])
    reg.register(a)
    assert len(reg) == 1
    assert reg.get("only") is a


def test_strategies_property_returns_copy():
    """Mutating the returned list must not affect the internal state."""
    reg = StrategyRegistry([_RecordingAfter("a", [])])
    snap = reg.strategies
    snap.clear()
    assert len(reg) == 1


async def test_registry_chains_model_changes():
    """A strategy can swap the model and downstream strategies see the new one."""
    seen: dict[str, str] = {}

    class _ModelChanger(TokenStrategy):
        name = "swap"

        async def before_llm_call(self, messages, tools, model):
            return messages, tools, "swapped-model"

    class _ModelObserver(TokenStrategy):
        name = "observe"

        async def before_llm_call(self, messages, tools, model):
            seen["model"] = model
            return messages, tools, model

    reg = StrategyRegistry([_ModelChanger(), _ModelObserver()])
    _, _, final = await reg.before_llm_call([], None, "original")
    assert final == "swapped-model"
    assert seen["model"] == "swapped-model"
