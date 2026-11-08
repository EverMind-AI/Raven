"""Trajectory exports run as background jobs whose result is fetched by download token.

A page asks for an export and polls for it: :meth:`ExportJobs.start` takes a
validated :class:`~raven.trajectory.selection.SelectionPlan`, registers a job
and returns at once, and a worker thread of the job's own collects the
selection, cuts the session record and writes the package under the cache
directory. A finished job carries a random token; the download route hands
the file out by that token alone, so no path ever travels to the page.

Every method but :meth:`ExportJobs.shutdown` and
:meth:`ExportJobs.sweep_stale_files` is safe to call from the event loop: the
registry lock guards in-memory state only, and anything touching the disk --
collecting, writing, deleting -- happens on a thread the registry started.

Threads are bounded two ways. At most ``limit`` export workers run, counted
by live thread rather than by job state: a job cancelled while its worker is
still busy keeps that slot until the worker has removed its partial file and
exited, so cancelling and starting again cannot pile workers up. A finished
package that has to go (cancelled, expired, shut down) is queued for the one
cleaning thread, which drains the queue and exits; however many packages are
retired at once, deletion never takes more than that one thread, and the
queue keeps every pending deletion until it is done.

A package lives ``ttl`` seconds after its job started. A failed or cancelled
job's record stays readable for ``keep_finished`` seconds so the page can see
how it ended; a finished job's record lasts as long as its package. Both are
swept lazily on every call. At startup, packages older than ``ttl`` left by an
earlier process are removed, and only those: another gateway on the same
machine may be serving the rest.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from raven.trajectory.export import ExportCancelledError, ExportSummary, new_export_id, write_export
from raven.trajectory.selection import SelectionPlan, collect_selection, cut_session_record

DOWNLOAD_PATH = "/trajectory/export/download"
EXPORTS_DIR = "trajectory-exports"
THREAD_PREFIX = "trajectory-export"

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
PHASE_COLLECTING = "collecting"
PHASE_WRITING = "writing"
PHASE_DONE = "done"

_ERROR_LIMIT = 300


class JobNotFoundError(LookupError):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"no export job {job_id!r}")


class JobLimitError(RuntimeError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"{limit} exports are already running")


def _default_root() -> Path:
    from raven.config.paths import get_cache_dir

    return get_cache_dir() / EXPORTS_DIR


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("trajectory export: could not remove {}", path)


@dataclass
class _Job:
    job_id: str
    token: str
    export_id: str
    file_name: str
    created: float
    state: str = STATE_RUNNING
    phase: str = PHASE_COLLECTING
    error: str | None = None
    summary: ExportSummary | None = None
    finished: float | None = None
    path: Path | None = None
    removed: bool = False
    worker_alive: bool = False
    stop: threading.Event = field(default_factory=threading.Event)


class ExportJobs:
    def __init__(
        self,
        root: Callable[[], Path] | None = None,
        *,
        limit: int = 2,
        ttl: float = 3600.0,
        keep_finished: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root_factory = root or _default_root
        self.limit = limit
        self.ttl = ttl
        self.keep_finished = keep_finished
        self._clock = clock
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._jobs: dict[str, _Job] = {}
        self._tokens: dict[str, str] = {}
        self._workers = 0
        self._doomed: deque[Path] = deque()
        self._cleaner_alive = False

    # -- the cleaning thread ----------------------------------------------

    def _doom_locked(self, job: _Job) -> bool:
        """Queue a retired job's package for removal; True when the cleaner must be started."""
        if job.path is None or job.removed or job.worker_alive:
            return False
        job.removed = True
        self._doomed.append(job.path)
        if self._cleaner_alive:
            return False
        self._cleaner_alive = True
        return True

    def _clean(self) -> None:
        while True:
            with self._lock:
                if not self._doomed:
                    self._cleaner_alive = False
                    self._idle.notify_all()
                    return
                path = self._doomed.popleft()
            _remove(path)

    def _start_cleaner(self) -> None:
        thread = threading.Thread(target=self._clean, name=f"{THREAD_PREFIX}-clean", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                self._cleaner_alive = False
                self._idle.notify_all()
            logger.warning("trajectory export: no thread to remove retired packages; the startup sweep will")

    # -- bookkeeping under the lock ---------------------------------------

    def _retire_locked(self, job: _Job) -> bool:
        """Cancel ``job`` in memory; True when the cleaner must be started."""
        if job.state not in (STATE_RUNNING, STATE_DONE):
            return False
        self._tokens.pop(job.token, None)
        job.stop.set()
        job.state, job.finished = STATE_CANCELLED, self._clock()
        return self._doom_locked(job)

    def _sweep_locked(self) -> bool:
        now = self._clock()
        wake = False
        for job_id, job in list(self._jobs.items()):
            if job.state == STATE_DONE and job.created + self.ttl <= now:
                wake = self._retire_locked(job) or wake
            elif (
                job.state in (STATE_FAILED, STATE_CANCELLED)
                and not job.worker_alive
                and job.finished is not None
                and job.finished + self.keep_finished <= now
            ):
                self._tokens.pop(job.token, None)
                del self._jobs[job_id]
        return wake

    # -- the worker -------------------------------------------------------

    def _run(self, job: _Job, plan: SelectionPlan, session_path: Path, state_dir: Path | None) -> None:
        try:
            selection = collect_selection(plan, state_dir=state_dir)
            cut = cut_session_record(session_path, plan, state_dir=state_dir)
            if job.stop.is_set():
                raise ExportCancelledError()
            root = self._root_factory()
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{job.token}.tar.gz"
            with self._lock:
                job.path = path
                if job.state == STATE_RUNNING:
                    job.phase = PHASE_WRITING
            summary = write_export(
                selection, cut, path, export_id=job.export_id, state_dir=state_dir, should_stop=job.stop.is_set
            )
            with self._lock:
                if job.state == STATE_RUNNING:
                    job.state, job.phase, job.summary = STATE_DONE, PHASE_DONE, summary
        except ExportCancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - every failure becomes the job's error, read by the page
            logger.opt(exception=True).warning("trajectory export {} failed", job.job_id)
            with self._lock:
                if job.state == STATE_RUNNING:
                    self._tokens.pop(job.token, None)
                    job.state, job.finished = STATE_FAILED, self._clock()
                    job.error = f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT]
        finally:
            self._leave(job)

    def _leave(self, job: _Job) -> None:
        """Remove the package unless the job is done, then give the slot back.

        The last look at the state and the release are one locked step: a
        cancel that lands after it finds the worker gone and queues the
        package for the cleaner, one that lands before it is seen here.
        """
        while True:
            with self._lock:
                path = job.path if job.state != STATE_DONE and not job.removed else None
                if path is None:
                    job.worker_alive = False
                    self._workers -= 1
                    self._idle.notify_all()
                    return
                job.removed = True
            _remove(path)

    # -- the surface ------------------------------------------------------

    def start(self, plan: SelectionPlan, *, file_name: str, session_path: Path, state_dir: Path | None = None) -> str:
        if not isinstance(plan, SelectionPlan):
            raise TypeError("start takes the SelectionPlan that plan_selection returned")
        job: _Job | None = None
        with self._lock:
            wake = self._sweep_locked()
            if self._workers < self.limit:
                job = _Job(
                    job_id=f"exj-{secrets.token_hex(8)}",
                    token=secrets.token_urlsafe(32),
                    export_id=new_export_id(),
                    file_name=file_name,
                    created=self._clock(),
                    worker_alive=True,
                )
                self._jobs[job.job_id] = job
                self._tokens[job.token] = job.job_id
                self._workers += 1
        if wake:
            self._start_cleaner()
        if job is None:
            raise JobLimitError(self.limit)
        thread = threading.Thread(
            target=self._run,
            args=(job, plan, Path(session_path), state_dir),
            name=f"{THREAD_PREFIX}-run-{job.job_id}",
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            with self._lock:
                self._jobs.pop(job.job_id, None)
                self._tokens.pop(job.token, None)
                self._workers -= 1
                self._idle.notify_all()
            raise
        return job.job_id

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            wake = self._sweep_locked()
            job = self._jobs.get(job_id)
            view = None if job is None else self._view(job)
        if wake:
            self._start_cleaner()
        if view is None:
            raise JobNotFoundError(job_id)
        return view

    @staticmethod
    def _view(job: _Job) -> dict[str, Any]:
        summary = job.summary if job.state == STATE_DONE else None
        return {
            "job_id": job.job_id,
            "state": job.state,
            "phase": job.phase,
            "file_name": job.file_name,
            "download_url": f"{DOWNLOAD_PATH}?token={job.token}" if summary is not None else None,
            "size_bytes": summary.size_bytes if summary is not None else None,
            "counts": dict(summary.counts) if summary is not None else None,
            "error": job.error if job.state == STATE_FAILED else None,
        }

    def cancel(self, job_id: str) -> None:
        with self._lock:
            wake = self._sweep_locked()
            job = self._jobs.get(job_id)
            if job is not None:
                wake = self._retire_locked(job) or wake
        if wake:
            self._start_cleaner()
        if job is None:
            raise JobNotFoundError(job_id)

    def download(self, token: str) -> tuple[Path, str] | None:
        """(file, download name) for a live token, else None."""
        with self._lock:
            wake = self._sweep_locked()
            job_id = self._tokens.get(token) if token else None
            job = self._jobs.get(job_id) if job_id else None
            found = (job.path, job.file_name) if job and job.state == STATE_DONE and job.path else None
        if wake:
            self._start_cleaner()
        return found

    def shutdown(self, *, timeout: float = 10.0) -> bool:
        """Cancel every job and wait until their files are gone; True when all were.

        Blocks, so it runs off the loop. A worker still deep in a scan by the
        deadline is left to finish on its own; what it leaves behind is old
        enough for the next startup sweep by then.
        """
        with self._lock:
            wake = False
            for job in self._jobs.values():
                wake = self._retire_locked(job) or wake
        if wake:
            self._start_cleaner()
        with self._lock:
            return self._idle.wait_for(lambda: self._workers == 0 and not self._cleaner_alive, timeout)

    def sweep_stale_files(self, *, now: float | None = None) -> int:
        """Remove packages and partial files older than ``ttl``; blocks on the disk."""
        cutoff = (time.time() if now is None else now) - self.ttl
        root = self._root_factory()
        if not root.is_dir():
            return 0
        removed = 0
        for entry in root.iterdir():
            name = entry.name
            if not (name.endswith(".tar.gz") or (name.startswith(".") and name.endswith(".part"))):
                continue
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


JOBS = ExportJobs()
"""The process-wide registry the RPC handlers and the download route share."""


__all__ = [
    "DOWNLOAD_PATH",
    "JOBS",
    "ExportJobs",
    "JobLimitError",
    "JobNotFoundError",
]
