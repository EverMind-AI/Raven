"""Unit tests for the wall-clock budget reminder."""

from __future__ import annotations

import pytest

from raven.agent.loop.time_budget import CHECKPOINT_ENV, DEADLINE_ENV, TimeBudgetReminder

# A budget long enough that the fraction thresholds all land far outside the
# absolute wrap-up window, so these tests exercise the fraction path alone.
_LONG = 100_000.0


def test_from_env_absent_or_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEADLINE_ENV, raising=False)
    assert TimeBudgetReminder.from_env(100.0) is None
    monkeypatch.setenv(DEADLINE_ENV, "not-a-number")
    assert TimeBudgetReminder.from_env(100.0) is None
    monkeypatch.setenv(DEADLINE_ENV, "50")
    assert TimeBudgetReminder.from_env(100.0) is None


def test_reminders_fire_once_per_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, str(_LONG))
    tb = TimeBudgetReminder.from_env(0.0)
    assert tb is not None
    assert tb.poll(100.0) is None
    note = tb.poll(50_000.0)
    assert note is not None and "833 minutes" in note
    assert tb.poll(50_100.0) is None
    assert tb.poll(75_000.0) is not None
    assert tb.poll(90_000.0) is not None
    assert tb.poll(95_000.0) is None


def test_jump_past_multiple_thresholds_fires_single_note() -> None:
    tb = TimeBudgetReminder(_LONG, 0.0)
    note = tb.poll(95_000.0)
    assert note is not None
    assert tb.poll(96_000.0) is None


def test_checkpoint_ask_only_from_second_threshold() -> None:
    tb = TimeBudgetReminder(_LONG, 0.0, checkpoint=True)
    first = tb.poll(50_000.0)
    assert first is not None and "Checkpoint your work" not in first
    second = tb.poll(75_000.0)
    assert second is not None and "Checkpoint your work" in second
    third = tb.poll(90_000.0)
    assert third is not None and "Checkpoint your work" in third


def test_jump_past_thresholds_reports_the_highest_crossed() -> None:
    tb = TimeBudgetReminder(_LONG, 0.0, checkpoint=True)
    note = tb.poll(95_000.0)
    assert note is not None and "Checkpoint your work" in note


def test_checkpoint_ask_is_off_by_default() -> None:
    tb = TimeBudgetReminder(_LONG, 0.0)
    for now in (50_000.0, 75_000.0, 90_000.0):
        note = tb.poll(now)
        assert note is not None and "Checkpoint your work" not in note


def test_from_env_reads_the_checkpoint_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, str(_LONG))
    monkeypatch.delenv(CHECKPOINT_ENV, raising=False)
    tb = TimeBudgetReminder.from_env(0.0)
    assert tb is not None and tb.checkpoint is False
    monkeypatch.setenv(CHECKPOINT_ENV, "1")
    tb = TimeBudgetReminder.from_env(0.0)
    assert tb is not None and tb.checkpoint is True


# ---- the absolute-seconds pair -------------------------------------------------


def test_wrapup_fires_on_absolute_seconds_not_fraction() -> None:
    """A short budget's fractions all arrive too late to be useful."""
    tb = TimeBudgetReminder(600.0, 0.0, wrapup_sec=420.0)
    # 30% spent: no fraction crossed, and the wrap-up window is not open yet.
    assert tb.poll(100.0) is None
    note = tb.poll(200.0)
    assert note is not None and "deadline notice" in note
    assert "400 seconds" in note


def test_wrapup_consumes_the_remaining_fraction_thresholds() -> None:
    """ "Start long work now" must never follow "compute nothing new"."""
    tb = TimeBudgetReminder(600.0, 0.0, wrapup_sec=420.0)
    assert tb.poll(200.0) is not None
    for now in (300.0, 450.0, 550.0, 599.0):
        assert tb.poll(now) is None


def test_wrapup_carries_the_checkpoint_clause_when_asked() -> None:
    tb = TimeBudgetReminder(600.0, 0.0, checkpoint=True, wrapup_sec=420.0)
    note = tb.poll(200.0)
    assert note is not None and "Checkpoint your work" in note


def test_wrapup_remaining_clamps_at_zero() -> None:
    tb = TimeBudgetReminder(600.0, 0.0, wrapup_sec=420.0)
    note = tb.poll(900.0)
    assert note is not None and "About 0 seconds" in note


def test_fraction_wins_when_it_arrives_first_on_a_long_budget() -> None:
    """On a long budget the 50% mark is nowhere near the wrap-up window."""
    tb = TimeBudgetReminder(_LONG, 0.0, wrapup_sec=420.0)
    note = tb.poll(50_000.0)
    assert note is not None and "time budget" in note and "deadline notice" not in note


def test_should_stop_only_inside_the_hard_stop_margin() -> None:
    tb = TimeBudgetReminder(1000.0, 0.0, hard_stop_sec=60.0)
    assert tb.should_stop(500.0) is False
    assert tb.should_stop(939.0) is False
    assert tb.should_stop(940.0) is True
    assert tb.should_stop(1200.0) is True


def test_hard_stop_can_be_disabled() -> None:
    """An attended session must not be ended from under the person in it."""
    tb = TimeBudgetReminder(1000.0, 0.0, hard_stop_sec=None)
    assert tb.should_stop(999.9) is False
    assert tb.should_stop(2000.0) is False


def test_in_wrapup_reports_the_window() -> None:
    tb = TimeBudgetReminder(1000.0, 0.0, wrapup_sec=420.0)
    assert tb.in_wrapup(500.0) is False
    assert tb.in_wrapup(580.0) is True


def test_from_env_passes_the_margins_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "1000")
    tb = TimeBudgetReminder.from_env(0.0, wrapup_sec=600.0, hard_stop_sec=None)
    assert tb is not None
    assert tb.wrapup_sec == 600.0
    assert tb.hard_stop_sec is None
