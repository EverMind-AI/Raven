"""The rolling-hour dispatch budget, kept where more than one process can see it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.subagent import dispatch_ledger as mod
from raven.agent.subagent.dispatch_ledger import DispatchLedger

HOUR = 3600.0


@pytest.fixture
def clock(monkeypatch):
    holder = [1_700_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: holder[0])
    return holder


@pytest.fixture
def ledger(tmp_path: Path) -> DispatchLedger:
    return DispatchLedger(tmp_path / "ledger.json", window_seconds=HOUR)


def test_a_budget_is_spent_and_then_refused(ledger, clock) -> None:
    assert [ledger.charge("s", limit=2) for _ in range(3)] == [True, True, False]
    assert ledger.spent("s") == 2


def test_a_refusal_does_not_cost_anything_itself(ledger, clock) -> None:
    """Otherwise a session that kept asking would never recover."""
    for _ in range(5):
        ledger.charge("s", limit=1)

    assert ledger.spent("s") == 1


def test_the_window_rolls_rather_than_resetting(ledger, clock) -> None:
    assert ledger.charge("s", limit=1) is True
    assert ledger.charge("s", limit=1) is False

    clock[0] += HOUR + 1

    assert ledger.charge("s", limit=1) is True


def test_one_busy_session_does_not_throttle_another(ledger, clock) -> None:
    assert ledger.charge("a", limit=1) is True
    assert ledger.charge("a", limit=1) is False
    assert ledger.charge("b", limit=1) is True


def test_a_second_reader_of_the_same_file_sees_the_first_one_s_spending(tmp_path: Path, clock) -> None:
    """The whole reason it is a file. In memory the cap was per process, so a
    gateway restart handed the caller a fresh allowance and two processes
    sharing one home each counted to it separately."""
    path = tmp_path / "ledger.json"
    DispatchLedger(path, window_seconds=HOUR).charge("s", limit=1)

    assert DispatchLedger(path, window_seconds=HOUR).charge("s", limit=1) is False


def test_a_session_that_was_torn_down_is_forgotten(ledger, clock) -> None:
    ledger.charge("s", limit=1)
    ledger.forget("s")

    assert ledger.spent("s") == 0
    assert ledger.charge("s", limit=1) is True


def test_an_aged_out_session_stops_being_a_line_in_the_file(ledger, clock) -> None:
    """Pruned on every charge rather than swept: the file is read and written
    whole anyway, so a session that stopped an hour ago should not still be in it."""
    ledger.charge("old", limit=5)
    clock[0] += HOUR + 1
    ledger.charge("new", limit=5)

    assert list(json.loads(ledger.path.read_text(encoding="utf-8"))) == ["new"]


def test_an_unreadable_ledger_is_an_empty_one_rather_than_a_crash(ledger, clock) -> None:
    """A rate limit is a guard rail. Refusing every dispatch because a file was
    truncated would turn a broken guard rail into a broken agent."""
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("{not json", encoding="utf-8")

    assert ledger.charge("s", limit=1) is True


def test_a_charge_that_cannot_be_written_is_allowed_and_not_silent(tmp_path: Path, clock, monkeypatch, caplog) -> None:
    """An unwritable home must not read as an exhausted budget."""
    ledger = DispatchLedger(tmp_path / "ledger.json", window_seconds=HOUR)

    def _refuse(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(ledger, "_write", _refuse)

    assert ledger.charge("s", limit=1) is True


def test_a_clock_stepped_back_costs_one_more_window_and_not_the_step(ledger, clock) -> None:
    """Wall clock is the only one two processes can compare, and a step is its
    price. Left alone a stamp in the future would hold a session out for as
    long as the step -- an NTP correction of a day would be a day's lock-out."""
    ledger.charge("s", limit=1)
    clock[0] -= HOUR * 48

    assert ledger.charge("s", limit=1) is False, "the entry is not simply forgiven"

    clock[0] += HOUR + 1

    assert ledger.charge("s", limit=1) is True, "and it costs one window, not two days"
