"""WindowsExecutor: the same job contract, with the shell taken out of it.

``ProcessExecutor`` assumes a POSIX machine. Its launcher is ``#!/bin/sh`` with
``setsid``, ``trap`` and ``kill -s TERM -- -$PID``; it decides liveness with
``kill -0``, reads timestamps with ``stat -c %Y``, and stages a case by building
a ``cp -as`` symlink tree. On Windows every one of those is "command not found",
and -- worse than failing -- the failures are quiet in exactly the two places
that matter:

  - the spend scan's script exits non-zero, the executor returns ``0.0``, and the
    campaign's budget is never debited;
  - the liveness probe's script exits non-zero, ``_status`` returns ``RUNNING``,
    and the job never reaches a terminal state.

So this module is not a translation of that shell. It is the same contract
written in Python:
``submit`` / ``poll`` / ``fetch_result`` / ``cancel`` / ``fetch_progress``,
a job directory holding ``config.json``, and terminality decided by the
``result.json`` the job writes.

Three things are Windows-specific and worth naming here, because the correct
choice is not the obvious one:

**Liveness is not ``os.kill(pid, 0)``.** CPython implements ``os.kill`` on
Windows as ``TerminateProcess``, so signal 0 does not probe -- it kills. Every
pid but the caller's own comes back as an error, which reads as "gone", and a
process you cannot see is a process you cannot stop. ``raven.utils.pid.pid_alive``
uses ``OpenProcess`` + a zero-timeout wait instead, and is used here.

**Cancellation is a tree kill.** ``os.killpg`` does not exist on Windows. The
work a job starts is a child of the launcher, which is a child of nothing we
hold, so a single-pid kill leaves that work running. ``taskkill /T /F /PID``
walks the child tree by pid, which is the platform's own answer and the one
``raven/ops/transport.py`` already uses.

**The case is copied, not linked.** ``cp -as`` is what keeps a multi-GB case
cheap to stage on a POSIX host; Windows would need administrator rights to make
symlinks, and a staged job must not need them. The case is copied instead, and
the tracked-writes list (``_WRITES_FILE``) is honoured so the files a run is
known to write get their own copy rather than being shared.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

from oncall_flow.backend import (
    JobBackend,
    JobBackendError,
    JobHandle,
    JobResult,
    JobSpec,
    JobStatus,
)
from oncall_flow.budget import ADDITIVE, accumulate
from oncall_flow.budget import from_meta as budget_from_meta
from oncall_flow.process_backend import (
    _GONE_GRACE_SLEEP_S,
    _GONE_GRACE_TRIES,
    _MARKER,
    _OWN_PID,
    _RESERVED,
    _RESOURCES,
    _WRITES_FILE,
    BUDGET_KEY,
    ProcessExecutor,
    _num,
    _stated_cause,
)


def _real_interpreter() -> str:
    """The interpreter to launch the job with.

    This is ``sys.executable`` -- the venv's own ``python.exe`` -- and NOT
    ``sys._base_executable``. The trampoline question was measured twice and the
    two answers pull opposite ways (2026-10-14):

      - launching through the base interpreter makes ``Popen``'s pid correct
        (the shim spawns a child, so the shim's pid is dead at once);
      - launching through the base interpreter also LOSES the job's stdout:
        ``job.log`` kept the launcher's own lines and none of the body's, for
        every flag combination. The shim path captured all three lines.

    A log is what a reader has when a run goes wrong, so the log wins, and the
    pid is taken from the launcher instead -- ``_await_pid`` reads the pid the
    launcher wrote for itself, which is correct under either interpreter. The
    trampoline's pid is then never used and cannot mislead anyone.
    """
    return sys.executable


# The relay a job is started through: it starts the launcher detached, prints its
# pid and exits, so the launcher's parent is a process that no longer exists (see
# ``submit``). CREATE_NEW_PROCESS_GROUP gives a Ctrl-Break-shaped signal somewhere
# to land, DETACHED_PROCESS keeps the job off any console -- the Windows spelling
# of the POSIX launcher's ``nohup ... &``. argv: log path, job dir, then the
# launcher's own command line.
_RELAY = """\
import subprocess, sys
log = open(sys.argv[1], "ab")
p = subprocess.Popen(sys.argv[3:], cwd=sys.argv[2], stdin=subprocess.DEVNULL, stdout=log,
                     stderr=subprocess.STDOUT,
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
print(p.pid)
"""


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a process that has not exited.

    The same answer ``raven.utils.pid.pid_alive`` gives, reached without
    importing it. The import cannot be relied on: the plugin runs with its own
    directory at the head of ``sys.path`` (that is how a host loads it), from
    which ``raven`` is not importable, and a fallback that reported "not alive"
    would make every job look finished the moment it was submitted -- measured
    while testing this backend, where the fallback turned every poll into a
    terminal FAILED. A probe that cannot answer must not answer at all, so the
    check is written here rather than delegated.

    ``os.kill(pid, 0)`` is not the POSIX probe on this platform: CPython
    implements it as ``TerminateProcess``, so it kills rather than asks.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL

    synchronize, wait_timeout, error_access_denied = 0x00100000, 0x00000102, 5

    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        # Only one refusal means the process is there: a handle this account may
        # not open. No such pid, and a pid already reaped, both land here too,
        # and both are dead.
        return ctypes.get_last_error() == error_access_denied
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == wait_timeout
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


# The launcher is a Python file rather than a .bat for one reason: it has to
# survive the job's own exit and still write an honest result. A .bat can do
# that too, but it cannot read a JSON file without another interpreter, and the
# number it would put in `gpu_minutes_used` -- how long the run actually held
# the machine -- is the one field the budget is summed from. Whatever runs this
# is the interpreter raven is already running under, so this is the one
# interpreter the machine is guaranteed to have.
_LAUNCHER_NAME = ".raven-launch.py"

_LAUNCHER_SRC = '''\
"""Run one job, then make sure a result.json exists.

Written per job directory by the windows backend. Not meant to be read as
documentation of anything -- it is the Windows equivalent of the POSIX
launcher's wrap-up half, and the reason it exists is that an upstream program
has never heard of the result.json contract: a wrapper script may, a bare
executable will not. A job that exits without a result is recorded as
ended from outside, and its spend is reconstructed from the clock.
"""

import json
import os
import subprocess
import sys
import time


def read_pid(path):
    try:
        with open(path, "r") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_result(payload):
    path = os.path.join(HERE, "result.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


HERE = os.path.dirname(os.path.abspath(__file__))
COMMAND = sys.argv[1]


def main():
    # The log is opened by the launcher, not inherited from the submitter. A
    # handle passed down through Popen is not reliably the one the child writes
    # to once the process is detached -- measured 2026-10-14, a detached job's
    # job.log stayed at 0 bytes for every shape while the job itself ran to
    # completion and wrote its result. Opening it here makes the question moot:
    # this process owns the handle, and os.dup2 points its own stdout and stderr
    # at it, so the body's output and any traceback below land in the same file
    # in the order they happened.
    try:
        _sink = open(os.path.join(HERE, "job.log"), "ab", buffering=0)
        os.dup2(_sink.fileno(), 1)
        os.dup2(_sink.fileno(), 2)
    except OSError:
        pass

    t0 = time.time()
    with open(os.path.join(HERE, ".raven-pid"), "w") as fh:
        fh.write(str(os.getpid()))

    rc = subprocess.call(COMMAND, cwd=HERE, shell=True)
    minutes = round((time.time() - t0) / 60.0, 3)

    # A command that detached itself leaves nothing to wait for: `pid` then names
    # a live process that is not this one. Adopting it is not possible (its exit
    # code cannot be read) and ignoring it bills running work as a finished
    # job, so it is stopped and the reason is recorded where the next submit
    # reads it.
    left = read_pid(os.path.join(HERE, "pid"))
    escaped = bool(left) and left != os.getpid() and _alive(left)
    if escaped:
        subprocess.call(["taskkill", "/T", "/F", "/PID", str(left)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    if escaped or not os.path.exists(os.path.join(HERE, "result.json")):
        payload = {
            "status": "failed" if escaped or rc != 0 else "succeeded",
            "exit_code": rc,
            "gpu_minutes_used": minutes,
            "written_by": "launcher",
        }
        if escaped:
            payload["error"] = (
                "the command detached itself and left the launcher nothing to wait for; "
                "run it in the foreground"
            )
        write_result(payload)
    return rc


# The wrap-up is the part that must not die quietly. If it raises, there is no
# result.json and the backend reports the job as "ended from outside before it
# could record a result" -- which is a true statement about a job whose failure
# was in the harness, and the least useful one. So anything that escapes the
# body above is written out as the failure it is.
try:
    sys.exit(main())
except SystemExit:
    raise
except BaseException:
    import traceback

    try:
        write_result({
            "status": "failed",
            "gpu_minutes_used": 0.0,
            "written_by": "launcher",
            "error": "launcher crashed: " + traceback.format_exc()[-600:],
        })
    finally:
        traceback.print_exc()
        sys.exit(70)
'''


def _alive_source() -> str:
    """The liveness helper the launcher gets, injected rather than imported.

    The launcher is a standalone script in a job directory; importing
    ``raven.utils.pid`` there works when raven is on that interpreter's path and
    silently reports "dead" when it is not -- which is the failure mode this
    whole module exists to avoid. So the check is written into the launcher
    itself.
    """
    return """\
def _alive(pid):
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x00000102
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False
"""


def _unused_runner(command: str) -> tuple[int, str]:
    raise JobBackendError("the windows backend runs no shell commands")


class WindowsExecutor(ProcessExecutor):
    """``ProcessExecutor``'s contract, executed locally on Windows.

    Inherits the budget arithmetic and the measurement surface (``poll_count``,
    ``detection_latency_ms``, ``unmeasured_spend``, ``restrict_spend_to``), and
    replaces every method that reached the machine through a shell.
    """

    name = "windows"

    def __init__(
        self,
        *,
        remote_dir: str,
        command: str,
        budget_minutes_total: float | None = None,
        budget: Any = None,
        objective: dict[str, Any] | None = None,
        staged_case: str = "",
        prefix: str = "ops-",
    ) -> None:
        super().__init__(
            _unused_runner,
            remote_dir=remote_dir,
            command=command,
            budget_minutes_total=budget_minutes_total,
            budget=budget,
            objective=objective,
            staged_case=staged_case,
            prefix=prefix,
        )
        self._remote_dir = os.path.normpath(remote_dir)

    # ---- paths ----

    def _job_dir(self, idem_key: str) -> str:
        return os.path.join(self._remote_dir, "jobs", idem_key)

    def job_dir(self, idem_key: str) -> str:
        return self._job_dir(idem_key)

    def _jobs_root(self) -> str:
        return os.path.join(self._remote_dir, "jobs")

    def _writes_path(self) -> str:
        return os.path.join(self._remote_dir, _WRITES_FILE)

    @staticmethod
    def _read_json(path: str) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _read_pid(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return int((fh.read() or "0").strip() or 0)
        except (OSError, ValueError):
            return 0

    # ---- staging ----

    def _stage_tree(self, job_dir: str) -> None:
        """Give the trial the owner's case, as real files.

        Copied rather than linked: a symlink tree needs administrator rights on
        Windows, and a job that needs them is a job that fails on the machine it
        was written for. The files a previous round was seen to write get their
        own copy here for the same reason ``ProcessExecutor`` removes the
        symlinks it created -- a redirect through a link writes to the case.
        """
        if not self._staged_case:
            return
        os.makedirs(job_dir, exist_ok=True)
        for name in os.listdir(self._staged_case):
            if name in _RESERVED:
                continue
            src = os.path.join(self._staged_case, name)
            dst = os.path.join(job_dir, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
        for rel in self._known_writes:
            src = os.path.join(self._staged_case, rel)
            dst = os.path.join(job_dir, rel)
            if os.path.isfile(src):
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.copy2(src, dst)

    def _write_launcher(self, job_dir: str) -> str:
        path = os.path.join(job_dir, _LAUNCHER_NAME)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_alive_source())
            fh.write(_LAUNCHER_SRC)
        return path

    def _existing_state(self, job_dir: str) -> str:
        """ "done" / "running" / "absent", for submit's idempotency check."""
        if os.path.isfile(os.path.join(job_dir, "result.json")):
            return "done"
        for name in (_OWN_PID, "pid"):
            pid = self._read_pid(os.path.join(job_dir, name))
            if pid and pid_alive(pid):
                return "running"
        return "absent"

    # ---- JobBackend ----

    async def submit(self, spec: JobSpec) -> JobHandle:
        job_id = f"{self._prefix}{spec.idem_key}"
        job_dir = self._job_dir(spec.idem_key)

        if self._existing_state(job_dir) in ("done", "running"):
            return JobHandle(self.name, job_id)

        config = dict(spec.payload)
        if self._budget is not None:
            remaining = await self.remaining_minutes() or 0.0
            if remaining <= 0:
                raise JobBackendError(
                    f"compute budget exhausted: {self._budget} minutes are spent, no further jobs can be submitted"
                )
            asked = config.get(BUDGET_KEY)
            try:
                asked = float(asked) if asked is not None else None
            except (TypeError, ValueError):
                asked = None
            allowed = min(asked, remaining) if asked is not None and asked > 0 else remaining
            config[BUDGET_KEY] = round(allowed, 3)

        await self._load_known_writes()
        labels = dict(getattr(spec, "labels", None) or {})
        device_ids = ",".join(x.strip() for x in str(labels.get("device_ids") or "").split(",") if x.strip())
        try:
            width = max(1.0, float(labels.get("width") or 1.0))
        except (TypeError, ValueError):
            width = 1.0
        self._declared_width[spec.idem_key] = width
        resources: dict[str, Any] = {"width": width}
        if device_ids:
            resources["device_ids"] = device_ids.split(",")

        os.makedirs(job_dir, exist_ok=True)
        self._stage_tree(job_dir)

        cfg_path = os.path.join(job_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        with open(os.path.join(job_dir, _RESOURCES), "w", encoding="utf-8") as fh:
            json.dump(resources, fh)

        launcher = self._write_launcher(job_dir)
        command = self._command.format(
            job_dir=job_dir,
            config=cfg_path,
            staged_case=self._staged_case,
            remote_dir=self._remote_dir,
        )

        # The previous attempt's log is moved aside rather than truncated or
        # appended: a reader looking at job.log must not see the earlier attempt's
        # tail and believe it. On Windows the move can fail -- a job that is still
        # dying holds an exclusive handle, and a rename of an open file is refused
        # (measured: WinError 32 out of a submit whose predecessor had left an
        # orphan). Losing the archive is a small thing; losing the submission is
        # not, so a refused move falls back to truncating in place, and a refused
        # truncate is ignored.
        log_path = os.path.join(job_dir, "job.log")
        if os.path.isfile(log_path) and os.path.getsize(log_path) > 0:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            try:
                os.replace(log_path, os.path.join(job_dir, f"job.log.{stamp}"))
            except OSError:
                try:
                    open(log_path, "w").close()
                except OSError:
                    pass

        # Started through a relay that exits at once, so the job is nobody's
        # child. DETACHED_PROCESS alone is not enough: the host stops this
        # agent with ``taskkill /T``, which walks parent pids and does not care
        # what flags a child was started with -- measured 2026-10-18, stopping
        # the page killed a job twenty minutes in, launcher and job body with
        # it, and only processes a system service had started ran on. With
        # the relay gone, the walk from this process finds no path to
        # the job. ``start_new_session`` is deliberately not passed: Windows
        # ignores it, and passing it here would only suggest it does something.
        try:
            relay = subprocess.run(
                [_real_interpreter(), "-c", _RELAY, log_path, job_dir, _real_interpreter(), launcher, command],
                cwd=job_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise JobBackendError(f"could not start the job in {job_dir}: {exc}") from exc
        relayed = relay.stdout.strip()
        if relay.returncode != 0 or not relayed.isdigit():
            raise JobBackendError(f"could not start the job in {job_dir}: {(relay.stderr or relayed).strip()[-500:]}")

        # The launcher's own pid, not the relay's report, whenever the launcher
        # has got far enough to write it. They differ under any trampoline (see
        # ``_real_interpreter``), and the launcher's is the one that names the
        # process doing the work -- the one a cancel has to reach and the one
        # whose death means the job is over.
        # ``.raven-pid`` is the launcher's own pid and only this backend writes
        # it. ``pid`` is NOT written here: it belongs to the job body, which puts
        # the pid of the process doing the work there so a cancel reaches it
        # rather than the wrapper around it -- and the launcher reads it back to
        # tell a detached run from a finished one. Writing our own pid into it (which this did
        # at first) made every detached job look finished: the launcher compared
        # `pid` against its own pid, found them equal, and concluded nothing had
        # escaped. Measured 2026-10-14 on the detach case.
        pid = self._await_pid(job_dir) or int(relayed)
        with open(os.path.join(job_dir, _OWN_PID), "w") as fh:
            fh.write(str(pid))
        with open(os.path.join(job_dir, _MARKER), "w") as fh:
            fh.write(str(time.time()))

        return JobHandle(self.name, job_id)

    def _await_pid(self, job_dir: str, tries: int = 50) -> int:
        """The pid the launcher wrote for itself, once it is there.

        Bounded: a launcher that never starts (a bad interpreter path, a policy
        refusing execution) must not hang the submit. The caller falls back to
        ``Popen``'s pid in that case, which is wrong only about which process
        it names -- the job still gets polled and the probe still answers.
        """
        path = os.path.join(job_dir, _OWN_PID)
        for _ in range(tries):
            pid = self._read_pid(path)
            if pid and pid_alive(pid):
                return pid
            time.sleep(0.1)
        return 0

    async def poll(self, handle: JobHandle) -> JobStatus:
        return await super().poll(handle)

    def _probe_cmd(self, job_dir: str) -> str:  # pragma: no cover - never called
        raise JobBackendError("the windows backend probes in Python, not in a shell")

    async def _status(self, idem: str) -> JobStatus:
        job_dir = self._job_dir(idem)
        decided = self._decide(job_dir)
        if decided is not None:
            return decided
        # "gone" with no result: not the same as a job that ended from outside.
        # A script writes its worker's pid into `pid` and then stays for its own
        # wrap-up, so a dead pid plus a live wrapper is a run that is finishing.
        # A first look at "gone" only starts a wait, exactly as on POSIX.
        for _ in range(_GONE_GRACE_TRIES):
            await asyncio.sleep(_GONE_GRACE_SLEEP_S)
            decided = self._decide(job_dir)
            if decided is not None:
                return decided
        return JobStatus.FAILED

    def _decide(self, job_dir: str) -> JobStatus | None:
        data = self._read_json(os.path.join(job_dir, "result.json"))
        if data is not None:
            return JobStatus.SUCCEEDED if data.get("status") == "succeeded" else JobStatus.FAILED
        if os.path.isfile(os.path.join(job_dir, "result.json")):
            # Present but unreadable: a job wrote half of it. Not a status to
            # guess at, so it stays RUNNING until the writer is done with it.
            return None
        for name in (_OWN_PID, "pid"):
            path = os.path.join(job_dir, name)
            if not os.path.isfile(path):
                continue
            pid = self._read_pid(path)
            if pid and pid_alive(pid):
                return JobStatus.RUNNING
            return None
        return JobStatus.PENDING

    async def _stamp_terminal(self, idem: str) -> None:
        job_dir = self._job_dir(idem)
        result = os.path.join(job_dir, "result.json")
        try:
            finished = os.path.getmtime(result)
        except OSError:
            self._finish_unknown[idem] = "no result.json to date the finish from"
            return
        now = time.time()
        latency = max(0, int(round((now - finished) * 1000)))
        self._latency_ms[idem] = latency
        payload = json.dumps(
            {"finished_at_host": finished, "first_observed_host": now, "detection_latency_ms": latency},
            ensure_ascii=False,
        )
        try:
            with open(os.path.join(job_dir, "detection.json"), "w", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError:
            pass

    async def fetch_result(self, handle: JobHandle) -> JobResult:
        idem = self._idem(handle)
        job_dir = self._job_dir(idem)
        result_path = os.path.join(job_dir, "result.json")
        if not os.path.isfile(result_path):
            for name in (_OWN_PID, "pid"):
                pid = self._read_pid(os.path.join(job_dir, name))
                if pid and pid_alive(pid):
                    return JobResult(
                        JobStatus.RUNNING,
                        error="still running: no result yet, which is what a job that has "
                        "not finished looks like. Read its progress instead.",
                    )
            tail = self._tail(os.path.join(job_dir, "job.log"), 30)
            said = (
                "the job was ended from outside before it could record a result, so the reason is not in its own output"
            )
            detail = f", and its last lines were:\n{tail}" if tail else ", and it left no log either"
            return JobResult(JobStatus.FAILED, error=said + detail)

        data = self._read_json(result_path)
        if data is None:
            raise JobBackendError(f"bad result.json for {handle.job_id}")

        metrics: dict[str, float] = {}
        metric = str(self._objective.get("metric") or "")
        points = data.get("eval_points") or []
        if points and metric:
            try:
                metrics[metric] = float(points[-1][1])
            except (TypeError, ValueError, IndexError):
                pass
        if data.get("gpu_minutes_used") is not None:
            try:
                metrics["gpu_minutes_used"] = float(data["gpu_minutes_used"])
            except (TypeError, ValueError):
                pass

        written = await self._case_files_written(idem)
        if written is not None:
            data["case_files_written"] = written
        status = JobStatus.SUCCEEDED if data.get("status") == "succeeded" else JobStatus.FAILED
        deliverable = self._deliverable(data, metric, job_dir) if status is JobStatus.SUCCEEDED else None
        return JobResult(
            status,
            metrics=metrics,
            output=data,
            deliverable=deliverable,
            error=None if status is JobStatus.SUCCEEDED else _stated_cause(data),
        )

    @staticmethod
    def _tail(path: str, lines: int) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:]).strip()[:500]
        except OSError:
            return ""

    async def fetch_progress(self, handle: JobHandle, tail: int = 5) -> list[dict]:
        path = os.path.join(self._job_dir(self._idem(handle)), "progress.jsonl")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.readlines()[-max(0, int(tail)) :]
        except OSError:
            return []
        rows = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    async def cancel(self, handle: JobHandle) -> str | None:
        job_dir = self._job_dir(self._idem(handle))
        note: str | None = None
        # Looked at before the kill: what the record needs is what was there when
        # the decision landed, not what is left afterwards.
        for name in (_OWN_PID, "pid"):
            pid = self._read_pid(os.path.join(job_dir, name))
            if pid and pid_alive(pid):
                started = self._mtime(os.path.join(job_dir, _MARKER))
                minutes = max(0.0, (time.time() - started) / 60.0) if started else None
                note = (
                    f"process was alive (pid {pid}, running {minutes:.1f} min) when killed"
                    if minutes is not None
                    else f"process was alive (pid {pid}) when killed"
                )
                subprocess.call(
                    [shutil.which("taskkill") or "taskkill", "/T", "/F", "/PID", str(pid)],  # noqa: S607
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                )
                break
        return note

    async def list_artifacts(self, handle: JobHandle, pattern: str = "step-*") -> tuple[str, ...] | None:
        import glob as _glob

        job_dir = self._job_dir(self._idem(handle))
        if not os.path.isdir(job_dir):
            return None
        return tuple(sorted(os.path.basename(p) for p in _glob.glob(os.path.join(job_dir, pattern))))

    async def busy_devices(self, device_ids: list[str]) -> dict[str, int]:
        """Empty unless nvidia-smi is on this machine.

        A probe that cannot answer must not refuse, and a Windows machine
        often has no nvidia-smi at all -- the campaign is billed by wall clock.
        """
        ids = [str(d) for d in device_ids if str(d).strip()]
        if not ids:
            return {}
        exe = shutil.which("nvidia-smi")
        if not exe:
            return {}
        try:
            out = subprocess.run(
                [exe, "--query-gpu=index,memory.used", "--format=csv,noheader,nounits", "-i", ",".join(ids)],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        except Exception:  # noqa: BLE001 - a probe that cannot answer must not refuse
            return {}
        busy: dict[str, int] = {}
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                idx, used = parts[0], int(float(parts[1]))
            except ValueError:
                continue
            if idx in ids and used > 1024:
                busy[idx] = used
        return busy

    # ---- spend ----

    @staticmethod
    def _mtime(path: str) -> float | None:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _spend_rows(self) -> list[tuple[str, str, str, str, str, str, str, str]]:
        """The same eight columns the POSIX scan printed, read in Python.

        Kept in the same shape on purpose: the arithmetic below is the parent's,
        and the two backends disagreeing about what a row means would put two
        different budgets in front of the same loop.
        """
        rows: list[tuple[str, str, str, str, str, str, str, str]] = []
        root = self._jobs_root()
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return rows
        for name in names:
            job_dir = os.path.join(root, name)
            if not os.path.isdir(job_dir):
                continue
            data = self._read_json(os.path.join(job_dir, "result.json"))
            minutes = "-"
            if data is not None and data.get("gpu_minutes_used") is not None:
                minutes = repr(data["gpu_minutes_used"])
            pid = self._read_pid(os.path.join(job_dir, "pid"))
            alive = "1" if pid and pid_alive(pid) else "0"
            started = self._mtime(os.path.join(job_dir, "pid"))
            elapsed, progress_mtime = "-", "-"
            prog = os.path.join(job_dir, "progress.jsonl")
            if os.path.isfile(prog):
                progress_mtime = repr(self._mtime(prog) or "")
                elapsed = self._last_elapsed(prog)
            width, exclusive = "1", "0"
            res = self._read_json(os.path.join(job_dir, _RESOURCES))
            if res is not None:
                width = repr(res.get("width", 1))
                exclusive = "1" if res.get("device_ids") else "0"
            rows.append(
                (
                    name,
                    minutes,
                    alive,
                    repr(started) if started else "-",
                    elapsed,
                    progress_mtime,
                    width,
                    exclusive,
                )
            )
        return rows

    @staticmethod
    def _last_elapsed(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                last = fh.readlines()[-1]
            return repr(json.loads(last).get("elapsed_s", ""))
        except Exception:  # noqa: BLE001 - an unreadable line is an absent number
            return "-"

    async def spent_minutes(self) -> float:
        rows = self._spend_rows()
        now = time.time()
        unplaced = 0.0
        spans: list[tuple[float, float, float]] = []
        exclusive_spans: list[tuple[float, float, float]] = []
        self._unmeasured.clear()
        for key, result_min, alive, started, elapsed, prog_mtime, width, exclusive in rows:
            if not self._counts_toward_spend(key):
                continue
            rm, st, el, pm = _num(result_min), _num(started), _num(elapsed), _num(prog_mtime)
            if rm is not None:
                minutes = rm
            elif alive == "1" and st is not None:
                minutes = max(0.0, (now - st) / 60.0)
            elif el is not None:
                minutes = el / 60.0
            elif pm is not None and st is not None:
                minutes = max(0.0, (pm - st) / 60.0)
            else:
                self._unmeasured[key] = "killed with nothing to measure its spend from"
                continue
            if st is None:
                unplaced += minutes
            else:
                span = (st, st + minutes * 60.0, max(1.0, _num(width) or 1.0))
                (exclusive_spans if exclusive == "1" else spans).append(span)
        return unplaced + accumulate(spans, overlap=self.budget_overlap) + accumulate(exclusive_spans, overlap=ADDITIVE)

    # ---- tracked writes ----

    async def _load_known_writes(self) -> None:
        if self._known_writes_loaded or not self._staged_case:
            return
        self._known_writes_loaded = True
        stored = self._read_json(self._writes_path())
        if not isinstance(stored, dict):
            return
        found = stored.get(self._staged_case)
        if isinstance(found, list):
            self._known_writes = [str(x) for x in found]

    async def _remember_writes(self, paths: list[str]) -> None:
        fresh = [p for p in paths if p not in self._known_writes]
        if not fresh:
            return
        self._known_writes = sorted({*self._known_writes, *fresh})
        cur = self._read_json(self._writes_path()) or {}
        if not isinstance(cur, dict):
            cur = {}
        cur[self._staged_case] = sorted(set(cur.get(self._staged_case, [])) | set(self._known_writes))
        try:
            with open(self._writes_path(), "w", encoding="utf-8") as fh:
                json.dump(cur, fh)
        except OSError:
            pass

    async def _case_files_written(self, idem: str) -> list[str] | None:
        if not self._staged_case:
            return None
        if idem in self._case_written:
            return self._case_written[idem]
        marker = self._mtime(os.path.join(self._job_dir(idem), _MARKER))
        if marker is None:
            return None
        found: list[str] = []
        case = os.path.normcase(os.path.abspath(self._staged_case))
        for root, _dirs, files in os.walk(self._staged_case):
            for name in files:
                path = os.path.join(root, name)
                mt = self._mtime(path)
                if mt is None or mt <= marker:
                    continue
                rel = os.path.relpath(os.path.abspath(path), case)
                found.append(rel.replace("\\", "/"))
                if len(found) >= 40:
                    break
        self._case_written[idem] = found
        await self._remember_writes(found)
        return found


def windows_from_meta(meta: dict[str, Any]) -> JobBackend:
    """Build the Windows executor from a campaign's meta.

    ``command`` is a black box with the same substitutions the process backend
    offers -- ``{job_dir}``, ``{config}``, ``{staged_case}``, ``{remote_dir}``.
    It runs through ``cmd.exe`` in the trial's own directory.
    """
    command = meta.get("command")
    if not command:
        raise JobBackendError("windows backend needs a 'command' template in the campaign meta")
    objective = meta.get("objective")
    # ops_submit fills a missing remote_dir with its POSIX default
    # (/root/raven-ops) for the round it launches, and that default never
    # reaches meta.json, so a later status read sees no remote_dir at all.
    # Read on Windows, /root/raven-ops is <drive>:\root\raven-ops: the job ran
    # there while every status look searched the default, found nothing, and
    # called a finished trial "ended from outside" (2026-10-18). A rooted POSIX
    # path names nothing on Windows, so it is treated exactly like no path at
    # all. The default sits in the user's home, as /root/raven-ops does for
    # root: not every Windows machine has a second drive.
    remote_dir = str(meta.get("remote_dir") or "")
    if not remote_dir or remote_dir.startswith("/"):
        remote_dir = os.path.join(os.path.expanduser("~"), "raven-ops")
    return WindowsExecutor(
        remote_dir=remote_dir,
        command=str(command),
        budget=budget_from_meta(meta),
        objective=objective if isinstance(objective, dict) else None,
        staged_case=str(meta.get("staged_case") or ""),
    )
