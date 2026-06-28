"""Tests for the require_confirm=True two-step flow (MS7).

Covers the second-leg yes/no parsing in DecisionRouter +
DecisionConsumer's awaiting_confirm state machine."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest
from raven.proactive_engine.sentinel.executor.action_executor import ActionExecutor
from raven.proactive_engine.sentinel.executor.decision_consumer import DecisionConsumer, MenuReply
from raven.proactive_engine.sentinel.executor.decision_router import DecisionRouter
from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.types import (
    PendingDecision,
    TaskOption,
)


_NOW = datetime(2026, 5, 8, 9, 0)
_NOW_MS = int(_NOW.timestamp() * 1000)


# ── helpers ───────────────────────────────────────────────────────────


def _option(oid: str = "opt_1", title: str = "草拟回复 X") -> TaskOption:
    return TaskOption(
        id=oid, title=title, why="why",
        type="ad_hoc", exec_kind="reply",
        exec_payload={"prompt": f"do the thing for {oid}"},
        created_at_ms=_NOW_MS,
    )


def _decision(*, options: list[TaskOption] | None = None) -> PendingDecision:
    return PendingDecision(
        decision_id="dec_x",
        channel="feishu",
        to="ou_xxx",
        created_at_ms=_NOW_MS,
        ttl_min=60,
        options=options or [_option("opt_1", "任务一"),
                            _option("opt_2", "任务二")],
    )


def _msg(content: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="feishu", chat_id="ou_xxx", sender_id="user",
            chat_type=ChatType.DM,
        ),
        text=content,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def pending_store(tmp_path: Path) -> PendingDecisionStore:
    return PendingDecisionStore(tmp_path / "pending.json")


@pytest.fixture
def feedback(workspace: Path) -> NudgeFeedbackTracker:
    return NudgeFeedbackTracker(workspace / "sentinel_feedback.jsonl")


def _make_consumer(
    *, pending_store: PendingDecisionStore,
    feedback: NudgeFeedbackTracker | None = None,
    router_provider=None,
) -> DecisionConsumer:
    router = DecisionRouter(
        pending_store=pending_store,
        provider=router_provider,
        model="x" if router_provider else None,
        now_fn=lambda: _NOW,
    )
    executor = ActionExecutor(now_fn=lambda: _NOW)
    submitted: list = []
    executor.set_submit(submitted.append)
    consumer = DecisionConsumer(
        router=router, executor=executor,
        pending_store=pending_store, feedback=feedback,
        require_confirm=True,         # ← THIS file's whole purpose
        now_fn=lambda: _NOW,
    )
    consumer._submitted = submitted  # captured exec-reply TurnRequests
    return consumer


# ── PendingDecisionStore confirm-state methods ────────────────────────


def test_mark_awaiting_confirm_sets_state(pending_store):
    pending_store.put(_decision())
    outcome = pending_store.mark_awaiting_confirm(
        "dec_x", picked_option_id="opt_1", picked_at_ms=_NOW_MS + 100,
    )
    assert outcome == PendingDecisionStore.AWAIT_OK

    raw = pending_store._store.load()["decisions"][0]
    assert raw["awaiting_confirm"] is True
    assert raw["picked_option_id"] == "opt_1"
    assert raw["picked_at_ms"] == _NOW_MS + 100
    assert raw["consumed"] is False


def test_mark_awaiting_confirm_idempotent_returns_already_second_time(
    pending_store,
):
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)
    # Second call returns AWAIT_ALREADY, distinct from AWAIT_NOT_FOUND
    # / AWAIT_CONSUMED — caller can decide whether to re-prompt.
    outcome = pending_store.mark_awaiting_confirm(
        "dec_x", picked_option_id="opt_2", picked_at_ms=_NOW_MS + 100,
    )
    assert outcome == PendingDecisionStore.AWAIT_ALREADY
    raw = pending_store._store.load()["decisions"][0]
    assert raw["picked_option_id"] == "opt_1"  # original preserved


def test_mark_awaiting_confirm_returns_not_found_for_missing(pending_store):
    outcome = pending_store.mark_awaiting_confirm(
        "dec_missing", picked_option_id="opt_1", picked_at_ms=_NOW_MS,
    )
    assert outcome == PendingDecisionStore.AWAIT_NOT_FOUND


def test_mark_awaiting_confirm_returns_consumed_for_already_consumed(
    pending_store,
):
    pending_store.put(_decision())
    pending_store.mark_consumed("dec_x", picked_option_id="opt_1",
                                consumed_at_ms=_NOW_MS)
    outcome = pending_store.mark_awaiting_confirm(
        "dec_x", picked_option_id="opt_2", picked_at_ms=_NOW_MS + 100,
    )
    assert outcome == PendingDecisionStore.AWAIT_CONSUMED


def test_cancel_confirm_marks_consumed_with_no_pick(pending_store):
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)

    ok = pending_store.cancel_confirm("dec_x", cancelled_at_ms=_NOW_MS + 200)
    assert ok is True

    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is True
    assert raw["picked_option_id"] is None
    assert raw["awaiting_confirm"] is False
    assert raw["consumed_at_ms"] == _NOW_MS + 200


def test_cancel_confirm_returns_false_when_not_awaiting(pending_store):
    pending_store.put(_decision())
    # Not in awaiting_confirm state — cancel is no-op
    ok = pending_store.cancel_confirm("dec_x", cancelled_at_ms=_NOW_MS)
    assert ok is False


def test_get_recent_returns_awaiting_confirm_decision(pending_store):
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)
    # Router needs to find decisions in AWAITING_CONFIRM state to parse
    # the second-leg yes/no reply.
    fetched = pending_store.get_recent("feishu", "ou_xxx",
                                       now_ms=_NOW_MS + 300)
    assert fetched is not None
    assert fetched.awaiting_confirm is True
    assert fetched.picked_option_id == "opt_1"


# ── DecisionRouter confirm-mode parsing ───────────────────────────────


@pytest.mark.asyncio
async def test_router_yes_regex_in_confirm_mode(pending_store):
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)

    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)

    for variant in ("yes", "YES", "y", "是", "确认", "好", "ok", "嗯",
                    "yes!", "  yes  "):
        result = await router.maybe_consume(
            channel="feishu", to="ou_xxx", content=variant,
        )
        assert result.consumed is True, f"failed for {variant!r}"
        assert result.confirm_intent == "confirm"
        assert result.option is not None
        assert result.option.id == "opt_1"
        assert result.raw_match_method == "regex_yesno"


@pytest.mark.asyncio
async def test_router_no_regex_in_confirm_mode(pending_store):
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)

    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)

    for variant in ("no", "NO", "n", "否", "取消", "不", "算了", "cancel"):
        result = await router.maybe_consume(
            channel="feishu", to="ou_xxx", content=variant,
        )
        assert result.consumed is True, f"failed for {variant!r}"
        assert result.confirm_intent == "cancel"


@pytest.mark.asyncio
async def test_router_pick_n_in_confirm_mode_falls_through(pending_store):
    """In awaiting_confirm state, /pick N is meaningless (the decision
    already has a picked option). Router should ignore it."""
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)

    router = DecisionRouter(pending_store=pending_store, now_fn=lambda: _NOW)
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="/pick 2",
    )
    assert result.consumed is False


@pytest.mark.asyncio
async def test_router_llm_confirm_classifier_high_confidence(pending_store):
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)

    class _Provider:
        async def chat_with_retry(self, *, messages, tools, model, tool_choice):
            class _Call:
                arguments = json.dumps({"intent": "confirm",
                                        "confidence": 0.92})
            class _Resp:
                has_tool_calls = True
                tool_calls = [_Call()]
            return _Resp()

    router = DecisionRouter(
        pending_store=pending_store,
        provider=_Provider(), model="x",
        now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="行吧那就这样",
    )
    assert result.consumed is True
    assert result.confirm_intent == "confirm"
    assert result.confidence == 0.92


@pytest.mark.asyncio
async def test_router_llm_confirm_low_confidence_falls_through(pending_store):
    pending_store.put(_decision())
    pending_store.mark_awaiting_confirm("dec_x", picked_option_id="opt_1",
                                        picked_at_ms=_NOW_MS)

    class _Provider:
        async def chat_with_retry(self, *, messages, tools, model, tool_choice):
            class _Call:
                arguments = json.dumps({"intent": "other",
                                        "confidence": 0.4})
            class _Resp:
                has_tool_calls = True
                tool_calls = [_Call()]
            return _Resp()

    router = DecisionRouter(
        pending_store=pending_store,
        provider=_Provider(), model="x",
        confidence_threshold=0.7,
        now_fn=lambda: _NOW,
    )
    result = await router.maybe_consume(
        channel="feishu", to="ou_xxx", content="什么意思",
    )
    assert result.consumed is False


# ── DecisionConsumer two-step state machine ───────────────────────────


@pytest.mark.asyncio
async def test_first_leg_pick_emits_confirm_prompt_no_execute(
    pending_store, feedback,
):
    pending_store.put(_decision())
    consumer = _make_consumer(
        pending_store=pending_store, feedback=feedback,
    )

    out = await consumer(_msg("/pick 2"))
    assert isinstance(out, MenuReply)
    assert "要执行" in out.content
    assert "任务二" in out.content
    assert "yes" in out.content.lower() or "确认" in out.content

    # Decision parked in AWAITING_CONFIRM
    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is False
    assert raw["awaiting_confirm"] is True
    assert raw["picked_option_id"] == "opt_2"

    # Bus has NOT received an executor-injected user prompt
    assert consumer._submitted == [], "executor should not have run"

    # No accept/dismiss recorded yet (confirm hasn't happened)
    counts = feedback.counts(since_days=7)
    assert counts.get("accepted", 0) == 0
    assert counts.get("dismissed", 0) == 0


@pytest.mark.asyncio
async def test_second_leg_yes_executes_and_records_accepted(
    pending_store, feedback,
):
    pending_store.put(_decision())
    consumer = _make_consumer(
        pending_store=pending_store, feedback=feedback,
    )

    # First leg
    out1 = await consumer(_msg("/pick 2"))
    assert "要执行" in out1.content

    # Second leg — confirm
    out2 = await consumer(_msg("yes"))
    assert isinstance(out2, MenuReply)
    # Output should be the executor's success message (not the confirm prompt)
    assert "已为您发起" in out2.content or "任务二" in out2.content

    # Decision fully consumed
    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is True
    assert raw["awaiting_confirm"] is False
    assert raw["picked_option_id"] == "opt_2"

    # Executor was actually invoked — it submitted the injected user prompt
    assert len(consumer._submitted) == 1
    assert "do the thing for opt_2" in consumer._submitted[0].text

    # Feedback recorded as accepted
    counts = feedback.counts(since_days=7)
    assert counts.get("accepted", 0) == 1
    assert counts.get("dismissed", 0) == 0


@pytest.mark.asyncio
async def test_second_leg_no_cancels_no_execute(pending_store, feedback):
    pending_store.put(_decision())
    consumer = _make_consumer(
        pending_store=pending_store, feedback=feedback,
    )

    # First leg
    await consumer(_msg("/pick 1"))

    # Second leg — cancel
    out = await consumer(_msg("no"))
    assert "取消" in out.content

    # Decision marked cancelled (consumed=True, picked=None)
    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is True
    assert raw["picked_option_id"] is None
    assert raw["awaiting_confirm"] is False

    # Executor not invoked
    assert consumer._submitted == []

    # Feedback recorded as dismissed
    counts = feedback.counts(since_days=7)
    assert counts.get("accepted", 0) == 0
    assert counts.get("dismissed", 0) == 1


@pytest.mark.asyncio
async def test_ambiguous_reply_during_awaiting_falls_through(
    pending_store, feedback,
):
    """If the user says something not yes/no (e.g. asks an unrelated
    question), router falls through (consumed=False) and the decision
    stays awaiting until they explicitly confirm/cancel or TTL expires."""
    pending_store.put(_decision())
    consumer = _make_consumer(
        pending_store=pending_store, feedback=feedback,
    )

    await consumer(_msg("/pick 1"))   # park awaiting_confirm

    # Ambiguous reply — no LLM provider configured, so confirm-mode
    # only matches yes/no regex; everything else falls through.
    out = await consumer(_msg("今天天气怎样"))
    assert out is None  # consumer falls through; AgentLoop processes normally

    # Decision still awaiting
    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is False
    assert raw["awaiting_confirm"] is True


@pytest.mark.asyncio
async def test_skip_does_not_use_confirm_path(pending_store, feedback):
    """First-leg skip ('跳过') should mark dismissed without going
    through awaiting_confirm — there's nothing to confirm."""
    pending_store.put(_decision())

    class _SkipProvider:
        async def chat_with_retry(self, *, messages, tools, model, tool_choice):
            class _Call:
                arguments = json.dumps({"intent": "skip", "confidence": 0.95})
            class _Resp:
                has_tool_calls = True
                tool_calls = [_Call()]
            return _Resp()

    consumer = _make_consumer(
        pending_store=pending_store, feedback=feedback,
        router_provider=_SkipProvider(),
    )

    out = await consumer(_msg("跳过"))
    assert "跳过" in out.content or "好的" in out.content

    raw = pending_store._store.load()["decisions"][0]
    assert raw["consumed"] is True
    assert raw["awaiting_confirm"] is False
    assert raw["picked_option_id"] is None

    counts = feedback.counts(since_days=7)
    assert counts.get("dismissed", 0) == 1
