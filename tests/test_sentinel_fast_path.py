"""Unit tests for SentinelRunner._fast_path_rules.

Covers the two active rules:
 (a) quiet_hours hard-hit → fast skip
 (b) context unchanged since last skip → fast skip
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from raven.proactive_engine.sentinel.executor.runner import SentinelRunner
from raven.proactive_engine.sentinel.types import (
    NudgePolicyState,
    PlannerContext,
    PlannerDecision,
)


def _mk_runner(decide_return: PlannerDecision | None = None) -> SentinelRunner:
    """Build a SentinelRunner with mocked planner/assembler/policy."""
    planner = MagicMock()
    if decide_return is not None:

        async def _decide(ctx):
            return decide_return

        planner.decide.side_effect = _decide
    assembler = MagicMock()
    policy = MagicMock()
    return SentinelRunner(
        planner=planner,
        assembler=assembler,
        policy=policy,
    )


def _mk_ctx(
    *,
    quiet: bool = False,
    memory: str = "",
    history: str = "",
    last_decision: PlannerDecision | None = None,
    active_sessions=None,
) -> PlannerContext:
    return PlannerContext(
        now=datetime(2026, 4, 22, 14, 0),
        memory_md=memory,
        history_md_recent=history,
        active_sessions=active_sessions or [],
        nudge_policy_state=NudgePolicyState(in_quiet_hours=quiet),
        last_decision=last_decision,
    )


# ── Rule (a): quiet hours ─────────────────────────────────────────────────


def test_fast_path_quiet_hours_returns_skip():
    runner = _mk_runner()
    dec = runner._fast_path_rules(_mk_ctx(quiet=True))
    assert dec is not None
    assert dec.action == "skip"
    assert "quiet_hours" in dec.reason


def test_fast_path_not_quiet_returns_none():
    runner = _mk_runner()
    dec = runner._fast_path_rules(_mk_ctx(quiet=False))
    assert dec is None


# ── Rule (b): context-unchanged dedup ─────────────────────────────────────


def test_fast_path_repeat_context_skips_on_same_signature():
    runner = _mk_runner()
    ctx1 = _mk_ctx(memory="hello", history="[14:00] event", quiet=False)
    sig = runner._context_signature(ctx1)

    last = PlannerDecision(action="skip", reason="nothing to do")
    setattr(last, "_ctx_signature", sig)

    ctx2 = _mk_ctx(memory="hello", history="[14:00] event", quiet=False, last_decision=last)
    dec = runner._fast_path_rules(ctx2)
    assert dec is not None
    assert dec.action == "skip"
    assert "context unchanged" in dec.reason


def test_fast_path_different_context_does_not_dedup():
    runner = _mk_runner()
    ctx1 = _mk_ctx(memory="hello", quiet=False)
    sig = runner._context_signature(ctx1)

    last = PlannerDecision(action="skip", reason="nothing to do")
    setattr(last, "_ctx_signature", sig)

    # memory changed → signature differs → fast-path doesn't fire rule (b)
    ctx2 = _mk_ctx(memory="hello world", quiet=False, last_decision=last)
    dec = runner._fast_path_rules(ctx2)
    assert dec is None


def test_fast_path_last_decision_nudge_does_not_dedup():
    runner = _mk_runner()
    ctx1 = _mk_ctx(memory="hello", quiet=False)
    sig = runner._context_signature(ctx1)

    # Last decision was a nudge, not a skip — dedup shouldn't apply
    # (we only dedup skip outcomes).
    last = PlannerDecision(action="nudge", reason="sent a nudge", nudge_message="hi", target_session="cli:direct")
    setattr(last, "_ctx_signature", sig)

    ctx2 = _mk_ctx(memory="hello", quiet=False, last_decision=last)
    assert runner._fast_path_rules(ctx2) is None


def test_fast_path_last_decision_no_signature_no_dedup():
    runner = _mk_runner()
    # A last-decision skip without a stored signature (e.g. first tick with
    # this feature disabled last time) should NOT trigger dedup.
    last = PlannerDecision(action="skip", reason="nothing")
    ctx = _mk_ctx(memory="hello", quiet=False, last_decision=last)
    assert runner._fast_path_rules(ctx) is None


# ── Integration: tick_with_context uses fast-path ─────────────────────────


@pytest.mark.asyncio
async def test_tick_with_context_fast_path_shortcircuits_planner():
    runner = _mk_runner(
        decide_return=PlannerDecision(action="nudge", reason="should not be reached", nudge_message="x")
    )
    outcome = await runner.tick_with_context(_mk_ctx(quiet=True))
    assert outcome.decision.action == "skip"
    assert outcome.route == "fast_path_skip"
    runner.planner.decide.assert_not_called()


@pytest.mark.asyncio
async def test_tick_with_context_falls_through_to_planner_when_no_rule_fires():
    planner_decision = PlannerDecision(
        action="skip",
        reason="planner said skip",
    )
    runner = _mk_runner(decide_return=planner_decision)
    runner._route = MagicMock()  # we don't care about routing here

    async def _fake_route(dec):
        from raven.proactive_engine.sentinel.executor.runner import TickOutcome

        return TickOutcome(decision=dec, result=None, route="skip")

    runner._route = _fake_route

    outcome = await runner.tick_with_context(_mk_ctx(quiet=False))
    assert outcome.decision is planner_decision
    assert outcome.route == "skip"
    runner.planner.decide.assert_called_once()


@pytest.mark.asyncio
async def test_tick_with_context_stashes_signature_on_planner_skip():
    planner_decision = PlannerDecision(action="skip", reason="quiet tick")
    runner = _mk_runner(decide_return=planner_decision)

    async def _fake_route(dec):
        from raven.proactive_engine.sentinel.executor.runner import TickOutcome

        return TickOutcome(decision=dec, result=None, route="skip")

    runner._route = _fake_route

    ctx = _mk_ctx(memory="hello", quiet=False)
    await runner.tick_with_context(ctx)
    # decision should now carry the signature matching ctx
    assert getattr(planner_decision, "_ctx_signature", None) == runner._context_signature(ctx)


# ── Scheduled fire: one-shot deadline slots defer to the Planner ──────────
#
# Recurring slots fast-fire; deadline_/birthday_/anniversary_ fall through so
# the completion-aware Planner owns the call (it sees the slot via the fire-
# plan attention section and skips an already-done task from the episodes
# tail — a deterministic keyword check could not, on non-space-delimited text).

_PLAN_HEADER = "## 今日 fire 计划"


def _wire_attention(runner, tmp_path, plan_body: str) -> None:
    att = tmp_path / "attention.md"
    att.write_text(f"{_PLAN_HEADER}\n{plan_body}\n", encoding="utf-8")
    updater = MagicMock()
    updater.memory_store.attention_file = att
    runner.attention_updater = updater
    runner.policy.topic_fired_today.return_value = False


def test_scheduled_fire_defers_deadline_slot(tmp_path):
    runner = _mk_runner()
    plan = "- 09:00 deadline_quarterly_report | priority=medium | msg=交季度报告 | 季度报告 6/26 截止"
    _wire_attention(runner, tmp_path, plan)
    # deadline_ is one-shot → not fast-fired; tick falls through to the Planner.
    assert runner._fast_path_scheduled_fire(datetime(2026, 6, 26, 9, 0)) is None


def test_scheduled_fire_defers_birthday_and_anniversary(tmp_path):
    runner = _mk_runner()
    plan = (
        "- 09:00 birthday_mom | priority=medium | msg=妈妈生日 | 提醒\n"
        "- 09:30 anniversary_wedding | priority=low | msg=纪念日 | 提醒"
    )
    _wire_attention(runner, tmp_path, plan)
    assert runner._fast_path_scheduled_fire(datetime(2026, 6, 26, 9, 0)) is None


def test_scheduled_fire_fires_recurring_slot(tmp_path):
    runner = _mk_runner()
    plan = "- 09:00 routine_morning_med | priority=high | msg=该吃药啦 | 每天9点吃药"
    _wire_attention(runner, tmp_path, plan)
    dec = runner._fast_path_scheduled_fire(datetime(2026, 6, 26, 9, 0))
    assert dec is not None and dec.action == "nudge"
    assert dec.topic_tag == "routine_morning_med"


# ── Planner-down fallback: high-priority deadlines get a safety net ───────
#
# Deadline slots normally defer to the Planner. But if the Planner LLM is
# down, that deferral would silently drop a hard deadline on its day. The
# fallback re-fires *only* high-priority deadline slots (completion is
# uncheckable with the LLM down, so the blind re-nag is scoped tight), and
# only those already in the fire plan (a deadline never scheduled was never
# fast-fireable, so this is no worse than before).

_HIGH_DEADLINE = "- 09:00 deadline_tax_filing | priority=high | msg=今天报税截止 | 报税 6/26 截止"
_MID_DEADLINE = "- 09:00 deadline_quarterly_report | priority=medium | msg=交季度报告 | 6/26 截止"


def test_fallback_fires_high_priority_deadline(tmp_path):
    runner = _mk_runner()
    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    dec = runner._fallback_deadline_fire(datetime(2026, 6, 26, 9, 0))
    assert dec is not None and dec.action == "nudge"
    assert dec.topic_tag == "deadline_tax_filing"
    assert dec.raw_llm_response.get("source") == "fallback_planner_down"


def test_fallback_silent_for_medium_priority_deadline(tmp_path):
    runner = _mk_runner()
    _wire_attention(runner, tmp_path, _MID_DEADLINE)
    # Only high-priority deadlines are worth a blind re-nag during an outage;
    # softer ones stay quiet ("prefer silent over over-nudging").
    assert runner._fallback_deadline_fire(datetime(2026, 6, 26, 9, 0)) is None


def test_fallback_silent_for_recurring_slot(tmp_path):
    runner = _mk_runner()
    # Recurring slots fast-fire on the normal path; the fallback is deadline-
    # only (a recurring habit has no irreversible "missed it" cost).
    plan = "- 09:00 routine_morning_med | priority=high | msg=该吃药啦 | 每天9点吃药"
    _wire_attention(runner, tmp_path, plan)
    assert runner._fallback_deadline_fire(datetime(2026, 6, 26, 9, 0)) is None


def test_fallback_silent_when_disabled(tmp_path):
    runner = _mk_runner()
    runner._deadline_outage_fallback = False
    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    assert runner._fallback_deadline_fire(datetime(2026, 6, 26, 9, 0)) is None


def test_fallback_respects_topic_fired_today(tmp_path):
    runner = _mk_runner()
    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    runner.policy.topic_fired_today.return_value = True
    assert runner._fallback_deadline_fire(datetime(2026, 6, 26, 9, 0)) is None


async def _noop_refresh() -> None:
    return None


@pytest.mark.asyncio
async def test_tick_fallback_fires_high_deadline_when_planner_errors(tmp_path):
    runner = _mk_runner()

    async def _boom(ctx):
        raise RuntimeError("llm down")

    runner.planner.decide.side_effect = _boom

    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    runner._now_fn = lambda: datetime(2026, 6, 26, 9, 0)
    runner._refresh_memory_state = _noop_refresh

    routed: dict = {}

    async def _fake_route(dec):
        from raven.proactive_engine.sentinel.executor.runner import TickOutcome

        routed["dec"] = dec
        return TickOutcome(decision=dec, result=None, route="nudge")

    runner._route = _fake_route

    outcome = await runner.tick_with_context(_mk_ctx(quiet=False))
    assert routed["dec"].topic_tag == "deadline_tax_filing"
    assert routed["dec"].raw_llm_response.get("source") == "fallback_planner_down"
    assert outcome.route == "nudge"


@pytest.mark.asyncio
async def test_tick_skips_when_planner_errors_and_deadline_not_high(tmp_path):
    runner = _mk_runner()

    async def _boom(ctx):
        raise RuntimeError("llm down")

    runner.planner.decide.side_effect = _boom

    _wire_attention(runner, tmp_path, _MID_DEADLINE)
    runner._now_fn = lambda: datetime(2026, 6, 26, 9, 0)
    runner._refresh_memory_state = _noop_refresh

    outcome = await runner.tick_with_context(_mk_ctx(quiet=False))
    assert outcome.route == "error"
    assert outcome.decision.action == "skip"
    assert "planner_error" in outcome.decision.reason


# ── Online-skip warning: the only otherwise-silent failure surface ────────
#
# When the Planner is *online* but skips a due deadline slot (e.g. a false
# "already done" call), nothing fires and nothing is logged by default. We
# trust the online judgment (no auto-fire), but warn so the eval can catch it.


def test_warns_when_planner_skips_due_deadline(tmp_path, monkeypatch):
    runner = _mk_runner()
    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    import raven.proactive_engine.sentinel.executor.runner as runner_mod

    fake_logger = MagicMock()
    monkeypatch.setattr(runner_mod, "logger", fake_logger)

    decision = PlannerDecision(action="skip", reason="looks already done")
    runner._warn_unfired_due_deadline(decision, datetime(2026, 6, 26, 9, 0))

    assert fake_logger.warning.called
    assert any("deadline_tax_filing" in str(c) for c in fake_logger.warning.call_args_list)


def test_no_warn_when_planner_fires_the_deadline(tmp_path, monkeypatch):
    runner = _mk_runner()
    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    import raven.proactive_engine.sentinel.executor.runner as runner_mod

    fake_logger = MagicMock()
    monkeypatch.setattr(runner_mod, "logger", fake_logger)

    decision = PlannerDecision(
        action="nudge",
        topic_tag="deadline_tax_filing",
        nudge_message="x",
    )
    runner._warn_unfired_due_deadline(decision, datetime(2026, 6, 26, 9, 0))

    assert not fake_logger.warning.called


# ── quiet-hours × deadline: rule (a) must not swallow a high deadline ──────
#
# A deferred deadline now reaches _fast_path_rules; rule (a) (quiet-hours hard
# skip) does not check priority. A due high-priority deadline must bypass both
# rules so it reaches the Planner (whose policy bypasses quiet hours for high
# priority) or, when the Planner is down, the fallback. Otherwise it would be
# silently dropped (the skip-warning runs only after the Planner).


def test_fast_path_rules_high_deadline_bypasses_quiet_hours(tmp_path):
    runner = _mk_runner()
    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    due = runner._due_plan_slots(datetime(2026, 6, 26, 9, 0))
    # In quiet hours, yet a due high-priority deadline must not be short-circuited.
    assert runner._fast_path_rules(_mk_ctx(quiet=True), due) is None


def test_fast_path_rules_quiet_skip_when_due_deadline_not_high(tmp_path):
    runner = _mk_runner()
    _wire_attention(runner, tmp_path, _MID_DEADLINE)
    due = runner._due_plan_slots(datetime(2026, 6, 26, 9, 0))
    # Bypass is high-only: a medium deadline does not earn it → quiet skip stands.
    dec = runner._fast_path_rules(_mk_ctx(quiet=True), due)
    assert dec is not None and dec.action == "skip"
    assert "quiet_hours" in dec.reason


@pytest.mark.asyncio
async def test_tick_quiet_hours_high_deadline_reaches_fallback_when_planner_down(tmp_path):
    runner = _mk_runner()

    async def _boom(ctx):
        raise RuntimeError("llm down")

    runner.planner.decide.side_effect = _boom

    _wire_attention(runner, tmp_path, _HIGH_DEADLINE)
    runner._now_fn = lambda: datetime(2026, 6, 26, 9, 0)
    runner._refresh_memory_state = _noop_refresh

    routed: dict = {}

    async def _fake_route(dec):
        from raven.proactive_engine.sentinel.executor.runner import TickOutcome

        routed["dec"] = dec
        return TickOutcome(decision=dec, result=None, route="nudge")

    runner._route = _fake_route

    # Quiet hours True: the old rule (a) would have skipped before the Planner
    # /fallback. The high-priority bypass keeps the deadline alive to the net.
    await runner.tick_with_context(_mk_ctx(quiet=True))
    assert routed.get("dec") is not None
    assert routed["dec"].topic_tag == "deadline_tax_filing"
    assert routed["dec"].raw_llm_response.get("source") == "fallback_planner_down"
