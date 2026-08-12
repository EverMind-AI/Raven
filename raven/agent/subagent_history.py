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
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.helpers import safe_path_segment

_HISTORY_DIRNAME = "subagents"
_SPAWN_DIRNAME = "spawn"
_DAG_DIRNAME = "mas_dag"


def make_call_id(task_id: str | None = None) -> str:
    """A sortable id for one ``spawn`` call: ``<UTC timestamp>-<8 hex>``.

    Same shape as ``make_run_id`` for DAG runs, so the two history trees sort
    and read alike. ``task_id`` reuses the id the manager already logs under
    (``Subagent [<task_id>] ...``), so a directory can be tied back to the log
    lines for that run; a caller without one gets a fresh suffix.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{safe_path_segment(task_id) if task_id else uuid.uuid4().hex[:8]}"


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

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    @classmethod
    def open(
        cls,
        session_dir: Path,
        *,
        task_id: str,
        task: str,
        meta: dict[str, Any],
    ) -> "SpawnRecord":
        record = cls(spawn_root(session_dir) / make_call_id(task_id))
        try:
            record.dir.mkdir(parents=True, exist_ok=True)
            (record.dir / "prompt.md").write_text(task, encoding="utf-8")
            record._write_meta({**meta, "status": "running", "started_at_ms": int(time.time() * 1000)})
        except OSError as exc:
            logger.warning("Subagent [{}] history could not be opened at {}: {}", task_id, record.dir, exc)
        return record

    def finish(self, *, status: str, output: str | None = None, error: str | None = None) -> None:
        """Record the outcome. ``output`` and ``error`` are written as files."""
        try:
            if not self.dir.is_dir():
                return
            if output is not None:
                (self.dir / "out.md").write_text(output, encoding="utf-8")
            if error is not None:
                (self.dir / "error.md").write_text(error, encoding="utf-8")
            meta = self._read_meta()
            meta.update(status=status, ended_at_ms=int(time.time() * 1000))
            self._write_meta(meta)
        except OSError as exc:
            logger.warning("Subagent history at {} could not be finished: {}", self.dir, exc)

    def _read_meta(self) -> dict[str, Any]:
        try:
            return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
