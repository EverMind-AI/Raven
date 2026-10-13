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
file left behind is a run to pick up (:mod:`raven.acp_client.remote_resume`).
No address is in it: the machine is named by its registry id.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from raven.home import raven_home

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
    """Remove the record of ``task``: the turn ended here."""
    with contextlib.suppress(FileNotFoundError, OSError):
        path_of(task).unlink()


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
    "forget",
    "mark_left",
    "path_of",
    "pending",
    "record",
    "runs_dir",
    "was_left",
]
