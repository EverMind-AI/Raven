"""A campaign has exactly one pending wake, however many times it schedules.

The incident (2026-08-06, round 8). Inside a single turn the loop alternated
``ops_check_later`` and ``ops_tune_status`` fifteen times -- each check_later
citing a real, freshly probed reading, so the basis gate accepted every one --
and each call added another one-shot wake. The turn stopped on its own at the
loop's 40-iteration cap, but the cron store was left holding sixteen pending
wakes for one campaign. Sixteen wakes means sixteen drivers: the same campaign
would have been woken sixteen times, each turn deciding again from scratch.

The invariant this pins: scheduling the next look is idempotent per campaign.
A second schedule supersedes the first rather than joining it. The loop is still
free to change when it wants to be woken -- that is its judgement, and the last
call wins -- but it cannot end up with two futures.

Related and deliberately opposite: ``tests/test_cron_service_ops_rearm.py`` keeps
a campaign from ending up with *no* pending wake. One guards against zero, this
one against many.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsCheckLaterTool, _schedule_ops_wake
from raven.config import paths as config_paths
from raven.proactive_engine.schedulers.cron.service import CronService

CAMPAIGN = "armb-embed-r8e"


def _svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "jobs.json", allowed_channels={"tui"})


def _pending(svc: CronService, campaign: str = CAMPAIGN) -> list:
    return [j for j in svc.list_jobs() if j.name.startswith(f"ops:{campaign}:")]


def _campaign(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "ops"
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: root)
    cdir = root / CAMPAIGN
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    ledger = cdir / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    "t1": {
                        "idem_key": "t1",
                        "status": "running",
                        "campaign": CAMPAIGN,
                        "handle": {"backend": "process", "job_id": "ops-t1"},
                        "result": None,
                        "attempts": 1,
                        "escalated": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return cdir


def test_scheduling_twice_leaves_one_pending_wake(tmp_path: Path) -> None:
    svc = _svc(tmp_path)

    first = _schedule_ops_wake(svc, "tui", "default", name=f"ops:{CAMPAIGN}:r1", message="look now", eta_seconds=600)
    second = _schedule_ops_wake(
        svc, "tui", "default", name=f"ops:{CAMPAIGN}:recheck", message="look later", eta_seconds=1800
    )

    assert "Scheduled" in first and "Scheduled" in second
    pending = _pending(svc)
    assert len(pending) == 1, f"expected the second to supersede the first, got {[j.name for j in pending]}"
    # the surviving one is the latest request
    assert pending[0].name == f"ops:{CAMPAIGN}:recheck"
    assert pending[0].payload.message == "look later"


def test_fifteen_schedules_in_one_turn_leave_one(tmp_path: Path) -> None:
    """The measured shape of the incident, at its measured count."""
    svc = _svc(tmp_path)
    for i in range(15):
        _schedule_ops_wake(
            svc, "tui", "default", name=f"ops:{CAMPAIGN}:recheck", message=f"look {i}", eta_seconds=600 + i
        )

    pending = _pending(svc)
    assert len(pending) == 1
    assert pending[0].payload.message == "look 14"


def test_replacing_says_so(tmp_path: Path) -> None:
    """The loop has to be able to tell it moved a wake rather than adding one --
    otherwise 'Scheduled a wake' reads as 'now there are two'."""
    svc = _svc(tmp_path)
    _schedule_ops_wake(svc, "tui", "default", name=f"ops:{CAMPAIGN}:r1", message="a", eta_seconds=600)
    note = _schedule_ops_wake(svc, "tui", "default", name=f"ops:{CAMPAIGN}:recheck", message="b", eta_seconds=1800)

    assert "replac" in note.lower() or "moved" in note.lower()


def test_another_campaigns_wake_is_untouched(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    _schedule_ops_wake(svc, "tui", "default", name="ops:other-campaign:r1", message="keep me", eta_seconds=600)
    _schedule_ops_wake(svc, "tui", "default", name=f"ops:{CAMPAIGN}:r1", message="mine", eta_seconds=600)

    assert len(_pending(svc, "other-campaign")) == 1
    assert len(_pending(svc)) == 1


def test_non_ops_reminder_is_untouched(tmp_path: Path) -> None:
    from raven.proactive_engine.schedulers.cron.types import CronSchedule

    svc = _svc(tmp_path)
    svc.add_job(
        name="drink water",
        schedule=CronSchedule(kind="at", at_ms=svc._now_ms() + 600_000),
        message="hydrate",
        deliver=True,
        channel="tui",
        to="default",
    )
    _schedule_ops_wake(svc, "tui", "default", name=f"ops:{CAMPAIGN}:r1", message="mine", eta_seconds=600)
    _schedule_ops_wake(svc, "tui", "default", name=f"ops:{CAMPAIGN}:recheck", message="mine2", eta_seconds=600)

    assert any(j.name == "drink water" for j in svc.list_jobs())
    assert len(_pending(svc)) == 1


@pytest.mark.parametrize("calls", [2, 5])
async def test_check_later_tool_does_not_stack(tmp_path: Path, monkeypatch, calls: int) -> None:
    """End-to-end through the tools the loop actually called, in the order it
    called them.

    The alternation is not incidental: the basis gate refuses a decision with no
    observation recorded since the previous one, so a loop that wants to
    reschedule has to probe first. That is the gate working -- and it is why the
    incident produced fifteen observe/decide pairs rather than fifteen bare
    schedules. The stacking had to be fixed at the scheduling layer.
    """
    from raven.agent.tools.ops import OpsTuneStatusTool
    from raven.ops import backends as ops_backends
    from raven.ops.backend import JobStatus

    cdir = _campaign(monkeypatch, tmp_path)
    svc = _svc(tmp_path)

    class _Backend:
        name = "process"

        async def fetch_progress(self, handle, tail: int = 40):
            return [{"step": 80, "eval_ndcg": 0.3059, "loss": 0.17767, "elapsed_s": 91.7}]

        async def poll(self, handle):
            return JobStatus.RUNNING

    monkeypatch.setattr(ops_backends, "backend_from_meta", lambda meta: _Backend())

    later = OpsCheckLaterTool(svc)
    later.set_context("tui", "default")
    status = OpsTuneStatusTool()

    for _ in range(calls):
        await status.execute(ledger=str(cdir / "ledger.json"), metric="ndcg")
        out = await later.execute(
            campaign=CAMPAIGN,
            ledger=str(cdir / "ledger.json"),
            eta_seconds=600,
            basis="ndcg 0.3059 at step 80, still running",
        )
        assert "REFUSED" not in out, out

    assert len(_pending(svc)) == 1
