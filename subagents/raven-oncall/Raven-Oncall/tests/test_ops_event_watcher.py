"""Tests for OpsEventWatcher: job completion / unhealthy progress -> early wake."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import raven.ops as ops_mod
from raven.ops.event_watcher import OpsEventWatcher, _unhealthy
from raven.ops.instrument import read_events


class _FakeCron:
    def __init__(self, jobs=None) -> None:
        self.jobs = jobs or []
        self.advanced: list[str] = []

    def list_jobs(self):
        return self.jobs

    def advance_job_to_now(self, job_id: str) -> bool:
        self.advanced.append(job_id)
        return True


def _cron_job(job_id: str, name: str, next_run: int = 9_999_999_999_999):
    return SimpleNamespace(id=job_id, name=name, state=SimpleNamespace(next_run_at_ms=next_run))


def _campaign(tmp_path: Path, name: str, records: dict, concluded: bool = False) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "ledger.json").write_text(json.dumps({"version": 1, "records": records}), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        {"host": "h", "port": 22, "key": "~/.ssh/id_rsa", "remote_dir": "/root/raven-ops", "image": "img"}
    ), encoding="utf-8")
    if concluded:
        (d / "concluded.json").write_text("{}", encoding="utf-8")
    return d


def _running_record(key: str, campaign: str) -> dict:
    return {"idem_key": key, "status": "running", "campaign": campaign,
            "handle": {"backend": "docker", "job_id": f"ops-{key}"},
            "result": None, "attempts": 1, "escalated": False}


def _patch_runner(monkeypatch, behavior):
    """behavior(cmd) -> (rc, out); installed as the ssh runner for any host.

    Patched on ``raven.ops`` rather than on the watcher, because the watcher now
    resolves its backend through ``backend_from_meta``; the docker factory
    imports the runner from ``raven.ops`` inside the call for exactly this.
    """
    monkeypatch.setattr(ops_mod, "make_ssh_runner", lambda *a, **k: behavior)


async def test_completion_advances_the_pending_wake(monkeypatch, tmp_path: Path) -> None:
    d = _campaign(tmp_path, "bm25-x", {"t1": _running_record("t1", "bm25_x")})

    def runner(cmd: str):
        if cmd.startswith("docker inspect"):
            return 0, "exited 0"
        if cmd.startswith("cat ") and "result.json" in cmd:
            return 0, json.dumps({"metrics": {"ndcg": 0.31}, "config": {"k1": 2}})
        return 0, ""

    _patch_runner(monkeypatch, runner)
    cron = _FakeCron([_cron_job("w1", "ops:bm25_x:r1")])

    advanced = await OpsEventWatcher(cron, ops_home=tmp_path, poll_interval=0).tick()

    assert advanced == ["bm25_x"]
    assert cron.advanced == ["w1"]  # the wake was pulled to now
    # the reconcile persisted: the wake turn will read a terminal ledger
    data = json.loads((d / "ledger.json").read_text())
    assert data["records"]["t1"]["status"] == "succeeded"
    kinds = [e["kind"] for e in read_events(d)]
    assert "trial_terminal_observed" in kinds and "event_wake_advanced" in kinds


async def test_unhealthy_progress_advances_even_mid_round(monkeypatch, tmp_path: Path) -> None:
    _campaign(tmp_path, "bm25-y", {"t1": _running_record("t1", "bm25_y")})

    def runner(cmd: str):
        if cmd.startswith("docker inspect"):
            return 0, "running 0"
        if cmd.startswith("tail ") and "progress.jsonl" in cmd:
            return 0, json.dumps({"step": 7, "loss": float("nan")})
        return 0, ""

    _patch_runner(monkeypatch, runner)
    cron = _FakeCron([_cron_job("w2", "ops:bm25_y:r1")])

    advanced = await OpsEventWatcher(cron, ops_home=tmp_path, poll_interval=0).tick()

    assert advanced == ["bm25_y"]  # still running, but diverged -> wake now


async def test_still_running_and_healthy_does_nothing(monkeypatch, tmp_path: Path) -> None:
    _campaign(tmp_path, "bm25-z", {"t1": _running_record("t1", "bm25_z")})

    def runner(cmd: str):
        if cmd.startswith("docker inspect"):
            return 0, "running 0"
        if cmd.startswith("tail "):
            return 0, json.dumps({"step": 7, "loss": 1.5})
        return 0, ""

    _patch_runner(monkeypatch, runner)
    cron = _FakeCron([_cron_job("w3", "ops:bm25_z:r1")])

    assert await OpsEventWatcher(cron, ops_home=tmp_path, poll_interval=0).tick() == []
    assert cron.advanced == []


async def test_concluded_and_unreachable_campaigns_are_skipped(monkeypatch, tmp_path: Path) -> None:
    _campaign(tmp_path, "done-c", {"t1": _running_record("t1", "done_c")}, concluded=True)
    _campaign(tmp_path, "dead-host", {"t1": _running_record("t1", "dead_host")})

    def runner(cmd: str):
        raise OSError("host unreachable")

    _patch_runner(monkeypatch, runner)
    cron = _FakeCron([_cron_job("w4", "ops:dead_host:r1")])

    # neither crashes the tick; nothing advanced
    assert await OpsEventWatcher(cron, ops_home=tmp_path, poll_interval=0).tick() == []


def test_unhealthy_detects_nonfinite_only() -> None:
    assert _unhealthy({"step": 3, "loss": float("nan")})
    assert _unhealthy({"residual": float("inf")})
    assert not _unhealthy({"step": 3, "loss": 2.5})


# ---- the backend named in meta.json is the one that gets polled --------------
# The watcher used to construct a DockerExecutor unconditionally. A real
# campaign ran on the bare-process backend, so its jobs were polled as if they
# were containers, nothing was ever observed, and the per-campaign except
# swallowed it -- so a finished job went unnoticed for 29m59s and the only thing
# setting detection latency was the agent's own ETA guess.


class _RecordingBackend:
    name = "recording"

    def __init__(self) -> None:
        self.polled: list[str] = []

    async def poll(self, handle):
        from raven.ops.backend import JobStatus

        self.polled.append(handle.job_id)
        return JobStatus.SUCCEEDED

    async def fetch_result(self, handle):
        from raven.ops.backend import JobResult, JobStatus

        return JobResult(status=JobStatus.SUCCEEDED, metrics={}, output="", error=None)


async def test_the_backend_named_in_meta_is_the_one_polled(monkeypatch, tmp_path: Path) -> None:
    from raven.ops import backends as backends_mod

    recorder = _RecordingBackend()
    monkeypatch.setitem(backends_mod._FACTORIES, "recording", lambda meta: recorder)

    d = _campaign(tmp_path, "c1", {"t1": _running_record("t1", "c1")})
    meta = json.loads((d / "meta.json").read_text())
    meta["backend"] = "recording"
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    cron = _FakeCron([_cron_job("j1", "ops:c1:recheck")])
    advanced = await OpsEventWatcher(cron, ops_home=tmp_path).tick()

    assert recorder.polled == ["ops-t1"]
    assert advanced == ["c1"]
    assert cron.advanced == ["j1"]


async def test_an_unresolvable_backend_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A campaign naming a backend nobody registered fails identically to a quiet
    campaign, so the first occurrence has to reach the log at warning level."""
    d = _campaign(tmp_path, "c1", {"t1": _running_record("t1", "c1")})
    meta = json.loads((d / "meta.json").read_text())
    meta["backend"] = "no-such-backend"
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    from loguru import logger

    seen: list[str] = []
    sink_id = logger.add(lambda m: seen.append(m.record["level"].name + ":" + m.record["message"]), level="DEBUG")
    try:
        watcher = OpsEventWatcher(_FakeCron(), ops_home=tmp_path)
        assert await watcher.tick() == []
        assert any(s.startswith("WARNING") and "c1" in s for s in seen)
        seen.clear()
        assert await watcher.tick() == []
        assert all(not s.startswith("WARNING") for s in seen)
    finally:
        logger.remove(sink_id)
