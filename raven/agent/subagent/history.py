"""Where a session's sub-agent call history is kept on disk.

Both ways of delegating to a sub-agent record what they were asked and what
they answered, inside that session's metadata directory:

    <agent home>/sessions/<group>/<chat_id>/subagents/
    |-- spawn/<call_id>/          one directory per `spawn` call
    `-- mas_dag/<run_id>/         one directory per `run_subagent_dag` run

``<group>`` is the project slug on ``raven tui`` / ``raven agent`` and the
channel name on the gateway. Rather than re-derive it, the roots below take
the directory from ``SessionManager.session_dir``, so ``<chat_id>.jsonl`` and
``<chat_id>/`` sit side by side even where the two derivations would disagree
-- a transcript written before project grouping keeps its old group, and only
the session manager knows that.

This is deliberately not inside the session's *working* directory: the history
is an audit trail the agent owns, with the same lifetime as the transcript,
while the working directory holds user artifacts and is configured per channel
(or is a project checkout). ``sessions`` is a protected subtree
(raven/agent/workdir.py), so a working directory can never be aimed at it and
no tool write can reach the history.

A file name is never derived from a sub-agent's output, only from ids raven
mints itself, so nothing here is attacker-controlled.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.activity import persisted_output
from raven.utils.atomic_io import atomic_replace
from raven.utils.helpers import safe_path_segment

_HISTORY_DIRNAME = "subagents"
_SPAWN_DIRNAME = "spawn"
_DAG_DIRNAME = "mas_dag"


_STAMP_LOCK = threading.Lock()
_LAST_STAMP_US = 0


def history_stamp() -> str:
    """A UTC stamp that is strictly increasing within this process.

    ``<stamp>-<suffix>`` ids are sorted lexicographically by every reader of
    this tree, and the suffix carries no order at all -- it is a task id or
    random hex. So the stamp is the whole ordering, and a stamp only accurate
    to the second made two calls in one second sort by random hex: the panel
    reshuffled them on every poll, and the listing test that asserts newest
    first failed about one run in six.

    Microseconds are not enough on their own -- two spawns land in the same
    microsecond often enough to tie -- so a tie (or a clock that steps back)
    takes the next microsecond instead. That keeps ids ordered by the order
    they were minted in, which is what the readers actually mean by newest.
    """
    global _LAST_STAMP_US
    with _STAMP_LOCK:
        now = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
        if now <= _LAST_STAMP_US:
            now = _LAST_STAMP_US + 1
        _LAST_STAMP_US = now
    whole = datetime.fromtimestamp(now // 1_000_000, tz=timezone.utc)
    return f"{whole.strftime('%Y%m%dT%H%M%S')}{now % 1_000_000:06d}Z"


def make_call_id(task_id: str | None = None) -> str:
    """A sortable id for one ``spawn`` call: ``<UTC timestamp>-<8 hex>``.

    Same shape as ``make_run_id`` for DAG runs, so the two history trees sort
    and read alike. ``task_id`` reuses the id the manager already logs under
    (``Subagent [<task_id>] ...``), so a directory can be tied back to the log
    lines for that run; a caller without one gets a fresh suffix.
    """
    return f"{history_stamp()}-{safe_path_segment(task_id) if task_id else uuid.uuid4().hex[:8]}"


def session_history_root(session_dir: Path) -> Path:
    """The history root inside one session's metadata directory. Not created here.

    ``session_dir`` is what ``SessionManager.session_dir(key)`` returns. Taking
    it rather than rebuilding it from agent home and the key is what keeps the
    history with its transcript: the manager resolves a pre-grouping session to
    the group its file is actually in, and a second derivation here would send
    the history to the group this process would have chosen instead.
    """
    return Path(session_dir) / _HISTORY_DIRNAME


def spawn_root(session_dir: Path) -> Path:
    """Where this session's ``spawn`` call records live."""
    return session_history_root(session_dir) / _SPAWN_DIRNAME


def dag_root(session_dir: Path) -> Path:
    """Where this session's ``run_subagent_dag`` run dirs live."""
    return session_history_root(session_dir) / _DAG_DIRNAME


def add_turn_to_instance_log(
    session_dir: Path | None,
    *,
    meta: dict[str, Any],
    prompt: str | None,
    output: str | None = None,
    error: str | None = None,
    activity: Any = None,
    kind: str = "",
) -> list[dict[str, Any]]:
    """Add one finished turn to the instance's own conversation log, and return it.

    Every lane that owns a record calls this, because an instance's conversation
    is spread across all of them: the same handle can be dispatched by ``spawn``,
    run as a DAG node and then direct-chatted, and only the union is that
    instance's conversation.

    Typed loosely and failing silently for the same reason the rest of this
    module is: an audit trail must never take down the run it describes.

    Returns the turn ``append_turn`` wrote, empty on an early return or a
    failed write. A caller that also has to hand this call's conversation to
    everos for extraction reads back exactly what landed on disk instead of
    building its own copy that could drift from it.
    """
    agent = str(meta.get("agent") or "")
    handle = str(meta.get("handle") or meta.get("instance") or "")
    if session_dir is None or not agent or not handle:
        return []
    # A transport that narrated as it went already has that prose on the steps it
    # was said before, so the closing row is what it said last -- not `output`,
    # which is every burst joined for the caller receiving the answer. A lane
    # that cannot tell them apart reports None and `output` stands in whole.
    closing = getattr(activity, "closing", None)
    answer = output if closing is None else (closing or None)
    try:
        from raven.agent.subagent.instance_log import append_turn

        return append_turn(
            session_dir,
            agent=agent,
            handle=handle,
            session_key=str(meta.get("session_key") or ""),
            kind=kind,
            # Both spellings, because the two lanes that dispatch write different
            # ones: a spawn record's meta says `task_summary`, a graph node's
            # says `node_summary`. A direct chat has neither, and must not: its
            # meta describes one turn, not what the instance is for.
            title=str(meta.get("task_summary") or meta.get("node_summary") or ""),
            prompt=prompt,
            messages=getattr(activity, "transcript", None),
            answer=answer,
            error=error,
        )
    except Exception as exc:  # noqa: BLE001 - the instance log may not break a run
        logger.warning("Subagent instance log could not be updated: {}", exc)
        return []


class SpawnRecord:
    """One ``spawn`` call's on-disk record: ``spawn/<call_id>/``.

    Mirrors what a DAG node leaves behind (a rendered prompt file and an output
    file), so the two delegation paths are inspectable the same way.

    ``open`` writes the prompt before the sub-agent is dispatched, so a call
    that never returns -- a hang, a killed gateway -- still leaves evidence of
    what was asked. ``finish`` then records the outcome, including failures:
    the failure case is the one worth keeping.

    Every method swallows its own I/O errors. History is an audit trail, and
    losing it must never take down the run it is describing.
    """

    def __init__(self, directory: Path, session_dir: Path | None = None, task: str | None = None) -> None:
        self.dir = directory
        # Kept so `finish` can add this turn to the instance's own log: that file
        # is addressed by (agent, handle) under the session, which the record
        # directory's path does not spell out.
        self.session_dir = session_dir
        self.task = task
        # What `finish` logged, for a caller that also has to hand this call's
        # conversation to everos. Empty until `finish` runs, and stays empty if
        # it returns early -- there is nothing to prime with in that case either.
        self.turn: list[dict[str, Any]] = []
        self._finished = False

    @classmethod
    def open(
        cls,
        session_dir: Path,
        *,
        task_id: str,
        task: str,
        meta: dict[str, Any],
    ) -> "SpawnRecord":
        record = cls(spawn_root(session_dir) / make_call_id(task_id), session_dir=Path(session_dir), task=task)
        try:
            record.dir.mkdir(parents=True, exist_ok=True)
            (record.dir / "prompt.md").write_text(task, encoding="utf-8")
            record._write_meta({**meta, "status": "running", "started_at_ms": int(time.time() * 1000)})
        except OSError as exc:
            logger.warning("Subagent [{}] history could not be opened at {}: {}", task_id, record.dir, exc)
        return record

    def finish(
        self,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        activity: Any = None,
    ) -> None:
        """Record the outcome. ``output`` and ``error`` are written as files.

        ``activity`` is a :class:`~raven.agent.subagent.activity.RunActivity` when
        the backend that ran this had anything to say about *how* it got there --
        which tools it called, what it cost. Merged into ``meta.json`` rather than
        given a file of its own: it is a handful of scalars and a short list, and a
        reader already opens meta.json for every row it draws.

        Typed loosely on purpose. The record is written from the manager and read
        by the RPC layer, and neither should have to import the other's module to
        pass a bag of counters through.
        """
        # First outcome wins: a cancel racing a completion ran finish twice,
        # appending a duplicate turn to the instance log and clobbering the
        # recorded status.
        if self._finished:
            return
        self._finished = True
        try:
            if not self.dir.is_dir():
                return
            # The whole answer, not the capped value the caller was handed: this
            # directory is what the spawn tool advertises as the record of the
            # call, and a copy of the truncation is no recovery path at all.
            if (whole := persisted_output(activity, output)) is not None:
                (self.dir / "out.md").write_text(whole, encoding="utf-8")
            if error is not None:
                (self.dir / "error.md").write_text(error, encoding="utf-8")
            meta = self._read_meta()
            meta.update(status=status, ended_at_ms=int(time.time() * 1000))
            if activity is not None:
                meta.update(getattr(activity, "as_meta", dict)() or {})
                # The run's own transcript, one provider-shaped message per
                # line. Its own file, not meta.json: the panel polls meta on
                # every redraw, and only the opened record reads this.
                transcript = getattr(activity, "transcript", None)
                if isinstance(transcript, list) and transcript:
                    (self.dir / "transcript.jsonl").write_text(
                        "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in transcript),
                        encoding="utf-8",
                    )
            self._write_meta(meta)
        except OSError as exc:
            logger.warning("Subagent history at {} could not be finished: {}", self.dir, exc)
        self.turn = add_turn_to_instance_log(
            self.session_dir,
            meta=self._read_meta(),
            prompt=self.task,
            output=output,
            error=error,
            activity=activity,
            kind="spawn",
        )

    def _read_meta(self) -> dict[str, Any]:
        try:
            return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        # Rewritten by `open` and again by `finish` while the panel polls it on
        # every redraw; the locked replace keeps a poller from reading a torn file.
        atomic_replace(self.dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
