"""A failed trial prints why, not only that it failed.

Measured 2026-08-12 on the OpenFOAM divergence leg, and it cost the run. The
first job died of SIGFPE with an eight-line stack trace sitting in job.log. The
backend had already captured it -- ``fetch_result`` tails the log into
``result.error`` precisely for this -- and the status output rendered:

    deltaT5em4_run1 [failed]  no metrics reported

Nothing else. So the loop had to guess the cause, guessed that the initial water
column was mis-specified, and rewrote setFieldsDict. The second job then "reached
endTime" in 51 seconds with a third of the water: it had solved a different
problem, and reported it as usable.

The information existed, in the ledger, one field away from the renderer. This is
not a missing measurement, it is a measurement not shown -- the same shape as the
budget spend fixed in 49ce55e.

Truncated rather than dumped whole: a stack trace is long, the status output is
read on every wake, and the point is to say what kind of ending this was. Whoever
wants the rest has ops_outputs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsTuneStatusTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


SIGFPE = (
    "#0  Foam::error::printStack(Foam::Ostream&) in libOpenFOAM.so\n"
    "#1  Foam::sigFpe::sigHandler(int) in libOpenFOAM.so\n"
    "#2  ? in /lib/x86_64-linux-gnu/libc.so.6\n"
    "#3  Foam::MULES::limiter<...>(...) in libfiniteVolume.so\n"
    "#4  ? in interFoam\n"
)


class _Backend:
    async def spent_minutes(self) -> float:
        return 11.77

    async def remaining_minutes(self) -> float:
        return 138.23

    def unmeasured_spend(self) -> dict:
        return {}

    async def poll(self, handle):  # pragma: no cover -- every record is terminal here
        raise AssertionError("no live trials in these tests")


def _campaign(tmp_path: Path, *, status: str, error: str | None) -> Path:
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    (cdir / "meta.json").write_text(
        json.dumps(
            {
                "backend": "process",
                "host": "h",
                "command": "x {config} {job_dir}",
                "budget": {"unit": "core-minute", "total": 150, "overlap": "additive"},
            }
        ),
        encoding="utf-8",
    )
    rec = {
        "idem_key": "deltaT5em4_run1",
        "status": status,
        "campaign": "c",
        "handle": {"backend": "process", "job_id": "ops-deltaT5em4_run1"},
        "result": {"status": status, "metrics": {}, "output": {}, "error": error, "deliverable": None},
        "attempts": 0,
        "escalated": False,
    }
    (cdir / "ledger.json").write_text(json.dumps({"version": 1, "records": {"deltaT5em4_run1": rec}}), encoding="utf-8")
    return cdir


@pytest.mark.asyncio
async def test_a_failed_trial_shows_what_the_job_said(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path, status="failed", error=SIGFPE)
    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))

    assert "[failed]" in out
    assert "sigFpe" in out, (
        "the cause was in result.error all along; a loop that cannot see it guesses, "
        "and on the run this test comes from it guessed wrong and rewrote the case"
    )


@pytest.mark.asyncio
async def test_the_reason_is_truncated_not_dumped(tmp_path, monkeypatch):
    """Status output is read on every wake. A full log tail would push the rest of
    the campaign's state out of view."""
    cdir = _campaign(tmp_path, status="failed", error="x" * 4000)
    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))

    assert "x" * 100 in out, "some of it must be shown"
    assert len(out) < 3000, "but not four thousand characters of it"


@pytest.mark.asyncio
async def test_a_failure_with_nothing_captured_says_so(tmp_path, monkeypatch):
    """Silence and 'we looked and there was nothing' are different facts. A killed
    process leaves no output at all, and that absence is itself the reading."""
    cdir = _campaign(tmp_path, status="failed", error=None)
    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))

    assert "[failed]" in out
    assert "no reason captured" in out


@pytest.mark.asyncio
async def test_a_succeeded_trial_prints_no_reason_line(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path, status="succeeded", error=None)
    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))

    assert "no reason captured" not in out
    assert "why:" not in out
