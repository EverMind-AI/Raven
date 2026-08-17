"""Tests for DockerExecutor via a fake docker runner (no real host).

The fake simulates a host where ``docker run`` completes a job immediately,
writing a result whose metric is derived from the trial config. This exercises
the executor's command construction, idempotency (one container per idem key),
status parsing, and result reading, and confirms a Campaign drives it end to end.
"""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

from raven.ops import (
    Campaign,
    DockerExecutor,
    JobSpec,
    JobStatus,
    Ledger,
    Trial,
)


class FakeDocker:
    """In-memory docker: `docker run` finishes a job at once with metric=f(cfg)."""

    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self.run_calls = 0

    def __call__(self, cmd: str) -> tuple[int, str]:
        tokens = shlex.split(cmd)
        if cmd.startswith("docker ps -aq -f name="):
            name = cmd.split("name=^", 1)[1].rstrip("$")
            return 0, ("cid-" + name if name in self.containers else "")
        if "docker" in tokens and "run" in tokens:
            name = tokens[tokens.index("--name") + 1]
            b64 = next(t.split("=", 1)[1] for t in tokens if t.startswith("OPS_CONFIG_B64="))
            cfg = json.loads(base64.b64decode(b64).decode())
            metric = round(1.0 - abs(cfg["k1"] - 1.5) - abs(cfg["b"] - 0.75), 4)
            self.containers[name] = {"state": "exited", "code": 0}
            self.results[name] = json.dumps({"metrics": {"ndcg": metric}, "config": cfg})
            self.run_calls += 1
            return 0, "cid-" + name
        if cmd.startswith("docker inspect"):
            name = tokens[-1]
            c = self.containers.get(name)
            return (0, f"{c['state']} {c['code']}") if c else (1, "no such container")
        if cmd.startswith("cat "):
            key = tokens[-1].split("/jobs/", 1)[1].rsplit("/", 1)[0]
            name = "ops-" + key
            return (0, self.results[name]) if name in self.results else (1, "No such file")
        if cmd.startswith("test -f"):
            key = tokens[-1].split("/jobs/", 1)[1].rsplit("/", 1)[0]
            return (0, "") if ("ops-" + key) in self.results else (1, "")
        if cmd.startswith("docker logs"):
            return 0, ""
        return 0, ""


def _executor(fake: FakeDocker) -> DockerExecutor:
    return DockerExecutor(fake, image="python:3.12-slim", remote_dir="/root/raven-ops")


async def test_submit_run_poll_fetch(tmp_path: Path) -> None:
    fake = FakeDocker()
    ex = _executor(fake)
    handle = await ex.submit(JobSpec({"k1": 1.5, "b": 0.75}, idem_key="t1"))

    assert handle.job_id == "ops-t1"
    assert await ex.poll(handle) is JobStatus.SUCCEEDED
    result = await ex.fetch_result(handle)
    assert result.status is JobStatus.SUCCEEDED
    assert result.metrics["ndcg"] == 1.0


async def test_submit_is_idempotent_on_container_name(tmp_path: Path) -> None:
    fake = FakeDocker()
    ex = _executor(fake)
    await ex.submit(JobSpec({"k1": 1.0, "b": 0.5}, idem_key="t1"))
    await ex.submit(JobSpec({"k1": 2.0, "b": 0.9}, idem_key="t1"))

    assert fake.run_calls == 1


async def test_poll_maps_container_states() -> None:
    fake = FakeDocker()
    ex = _executor(fake)
    fake.containers["ops-x"] = {"state": "running", "code": 0}
    assert await ex.poll(await _handle(ex, "ops-x")) is JobStatus.RUNNING
    fake.containers["ops-x"] = {"state": "created", "code": 0}
    assert await ex.poll(await _handle(ex, "ops-x")) is JobStatus.PENDING
    fake.containers["ops-x"] = {"state": "exited", "code": 1}
    assert await ex.poll(await _handle(ex, "ops-x")) is JobStatus.FAILED


async def _handle(ex: DockerExecutor, cname: str):
    from raven.ops import JobHandle

    return JobHandle(ex.name, cname)


async def test_poll_falls_back_to_result_when_container_reaped(tmp_path: Path) -> None:
    fake = FakeDocker()
    ex = _executor(fake)
    handle = await ex.submit(JobSpec({"k1": 1.5, "b": 0.75}, idem_key="t1"))
    del fake.containers["ops-t1"]  # host auto-removed the exited container; result remains

    assert await ex.poll(handle) is JobStatus.SUCCEEDED
    assert (await ex.fetch_result(handle)).status is JobStatus.SUCCEEDED
    assert await ex.poll(await _handle(ex, "ops-never-ran")) is JobStatus.FAILED


async def test_campaign_drives_docker_backend(tmp_path: Path) -> None:
    fake = FakeDocker()
    ex = _executor(fake)
    trials = [
        Trial("g1", {"k1": 0.8, "b": 0.5}),
        Trial("g2", {"k1": 1.5, "b": 0.75}),
        Trial("g3", {"k1": 2.2, "b": 0.9}),
    ]
    campaign = Campaign("bm25", trials, ex, Ledger(tmp_path / "l.json"), metric="ndcg", goal="max")

    await campaign.run()

    assert campaign.is_done()
    assert campaign.best().idem_key == "g2"
    assert fake.run_calls == 3


async def test_fetch_progress_tails_the_progress_file() -> None:
    lines = '{"step": 1, "loss": 2.0}\n{"step": 2, "loss": 1.5}\nnot-json\n'

    def runner(cmd: str):
        if cmd.startswith("tail ") and "progress.jsonl" in cmd:
            return 0, lines
        return 1, ""

    ex = DockerExecutor(runner, image="img", remote_dir="/root/raven-ops")
    from raven.ops import JobHandle

    samples = await ex.fetch_progress(JobHandle("docker", "ops-t1"), tail=5)
    assert samples == [{"step": 1, "loss": 2.0}, {"step": 2, "loss": 1.5}]  # bad line skipped

    def no_file(cmd: str):
        return 1, "No such file"

    ex2 = DockerExecutor(no_file, image="img", remote_dir="/root/raven-ops")
    assert await ex2.fetch_progress(JobHandle("docker", "ops-t1")) == []  # no contract -> empty, not error
