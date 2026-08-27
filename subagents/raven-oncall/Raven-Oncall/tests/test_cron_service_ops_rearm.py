"""An on-call wake that ends without advancing its campaign gets re-armed.

The incident (2026-08-06, round 7). The wake fired on time and carried a
complete instruction -- campaign name, ledger path, objective, starting value,
and the four actions to choose between. The turn read a mood-state file that no
part of the product defines, created it, wrote ``{"mood": "neutral"}``, read the
episodic memory, and answered "initialization complete". It never called
ops_tune_status and never scheduled another wake, so the cron store went to zero
jobs: the job kept burning GPU minutes and nothing was watching it any more.

That is the on-call loop's first obligation -- hold the schedule -- failing, and
failing silently. One turn of inattention is enough, and no amount of prompt
wording rules it out. So the guarantee belongs in the mechanism: while a
campaign is live, there must always be a next turn.

What this deliberately does NOT do: decide anything. It does not submit, kill,
wait, or report, and the re-armed wake carries the same message the original
did. The judgement stays with the loop; only the chance to make it is
guaranteed.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.config import paths as config_paths
from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule

CAMPAIGN = "armb-embed-r7e"


def _use_ops_home(monkeypatch, root: Path) -> Path:
    monkeypatch.setattr(config_paths, "get_ops_home", lambda: root)
    cdir = root / CAMPAIGN
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    return cdir


def _soon(svc: CronService) -> int:
    return svc._now_ms() + 60_000


def _add_ops_wake(svc: CronService, *, name: str = f"ops:{CAMPAIGN}:r1") -> str:
    job = svc.add_job(
        name=name,
        schedule=CronSchedule(kind="at", at_ms=_soon(svc)),
        message=f"[Ops campaign '{CAMPAIGN}' round 0 due] Call ops_tune_status(...)",
        channel="tui",
        to="default",
        delete_after_run=True,
        dedup=False,
    )
    # Force it due, persisted: _process_due reloads from disk.
    for j in svc._store.jobs:
        if j.id == job.id:
            j.state.next_run_at_ms = 1
    svc._save_store()
    return job.id


def _events(cdir: Path) -> list[dict]:
    path = cdir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _run_one_wake(svc: CronService, on_job) -> None:
    svc.on_job = on_job
    await svc._process_due()


async def test_wake_that_advanced_nothing_is_rearmed(tmp_path: Path, monkeypatch) -> None:
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def did_nothing(job) -> None:
        return None

    await _run_one_wake(svc, did_nothing)

    pending = [j for j in svc._store.jobs if j.name.startswith(f"ops:{CAMPAIGN}:")]
    assert pending, "a live campaign must always have a next wake"
    kinds = [e.get("kind") for e in _events(cdir)]
    assert "wake_rearmed" in kinds, "the re-arm has to be visible on the trail, not silent"


async def test_wake_that_scheduled_its_own_next_turn_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    """ops_check_later already scheduled one. Adding a second would give the
    campaign two drivers, which is the defect this is not allowed to create."""
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def scheduled_next(job) -> None:
        svc.add_job(
            name=f"ops:{CAMPAIGN}:recheck",
            schedule=CronSchedule(kind="at", at_ms=svc._now_ms() + 600_000),
            message="next look",
            channel="tui",
            to="default",
            delete_after_run=True,
            dedup=False,
        )

    await _run_one_wake(svc, scheduled_next)

    pending = [j for j in svc._store.jobs if j.name.startswith(f"ops:{CAMPAIGN}:")]
    assert len(pending) == 1, "exactly the one the loop scheduled itself"
    # Which one it is, not just how many. A re-arm that fired here would replace
    # the turn's own next look rather than sit beside it, so the count stays at
    # one while the message goes back a round. Raised by the parallel CFD line.
    assert pending[0].name == f"ops:{CAMPAIGN}:recheck"
    assert pending[0].payload.message == "next look"
    assert "wake_rearmed" not in [e.get("kind") for e in _events(cdir)]


async def test_concluded_campaign_is_not_rearmed(tmp_path: Path, monkeypatch) -> None:
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    (cdir / "concluded.json").write_text(json.dumps({"concluded_at": "2026-08-06T15:00:00"}), encoding="utf-8")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def did_nothing(job) -> None:
        return None

    await _run_one_wake(svc, did_nothing)

    assert not [j for j in svc._store.jobs if j.name.startswith(f"ops:{CAMPAIGN}:")]
    assert "wake_rearmed" not in [e.get("kind") for e in _events(cdir)]


async def test_finished_report_ends_the_watch(tmp_path: Path, monkeypatch) -> None:
    """A campaign that filed its final report is done; re-arming would hand it
    turns nobody asked for."""
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    (cdir / "events.jsonl").write_text(
        json.dumps({"ts": "2026-08-06T15:00:00", "kind": "report_accepted", "report_kind": "finished"}) + "\n",
        encoding="utf-8",
    )
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def did_nothing(job) -> None:
        return None

    await _run_one_wake(svc, did_nothing)

    assert not [j for j in svc._store.jobs if j.name.startswith(f"ops:{CAMPAIGN}:")]


async def test_rearming_stops_after_a_cap(tmp_path: Path, monkeypatch) -> None:
    """A loop that ignores every wake must not be re-armed forever: an unbounded
    retry is its own silent failure, and the cap has to be recorded so the trail
    says the watch was given up rather than never started."""
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def did_nothing(job) -> None:
        return None

    for _ in range(6):
        for job in svc._store.jobs:
            job.state.next_run_at_ms = 1
        svc._save_store()
        await _run_one_wake(svc, did_nothing)

    kinds = [e.get("kind") for e in _events(cdir)]
    assert kinds.count("wake_rearmed") <= 3
    assert "wake_rearm_capped" in kinds
    assert not [j for j in svc._store.jobs if j.name.startswith(f"ops:{CAMPAIGN}:")]


async def test_non_ops_job_is_untouched(tmp_path: Path, monkeypatch) -> None:
    _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    svc.add_job(
        name="drink water",
        schedule=CronSchedule(kind="at", at_ms=_soon(svc)),
        message="hydrate",
        channel="tui",
        to="default",
        delete_after_run=True,
    )
    for j in svc._store.jobs:
        j.state.next_run_at_ms = 1
    svc._save_store()

    async def did_nothing(job) -> None:
        return None

    await _run_one_wake(svc, did_nothing)

    assert not svc._store.jobs, "a plain one-shot reminder still deletes itself"


async def test_rearmed_wake_repeats_the_original_message(tmp_path: Path, monkeypatch) -> None:
    """The message is what makes a wake turn self-sufficient (it arrives with no
    conversation history). Re-arming with anything else would change what the
    loop is asked, which is not this mechanism's business."""
    _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)
    original = svc._store.jobs[0].payload.message

    async def did_nothing(job) -> None:
        return None

    await _run_one_wake(svc, did_nothing)

    rearmed = [j for j in svc._store.jobs if j.name.startswith(f"ops:{CAMPAIGN}:")]
    assert rearmed and rearmed[0].payload.message == original
    assert rearmed[0].payload.channel == "tui" and rearmed[0].payload.to == "default"


async def test_a_next_look_scheduled_through_the_generic_tool_stops_the_rearm(
    tmp_path: Path, monkeypatch
) -> None:
    """The producer the two keyed checks cannot see.

    A turn that schedules through the generic cron tool tags no campaign and
    follows no naming rule, so neither the campaign field nor the ops: prefix
    finds its next look, and the re-arm lands on top of it. When it was scheduled
    is the one thing no producer can fail to carry, so that is what this keys on.

    The count alone does not separate the two outcomes once the campaign
    invariant is in place -- a re-arm would REPLACE this job rather than join it,
    leaving one pending wake carrying the previous round's message. Which job
    survives is the discriminator.
    """
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def scheduled_next_the_generic_way(job) -> None:
        svc.add_job(
            name="check the dambreak run",
            schedule=CronSchedule(kind="at", at_ms=svc._now_ms() + 3_600_000),
            message="check the dambreak run",
            channel="tui",
            to="default",
            delete_after_run=True,
        )

    await _run_one_wake(svc, scheduled_next_the_generic_way)

    pending = [j for j in svc._store.jobs if j.enabled]
    assert len(pending) == 1
    assert pending[0].name == "check the dambreak run"
    assert "wake_rearmed" not in [e.get("kind") for e in _events(cdir)]


async def test_a_job_scheduled_before_the_turn_does_not_stop_the_rearm(
    tmp_path: Path, monkeypatch
) -> None:
    """The creation-time check must not read somebody else's older reminder as
    this turn's next look, or an inattentive turn goes unwatched whenever any
    unrelated job happens to be pending."""
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    svc.add_job(
        name="water the plants",
        schedule=CronSchedule(kind="at", at_ms=svc._now_ms() + 7_200_000),
        message="water the plants",
        channel="tui",
        to="default",
        delete_after_run=True,
    )
    _add_ops_wake(svc)

    async def did_nothing(job) -> None:
        return None

    await _run_one_wake(svc, did_nothing)

    assert "wake_rearmed" in [e.get("kind") for e in _events(cdir)]
    assert [j.name for j in svc._store.jobs if j.name.startswith(f"ops:{CAMPAIGN}:")]


async def test_a_next_look_written_by_another_process_stops_the_rearm(
    tmp_path: Path, monkeypatch
) -> None:
    """The producer every other test in this file cannot model: a child process.

    Under ``wake_shell`` the turn is not a callback that shares this service's
    memory -- it is ``raven agent -m`` in its own process, and the wake it
    schedules reaches jobs.json and nothing else. The tests above all add through
    the same ``svc``, so the new job appears in ``self._store`` for free and the
    "did the turn schedule its own next look?" check finds it. That made the
    check look correct while it was reading a snapshot taken when the job was
    claimed, one whole turn before the turn could have scheduled anything.

    Measured 2026-08-25 on cantilever-limit-load, twice: the turn submitted round
    2 and scheduled 'r3'; the check read the pre-turn snapshot, saw nothing, and
    re-armed -- and ``add_job``, which does reload, then deleted r3 as the
    campaign's stale pending job. The loop replaced its own next look with one
    ten minutes out and logged "turn ended with no campaign action" one line
    after that turn's own submit.

    A second CronService on the same store is the child process: separate
    in-memory store, same file, same lock.
    """
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def scheduled_next_from_another_process(job) -> None:
        child = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
        child.add_job(
            campaign=CAMPAIGN,
            name=f"ops:{CAMPAIGN}:r2",
            schedule=CronSchedule(kind="at", at_ms=child._now_ms() + 300_000),
            message="round 1 due",
            channel="tui",
            to="default",
            delete_after_run=True,
            dedup=False,
        )

    await _run_one_wake(svc, scheduled_next_from_another_process)

    svc._store = None
    pending = [j for j in svc._load_store().jobs if j.enabled]
    assert [j.name for j in pending] == [f"ops:{CAMPAIGN}:r2"], (
        "the turn's own next look must survive, not be replaced by a re-arm"
    )
    assert pending[0].payload.message == "round 1 due"
    assert "wake_rearmed" not in [e.get("kind") for e in _events(cdir)]


async def test_an_inattentive_turn_in_another_process_is_still_rearmed(
    tmp_path: Path, monkeypatch
) -> None:
    """The other side of the reload: reading the file must not make the wake that
    just ran count as its own next look.

    ``_writeback_after_run`` is what removes a fired one-shot, and it runs after
    this check -- so on disk the job is still there and still enabled, tagged with
    this very campaign. Counting it would suppress every re-arm the mechanism
    exists to make, turning the 2026-08-06 incident back on while the file-read
    fix made it look addressed.
    """
    cdir = _use_ops_home(monkeypatch, tmp_path / "ops")
    svc = CronService(tmp_path / "jobs.json", allowed_channels={"tui"})
    _add_ops_wake(svc)

    async def did_nothing_in_another_process(job) -> None:
        CronService(tmp_path / "jobs.json", allowed_channels={"tui"})._load_store()

    await _run_one_wake(svc, did_nothing_in_another_process)

    assert "wake_rearmed" in [e.get("kind") for e in _events(cdir)]
    svc._store = None
    assert [j.name for j in svc._load_store().jobs if j.enabled] == [f"ops:{CAMPAIGN}:rearm1"]
