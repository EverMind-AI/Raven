"""A budget that is not machine time, and what stops a watch that has spent it.

Every budget this layer had was compute, measured by the host. A campaign that
sits on a price feed for a day runs no jobs: the host's answer for what it spent
is zero however long it watched, and zero is the reading a loop acts on most
freely. What such a watch spends is the span it was asked to cover and the number
of times it woke to look -- each look a full cold start -- so those are declared,
counted from the campaign's own record, and enforced where the loop would
otherwise arrange the next one.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.state_claims import StateFacts, write_facts  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import (
    OpsCheckLaterTool,
    _budget_line,
    _watch_budget_spent,
)  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _watch(tmp_path, *, total, unit, meter, opened_minutes_ago=0, looks=0):
    cdir = tmp_path / "watch-volt"
    cdir.mkdir(exist_ok=True)
    opened = datetime.now() - timedelta(minutes=opened_minutes_ago)
    (cdir / "meta.json").write_text(
        json.dumps(
            {
                "connection": "conn_cpu_32c",
                "declared_at": opened.isoformat(timespec="seconds"),
                "budget": {"unit": unit, "total": total, "meter": meter},
            }
        ),
        encoding="utf-8",
    )
    if looks:
        write_facts(cdir, StateFacts(probe_seq=looks))
    return cdir


def test_the_status_line_says_who_counted_the_spend(tmp_path) -> None:
    """ "measured on the host" would misattribute it: the host was idle."""
    cdir = _watch(tmp_path, total=40, unit="look", meter="look", looks=12)
    meta = json.loads((cdir / "meta.json").read_text())

    line = _budget_line(meta, cdir=cdir)

    assert "40 look" in line and "12 used" in line and "28 left" in line
    assert "own record" in line and "host" in line.split("own record")[1]


def test_a_wall_clock_watch_is_spent_by_the_clock_not_by_the_jobs(tmp_path) -> None:
    cdir = _watch(tmp_path, total=240, unit="minute", meter="wall-clock", opened_minutes_ago=60)
    meta = json.loads((cdir / "meta.json").read_text())

    line = _budget_line(meta, cdir=cdir)
    used = float(line.split(" in total; ")[1].split(" used")[0])

    assert "240 minute" in line and 59.5 <= used <= 60.5


def test_a_compute_budget_is_unaffected(tmp_path) -> None:
    """The wording every campaign on disk gets, and the host is still the source."""
    cdir = _watch(tmp_path, total=150, unit="core-minute", meter="compute")
    meta = json.loads((cdir / "meta.json").read_text())

    line = _budget_line(meta, spent=32.1, remaining=117.9, cdir=cdir)

    assert "Campaign compute budget: 150 core-minute" in line
    assert "measured on the host" in line


def test_a_watch_with_looks_left_may_take_another(tmp_path) -> None:
    cdir = _watch(tmp_path, total=40, unit="look", meter="look", looks=39)

    assert _watch_budget_spent(cdir) == ""


def test_a_watch_that_has_used_its_looks_may_not_arrange_another(tmp_path) -> None:
    cdir = _watch(tmp_path, total=40, unit="look", meter="look", looks=40)

    refusal = _watch_budget_spent(cdir, attempting="another look")

    assert refusal.startswith("REFUSED")
    assert "40 of 40 look" in refusal
    assert "ops_finish" in refusal


def test_the_refusal_says_a_condition_that_never_held_is_still_a_result(tmp_path) -> None:
    """Twenty of SentinelBench's hundred tasks have "stay silent" as the right
    answer. A watch that ends with nothing to report has something to report."""
    cdir = _watch(tmp_path, total=10, unit="look", meter="look", looks=10)

    assert "never held" in _watch_budget_spent(cdir)


def test_a_compute_campaign_is_never_stopped_by_this(tmp_path) -> None:
    """The backend refuses a submit it cannot pay for, and looking costs it
    nothing. This check must not add a second, wrong ceiling on top."""
    cdir = _watch(tmp_path, total=150, unit="core-minute", meter="compute", looks=900)

    assert _watch_budget_spent(cdir) == ""


def test_a_campaign_with_no_budget_is_never_stopped_by_this(tmp_path) -> None:
    cdir = tmp_path / "no-budget"
    cdir.mkdir()
    (cdir / "meta.json").write_text(json.dumps({"connection": "c"}), encoding="utf-8")

    assert _watch_budget_spent(cdir) == ""


def test_the_backend_does_not_read_a_look_count_as_minutes(tmp_path) -> None:
    """The one place that would silently reinterpret the number: a watch allowed
    40 looks would have every job it does run clamped to what was left of "40
    minutes". A ceiling only the campaign can count does not belong to the device."""
    from oncall_flow.budget import Budget
    from oncall_flow.process_backend import ProcessExecutor

    looks = ProcessExecutor(
        lambda cmd: (0, ""), remote_dir=str(tmp_path), command="run.sh", budget=Budget("look", 40.0, meter="look")
    )
    compute = ProcessExecutor(
        lambda cmd: (0, ""), remote_dir=str(tmp_path), command="run.sh", budget=Budget("core-minute", 150.0)
    )

    assert looks._budget is None
    assert compute._budget == 150.0
    assert looks.budget_unit == "look", "and the unit is still printed as declared"


@pytest.mark.asyncio
async def test_a_refused_check_later_leaves_the_turn_open(tmp_path, monkeypatch) -> None:
    """This tool's default is that the turn is over, because a wake was arranged.
    On a refusal none was: closing the turn there leaves the campaign with nothing
    pending and nothing reported, which is the one state no wake recovers from."""
    monkeypatch.setenv("RAVEN_OPS_HOME", str(tmp_path))
    cdir = _watch(tmp_path, total=6, unit="look", meter="look", looks=6)

    out = await OpsCheckLaterTool().execute(
        eta_seconds=600,
        campaign=cdir.name,
        ledger=str(cdir / "ledger.json"),
        basis="the price has not moved",
    )

    assert out.startswith("REFUSED"), "a refusal, never the turn-closing wake note"
