"""A campaign's spend is its own jobs, even when campaigns share a rounds directory.

Measured 2026-09-11 on a shared 2xA800 box: five campaigns of one task declared
the same ``remote_dir``, so ``runs/jobs/`` held every round's jobs. The spend
scan walked the directory, and the second-round campaign (budget 130) read 125.6
spent on its first look -- 34.6 of it its own, the rest two earlier rounds --
and stopped with 142 real minutes unspent and fifteen runs never submitted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backends import backend_from_meta, billing_only  # noqa: E402
from oncall_flow.ledger import Ledger  # noqa: E402
from oncall_flow.process_backend import ProcessExecutor  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops_declare import OpsDeclareTool  # noqa: E402

from tests.test_agents_oncall_flow_process_backend import CMD, FakeHost, _spec  # noqa: E402


def _finished_job(host: FakeHost, key: str, *, minutes: float, started_at: float) -> None:
    """A job that sits in the shared jobs directory but was never submitted here."""
    host.jobs[key] = {
        "alive": False,
        "started_at": started_at,
        "finished_at": started_at + minutes * 60,
        "result": {"status": "succeeded", "gpu_minutes_used": minutes, "eval_points": [[200, 0.35]]},
    }


@pytest.mark.asyncio
async def test_a_sibling_campaigns_job_in_the_same_directory_is_not_billed(tmp_path: Path) -> None:
    host = FakeHost()
    ledger = Ledger(tmp_path / "ledger.json")
    exe = ProcessExecutor(host, remote_dir="/w", command=CMD, budget_minutes_total=130)
    exe.restrict_spend_to(ledger)

    # The round before, in the same runs/jobs directory: 91 minutes of someone else's runs.
    _finished_job(host, "round1_a", minutes=45.0, started_at=host.now - 7200)
    _finished_job(host, "round1_b", minutes=46.0, started_at=host.now - 3600)

    # This campaign's own two runs, recorded in its ledger as they are submitted.
    await exe.submit(_spec({"lr": 2e-6}, key="own_a"))
    ledger.record("own_a", campaign="round2")
    host.finish("own_a", minutes=20.0)
    host.now += 20 * 60  # the second run starts when the first is done: sequential, not overlapping
    await exe.submit(_spec({"lr": 5e-6}, key="own_b"))
    ledger.record("own_b", campaign="round2")
    host.finish("own_b", minutes=14.6)

    assert await exe.spent_minutes() == pytest.approx(34.6), "only this campaign's runs are billed"
    assert await exe.remaining_minutes() == pytest.approx(130 - 34.6)

    unrestricted = ProcessExecutor(host, remote_dir="/w", command=CMD, budget_minutes_total=130)
    assert await unrestricted.spent_minutes() == pytest.approx(34.6 + 91.0), (
        "the same directory read without the ledger bills the sibling's runs too -- the 2026-09-11 reading"
    )


@pytest.mark.asyncio
async def test_own_runs_on_two_cards_are_still_billed_by_the_overlap_rules(tmp_path: Path) -> None:
    """The filter sits before the timeline arithmetic, not instead of it."""
    host = FakeHost()
    ledger = Ledger(tmp_path / "ledger.json")
    exe = ProcessExecutor(host, remote_dir="/w", command=CMD, budget_minutes_total=200)
    exe.restrict_spend_to(ledger)
    _finished_job(host, "elsewhere", minutes=60.0, started_at=host.now)

    await exe.submit(_spec({"lr": 1e-6}, key="card0"))
    ledger.record("card0", campaign="c")
    await exe.submit(_spec({"lr": 2e-6}, key="card1"))
    ledger.record("card1", campaign="c")
    host.now += 20 * 60  # both still running, side by side, on one pinned device

    assert await exe.spent_minutes() == pytest.approx(20.0), (
        "two of our own runs sharing the device for twenty minutes occupy it for twenty, not forty; "
        "the sixty-minute stranger in the same directory is not ours"
    )


@pytest.mark.asyncio
async def test_a_run_this_executor_submitted_is_billed_before_the_ledger_hears_of_it(tmp_path: Path) -> None:
    host = FakeHost()
    ledger = Ledger(tmp_path / "ledger.json")
    exe = ProcessExecutor(host, remote_dir="/w", command=CMD, budget_minutes_total=90)
    exe.restrict_spend_to(ledger)
    await exe.submit(_spec({"lr": 2e-6}, key="j1"))  # not yet recorded in the ledger
    host.now += 10 * 60
    assert await exe.spent_minutes() == pytest.approx(10.0), "a silent zero here would be a refund"


def test_billing_only_hands_the_ledger_to_a_backend_that_measures_by_directory(tmp_path: Path, monkeypatch) -> None:
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.record("mine", campaign="c")
    host = FakeHost()
    monkeypatch.setattr("oncall_flow.transport.runner_from", lambda meta: host)
    meta = {"backend": "process", "remote_dir": "/w", "command": CMD, "budget": {"unit": "gpu-minute", "total": 10}}

    backend = billing_only(backend_from_meta(meta), ledger)
    assert isinstance(backend, ProcessExecutor)
    assert backend._counts_toward_spend("mine") and not backend._counts_toward_spend("theirs")

    class _Double:
        pass

    double = _Double()
    assert billing_only(double, ledger) is double, "a backend without the seam is handed back untouched"
    assert billing_only(backend_from_meta(meta), None)._counts_toward_spend("theirs"), "no ledger, no filter"


@pytest.fixture
def ops_home(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "ops"
    d.mkdir()
    tools_base.set_home(d)
    monkeypatch.setattr(
        "oncall_flow.connections.get", lambda cid: {"host": "h", "port": 64106} if cid == "conn_ok" else None
    )
    monkeypatch.setattr("oncall_flow.connections.display_name", lambda cid: "the box" if cid == "conn_ok" else "")
    return d


def _declare(**over):
    args = dict(
        campaign="round2",
        objective="lowest val_bpb",
        objective_kind="optimize",
        metric="val_bpb",
        goal="min",
        connection="conn_ok",
        staged_case="/srv/case",
        command="bash {job_dir}/run.sh",
        remote_dir="/srv/runs",
    )
    args.update(over)
    return OpsDeclareTool().execute(**args)


def _sibling(ops_home: Path, name: str, remote_dir: str, *, concluded: bool = False) -> None:
    d = ops_home / name
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"connection": "conn_ok", "remote_dir": remote_dir}), encoding="utf-8")
    if concluded:
        (d / "concluded.json").write_text("{}", encoding="utf-8")


@pytest.mark.asyncio
async def test_a_rounds_directory_a_live_sibling_keeps_is_refused(ops_home: Path) -> None:
    _sibling(ops_home, "round1", "/srv/runs")
    out = await _declare()
    assert out.startswith("REFUSED"), out
    assert "'round1'" in out and "/srv/runs" in out
    assert not (ops_home / "round2" / "meta.json").exists(), "a refusal writes nothing"


@pytest.mark.asyncio
async def test_a_concluded_siblings_directory_and_ones_own_are_free_to_use(ops_home: Path) -> None:
    _sibling(ops_home, "round1", "/srv/runs", concluded=True)
    assert "Declared 'round2'" in await _declare()
    assert "Declared 'round2'" in await _declare(), "re-declaring the same campaign is not a collision"
    assert "Declared 'round3'" in await _declare(campaign="round3", remote_dir="/srv/runs3")
