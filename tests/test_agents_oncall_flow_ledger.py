"""Tests for the durable Ops ledger.

The load-bearing property is crash-resume: a ledger reopened from disk must
recover every job's handle, status, and terminal result, so the loop can resume
without re-submitting or re-running finished work.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backend import JobHandle, JobResult, JobStatus  # noqa: E402
from oncall_flow.ledger import Ledger  # noqa: E402


def test_record_is_tracked_and_idempotent(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    led.record("trial-1", campaign="bm25")
    again = led.record("trial-1", campaign="bm25")

    assert led.has("trial-1")
    assert again is led.get("trial-1")
    assert len(led.all()) == 1


def test_reopen_recovers_handle_and_terminal_result(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.record("trial-1", campaign="bm25")
    led.set_handle("trial-1", JobHandle("mock", "mock-1"))
    led.set_result("trial-1", JobResult(JobStatus.SUCCEEDED, metrics={"ndcg": 0.73}))

    reopened = Ledger(path)
    rec = reopened.get("trial-1")
    assert rec is not None
    assert rec.handle == JobHandle("mock", "mock-1")
    assert rec.status is JobStatus.SUCCEEDED and rec.is_terminal
    assert rec.result is not None and rec.result.metrics == {"ndcg": 0.73}


def test_pending_excludes_terminal(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    led.record("a")
    led.record("b")
    led.set_result("b", JobResult(JobStatus.FAILED, error="diverged"))

    pending_keys = {r.idem_key for r in led.pending()}
    assert pending_keys == {"a"}


def test_by_campaign_filters(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    led.record("a", campaign="bm25")
    led.record("b", campaign="bm25")
    led.record("c", campaign="hybrid")

    assert {r.idem_key for r in led.by_campaign("bm25")} == {"a", "b"}


def test_bump_attempts_persists(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.record("trial-1")
    assert led.bump_attempts("trial-1") == 1
    assert led.bump_attempts("trial-1") == 2

    assert Ledger(path).get("trial-1").attempts == 2


def test_mutating_unknown_key_raises(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    try:
        led.set_status("ghost", JobStatus.RUNNING)
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown idem_key")


def test_the_deliverable_survives_a_reopen(tmp_path):
    """The deliverable names the artifact to hand over at the end of the campaign,
    and the report is written many wakes after the run finished. A field the ledger
    drops is a field the report cannot cite -- and every wake starts from disk."""
    from oncall_flow.backend import JobResult, JobStatus
    from oncall_flow.ledger import Ledger

    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.record("j1")
    led.set_result(
        "j1",
        JobResult(
            JobStatus.SUCCEEDED,
            metrics={"ndcg": 0.3564},
            output={"eval_points": [[1200, 0.362], [1518, 0.3564]]},
            deliverable={"ref": "/w/jobs/j1/step-1200", "label": "ndcg", "value": 0.362},
        ),
    )

    reopened = Ledger(path).all()

    assert reopened[0].result.deliverable == {
        "ref": "/w/jobs/j1/step-1200",
        "label": "ndcg",
        "value": 0.362,
    }


def test_what_a_job_holds_survives_a_reopen_and_an_old_record_holds_nothing_named(tmp_path: Path) -> None:
    """The occupancy gate reads this across campaigns; a record written before
    the field existed reads back as None, which the gate counts as one unit."""
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.record("j1", campaign="c", config={"lr": 1}, resources_held={"gpus": 2, "device_ids": ["0", "1"]})
    led.record("j2", campaign="c", config={"lr": 2})

    again = Ledger(path)
    assert again.get("j1").resources_held == {"gpus": 2, "device_ids": ["0", "1"]}
    assert again.get("j2").resources_held is None

    led.record("j2", campaign="c", resources_held={"cores": 8})
    assert Ledger(path).get("j2").resources_held == {"cores": 8}, "a resubmit may name what an old record holds"
