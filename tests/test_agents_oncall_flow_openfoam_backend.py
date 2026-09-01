"""Core-hour accounting and solver-marker status for the OpenFOAM backend.

The cases that matter are the ones where a naive implementation quietly refunds
compute: a job killed before it could write anything, and a job that ran on many
cores. Both are asserted here against a fake shell whose replies stand in for the
remote host.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backend import JobHandle, JobStatus  # noqa: E402
from oncall_flow.openfoam_backend import OpenFoamExecutor  # noqa: E402


class FakeShell:
    """Answers the executor's probe scripts with canned text, and records them."""

    def __init__(self, replies: list[tuple[int, str]]) -> None:
        self._replies = list(replies)
        self.commands: list[str] = []

    def __call__(self, cmd: str) -> tuple[int, str]:
        self.commands.append(cmd)
        return self._replies.pop(0) if self._replies else (0, "")


def _executor(replies, **kwargs):
    shell = FakeShell(replies)
    ex = OpenFoamExecutor(
        shell,
        remote_dir="/root/raven-ops",
        command="bash run_case.sh",
        **kwargs,
    )
    return ex, shell


# Row layout of the spend probe: dir|procs|alive|pid_mtime|clocktime|last_touch|ended
def _row(d, procs, alive, started, clock, touch, ended):
    return f"{d}|{procs}|{alive}|{started}|{clock}|{touch}|{ended}"


@pytest.mark.asyncio
async def test_spend_is_multiplied_by_the_cores_the_job_actually_used():
    # 600s of ClockTime on 8 processor directories = 10 wall minutes = 80 core-minutes.
    out = "\n".join([_row("caseA", 8, 0, 1000, 600, 1600, 1), "NOW|2000"])
    ex, _ = _executor([(0, out)])
    assert await ex.spent_minutes() == pytest.approx(80.0)
    assert ex.cores_used("caseA") == 8


@pytest.mark.asyncio
async def test_serial_run_counts_as_one_core():
    out = "\n".join([_row("caseA", 0, 0, 1000, 600, 1600, 1), "NOW|2000"])
    ex, _ = _executor([(0, out)])
    assert await ex.spent_minutes() == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_killed_job_with_no_clocktime_is_billed_from_its_last_write():
    # No ClockTime (killed before the solver printed one), but the pid file and the
    # last touched file bracket 300s of life. Billing zero here would refund a
    # cancel-and-resubmit loop, which is the whole point of this test.
    out = "\n".join([_row("caseA", 4, 0, 1000, "-", 1300, 0), "NOW|9999"])
    ex, _ = _executor([(0, out)])
    assert await ex.spent_minutes() == pytest.approx(20.0)  # 5 min x 4 cores
    assert ex.unmeasured_spend() == {}


@pytest.mark.asyncio
async def test_spend_takes_the_larger_of_the_two_estimates():
    # ClockTime covers only the solve (60s); the pid-to-last-write span also covers
    # meshing and decomposition (600s). The larger one is the honest bill.
    out = "\n".join([_row("caseA", 1, 0, 1000, 60, 1600, 1), "NOW|2000"])
    ex, _ = _executor([(0, out)])
    assert await ex.spent_minutes() == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_running_job_is_billed_up_to_now():
    out = "\n".join([_row("caseA", 2, 1, 1000, "-", 1100, 0), "NOW|1600"])
    ex, _ = _executor([(0, out)])
    assert await ex.spent_minutes() == pytest.approx(20.0)  # 10 min x 2 cores


@pytest.mark.asyncio
async def test_job_with_nothing_to_measure_is_recorded_not_zeroed():
    out = "\n".join([_row("caseA", 1, 0, "-", "-", "-", 0), "NOW|2000"])
    ex, _ = _executor([(0, out)])
    assert await ex.spent_minutes() == pytest.approx(0.0)
    assert "caseA" in ex.unmeasured_spend()


@pytest.mark.asyncio
async def test_remaining_budget_is_core_minutes():
    out = "\n".join([_row("caseA", 8, 0, 1000, 600, 1600, 1), "NOW|2000"])
    ex, _ = _executor([(0, out)], budget_minutes_total=100.0)
    assert await ex.remaining_minutes() == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_end_marker_means_succeeded_without_a_result_file():
    ex, _ = _executor([(0, "ended")])
    assert await ex._status("caseA") is JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_dead_without_end_marker_is_failed():
    ex, _ = _executor([(0, "gone")])
    assert await ex._status("caseA") is JobStatus.FAILED


@pytest.mark.asyncio
async def test_fetch_result_returns_raw_evidence_and_no_metric():
    replies = [
        (0, "ended"),
        (0, "Time = 281\nSIMPLE solution converged in 281 iterations\nEnd"),
        (0, "0 100 200 281"),
    ]
    ex, _ = _executor(replies)
    result = await ex.fetch_result(JobHandle("openfoam", "ops-caseA"))
    assert result.status is JobStatus.SUCCEEDED
    # No derived metric: judging the physics is the thing being measured.
    assert result.metrics == {}
    assert "SIMPLE solution converged" in result.output["log_tail"]
    assert result.output["time_directories"] == ["0", "100", "200", "281"]


@pytest.mark.asyncio
async def test_a_failure_reason_carries_the_job_s_last_words():
    """ "solver did not print End" restates how the failure was detected, and says
    nothing about what happened. It is the only text that reaches the status
    output's ``why:`` line, so a crash arrived there as a tautology.

    Measured 2026-08-13 on the CFD divergence leg: the solver took a SIGFPE with
    alpha.water at -9.8e+88 and an eight-frame stack trace sitting in the log the
    backend had already fetched, while ``why:`` read "solver did not print End".
    The arm recovered by reading job.log itself -- which is the tool face doing the
    reader's work for it.

    The last lines are appended verbatim, not summarised: which line matters is
    domain knowledge, and the caller truncates.
    """
    crash = (
        "Time = 0.4885\n"
        "Phase-1 volume fraction = 6.22e+68  Min(alpha.water) = -9.82e+88\n"
        "[stack trace]\n"
        "#1  Foam::sigFpe::sigHandler(int) in libOpenFOAM.so\n"
    )
    ex, _ = _executor([(0, "gone"), (0, crash), (0, "0 0.05 0.1")])
    result = await ex.fetch_result(JobHandle("openfoam", "ops-caseA"))

    assert result.status is JobStatus.FAILED
    assert "solver did not print End" in (result.error or ""), "how it was detected is still worth saying"
    assert "sigFpe" in (result.error or ""), "and what the job said on the way out"
    assert "-9.82e+88" in (result.error or "")


@pytest.mark.asyncio
async def test_a_failure_with_no_log_says_so_rather_than_going_quiet():
    ex, _ = _executor([(0, "gone"), (1, ""), (0, "")])
    result = await ex.fetch_result(JobHandle("openfoam", "ops-caseA"))
    assert result.status is JobStatus.FAILED
    assert "solver did not print End" in (result.error or "")
    assert "no log" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_a_succeeded_job_still_has_no_error():
    ex, _ = _executor([(0, "ended"), (0, "Time = 1\nEnd"), (0, "0 1")])
    result = await ex.fetch_result(JobHandle("openfoam", "ops-caseA"))
    assert result.error is None


@pytest.mark.asyncio
async def test_fetch_progress_returns_verbatim_lines():
    ex, _ = _executor([(0, "GAMG:  Solving for p, Initial residual = 0.0008\nEnd")])
    rows = await ex.fetch_progress(JobHandle("openfoam", "ops-caseA"), tail=2)
    assert rows == [
        {"line": "GAMG:  Solving for p, Initial residual = 0.0008"},
        {"line": "End"},
    ]


@pytest.mark.asyncio
async def test_preprocessing_End_does_not_count_as_solver_success():
    """A killed solver must not read SUCCEEDED just because meshing finished.

    blockMesh, setFields and decomposePar each print their own "End" line. An
    earlier version scanned job.log AND log.*, so a cancelled parallel job came
    back SUCCEEDED -- caught only by running a real parallel job, since the serial
    smoke never produced a log.decomposePar. The probe must consult job.log alone.
    """
    ex, shell = _executor([(0, "gone")])
    assert await ex._status("caseA") is JobStatus.FAILED
    probe = shell.commands[-1]
    assert "job.log" in probe
    assert "log.*" not in probe


@pytest.mark.asyncio
async def test_two_solvers_running_at_once_are_billed_for_both():
    """Cores that run side by side are separately busy, and the two jobs overlap in
    wall clock -- so the total is the sum, not the span they share.

    Pinned end to end because the accumulation moved to a shared accountant that
    also serves the training backend, where an overlap on one device counts once.
    The two rules are one function apart, and the unit tests for it prove the
    function; this proves this backend asks it for the right one.
    """
    out = "\n".join(
        [
            _row("caseA", 8, 0, 1000, 600, 1600, 1),
            _row("caseB", 4, 0, 1000, 600, 1600, 1),
            "NOW|2000",
        ]
    )
    ex, _ = _executor([(0, out)])

    # 10 wall minutes each: 80 core-minutes on eight cores plus 40 on four.
    assert await ex.spent_minutes() == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_a_campaign_may_declare_that_its_spend_is_occupancy():
    """The rule follows the declaration, not the backend. A site that prices a
    whole node whatever runs on it declares shared, and the same two overlapping
    jobs then cost what the wider one cost."""
    from oncall_flow.budget import SHARED, Budget

    out = "\n".join(
        [
            _row("caseA", 8, 0, 1000, 600, 1600, 1),
            _row("caseB", 4, 0, 1000, 600, 1600, 1),
            "NOW|2000",
        ]
    )
    ex, _ = _executor([(0, out)], budget=Budget(unit="node-minute", total=500, overlap=SHARED))

    assert await ex.spent_minutes() == pytest.approx(80.0)
