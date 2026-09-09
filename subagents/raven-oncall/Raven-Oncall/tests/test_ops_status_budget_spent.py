"""The budget line states what is left, not only what was allowed.

Measured 2026-08-12 on the OpenFOAM leg, and it cost the run. The status output
said only:

    Campaign compute budget: 150 core-minute in total.

No spend, no remainder -- while the backend could answer both (``spent_minutes``
read 16.65 off the box at that moment). So the loop derived the spend itself, from
ExecutionTime in the solver log, and multiplied by a core count it had passed in
its own config and that the job script silently ignores. It concluded 70.9
core-minutes were gone with 79.1 left, against a true 17.7 and 132.3, and on that
4x overestimate it cut ``endTime`` from 1 to 0.5 -- reducing the objective the
task had set, in a budget that would have covered the original.

Its arithmetic was right on its own premises. The premise was missing from the
only place that could supply it.

This is the mirror image of the usual gate here. Most refusals exist because we
can CHECK a thing the loop must supply. This one is a number we can SUPPLY and
were not supplying.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsTuneStatusTool


class _Backend:
    """Stands in for a real backend: answers spend, nothing else interesting."""

    def __init__(self, spent: float | None = 16.65, remaining: float | None = 133.35,
                 unmeasured: dict | None = None, raises: bool = False) -> None:
        self._spent, self._remaining = spent, remaining
        self._unmeasured = unmeasured or {}
        self._raises = raises

    async def spent_minutes(self) -> float:
        if self._raises:
            raise OSError("host unreachable")
        return self._spent

    async def remaining_minutes(self) -> float | None:
        if self._raises:
            raise OSError("host unreachable")
        return self._remaining

    def unmeasured_spend(self) -> dict:
        return dict(self._unmeasured)

    async def poll(self, handle):
        from raven.ops import JobStatus
        return JobStatus.RUNNING


def _campaign(tmp_path: Path, *, with_trial: bool = True, **meta_over) -> Path:
    """A campaign with one running trial by default.

    The spend only matters once something is running, and an empty ledger takes a
    different branch (``_meta_lines``, which is synchronous and has no backend) --
    so the default here is the state the reading is for.
    """
    cdir = tmp_path / "c"
    cdir.mkdir(exist_ok=True)
    meta = {"backend": "process", "host": "h", "command": "x {config} {job_dir}",
            "budget": {"unit": "core-minute", "total": 150, "overlap": "additive"}}
    meta.update(meta_over)
    (cdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    records = {}
    if with_trial:
        records["t1"] = {"idem_key": "t1", "status": "running", "campaign": "c",
                         "handle": {"backend": "process", "job_id": "ops-t1"},
                         "result": None, "attempts": 0, "escalated": False}
    (cdir / "ledger.json").write_text(
        json.dumps({"version": 1, "records": records}), encoding="utf-8")
    return cdir


@pytest.mark.asyncio
async def test_the_budget_line_carries_spend_and_remainder(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path)
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))

    assert "150 core-minute in total" in out, "the total must not disappear"
    assert "16.65" in out and "133.35" in out, (
        "a loop that cannot read its spend derives it, and a derived spend was wrong "
        "by 4x on the run this test comes from"
    )
    assert "core-minute" in out


@pytest.mark.asyncio
async def test_an_unreachable_host_says_so_instead_of_printing_a_zero(tmp_path, monkeypatch):
    """A silent zero is worse than an absence: it reads as "nothing spent yet",
    which is exactly the state a loop acts on most freely."""
    cdir = _campaign(tmp_path)
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend(raises=True))
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))

    assert "150 core-minute in total" in out
    assert "could not be read" in out or "unavailable" in out
    assert "0 core-minute used" not in out
    assert "0.0" not in out.split("budget")[1][:80] if "budget" in out else True


@pytest.mark.asyncio
async def test_jobs_whose_spend_is_unmeasurable_are_named(tmp_path, monkeypatch):
    """The spend that could not be measured is part of the reading. Measured on the
    ML line: three of six trials recorded no duration at all, so a total summed
    from the rest reads complete and is not."""
    cdir = _campaign(tmp_path)
    monkeypatch.setattr(
        "raven.ops.backends.backend_from_meta",
        lambda meta: _Backend(spent=57.76, remaining=82.24,
                              unmeasured={"trial-a": "killed with nothing to measure its spend from"}),
    )
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))

    assert "57.76" in out
    assert "trial-a" in out, "a job with no measurable spend must be named, not folded into zero"


@pytest.mark.asyncio
async def test_an_undeclared_budget_still_says_nothing_will_stop_it(tmp_path, monkeypatch):
    cdir = _campaign(tmp_path, budget=None)
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))
    assert "none declared" in out and "nothing will stop" in out


@pytest.mark.asyncio
async def test_the_pre_submit_path_prints_the_total_without_a_spend(tmp_path, monkeypatch):
    """Before round 0 there is nothing spent and no job dir to read -- printing a
    measured 0 there would be a different claim from "not started"."""
    cdir = _campaign(tmp_path)
    (cdir / "ledger.json").unlink()
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))
    assert "round 0 has not been submitted" in out
    assert "150 core-minute in total" in out


@pytest.mark.asyncio
async def test_an_empty_ledger_prints_the_total_alone(tmp_path, monkeypatch):
    """A ledger that exists and holds nothing is the turn that decides what to
    submit. Nothing has been spent yet, so there is no measurement to print --
    and this branch is synchronous, with no backend in reach."""
    cdir = _campaign(tmp_path, with_trial=False)
    monkeypatch.setattr("raven.ops.backends.backend_from_meta", lambda meta: _Backend())
    out = await OpsTuneStatusTool().execute(ledger=str(cdir / "ledger.json"))
    assert "is empty (starting up)" in out
    assert "150 core-minute in total" in out
    assert "used" not in out
