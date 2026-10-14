"""What this computer keeps of a run on a registered machine, for as long as the run is in flight.

A run on a machine belongs to the machine's daemon (:mod:`raven.node.server`):
it goes on, and its frames are kept there, when this process is gone -- killed,
restarted with ``/restart``, or stopped on purpose. What this process has to
keep is what the machine does not know, so that the next process can pick the
run up where this one left it: whose run it is (the Raven session, the agent,
the instance handle, the dispatch), the ACP session the turn runs in, the id
of the prompt whose answer is awaited, how many of the agent's frames reached
here, the ids this side has used, and what the agent answered to
``initialize`` on the connection. One file per run under
``<raven home>/machines/runs/<task>.json``, owner-only, written when the turn
starts, kept current as frames arrive, and removed when the turn ends here. A
file left behind is a run to pick up (:mod:`raven.acp_client.remote_resume`),
by one process at a time: the pick-up is made under a claim beside the record
(:func:`claim`), since two processes of one Raven home can start together.
No address is in it: the machine is named by its registry id.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.home import raven_home
from raven.utils.pid import pid_alive

#: Where the records live under the raven home.
RUNS_DIR = ("machines", "runs")
VERSION = 1


@dataclass
class RemoteRun:
    """One turn running on a machine, as this computer must remember it to pick it up again."""

    machine: str
    """The registry id of the machine."""
    task: str
    """The task id the machine's daemon runs the agent under."""
    agent: str
    session_key: str
    handle: str
    task_id: str
    """The dispatch's id on this computer (the manager's), which the announce names."""
    session_id: str
    """The ACP session the turn runs in."""
    prompt_id: int
    """The JSON-RPC id of the ``session/prompt`` whose answer is awaited."""
    next_id: int
    """The id the request after the prompt would carry; a later process goes on past it."""
    initialize: Any
    """The agent's answer to ``initialize`` on this connection: the capabilities of the process serving it."""
    launch_key: str
    config_key: str
    session_cwd: str
    stateful: bool
    received: int = 0
    """How many of the agent's frames this computer has read on the connection: where an attach goes on from."""
    started: float = field(default_factory=time.time)

    def as_json(self) -> dict[str, Any]:
        return {"version": VERSION, **asdict(self)}

    @classmethod
    def from_json(cls, data: Any) -> RemoteRun | None:
        """A record read back, or ``None`` for one this version does not read."""
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return None
        fields = {name for name in cls.__dataclass_fields__}
        try:
            run = cls(**{key: value for key, value in data.items() if key in fields})
        except TypeError:
            return None
        if not (run.machine and run.task and run.agent and run.session_id):
            return None
        return run


def runs_dir() -> Path:
    """The directory of the records, made owner-only."""
    path = raven_home().joinpath(*RUNS_DIR)
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def path_of(task: str) -> Path:
    return runs_dir() / f"{task}.json"


def record(run: RemoteRun) -> Path:
    """Write ``run``'s record, whole and owner-only, in place of any earlier one."""
    path = path_of(run.task)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(run.as_json(), handle)
    os.replace(tmp, path)
    return path


def forget(task: str) -> None:
    """Remove the record of ``task``, and the claim on it: the turn ended here, or was concluded here."""
    with contextlib.suppress(FileNotFoundError, OSError):
        path_of(task).unlink()
    release(task)


def claim_path(task: str) -> Path:
    """Beside the record: the file naming the process that is picking ``task`` up."""
    return runs_dir() / f"{task}.json.claim"


def claim(task: str) -> bool:
    """Take the pick-up of ``task`` for this process; ``False`` while a live process of this computer holds it.

    Two processes of one Raven home can start together -- a gateway and a
    page stack -- and each would pick the same run up: the machine's daemon
    hands the task to whichever attaches last, and the other waits out a
    prompt that will never answer it, then reports to the conversation a
    failure the run did not have. So a run is picked up under a claim: a
    file beside its record naming the process that holds it, made whole and
    linked into place, so two processes cannot both make it. A claim whose
    process is gone is taken over. :func:`forget` removes it with the
    record; :func:`release` alone, when the run is left for a later start.
    """
    path = claim_path(task)
    with _one_at_a_time():
        for _ in range(2):
            if _take(path):
                return True
            holder = _holder(path)
            if holder is not None and pid_alive(holder):
                return False
            with contextlib.suppress(OSError):
                path.unlink()
    return False


@contextlib.contextmanager
def _one_at_a_time() -> Iterator[None]:
    """The claims of this home taken one process at a time, so the claim a process removes is the one it judged dead.

    Judging a claim stale and taking it over are two steps, and between them
    another process could make a fresh claim at the same path -- which the
    first then removed and took, two processes picking one run up after all.
    An advisory lock on one file beside the claims holds the two steps
    together; the kernel drops it with the process, so a holder that dies
    leaves no lock behind. Where the lock file cannot be made, the claim
    goes on without it and says so if it fails.
    """
    try:
        fd = os.open(runs_dir() / "claims.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    try:
        _lock(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            _unlock(fd)
        os.close(fd)


try:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - the other platform
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def release(task: str) -> None:
    """Give the claim on ``task`` up: the run waits for a later start."""
    with contextlib.suppress(FileNotFoundError, OSError):
        claim_path(task).unlink()


def _take(path: Path) -> bool:
    """``path`` made as this process's claim, whole, and only where nothing is: ``True`` when it is ours."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        except OSError:
            # No hard links here: made in place, with a moment in which it names nobody yet.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        logger.warning("remote run record {}: a claim could not be made: {}", path.name, exc)
        return False
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _holder(path: Path) -> int | None:
    """The process a claim names, or ``None`` for one that names nobody."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def pending() -> list[RemoteRun]:
    """Every run a process of this Raven left on a machine, oldest first; a record that cannot be read is skipped and named."""
    out: list[RemoteRun] = []
    for path in sorted(runs_dir().glob("*.json")):
        try:
            run = RemoteRun.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("remote run record {} could not be read: {}", path.name, exc)
            continue
        if run is None:
            logger.warning("remote run record {} is not one this version reads; left alone", path.name)
            continue
        out.append(run)
    return sorted(out, key=lambda run: run.started)


class Tracker:
    """One run's record, kept current as the agent's frames arrive, and removed when the turn ends here.

    ``received`` is what a channel endpoint's watcher calls after every frame
    the connection has handed on (:class:`raven.node.client.ChannelEndpoint`),
    so a later attach replays only what this computer never read.
    """

    def __init__(self, run: RemoteRun) -> None:
        self.run = run
        self.kept = False
        record(run)

    def prompt_sent(self, request_id: int) -> None:
        """The prompt went out under ``request_id``: what a later process awaits the answer to."""
        self.run.prompt_id = request_id
        self.run.next_id = request_id + 1
        try:
            record(self.run)
        except OSError as exc:
            logger.warning("remote run record {} could not be updated: {}", self.run.task, exc)

    def received(self, n: int) -> None:
        if n <= self.run.received:
            return
        self.run.received = n
        try:
            record(self.run)
        except OSError as exc:
            logger.warning("remote run record {} could not be updated: {}", self.run.task, exc)

    def close(self, *, keep: bool) -> None:
        """The turn ended here (``keep`` false) or goes on without this process (``keep`` true)."""
        self.kept = keep
        if not keep:
            forget(self.run.task)


# The dispatches whose turns this process left running on a machine, by the manager's task id: the
# manager reads one as left, not cancelled, when the run's task is cancelled at shutdown.
_LEFT: set[str] = set()


def mark_left(task_id: str) -> None:
    _LEFT.add(task_id)


def was_left(task_id: str) -> bool:
    """Whether the dispatch ``task_id`` was left running on its machine; read once."""
    try:
        _LEFT.remove(task_id)
    except KeyError:
        return False
    return True


__all__ = [
    "RUNS_DIR",
    "RemoteRun",
    "Tracker",
    "claim",
    "claim_path",
    "forget",
    "mark_left",
    "path_of",
    "pending",
    "record",
    "release",
    "runs_dir",
    "was_left",
]
