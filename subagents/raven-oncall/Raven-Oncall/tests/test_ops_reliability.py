"""Tests for the Ops reliability eval.

Two things must hold: the suite passes on the current (correct) orchestrator, so
reliability is 1.0; and the eval has teeth -- given a broken environment (a
non-idempotent backend) the crash-window scenario detects the double-submit and
reports failure. An eval that can never fail measures nothing.
"""

from __future__ import annotations

from pathlib import Path

from raven.ops import run_suite
from raven.ops.reliability import submit_window_no_double_submit


async def test_suite_all_invariants_hold(tmp_path: Path) -> None:
    card = await run_suite(tmp_path)

    assert card.reliability == 1.0, [f"{r.name}: {r.detail}" for r in card.failed]
    assert {r.name for r in card.results} == {
        "resume_completes",
        "submit_window_no_double_submit",
        "duplicate_event_idempotent",
        "transient_backend_recovers",
        "escalation_fires_once",
    }


async def test_eval_detects_double_submit_on_non_idempotent_backend(tmp_path: Path) -> None:
    passed = await submit_window_no_double_submit(tmp_path, idempotent=True)
    broken = await submit_window_no_double_submit(tmp_path / "b", idempotent=False)

    assert passed.passed is True
    assert broken.passed is False
    assert "jobs=2" in broken.detail
