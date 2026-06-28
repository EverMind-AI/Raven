"""Unit tests for DecisionRouter (MS3)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from raven.proactive_engine.sentinel.executor.decision_router import DecisionRouter
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.types import PendingDecision, TaskOption


_NOW = datetime(2026, 5, 8, 8, 30)
_NOW_MS = int(_NOW.timestamp() * 1000)


# ── helpers ───────────────────────────────────────────────────────────


class _StubProvider:
    """Stub LLM provider returning a canned classifier response."""

    def __init__(self, intent: str | None, *,
                 option_index: int | None = None,
                 confidence: float = 0.95,
                 has_tool_calls: bool = True,
                 raw_args: str | None = None):
        if has_tool_calls:
            payload = {"intent": intent, "confidence": confidence}
            if option_index is not None:
                payload["option_index"] = option_index
            args_str = raw_args if raw_args is not None else json.dumps(payload)

            class _Call:
                arguments = args_str

            class _Resp:
                pass

            self._resp = _Resp()
            self._resp.has_tool_calls = True
            self._resp.tool_calls = [_Call()]
        else:
            class _Resp:
                pass
            self._resp = _Resp()
            self._resp.has_tool_calls = False
            self._resp.tool_calls = []
        self.calls: list[dict] = []

    async def chat_with_retry(self, *, messages, tools, model, tool_choice):
        self.calls.append({"messages": messages})
        return self._resp


def _decision(decision_id: str = "dec_test", channel: str = "feishu",
              to: str = "ou_xxx", n_options: int = 3) -> PendingDecision:
    options = [
        TaskOption(
            id=f"opt_{i}", title=f"option {i}", why=f"why {i}",
            type="ad_hoc", exec_kind="reply",
            exec_payload={"prompt": f"do {i}"},
            created_at_ms=_NOW_MS,
        )
        for i in range(1, n_options + 1)
    ]
    return PendingDecision(
        decision_id=decision_id,
        channel=channel,
        to=to,
        created_at_ms=_NOW_MS,
        ttl_min=60,
        options=options,
    )


@pytest.fixture
def pending_store(tmp_path: Path) -> PendingDecisionStore:
    return PendingDecisionStore(tmp_path / "pending.json")


# ── consumed=False when nothing pending ───────────────────────────────


@pytest.mark.asyncio
async def test_no_pending_decision_returns_consumed_false(pending_store):
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="hello"
    )
    assert result.consumed is False


@pytest.mark.asyncio
async def test_empty_content_returns_consumed_false(pending_store):
    pending_store.put(_decision())
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="   "
    )
    assert result.consumed is False


# ── /pick N regex tier ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pick_regex_consumes_with_confidence_one(pending_store):
    pending_store.put(_decision())
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)

    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="/pick 2"
    )
    assert result.consumed is True
    assert result.option is not None
    assert result.option.id == "opt_2"
    assert result.confidence == 1.0
    assert result.raw_match_method == "regex_pick"
    assert result.pending_decision_id == "dec_test"


@pytest.mark.asyncio
async def test_pick_regex_case_insensitive_and_whitespace(pending_store):
    pending_store.put(_decision())
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)

    for variant in ("/pick 1", "/PICK 1", "  /pick   1  ",
                    "/Pick 1\n"):
        result = await router.maybe_consume(
            channel="feishu", to="ou_xxx", content=variant
        )
        assert result.consumed is True, f"failed for {variant!r}"
        assert result.option.id == "opt_1"


@pytest.mark.asyncio
async def test_pick_regex_out_of_range_falls_through_to_no_match(pending_store):
    pending_store.put(_decision(n_options=3))
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)

    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="/pick 99"
    )
    # Decision is still live; we just don't consume it
    assert result.consumed is False


@pytest.mark.asyncio
async def test_pick_inside_sentence_does_not_match_regex(pending_store):
    pending_store.put(_decision())
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)
    # No LLM provider — only deterministic path. "I want to /pick 2 of these"
    # is anchored-regex-rejected → consumed=False.
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx",
        content="I want to /pick 2 of these"
    )
    assert result.consumed is False


@pytest.mark.asyncio
async def test_no_provider_means_only_pick_n_works(pending_store):
    pending_store.put(_decision())
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)
    # Plain "1" without /pick prefix → no match (no LLM available)
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="1"
    )
    assert result.consumed is False


# ── LLM classifier tier ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_classifier_pick_consumes(pending_store):
    pending_store.put(_decision())
    provider = _StubProvider(intent="pick", option_index=2, confidence=0.92)
    router = DecisionRouter(
        pending_store=pending_store,
        provider=provider, model="qwen3.5-27B",
        now_fn=lambda: _NOW,
    )

    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="选第二个"
    )
    assert result.consumed is True
    assert result.option.id == "opt_2"
    assert result.confidence == 0.92
    assert result.raw_match_method == "llm_classifier"


@pytest.mark.asyncio
async def test_llm_classifier_skip_returns_no_option(pending_store):
    pending_store.put(_decision())
    provider = _StubProvider(intent="skip", confidence=0.9)
    router = DecisionRouter(
        pending_store=pending_store, provider=provider,
        model="x", now_fn=lambda: _NOW,
    )

    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="跳过"
    )
    assert result.consumed is True
    assert result.option is None
    assert result.confidence == 0.9


@pytest.mark.asyncio
async def test_llm_low_confidence_treats_as_other(pending_store):
    pending_store.put(_decision())
    provider = _StubProvider(intent="pick", option_index=2, confidence=0.55)
    router = DecisionRouter(
        pending_store=pending_store, provider=provider, model="x",
        confidence_threshold=0.7, now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="都行你随便"
    )
    assert result.consumed is False
    assert result.confidence == 0.55
    assert result.raw_match_method == "llm_classifier"


@pytest.mark.asyncio
async def test_llm_intent_other_returns_consumed_false(pending_store):
    pending_store.put(_decision())
    provider = _StubProvider(intent="other", confidence=0.95)
    router = DecisionRouter(
        pending_store=pending_store, provider=provider, model="x",
        now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="今天天气不错"
    )
    assert result.consumed is False
    assert result.confidence == 0.95


@pytest.mark.asyncio
async def test_llm_pick_with_out_of_range_index_falls_through(pending_store):
    pending_store.put(_decision(n_options=3))
    provider = _StubProvider(intent="pick", option_index=5, confidence=0.99)
    router = DecisionRouter(
        pending_store=pending_store, provider=provider, model="x",
        now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="选第五个"
    )
    # Out-of-range from confident LLM → no consume (better safe than
    # executing a hallucinated option)
    assert result.consumed is False


@pytest.mark.asyncio
async def test_llm_no_tool_call_returns_consumed_false(pending_store):
    pending_store.put(_decision())
    provider = _StubProvider(intent=None, has_tool_calls=False)
    router = DecisionRouter(
        pending_store=pending_store, provider=provider, model="x",
        now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="不知道"
    )
    assert result.consumed is False


@pytest.mark.asyncio
async def test_llm_malformed_args_returns_consumed_false(pending_store):
    pending_store.put(_decision())
    provider = _StubProvider(intent=None, has_tool_calls=True,
                             raw_args="not json at all")
    router = DecisionRouter(
        pending_store=pending_store, provider=provider, model="x",
        now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="something"
    )
    assert result.consumed is False


@pytest.mark.asyncio
async def test_provider_exception_does_not_propagate(pending_store):
    """Defensive: a flaky LLM call should not crash AgentLoop."""
    pending_store.put(_decision())

    class _Boom:
        async def chat_with_retry(self, **kw):
            raise RuntimeError("oh no")

    router = DecisionRouter(
        pending_store=pending_store, provider=_Boom(), model="x",
        now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="选第一个"
    )
    assert result.consumed is False


# ── expired / cross-channel guards ────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_decision_is_skipped_by_get_recent(pending_store):
    """Sanity check: PendingDecisionStore handles expiry; router just
    sees None and returns consumed=False."""
    decision = _decision()
    decision.created_at_ms = _NOW_MS - 24 * 60 * 60_000  # 24h ago
    pending_store.put(decision)
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="/pick 1"
    )
    assert result.consumed is False


@pytest.mark.asyncio
async def test_other_channel_does_not_consume(pending_store):
    pending_store.put(_decision(channel="feishu", to="ou_xxx"))
    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)
    result = await router.maybe_consume(
        channel="cli", to="ou_xxx", content="/pick 1"
    )
    assert result.consumed is False
