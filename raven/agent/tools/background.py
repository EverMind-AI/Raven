"""Detached background jobs: processes that outlive both the call and the agent.

Separate from :class:`~raven.agent.tools.shell.ExecSessionRegistry` on purpose. A
*session* is a stateful interactive shell — it holds a PTY so a debugger or an ssh
password prompt can be driven turn by turn, and it must die with the agent that owns
it. A *job* is the opposite contract: the agent starts a server or a long build and
the process is expected to still be there afterwards, whether that is three tool calls
later or after the agent has exited entirely.

Running a job inside a session conflates the two, and the PTY is what breaks it:
closing the master fd hangs up the terminal, so the kernel sends SIGHUP to the
foreground process group and the "background" server dies with the shell. Jobs
therefore get no terminal at all — stdin is /dev/null, output goes to a log file, and
the process is its own session leader.

Nothing kills a job implicitly. It stops when it exits, when the agent cancels it, or
when the operator reaps it from the registry. The registry is on disk so that a job
outliving its agent stays visible and killable instead of becoming an orphan nobody
can find — which is the failure mode that made the blunt "close everything on exit"
teardown look attractive in the first place.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.tools.base import Tool
from raven.sandbox import SandboxExecutor

# SIGTERM first, then SIGKILL if the job ignores it. Mirrors the grace period a
# supervisor gives a service: enough for a clean shutdown, short enough that a wedged
# process cannot block the caller.
_KILL_GRACE_S = 3.0

_MAX_LOG_CHARS = 30_000


def _jobs_root() -> Path:
    """Where job logs and the registry live.

    Deliberately under the raven home rather than the workspace: the workspace is
    often the thing being graded or diffed, and job logs are scaffolding, not work
    product.
    """
    return Path(os.path.expanduser("~/.raven/jobs"))


@dataclass
class BackgroundJob:
    """A detached process plus the files that make it observable after a restart."""

    name: str
    command: str
    pid: int
    cwd: str
    log_path: str
    status_path: str
    started_at: float
    owner: str = ""
    # Byte offset already returned to the caller, so polling reads only what is new.
    read_offset: int = field(default=0, compare=False)

    def to_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "pid": self.pid,
            "cwd": self.cwd,
            "log_path": self.log_path,
            "status_path": self.status_path,
            "started_at": self.started_at,
            "owner": self.owner,
        }

    @property
    def exit_code(self) -> int | None:
        """Exit status once the job has finished, else None.

        Read from the status file rather than from a live process handle: the whole
        point of a job is that it can outlive the process that started it, so the
        answer has to survive an agent restart.
        """
        try:
            raw = Path(self.status_path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @property
    def running(self) -> bool:
        if self.exit_code is not None:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            # Gone without writing a status file — killed by a signal, or the machine
            # restarted underneath it. Either way it is not running.
            return False
        except PermissionError:
            # Alive, owned by somebody else. Still running as far as we can tell.
            return True
        return True

    def describe(self) -> str:
        if self.running:
            state = f"running (pid {self.pid})"
        else:
            code = self.exit_code
            state = f"exited (code {code})" if code is not None else "gone (no exit status)"
        age = int(time.time() - self.started_at)
        return f"{self.name}: {state}, {age}s old, log {self.log_path}\n  $ {self.command}"


class BackgroundJobRegistry:
    """Owns the jobs one agent started, and persists them for whoever comes after.

    ``close_on_exit`` is intentionally absent: an agent shutting down never cancels
    jobs. A server the task was asked to leave running must still be listening when
    something else — a grader, the next agent turn, a human — goes looking for it.
    """

    MAX_JOBS = 16

    def __init__(self, executor: SandboxExecutor, owner: str = "", root: Path | None = None):
        self._executor = executor
        self._owner = owner or f"pid{os.getpid()}"
        self._root = root or _jobs_root()
        self._jobs: dict[str, BackgroundJob] = {}
        self._seq = 0

    @property
    def supported(self) -> bool:
        return self._executor.supports_background_jobs

    def names(self) -> list[str]:
        return sorted(self._jobs)

    def get(self, name: str) -> BackgroundJob | None:
        return self._jobs.get(name)

    def next_name(self) -> str:
        self._seq += 1
        return f"job{self._seq}"

    async def start(
        self,
        name: str,
        command: str,
        cwd: str,
        env: dict[str, str] | None,
    ) -> BackgroundJob:
        existing = self._jobs.get(name)
        if existing is not None and existing.running:
            raise RuntimeError(f"job {name!r} is already running (pid {existing.pid}); cancel it first")
        if len([j for j in self._jobs.values() if j.running]) >= self.MAX_JOBS:
            raise RuntimeError(f"too many running jobs ({self.MAX_JOBS}); cancel one first: {', '.join(self.names())}")

        self._root.mkdir(parents=True, exist_ok=True)
        stem = f"{self._owner}-{name}"
        log_path = self._root / f"{stem}.log"
        status_path = self._root / f"{stem}.status"
        # A stale status file from a previous job of the same name would make the new
        # one look finished the moment it starts.
        status_path.unlink(missing_ok=True)
        log_path.write_text("", encoding="utf-8")

        pid = await self._executor.start_background(
            command,
            cwd=cwd,
            env=env,
            log_path=str(log_path),
            status_path=str(status_path),
        )
        job = BackgroundJob(
            name=name,
            command=command,
            pid=pid,
            cwd=cwd,
            log_path=str(log_path),
            status_path=str(status_path),
            started_at=time.time(),
            owner=self._owner,
        )
        self._jobs[name] = job
        self._persist()
        return job

    def read(self, name: str) -> str:
        """Return log output produced since the previous read of this job."""
        job = self._jobs.get(name)
        if job is None:
            return f"Error: no background job named {name!r}. Running: {', '.join(self.names()) or 'none'}"
        try:
            with open(job.log_path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(job.read_offset)
                chunk = handle.read()
                job.read_offset = handle.tell()
        except OSError as exc:
            return f"Error reading log for job {name!r}: {exc}"
        if len(chunk) > _MAX_LOG_CHARS:
            half = _MAX_LOG_CHARS // 2
            chunk = (
                chunk[:half]
                + f"\n\n... ({len(chunk) - _MAX_LOG_CHARS:,} chars omitted; "
                + f"full log at {job.log_path}, grep/read_file it) ...\n\n"
                + chunk[-half:]
            )
        body = chunk or "(no new output)"
        if job.running:
            return f"{body}\n[job {name} still running - poll again for more]"
        code = job.exit_code
        return f"{body}\n[job {name} finished, exit code: {code if code is not None else 'unknown'}]"

    async def _settle(self, job: BackgroundJob, timeout: float = 2.0) -> None:
        """Wait for the exit status to land after the process disappears.

        The shell writes it from an EXIT trap, so there is a short window where the
        process is already gone but the outcome is not yet readable. Returning inside
        that window would report a finished job as having no exit status at all.
        """
        deadline = time.monotonic() + timeout
        while job.exit_code is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def wait(self, name: str, timeout: float) -> BackgroundJob | str:
        job = self._jobs.get(name)
        if job is None:
            return f"Error: no background job named {name!r}. Running: {', '.join(self.names()) or 'none'}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not job.running:
                await self._settle(job)
                return job
            await asyncio.sleep(0.25)
        return job

    async def cancel(self, name: str) -> str:
        job = self._jobs.get(name)
        if job is None:
            return f"Error: no background job named {name!r}. Running: {', '.join(self.names()) or 'none'}"
        if not job.running:
            return f"[job {name} was already finished, exit code: {job.exit_code}]"
        await self._executor.signal_background(job.pid, signal.SIGTERM)
        deadline = time.monotonic() + _KILL_GRACE_S
        while time.monotonic() < deadline:
            if not job.running:
                await self._settle(job)
                self._record_signal_exit(job, signal.SIGTERM)
                self._persist()
                return f"[job {name} terminated]"
            await asyncio.sleep(0.1)
        await self._executor.signal_background(job.pid, signal.SIGKILL)
        await self._settle(job)
        self._record_signal_exit(job, signal.SIGKILL)
        self._persist()
        return f"[job {name} killed after {_KILL_GRACE_S:g}s grace period]"

    def _record_signal_exit(self, job: BackgroundJob, sig: int) -> None:
        """Record 128+signo when the job died before its own trap could run.

        A job cancelled within milliseconds of starting can be signalled before the
        shell has installed the EXIT trap, leaving no status behind. The signal we
        sent is what killed it, so the conventional encoding of it is the honest
        answer — better than reporting a cancelled job as having no outcome.
        """
        if job.exit_code is not None:
            return
        try:
            Path(job.status_path).write_text(str(128 + int(sig)), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not record exit status for job {}: {}", job.name, exc)

    def survivors(self) -> list[BackgroundJob]:
        return [job for job in self._jobs.values() if job.running]

    def report_survivors(self) -> None:
        """Log jobs still running at shutdown.

        Leaving a job running is the correct default, but doing it silently turns
        every forgotten server into an orphan. Naming them — with pid and log path —
        is what keeps "outlives the agent" from meaning "unreclaimable".
        """
        alive = self.survivors()
        if not alive:
            return
        logger.info(
            "{} background job(s) still running after shutdown; reap with `raven jobs` or kill the pid:\n{}",
            len(alive),
            "\n".join(f"  {job.name}  pid {job.pid}  {job.log_path}  $ {job.command}" for job in alive),
        )

    def _persist(self) -> None:
        """Write the registry so a later process can find and reap these jobs."""
        path = self._root / "registry.json"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            records: dict[str, Any] = {}
            if path.exists():
                try:
                    records = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    records = {}
            if not isinstance(records, dict):
                records = {}
            records[self._owner] = [job.to_record() for job in self._jobs.values()]
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(records, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            # The registry is a convenience for reaping, not a correctness
            # requirement; a read-only home must not break starting a job.
            logger.warning("Could not persist background job registry: {}", exc)


class _JobTool(Tool):
    """Shared plumbing for the job tools."""

    def __init__(self, jobs: BackgroundJobRegistry):
        self.jobs = jobs


class JobStatusTool(_JobTool):
    """Tool to list background jobs and read their new output."""

    @property
    def name(self) -> str:
        return "job_status"

    @property
    def description(self) -> str:
        return (
            "Report background jobs started with exec(background=true). With no name, "
            "lists every job and whether it is still running. With a name, also returns "
            "the log output produced since your last check — poll this to follow a build "
            "or watch a server's log."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Job name. Omit to list all jobs.",
                },
            },
        }

    async def execute(self, name: str | None = None, **kwargs: Any) -> str:
        if name:
            job = self.jobs.get(name)
            if job is None:
                return f"Error: no background job named {name!r}. Known: {', '.join(self.jobs.names()) or 'none'}"
            return job.describe() + "\n\n" + self.jobs.read(name)
        names = self.jobs.names()
        if not names:
            return "No background jobs."
        return "\n".join(self.jobs.get(n).describe() for n in names if self.jobs.get(n))


class JobWaitTool(_JobTool):
    """Tool to block until a background job finishes."""

    @property
    def name(self) -> str:
        return "job_wait"

    @property
    def description(self) -> str:
        return (
            "Wait for a background job to finish and return its final output. Use for a "
            "long build you now need the result of. Returns as soon as the job ends, or "
            "reports that it is still running when the timeout elapses. Never use it on "
            "a server you intend to leave running — it would just burn the timeout."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Job name to wait for"},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait (default 300)",
                    "minimum": 1,
                    "maximum": 3600,
                },
            },
            "required": ["name"],
        }

    async def execute(self, name: str, timeout: int = 300, **kwargs: Any) -> str:
        result = await self.jobs.wait(name, timeout=float(timeout))
        if isinstance(result, str):
            return result
        return result.describe() + "\n\n" + self.jobs.read(name)


class JobCancelTool(_JobTool):
    """Tool to stop a background job."""

    @property
    def name(self) -> str:
        return "job_cancel"

    @property
    def description(self) -> str:
        return (
            "Stop a background job (SIGTERM, then SIGKILL after a grace period). Jobs "
            "keep running after you finish otherwise, so cancel the ones that were only "
            "scaffolding — but leave any service the task asked you to have running."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Job name to cancel"}},
            "required": ["name"],
        }

    async def execute(self, name: str, **kwargs: Any) -> str:
        return await self.jobs.cancel(name)
