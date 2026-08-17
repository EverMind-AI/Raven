"""Tests for the Campaign orchestrator.

The properties that matter: a campaign runs its trials to completion and selects
the best by metric; it is idempotent and crash-resumable (a fresh Campaign from
the same ledger + backend never double-submits, even across a crash between
submit and persisting the handle); transient backend outages don't abort it; a
trial that runs and fails stays terminal.
"""

from __future__ import annotations

from pathlib import Path

from raven.ops import (
    Campaign,
    JobPlan,
    JobSpec,
    JobStatus,
    Ledger,
    MockJobBackend,
    RetryPolicy,
    Trial,
)

_TRIALS = [Trial("t1", {"k1": 0.9}), Trial("t2", {"k1": 1.5}), Trial("t3", {"k1": 2.1})]


def _backend_best_ndcg() -> MockJobBackend:
    return MockJobBackend(
        plans={
            "t1": JobPlan(succeed_after_polls=1, metrics={"ndcg": 0.61}),
            "t2": JobPlan(succeed_after_polls=1, metrics={"ndcg": 0.74}),
            "t3": JobPlan(succeed_after_polls=1, metrics={"ndcg": 0.68}),
        }
    )


async def test_run_completes_and_selects_best(tmp_path: Path) -> None:
    backend = _backend_best_ndcg()
    campaign = Campaign("bm25", _TRIALS, backend, Ledger(tmp_path / "l.json"), metric="ndcg", goal="max")

    await campaign.run()

    assert campaign.is_done()
    best = campaign.best()
    assert best is not None and best.idem_key == "t2"
    assert best.result.metrics["ndcg"] == 0.74


async def test_resume_after_crash_no_double_submit(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    backend = MockJobBackend(default_plan=JobPlan(succeed_after_polls=3, metrics={"ndcg": 0.5}))

    first = Campaign("bm25", _TRIALS, backend, Ledger(path), metric="ndcg")
    await first.step()
    await first.step()
    assert not first.is_done()

    resumed = Campaign("bm25", _TRIALS, backend, Ledger(path), metric="ndcg")
    await resumed.run()

    assert resumed.is_done()
    assert len(backend._jobs) == 3


async def test_crash_between_submit_and_handle_is_not_double_submitted(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    backend = MockJobBackend()
    ledger = Ledger(path)

    handle = await backend.submit(JobSpec({}, idem_key="t1"))
    ledger.record("t1", campaign="bm25")
    assert ledger.get("t1").handle is None

    await Campaign("bm25", [Trial("t1")], backend, ledger).step()

    assert backend._seq == 1
    assert ledger.get("t1").handle == handle


async def test_transient_backend_error_does_not_abort(tmp_path: Path) -> None:
    backend = _backend_best_ndcg()
    backend._transient_poll_errors = 2
    campaign = Campaign("bm25", _TRIALS, backend, Ledger(tmp_path / "l.json"), metric="ndcg")

    await campaign.run()

    assert campaign.is_done()
    assert campaign.best() is not None


async def test_failed_trial_stays_terminal(tmp_path: Path) -> None:
    backend = MockJobBackend(
        plans={
            "t1": JobPlan(metrics={"ndcg": 0.6}),
            "t2": JobPlan(fail=True, error="diverged"),
            "t3": JobPlan(metrics={"ndcg": 0.7}),
        }
    )
    ledger = Ledger(tmp_path / "l.json")
    campaign = Campaign("bm25", _TRIALS, backend, ledger, metric="ndcg")

    await campaign.run()

    assert ledger.get("t2").status is JobStatus.FAILED
    assert campaign.best().idem_key == "t3"


async def test_retry_succeeds_on_a_later_attempt(tmp_path: Path) -> None:
    backend = MockJobBackend(
        plans={
            "solo": JobPlan(fail=True, error="transient diverge"),
            "solo#a2": JobPlan(metrics={"ndcg": 0.8}),
        }
    )
    ledger = Ledger(tmp_path / "l.json")
    campaign = Campaign(
        "bm25", [Trial("solo")], backend, ledger, metric="ndcg", retry_policy=RetryPolicy(max_retries=1)
    )

    await campaign.run()

    assert campaign.is_done()
    assert ledger.get("solo").status is JobStatus.FAILED
    assert ledger.get("solo#a2").status is JobStatus.SUCCEEDED
    assert campaign.best().idem_key == "solo#a2"


async def test_escalates_once_after_retries_exhausted(tmp_path: Path) -> None:
    backend = MockJobBackend(
        plans={
            "solo": JobPlan(fail=True, error="boom"),
            "solo#a2": JobPlan(fail=True, error="boom"),
        }
    )
    ledger = Ledger(tmp_path / "l.json")
    escalations: list[str] = []

    async def on_escalate(trial_id: str, rec) -> None:
        escalations.append(trial_id)

    campaign = Campaign(
        "bm25",
        [Trial("solo")],
        backend,
        ledger,
        retry_policy=RetryPolicy(max_retries=1),
        escalation=on_escalate,
    )

    await campaign.run()

    assert campaign.is_done()
    assert escalations == ["solo"]
    assert ledger.get("solo#a2").escalated is True


async def test_escalation_not_refired_after_resume(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    backend = MockJobBackend(plans={"solo": JobPlan(fail=True, error="boom")})
    calls: list[str] = []

    async def on_escalate(trial_id: str, rec) -> None:
        calls.append(trial_id)

    trials = [Trial("solo"), Trial("other")]
    backend._plans["other"] = JobPlan(succeed_after_polls=5, metrics={"ndcg": 0.5})

    first = Campaign(
        "bm25",
        trials,
        backend,
        Ledger(path),
        metric="ndcg",
        retry_policy=RetryPolicy(max_retries=0),
        escalation=on_escalate,
    )
    await first.step()
    assert calls == ["solo"]

    resumed = Campaign(
        "bm25",
        trials,
        backend,
        Ledger(path),
        metric="ndcg",
        retry_policy=RetryPolicy(max_retries=0),
        escalation=on_escalate,
    )
    await resumed.run()

    assert calls == ["solo"]
    assert resumed.best().idem_key == "other"
