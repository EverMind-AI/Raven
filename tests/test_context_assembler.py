"""Unit tests for ContextAssembler.

Verifies assembly from each source independently + graceful degradation
when a source is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.config.raven import NudgePolicyConfig
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.predictor.context_assembler import ContextAssembler
from raven.proactive_engine.sentinel.predictor.routine_learner import RoutineLearner
from raven.proactive_engine.sentinel.trigger_policy.policy import NudgePolicy


def _now():
    return datetime(2026, 4, 21, 14, 0, 0)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "memory").mkdir()
    return tmp_path


@pytest.fixture
def memory_store(workspace) -> MemoryStore:
    m = MemoryStore(workspace)
    m.write_long_term("- prefers gentle tone\n- Duolingo streak 47 days")
    m.append_history("[2026-04-07 08:00] morning duolingo")
    m.append_history("[2026-04-14 08:00] morning duolingo")
    m.append_history("[2026-04-21 08:00] morning duolingo")
    return m


# ---------------------------------------------------------------------------
# Empty sources — everything defaults to a safe empty context.


def test_assemble_empty_sources():
    asm = ContextAssembler(now_fn=_now)
    ctx = asm.assemble()
    assert ctx.now == _now()
    assert ctx.memory_md == ""
    assert ctx.history_md_recent == ""
    assert ctx.routines == []
    assert ctx.active_sessions == []
    assert ctx.calendar == []
    assert ctx.nudge_policy_state.remaining_today == 10
    assert ctx.last_decision is None


# ---------------------------------------------------------------------------
# Memory + history


def test_assemble_reads_memory_and_history(memory_store):
    asm = ContextAssembler(memory_store=memory_store, now_fn=_now)
    ctx = asm.assemble()
    assert "Duolingo streak 47" in ctx.memory_md
    assert "morning duolingo" in ctx.history_md_recent


def test_history_tail_limits_lines(workspace, memory_store):
    # Write 200 entries; assembler with tail=10 should keep only 10.
    for i in range(200):
        memory_store.append_history(f"[2026-04-{(i % 28) + 1:02d} 08:00] entry {i}")
    asm = ContextAssembler(
        memory_store=memory_store,
        now_fn=_now,
        history_tail_lines=10,
    )
    ctx = asm.assemble()
    # Each append writes "entry\n\n", so 10 tail lines → ≤ 20 incl blanks.
    non_blank = [l for l in ctx.history_md_recent.splitlines() if l.strip()]
    assert len(non_blank) <= 10


# ---------------------------------------------------------------------------
# Routines


def test_assemble_learns_routines_from_history(memory_store):
    learner = RoutineLearner(
        min_occurrences=3,
        hour_slot_size=3,
        learning_window_days=60,
        now_fn=_now,
    )
    asm = ContextAssembler(
        memory_store=memory_store,
        routine_learner=learner,
        now_fn=_now,
    )
    ctx = asm.assemble()
    assert len(ctx.routines) >= 1
    assert ctx.routines[0].status == "candidate"
    assert any("duolingo" in k for k in ctx.routines[0].keywords)


def test_assemble_no_routines_if_learner_absent(memory_store):
    asm = ContextAssembler(memory_store=memory_store, now_fn=_now)
    ctx = asm.assemble()
    assert ctx.routines == []


# ---------------------------------------------------------------------------
# Active sessions


def test_assemble_active_sessions_from_manager():
    now = _now()
    # Fake SessionManager: exposes .sessions dict.
    sess = SimpleNamespace(
        key="cli:direct",
        updated_at=now - timedelta(minutes=5),
        messages=[
            {"role": "user", "content": "hey how do I add a CSP header"},
            {"role": "assistant", "content": "you can set Content-Security-Policy…"},
        ],
    )
    session_manager = SimpleNamespace(sessions={"cli:direct": sess})
    asm = ContextAssembler(session_manager=session_manager, now_fn=_now)
    ctx = asm.assemble()
    assert len(ctx.active_sessions) == 1
    assert ctx.active_sessions[0].key == "cli:direct"
    assert "CSP header" in (ctx.active_sessions[0].last_user_message or "")


def test_assemble_excludes_stale_sessions():
    now = _now()
    stale = SimpleNamespace(
        key="cli:old",
        updated_at=now - timedelta(hours=5),
        messages=[],
    )
    session_manager = SimpleNamespace(sessions={"cli:old": stale})
    asm = ContextAssembler(
        session_manager=session_manager,
        now_fn=_now,
        active_session_window_seconds=3600,  # 1h window
    )
    ctx = asm.assemble()
    assert ctx.active_sessions == []


def test_assemble_sessions_sorted_recent_first():
    now = _now()
    old = SimpleNamespace(key="cli:a", updated_at=now - timedelta(minutes=30), messages=[])
    new = SimpleNamespace(key="cli:b", updated_at=now - timedelta(minutes=5), messages=[])
    session_manager = SimpleNamespace(sessions={"cli:a": old, "cli:b": new})
    asm = ContextAssembler(session_manager=session_manager, now_fn=_now)
    ctx = asm.assemble()
    assert [s.key for s in ctx.active_sessions] == ["cli:b", "cli:a"]


# ---------------------------------------------------------------------------
# Nudge policy state snapshot


def test_assemble_nudge_state():
    policy = NudgePolicy(
        NudgePolicyConfig(
            max_nudges_per_hour=3,
            max_nudges_per_day=10,
            min_interval_seconds=300,
            quiet_hours=(23, 7),
            cooldown_on_dismiss_seconds=1800,
            high_priority_bypasses_limits=True,
            dedup_window_seconds=3600,
            inject_ttl_seconds=1800,
            inject_max_pending_per_session=3,
            defer_idle_threshold_seconds=300,
            defer_max_wait_seconds=86400,
        ),
        now_fn=_now,
    )
    policy.record_fired("nudge", "s1", "test")
    asm = ContextAssembler(nudge_policy=policy, now_fn=_now)
    ctx = asm.assemble()
    assert ctx.nudge_policy_state.nudges_used_this_hour == 1
    assert ctx.nudge_policy_state.remaining_today == 9
    assert ctx.nudge_policy_state.in_quiet_hours is False


# ---------------------------------------------------------------------------
# Calendar injection


def test_assemble_calendar_from_callback():
    asm = ContextAssembler(
        now_fn=_now,
        calendar_fn=lambda: ["15:00 standup", "17:00 1:1 with manager"],
    )
    ctx = asm.assemble()
    assert len(ctx.calendar) == 2
    assert "standup" in ctx.calendar[0]


def test_assemble_calendar_callback_error_silent():
    def boom():
        raise RuntimeError("boom")

    asm = ContextAssembler(now_fn=_now, calendar_fn=boom)
    ctx = asm.assemble()
    assert ctx.calendar == []


# ---------------------------------------------------------------------------
# Last decision


def test_remember_last_decision_populates_ctx():
    from raven.proactive_engine.sentinel.types import PlannerDecision

    last = PlannerDecision(action="skip", reason="nothing", proactivity_score=0.1)
    asm = ContextAssembler(now_fn=_now)
    asm.remember_last_decision(last)
    ctx = asm.assemble()
    assert ctx.last_decision is last


# ---------------------------------------------------------------------------
# User profile passthrough


def test_user_profile_passthrough():
    asm = ContextAssembler(
        now_fn=_now,
        user_profile="Chinese native speaker, works in fintech",
    )
    ctx = asm.assemble()
    assert "Chinese" in ctx.user_profile
