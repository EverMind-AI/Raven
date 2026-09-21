"""A multi-round run's own record, and the file it survives a restart in.

One stint is one run of a `mode: stint` playbook: an id, a tree of its own, and a
round counter. Everything about it that matters is in one JSON file, which is
what makes "any process can pick this up again" true rather than aspirational.
The alternative -- keeping it in the memory of whichever gateway started it --
is the arrangement H* has, and a gateway restart ends those runs.

The file carries a *snapshot of the filled spec*, not the playbook's name. A
stint resumed a day later must not depend on the playbook still being installed,
on its parameters still being in somebody's conversation, or on the library not
having been edited in between. What the stint runs is what it was started with.

Written whole and moved into place, because the reader is often another process
and a half-written stint reads as a corrupt one.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace

__all__ = [
    "HEARTBEAT_EVERY_SEC",
    "RoundRecord",
    "STALE_AFTER_SEC",
    "STINTS_DIRNAME",
    "StintRecord",
    "StintRef",
    "StintStore",
    "make_stint_id",
    "mark_adrift",
]

STINTS_DIRNAME = "stints"

RUNNING = "running"
FINISHED = "finished"
STOPPED = "stopped"
PAUSED = "paused"
INTERRUPTED = "interrupted"

#: How often a process holding a stint says so, in seconds. Short enough that
#: the stamp is fresh when anyone looks, long enough that a stint costs one
#: small write a minute while it is working.
HEARTBEAT_EVERY_SEC = 60.0

#: How long a stamp may go unmoved before the holder is taken to be gone. Five
#: beats, because one missed write during a slow disk moment is not evidence of
#: a dead process, and the cost of being wrong in the other direction -- two
#: hosts advancing one stint -- is the one worth avoiding.
STALE_AFTER_SEC = 300.0


_last_stamp_us = 0


def peer_stores(root: Path) -> list["StintStore"]:
    """Every conversation's stint store on this machine, found from any one of them.

    A stint is kept beside the conversation that started it, and "is one already
    running here" is a question about all of them: a second window on the same
    repository is a new conversation with a store of its own, which is exactly
    the case where starting a second stint silently is the wrong answer.

    Derived from a store's own path rather than passed in, so a caller that can
    name one store can ask about the rest without being handed the session
    layout as well. A path with no ``sessions`` segment is its own only peer.
    """
    root = Path(root)
    parts = root.parts
    if "sessions" not in parts:
        return [StintStore(root)]
    cut = parts.index("sessions") + 1
    base = Path(*parts[:cut])
    tail = Path(*parts[cut + 2 :]) if len(parts) > cut + 2 else Path()
    found = [StintStore(path / tail) for path in sorted(base.glob("*/*")) if path.is_dir()]
    return found or [StintStore(root)]


def mark_adrift(
    stores: "Sequence[StintStore]",
    *,
    held: "Collection[str]" = (),
    now_ms: int | None = None,
) -> list["StintRecord"]:
    """Say of every stint whose holder has gone quiet that it was interrupted.

    A stint's file is the only claim it is going, and a host that dies mid-round
    writes nothing on its way out. Left alone the record says ``running`` for
    ever, `stints list` reports a corpse as work in progress, and a person
    waiting for a notification waits for one nobody will send.

    Judged on the stamp rather than on any process's recollection, which is what
    makes it safe to run anywhere: a stint another host is working has a fresh
    stamp and is left alone, where "its run id is not in my active set" would
    have called it dead and invited a second host to take it up. On the machine
    the holder ran on, a dead pid is taken as the same answer sooner -- see
    :meth:`StintRecord.holder_gone`.

    ``held`` names the stints the caller knows it is working itself, and is
    checked *before* the write rather than filtered out of the answer: a caller
    that removed them afterwards would have already rewritten the record it
    meant to protect.

    Marking only. Taking one up again spends money and hours, so it stays a
    thing a person asks for (`stints resume`, or `sweep` for all of them) rather
    than something a read does on their behalf.
    """
    mine = set(held)
    found: list[StintRecord] = []
    for store in stores:
        for record in store.list():
            if record.stint_id in mine or record.status != RUNNING or not record.abandoned(now_ms):
                continue
            logger.warning("stint {} was left in flight by a process that is gone", record.stint_id)
            record.status = INTERRUPTED
            store.write(record)
            found.append(record)
    return found


def stint_token(stint_id: str) -> str:
    """The part of a stint id that tells it from every other stint, short enough for a node id.

    The stamp's digits between the ``T`` and the ``Z`` -- hour, minute, second
    and microsecond -- which is what makes two ids differ and reads as a time
    to a person scanning a run. An id of another shape gets a hash instead.
    """
    match = re.fullmatch(r"stint-\d{8}T(\d+)Z", stint_id)
    if match:
        return match.group(1)
    import hashlib

    return hashlib.sha1(stint_id.encode("utf-8")).hexdigest()[:8]


def make_stint_id() -> str:
    """A sortable id that reads as a stint at a glance in a directory listing.

    Stamped here rather than borrowed from the sub-agent history's own minter,
    which is the same three lines: this package is a leaf on purpose, and one
    import of the package that imports it would put both inside a cycle -- the
    graph the repo measures counts a function-local import exactly like a
    top-level one.

    A tie, or a clock that steps back, takes the next microsecond. Ids are read
    back in lexicographic order and two stints minted in one microsecond would
    otherwise sort at random.
    """
    global _last_stamp_us
    now = max(int(time.time() * 1_000_000), _last_stamp_us + 1)
    _last_stamp_us = now
    stamp = datetime.fromtimestamp(now / 1_000_000, tz=timezone.utc)
    return f"stint-{stamp.strftime('%Y%m%dT%H%M%S%f')}Z"


@dataclass(frozen=True)
class StintRef:
    """What one round's run carries so it knows it belongs to a stint.

    Passed down the dispatch path as an argument, never looked up. That is the
    whole trick that keeps the stint out of the hot path: an ordinary graph run
    has ``None`` here, exactly as it has ``None`` for the outbox, and neither
    costs it a disk read on the way out.
    """

    stint_id: str
    round_index: int
    rounds: int = 0
    """The budget this round is one of, for a reader that is shown the round.

    Carried rather than looked up for the same reason the rest of this is: the
    number is known where the round is submitted, and a surface drawing "round 3
    of 30" should not have to open the stint file to learn the 30. Zero means
    nobody said, and a reader shows the round alone."""

    workdir: str = ""
    session_key: str = ""
    """Whose stint directory holds this stint.

    Carried rather than derived, because the question is asked from the finished
    run's own task -- there is no turn there whose conversation could answer it,
    and the session that started the stint may have ended hours ago.
    """


@dataclass
class RoundRecord:
    """One round: which run carried it, and what it left behind."""

    index: int
    run_id: str = ""
    attempt: int = 0
    """How many times this round has been submitted.

    Nought is the first. A round taken up again after an interruption is a new
    graph with new node ids, because the ids of the interrupted one are claimed
    for the life of the conversation and cannot be submitted twice.
    """

    status: str = RUNNING
    summary: str = ""
    verify: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    finished: dict[str, str] = field(default_factory=dict)
    """Each role that was judged and committed this round, by the node id it ran as.

    Written as roles finish, so a round cut short says which of its roles are
    done. `resume` names those nodes as met dependencies instead of running the
    roles again, and reads them here rather than off the node registry alone:
    the registry belongs to one conversation's directory, and a resume from a
    terminal or an RPC call reads the wrong one.
    """


@dataclass
class StintRecord:
    """A whole stint, as it is written to disk."""

    stint_id: str
    playbook: str
    spec: dict[str, Any]
    values: dict[str, str] = field(default_factory=dict)
    workdir: str = ""
    project: str = ""
    """The repository this stint belongs to, which ``workdir`` stops being.

    Once a checkout of its own is opened, ``workdir`` is that checkout -- a
    directory the next stint may remove. The question "is a stint already running
    on this project" has no answer without keeping the project itself.
    """

    branch: str = ""
    round_index: int = 0
    status: str = RUNNING
    token: str = ""
    """What this stint's node ids carry so a second stint of the same playbook can run.

    Node ids are claimed for the life of a conversation, and a round's ids were
    ``<playbook>-rNN-<role>``: the second stint of one playbook in one
    conversation -- a CLI's keyless session, a chat where a person says "run it
    again" -- was refused at round one for ids the first stint still owned.
    Empty on a record written before the field existed, whose ids stay as they
    were; :func:`stint_token` mints one for a new stint.
    """

    untracked_at_start: list[str] = field(default_factory=list)
    """Paths already in the tree, uncommitted, when the stint opened it.

    Nobody's writes: the layout the setup pass just wrote, and whatever else the
    checkout carried. The first role judged in a round is graded against the
    whole dirty tree -- there is no earlier commit to measure it from -- so
    without this list the run's own standing orders are attributed to the
    Planner and quarantined as its stray writes, and the round after cannot read
    the file it was told to read.

    The list as it was at the start; the round applies it only while a path is
    still absent from the commit a role is measured from. The first role's
    commit takes the whole tree with it, standing orders included, and from
    then on an edit to one of them is that role's edit -- an exemption that
    outlived the commit would let any role rewrite any other's orders unseen.
    """
    stop_reason: str = ""
    origin: dict[str, Any] = field(default_factory=dict)
    rounds: list[RoundRecord] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    handbacks: dict[str, int] = field(default_factory=dict)
    stage_base: str = ""
    started_at_ms: int = 0
    ended_at_ms: int = 0
    holder_pid: int = 0
    holder_host: str = ""
    """Which process last held a round of this stint, and on which machine.

    The stamp above says *when* the holder last spoke; this says *who*, so a
    reader on the same machine can ask the kernel instead of waiting out the
    stamp. A gateway killed with a stint in flight leaves a fresh stamp for five
    minutes, and a person who restarts it and says "carry on" inside those
    minutes was told the stint was still being worked. A pid that is not alive
    on this host is an answer the clock cannot give.

    Stamped by the driver when it submits a round and on every beat, never by
    the CLI or the RPC layer: those write the file from processes that are not
    holding anything, and a claim from one of them would be believed.
    """

    touched_at_ms: int = 0
    """When a process last said it still holds this stint, by the wall clock.

    The file is the only claim a stint makes about itself, and a host that dies
    mid-round writes nothing on its way out -- so without this the record says
    ``running`` for ever and every reader believes it. Stamped on every write and
    by a beat while a round is in flight, it is the one signal a *second* process
    can read: a stale stamp means the holder is gone, where "this run id is not
    in my own active set" only ever meant "not mine".

    Wall clock rather than monotonic: a monotonic reading is measured from a
    zero that changes every time a process starts, so no other process can
    compare it and a restart cannot read its own.
    """

    @property
    def live(self) -> bool:
        """Something should be advancing this stint right now.

        A paused stint is deliberately not live: nothing should pick it up, and
        a sweep that did would undo the pause.
        """
        return self.status in (RUNNING, INTERRUPTED)

    def stale(self, now_ms: int | None = None) -> bool:
        """Nothing has said it holds this stint for longer than a stint may go quiet.

        A record written before the stamp existed has ``0`` here and reads as
        stale, which is the right answer for it: it was written by a build that
        is no longer running.
        """
        now = int(time.time() * 1000) if now_ms is None else now_ms
        return now - self.touched_at_ms > STALE_AFTER_SEC * 1000

    def holder_gone(self) -> bool:
        """The process that held this stint is known, on this machine, and not alive.

        False whenever the question cannot be answered here: no holder recorded,
        a holder on another host, or a pid this process may not signal (which is
        a pid that exists). Only a `ProcessLookupError` says gone.
        """
        if self.holder_pid <= 0 or self.holder_host != socket.gethostname():
            return False
        try:
            os.kill(self.holder_pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def abandoned(self, now_ms: int | None = None) -> bool:
        """Nothing is advancing this stint: its holder went quiet, or is known to be dead."""
        return self.stale(now_ms) or self.holder_gone()

    def claim(self) -> None:
        """Say that this process holds a round of this stint."""
        self.holder_pid = os.getpid()
        self.holder_host = socket.gethostname()

    @property
    def unfinished(self) -> bool:
        """This stint is not over -- running, interrupted or paused alike.

        The question a *second* stint on the same project has to ask. A paused
        stint still owns its branch and its rounds, so starting another beside it
        is the same mistake as starting one beside a running stint.
        """
        return self.status not in (FINISHED, STOPPED)

    def ref(self, round_index: int | None = None, *, rounds: int = 0) -> StintRef:
        return StintRef(
            stint_id=self.stint_id,
            round_index=self.round_index if round_index is None else round_index,
            rounds=rounds,
            workdir=self.workdir,
            session_key=str(self.origin.get("session_key") or ""),
        )

    def round(self, index: int) -> RoundRecord | None:
        for record in self.rounds:
            if record.index == index:
                return record
        return None

    def open_round(self, index: int, run_id: str, *, attempt: int | None = None) -> RoundRecord:
        """The record for a round about to start, reusing one left interrupted."""
        existing = self.round(index)
        if existing is not None:
            existing.run_id = run_id
            existing.status = RUNNING
            if attempt is not None:
                existing.attempt = attempt
            return existing
        record = RoundRecord(index=index, run_id=run_id, attempt=attempt or 0)
        self.rounds.append(record)
        return record

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StintRecord":
        rounds = [RoundRecord(**entry) for entry in data.get("rounds", [])]
        known = {f for f in cls.__dataclass_fields__}
        # Unknown keys are dropped rather than refused: a stint written by a
        # newer build has to stay readable by an older one long enough for it to
        # say so, and a stint that cannot be read cannot be stopped either.
        fields = {key: value for key, value in data.items() if key in known and key != "rounds"}
        return cls(rounds=rounds, **fields)


#: What a reader of the store gets back. Spelled as an alias because the reader
#: is ``StintStore.list``, and a method of that name shadows the builtin for every
#: annotation in the class body that follows it.
StintRecords = list[StintRecord]


class StintStore:
    """Where stints are kept, one JSON file each, under one directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path_for(self, stint_id: str) -> Path:
        if "/" in stint_id or stint_id in ("", ".", ".."):
            raise ValueError(f"{stint_id!r} is not a stint id")
        return self.root / f"{stint_id}.json"

    def artifacts_for(self, stint_id: str) -> Path:
        """Where this stint keeps what its rounds produce that is not the record.

        Check logs and quarantined writes: things a person opens when they want
        to know what actually happened, kept per stint because that is the unit
        somebody reasons about.
        """
        return self.path_for(stint_id).with_suffix("")

    def write(self, record: StintRecord) -> Path:
        """Write the stint whole, then move it into place.

        A reader is usually another process -- the CLI asking for status, a
        gateway that just started looking for work left behind -- and a
        half-written file reads as a corrupt stint rather than as a stint being
        written.
        """
        path = self.path_for(record.stint_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not record.started_at_ms:
            record.started_at_ms = int(time.time() * 1000)
        if record.status not in (RUNNING, INTERRUPTED) and not record.ended_at_ms:
            record.ended_at_ms = int(time.time() * 1000)
        record.touched_at_ms = int(time.time() * 1000)
        # Through the locked, fsynced replace rather than a bare rename: two
        # processes write this file (a beat, a `stop`, an `answer`), and a
        # torn file read as "no such stint", which a `stop` could not reach.
        atomic_replace(path, json.dumps(record.to_dict(), indent=2, ensure_ascii=False) + "\n")
        return path

    def read(self, stint_id: str) -> StintRecord | None:
        path = self.path_for(stint_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return StintRecord.from_dict(data)

    def list(self) -> StintRecords:
        """Every readable stint, newest first."""
        if not self.root.is_dir():
            return []
        found: list[StintRecord] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            record = self.read(path.stem)
            if record is not None:
                found.append(record)
        return found

    def live(self) -> StintRecords:
        """Plans that believe they are still going, whoever started them."""
        return [record for record in self.list() if record.live]
