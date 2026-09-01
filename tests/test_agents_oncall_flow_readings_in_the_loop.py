"""Where the readings get taken: declaring, looking, and finishing a round.

The table itself is tested in test_ops_readings.py. What is here is the wiring,
and each case is a hole the wiring exists to close:

  * the starting value has to be taken WHEN THE CAMPAIGN IS DECLARED. A wake turn
    is a cold start; "down 10% from where it started" is answerable against a
    record and nothing else, and by the time the loop notices it needs the
    number, the world has moved.
  * a watch may never submit a trial, so a look that only reconciles trials would
    tell the loop about the world by never looking at it.
  * a finished round's reading has to reach the trial's metrics, because that is
    the field every existing reader ranks and reports on. The FEA campaign that
    optimised max_penetration had it nowhere in the ledger.
  * and the table has to be extensible after round 0, because which line of
    job.dat holds the number is worked out from the first round's output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import readings as r  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsTuneStatusTool  # noqa: E402
from oncall_flow.tools.ops_declare import OpsDeclareTool  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


def _bound(tool, scheduler):
    tool.bind_runtime(SimpleNamespace(wake_scheduler=scheduler))
    return tool


@pytest.fixture
def home(tmp_path, monkeypatch):

    d = tmp_path / "ops"
    d.mkdir()
    tools_base.set_home(d)
    monkeypatch.setattr(
        "oncall_flow.connections.get", lambda cid: {"id": cid, "transport": "local"} if cid == "conn_ok" else None
    )
    monkeypatch.setattr("oncall_flow.connections.display_name", lambda cid: "the box")
    monkeypatch.setattr("oncall_flow.connections.describe", lambda: "conn_ok")
    monkeypatch.setattr("oncall_flow.connections.resolve_into", lambda meta: {**meta, "transport": "local"})
    return d


def _declare(**over):
    args = dict(
        campaign="watch",
        objective="tell me before /data fills up",
        objective_kind="condition",
        condition="free space under 10 GiB",
        connection="conn_ok",
        command="echo would-alert",
    )
    args.update(over)
    return OpsDeclareTool().execute(**args)


@pytest.mark.asyncio
async def test_the_starting_value_is_taken_while_declaring(home) -> None:
    out = await _declare(readings=[{"name": "free", "command": "echo 41.5", "when": "at_declare"}])

    assert "-> now 41.5" in out
    assert r.baseline(home / "watch") == {"free": 41.5}


@pytest.mark.asyncio
async def test_a_starting_value_that_could_not_be_read_says_so(home) -> None:
    """A declaration is still worth having without it, provided it does not look
    like it has one: a missing starting point is what quietly makes a relative
    claim unanswerable later."""
    out = await _declare(readings=[{"name": "free", "command": "exit 3", "when": "at_declare"}])

    assert "Declared 'watch'" in out
    assert "COULD NOT READ" in out


@pytest.mark.asyncio
async def test_a_declaration_with_no_readings_says_that_too(home) -> None:
    out = await _declare()

    assert "reads        nothing" in out


@pytest.mark.asyncio
async def test_readings_may_be_added_after_a_round_has_already_succeeded(home) -> None:
    """The rule this excuses is right about the target and the setup: a result
    already recorded was produced under those. Adding a reading changes nothing
    recorded -- and it has to be allowed here, because which line of job.dat holds
    the penetration was worked out from round 0's own output (74 commands, on
    2026-08-17)."""
    await _declare(readings=[{"name": "free", "command": "echo 1", "when": "each_wake"}])
    cdir = home / "watch"
    (cdir / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "r0": {
                        "idem_key": "r0",
                        "status": "succeeded",
                        "campaign": "watch",
                        "handle": {"backend": "process", "job_id": "ops-r0"},
                        "result": {"status": "succeeded", "metrics": {"core_minutes": 3.0}},
                        "attempts": 1,
                        "escalated": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    out = await _declare(readings=[{"name": "pen", "command": "cat {job_dir}/job.dat", "when": "after_trial"}])

    assert "Declared 'watch'" in out
    assert {row["name"] for row in json.loads((cdir / "meta.json").read_text())["readings"]} == {"free", "pen"}


@pytest.mark.asyncio
async def test_changing_anything_else_after_a_result_is_still_refused(home) -> None:
    """The exemption is for the readings table alone."""
    await _declare(readings=[{"name": "free", "command": "echo 1", "when": "each_wake"}])
    cdir = home / "watch"
    (cdir / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "r0": {
                        "idem_key": "r0",
                        "status": "succeeded",
                        "campaign": "watch",
                        "handle": {"backend": "process", "job_id": "ops-r0"},
                        "result": {"status": "succeeded", "metrics": {}},
                        "attempts": 1,
                        "escalated": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    out = await _declare(command="echo something-else")

    assert out.startswith("REFUSED") and "already has results" in out


@pytest.mark.asyncio
async def test_a_watch_that_never_submits_still_takes_its_readings(home) -> None:
    """This is the whole path a condition campaign lives on: no ledger, no trial,
    and a series that has to grow anyway."""
    await _declare(readings=[{"name": "free", "command": "echo 22.4", "when": "each_wake"}])
    cdir = home / "watch"

    out = await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))

    assert "Readings (taken by this campaign" in out
    assert "22.4" in out
    # Two: the starting value taken while declaring, and this look.
    assert [(row["when"], row["value"]) for row in r.read(cdir)] == [("at_declare", 22.4), ("each_wake", 22.4)]


@pytest.mark.asyncio
async def test_looking_at_a_watch_counts_as_a_look(home) -> None:
    """A look budget is spent from this count, and on the no-ledger path nothing
    else would record that a look happened."""
    await _declare(
        budget_total=3,
        budget_unit="look",
        budget_meter="look",
        readings=[{"name": "free", "command": "echo 1", "when": "each_wake"}],
    )
    cdir = home / "watch"

    first = await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))
    second = await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))

    assert "1 used, 2 left" in first
    assert "2 used, 1 left" in second


class _Backend:
    """A trial that finishes on the first poll, in a directory of its own."""

    def __init__(self, job_dir: str) -> None:
        self._job_dir = job_dir

    def job_dir(self, idem_key: str) -> str:
        return self._job_dir

    async def spent_minutes(self) -> float:
        return 3.0

    async def remaining_minutes(self) -> float | None:
        return 147.0

    async def poll(self, handle):
        from oncall_flow.backend import JobStatus

        return JobStatus.SUCCEEDED

    async def fetch_result(self, handle):
        from oncall_flow.backend import JobResult, JobStatus

        return JobResult(status=JobStatus.SUCCEEDED, metrics={"core_minutes": 3.0})


@pytest.mark.asyncio
async def test_a_finished_rounds_reading_lands_in_its_metrics(home, monkeypatch, tmp_path) -> None:
    """So it can be ranked. max_penetration was declared as an objective and lived
    only in the loop's prose, so best() had nothing to order the rounds by."""
    job_dir = tmp_path / "jobs" / "r0"
    job_dir.mkdir(parents=True)
    (job_dir / "job.dat").write_text("-70.88\n", encoding="utf-8")
    await _declare(
        objective_kind="optimize",
        metric="max_penetration",
        goal="min",
        condition="",
        readings=[{"name": "max_penetration", "command": "cat {job_dir}/job.dat", "when": "after_trial"}],
    )
    cdir = home / "watch"
    (cdir / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "r0": {
                        "idem_key": "r0",
                        "status": "running",
                        "campaign": "watch",
                        "handle": {"backend": "process", "job_id": "ops-r0"},
                        "result": None,
                        "attempts": 1,
                        "escalated": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend(str(job_dir)))

    out = await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))

    stored = json.loads((cdir / "ledger.json").read_text())["records"]["r0"]
    assert stored["result"]["metrics"]["max_penetration"] == -70.88
    assert "-70.88" in out


@pytest.mark.asyncio
async def test_the_same_finished_round_is_not_read_again(home, monkeypatch, tmp_path) -> None:
    job_dir = tmp_path / "jobs" / "r0"
    job_dir.mkdir(parents=True)
    (job_dir / "job.dat").write_text("-70.88\n", encoding="utf-8")
    await _declare(readings=[{"name": "pen", "command": "cat {job_dir}/job.dat", "when": "after_trial"}])
    cdir = home / "watch"
    (cdir / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "r0": {
                        "idem_key": "r0",
                        "status": "running",
                        "campaign": "watch",
                        "handle": {"backend": "process", "job_id": "ops-r0"},
                        "result": None,
                        "attempts": 1,
                        "escalated": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("oncall_flow.backends.backend_from_meta", lambda meta: _Backend(str(job_dir)))

    await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))
    await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))

    assert len([row for row in r.read(cdir) if row["name"] == "pen"]) == 1


@pytest.mark.asyncio
async def test_an_each_wake_reading_gets_its_starting_value_for_free(home) -> None:
    """ "Down 10% from where it started" is answerable against a record and nothing
    else, and a cold-started wake has no memory to fall back on. So the starting
    point is not something the loop has to remember to ask for: declaring what to
    watch every wake takes the first reading now, and that reading is the start."""
    out = await _declare(readings=[{"name": "volt", "command": "echo 248.42", "when": "each_wake"}])

    assert "-> now 248.42" in out
    assert r.baseline(home / "watch") == {"volt": 248.42}


@pytest.mark.asyncio
async def test_the_starting_value_is_shown_beside_the_series(home) -> None:
    await _declare(readings=[{"name": "volt", "command": "echo 248.42", "when": "each_wake"}])
    cdir = home / "watch"

    out = await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))

    assert "at declare 248.42" in out


@pytest.mark.asyncio
async def test_a_concluded_watch_reads_back_as_concluded_and_spends_nothing(home) -> None:
    """A zero-trial campaign never grows a ledger, so the no-ledger branch is the
    only status it ever renders. Before this said CONCLUDED, a campaign that had
    finished read back as live -- "round 0 has not been submitted" -- and every
    such look took a reading and spent the closed campaign's budget."""
    await _declare(readings=[{"name": "volt", "command": "echo 248.42", "when": "each_wake"}])
    cdir = home / "watch"
    rows_before = len(r.read(cdir))
    (cdir / "concluded.json").write_text(json.dumps({"concluded_at": "2026-08-28T14:28:11", "outcome": "stopped"}))

    out = await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))

    assert "CONCLUDED" in out
    assert len(r.read(cdir)) == rows_before


@pytest.mark.asyncio
async def test_check_later_refuses_a_concluded_campaign(home) -> None:
    """ops_finish stands down the pending wakes, not the right to arrange new
    ones: a look-then-re-arm after conclusion scheduled a fresh wake, and the
    wake shell adopted and ran it (measured 2026-08-28). The cron re-arm guard
    covers only the wake the machinery adds by itself."""
    from oncall_flow.tools.ops import OpsCheckLaterTool

    await _declare(readings=[{"name": "volt", "command": "echo 248.42", "when": "each_wake"}])
    cdir = home / "watch"
    await OpsTuneStatusTool().execute(campaign="watch", ledger=str(cdir / "ledger.json"))
    (cdir / "concluded.json").write_text(json.dumps({"concluded_at": "2026-08-28T14:28:11", "outcome": "stopped"}))

    scheduled = []

    class _Wakes:
        def pending_wakes(self, prefix=""):
            return []

        def schedule_wake(self, key, at_ms, message, **route):
            scheduled.append({"key": key, "message": message})
            raise AssertionError("a concluded campaign must not reach the scheduler")

    tool = _bound(OpsCheckLaterTool(), _Wakes())
    tool.set_context("tui", "default")
    out = await tool.execute(
        campaign="watch", ledger=str(cdir / "ledger.json"), eta_seconds=60, basis="fresh look taken just now"
    )

    text = getattr(out, "model_text", None) or str(out)
    assert "REFUSED" in text and "concluded" in text.lower()
    assert scheduled == []
