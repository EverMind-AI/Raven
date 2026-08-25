"""DockerExecutor: run each trial as a detached container on a remote host.

A JobBackend that launches one Docker container per job, named by the job's
idempotency key (which is what makes ``submit`` idempotent), on a machine
reached through an injected command runner. The trial config is passed as a
base64 env var and the result is read from a bind-mounted per-job directory, so
only the container touches the host. The runner is injected so command
construction and output parsing are unit-testable without a real host; the real
runner (``make_ssh_runner``) shells out over SSH.

Detection latency is measured here rather than estimated. The instant a job
became terminal is unobservable to a loop that was not looking, but the host
knows it (``State.FinishedAt``), so the gap between that instant and the loop's
first terminal poll is ground truth on real hardware too.

One detail decides whether that number means anything: ``FinishedAt`` is on the
host clock and the loop runs somewhere else, so a skew of seconds between the two
machines would silently corrupt every latency. The host's own current time is
therefore read in the same command as the inspect, and the subtraction happens
entirely in host time. Nothing here assumes the clocks agree.

A container that was reaped before the first terminal poll has no ``FinishedAt``
left to read. Those are counted rather than defaulted to zero, because a silent
zero would read as perfect detection.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from datetime import datetime

from raven.ops.backend import (
    JobBackend,
    JobBackendError,
    JobHandle,
    JobResult,
    JobSpec,
    JobStatus,
)

CommandRunner = Callable[[str], tuple[int, str]]


class DockerExecutor(JobBackend):
    name = "docker"

    def __init__(
        self,
        run: CommandRunner,
        *,
        image: str,
        remote_dir: str,
        app_dir: str | None = None,
        prefix: str = "ops-",
    ) -> None:
        self._run = run
        self._image = image
        self._remote_dir = remote_dir.rstrip("/")
        self._app_dir = app_dir or f"{self._remote_dir}/app"
        self._prefix = prefix
        self._polls: dict[str, int] = {}
        self._latency_ms: dict[str, int] = {}
        self._finish_unknown: dict[str, str] = {}

    def _cname(self, idem_key: str) -> str:
        return f"{self._prefix}{idem_key}"

    def _job_dir(self, idem_key: str) -> str:
        return f"{self._remote_dir}/jobs/{idem_key}"

    async def _arun(self, cmd: str) -> tuple[int, str]:
        return await asyncio.to_thread(self._run, cmd)

    async def submit(self, spec: JobSpec) -> JobHandle:
        cname = self._cname(spec.idem_key)
        rc, out = await self._arun(f"docker ps -aq -f name=^{cname}$")
        if rc == 0 and out.strip():
            return JobHandle(self.name, cname)
        job_dir = self._job_dir(spec.idem_key)
        cfg = base64.b64encode(json.dumps(spec.payload).encode()).decode()
        rc, out = await self._arun(
            f"mkdir -p {job_dir} && chmod 777 {job_dir} && docker run -d --name {cname} "
            f"-e OPS_CONFIG_B64={cfg} "
            f"-v {self._app_dir}:/app:ro -v {job_dir}:/job "
            f"{self._image} python3 /app/trial.py"
        )
        if rc != 0:
            raise JobBackendError(f"docker run failed for {cname}: {out.strip()[:300]}")
        return JobHandle(self.name, cname)

    async def poll(self, handle: JobHandle) -> JobStatus:
        idem = self._idem(handle)
        self._polls[idem] = self._polls.get(idem, 0) + 1
        status = await self._poll_status(handle)
        if status.is_terminal and idem not in self._latency_ms and idem not in self._finish_unknown:
            await self._stamp_terminal(handle, idem)
        return status

    async def _poll_status(self, handle: JobHandle) -> JobStatus:
        rc, out = await self._arun(f"docker inspect -f '{{{{.State.Status}}}} {{{{.State.ExitCode}}}}' {handle.job_id}")
        if rc != 0:
            # Some hosts auto-remove exited containers, so a gone container means
            # the job finished and was reaped -- decide from the durable result
            # file in the bind-mounted job dir rather than treating it as an error.
            idem_key = handle.job_id[len(self._prefix) :]
            probe, _ = await self._arun(f"test -f {self._job_dir(idem_key)}/result.json")
            return JobStatus.SUCCEEDED if probe == 0 else JobStatus.FAILED
        parts = out.split()
        state = parts[0] if parts else ""
        code = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
        if state == "created":
            return JobStatus.PENDING
        if state == "running":
            return JobStatus.RUNNING
        if state == "exited":
            return JobStatus.SUCCEEDED if code == 0 else JobStatus.FAILED
        return JobStatus.FAILED

    async def _stamp_terminal(self, handle: JobHandle, idem: str) -> None:
        """Record how late the loop was, measured only in host time.

        Both values come from one command so they share a clock and a round trip.
        ``date`` runs first: taking the host's now *before* the inspect can only
        understate the gap, and understating our own lateness is the safe
        direction for a number we report about ourselves.
        """
        rc, out = await self._arun(
            f"date -u +%s.%N; docker inspect -f '{{{{.State.FinishedAt}}}}' {handle.job_id}"
        )
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if rc != 0 or len(lines) < 2:
            self._finish_unknown[idem] = "container gone before the first terminal poll"
            return
        try:
            host_now = float(lines[0])
        except ValueError:
            self._finish_unknown[idem] = f"unreadable host time {lines[0]!r}"
            return
        finished = _parse_docker_time(lines[-1])
        if finished is None:
            self._finish_unknown[idem] = f"unreadable FinishedAt {lines[-1]!r}"
            return
        self._latency_ms[idem] = max(0, int(round((host_now - finished) * 1000)))

    # ---- measurement surface (evals read these; orchestration never does) ----

    def detection_latency_ms(self, idem_key: str) -> int | None:
        """Host-time gap between the job becoming terminal and the loop first
        seeing it. None when the container was reaped before we looked."""
        return self._latency_ms.get(idem_key)

    def poll_count(self, idem_key: str | None = None) -> int:
        if idem_key is None:
            return sum(self._polls.values())
        return self._polls.get(idem_key, 0)

    def unknown_finish_times(self) -> dict[str, str]:
        """Jobs whose finish instant could not be read, and why.

        Reported rather than defaulted: a missing latency silently treated as
        zero would read as instant detection.
        """
        return dict(self._finish_unknown)

    def _idem(self, handle: JobHandle) -> str:
        return handle.job_id[len(self._prefix) :]

    async def fetch_result(self, handle: JobHandle) -> JobResult:
        idem_key = handle.job_id[len(self._prefix) :]
        rc, out = await self._arun(f"cat {self._job_dir(idem_key)}/result.json")
        if rc == 0:
            try:
                data = json.loads(out)
            except json.JSONDecodeError as exc:
                raise JobBackendError(f"bad result.json for {handle.job_id}: {exc}") from None
            metrics = {k: float(v) for k, v in data.get("metrics", {}).items()}
            return JobResult(JobStatus.SUCCEEDED, metrics=metrics, output=data)
        _, logs = await self._arun(f"docker logs --tail 30 {handle.job_id}")
        return JobResult(JobStatus.FAILED, error=(logs.strip()[:500] or "no result.json and no logs"))

    async def cancel(self, handle: JobHandle) -> None:
        await self._arun(f"docker rm -f {handle.job_id}")

    async def fetch_progress(self, handle: JobHandle, tail: int = 5) -> list[dict]:
        """Tail the job's progress file (JSON lines the trial appends as it runs).
        Missing file -- trial has no progress contract or none written yet -- is
        normal and returns []."""
        idem_key = handle.job_id[len(self._prefix) :]
        rc, out = await self._arun(f"tail -n {int(tail)} {self._job_dir(idem_key)}/progress.jsonl")
        if rc != 0:
            return []
        samples = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return samples


def _parse_docker_time(value: str) -> float | None:
    """Docker's RFC3339 with nanoseconds, as a POSIX timestamp.

    ``fromisoformat`` rejects nine fractional digits, so the fraction is trimmed
    to microseconds. A container that never ran reports the zero time, which is
    not a finish instant and must not be treated as one.
    """
    text = (value or "").strip().strip('"').replace("Z", "+00:00")
    if text.startswith("0001-01-01"):
        return None
    if "." in text:
        head, rest = text.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())[:6]
        offset = rest[len(digits):] if not rest[len(digits):].isdigit() else ""
        for marker in ("+", "-"):
            if marker in rest:
                offset = rest[rest.index(marker):]
                break
        text = f"{head}.{digits or '0'}{offset}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def make_ssh_runner(
    host: str,
    port: int,
    key: str,
    *,
    user: str = "root",
    connect_timeout: int = 15,
) -> CommandRunner:
    import subprocess

    def run(cmd: str) -> tuple[int, str]:
        argv = [
            "ssh", "-i", key, "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{user}@{host}", cmd,
        ]
        proc = subprocess.run(argv, capture_output=True, text=True)
        tail = f"\n{proc.stderr}" if proc.stderr and proc.returncode != 0 else ""
        return proc.returncode, proc.stdout + tail

    return run
