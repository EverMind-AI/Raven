"""Where a session's compaction state lives between turns and across processes.

One folder per session under ``<workspace>/memory/.context/checkpoints/``,
named after the session key plus a short hash of it, so two keys that
sanitise to the same name never share one:

- ``<generation>-<checkpoint id>.json`` -- one file per checkpoint, the chain
  a reader outside the process walks;
- ``state.json`` -- what the next turn needs to send the same history again:
  the revision scope it was written under, how long the log was, how many
  compactions that revision has had, which ordinals the pruner rewrote, and
  which checkpoint stands.

Every turn reads the state back for the session's current
:class:`~raven.contracts.context.SessionRevision` and trusts none of it
blindly. A state written under another revision is a different log -- ``/new``
started one, or an undo handed an ordinal to a different message -- so it is
ignored whole. A checkpoint is only standing if it was written under this
revision and every ordinal it names still exists; otherwise the newest written
one that qualifies stands, or none does. The retired ordinals are read off the
standing checkpoint rather than kept beside it, so the two cannot disagree.

Within one revision the log only grows, and within one log the revision's
generation only rises. A reader is therefore behind another process when its
log is shorter than the one the state was written against, or when the state
or a checkpoint on disk belongs to a later generation of the same log -- an
undo another process has merged and this one has not -- and is told so
(:attr:`CompactingState.lagging`). It may read, never write: no state, no
checkpoint, and no clean-up, since anything it decided would be decided on less
than the writer already saw. A different log id is a different log, ``/new``
having started one, and its files are what a writer clears.

Best-effort on purpose. A write that fails is logged and the turn continues --
the history is already decided, and a read-only workspace is not a reason to
fail a conversation. Losing the state costs a recompaction, never a wrong
answer: the log is intact. Nothing is created until the first write, so
selecting the strategy still leaves the disk untouched. Every write is a
temp file plus ``os.replace``, so a reader never sees half of one.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.context_engine.history.compacting.checkpoint import (
    CompactionCheckpoint,
    ordinal_runs,
    ordinals_from_runs,
    runs_end,
)
from raven.utils.atomic_io import atomic_replace, write_transaction
from raven.utils.paths import safe_path_segment

if TYPE_CHECKING:
    from raven.contracts.context import SessionRevision

CHECKPOINT_DIR = Path("memory") / ".context" / "checkpoints"
"""Where the chain and the state are kept, relative to the workspace."""

STATE_FILE = "state.json"
_STATE_VERSION = 2
_KEY_HASH_CHARS = 8


@dataclass(frozen=True)
class CompactingState:
    """What compacting carries from one turn of a session to the next."""

    scope: str = ""
    """The revision scope this state belongs to; empty for a fresh one."""

    message_count: int = 0
    """How long the log was when this state was decided."""

    generation: int = 0
    """How many compactions this revision of the log has had.

    Not the log's own generation: that one counts undos, this one counts
    removals, and it is what decides whether the head is still protected.
    """

    pruned: frozenset[int] = frozenset()
    """Ordinals the pruner has rewritten, re-applied on every later turn."""

    checkpoint: CompactionCheckpoint | None = None
    """The standing checkpoint, already validated against the revision."""

    stale: bool = field(default=False, compare=False)
    """True when what was on disk had to be corrected to be used -- a named
    checkpoint that no longer qualifies, an ordinal past the end of the log --
    so the next save is worth making even if the turn changes nothing."""

    lagging: bool = field(default=False, compare=False)
    """True when the disk holds a later view of this log than this reader --
    a longer log under the same revision, or a later generation of the same
    log id: the reader is behind, and must not write what it decides."""

    @property
    def retired(self) -> frozenset[int]:
        """The ordinals the standing checkpoint stands in for."""
        return frozenset(self.checkpoint.retired) if self.checkpoint is not None else frozenset()


class CheckpointStore:
    """Checkpoints and per-session state on disk, one folder per session."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def folder(self, session_key: str) -> Path:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:_KEY_HASH_CHARS]
        return self.root / f"{safe_path_segment(session_key) or '_'}-{digest}"

    def path_for(self, checkpoint: CompactionCheckpoint) -> Path:
        return self.folder(checkpoint.session_key) / f"{checkpoint.generation:03d}-{checkpoint.checkpoint_id}.json"

    def write(self, checkpoint: CompactionCheckpoint) -> "Path | None":
        """Store ``checkpoint``, and drop the files of revisions it supersedes.

        Refused, writing and deleting nothing, when the disk already holds a
        later generation of the same log: the writer is behind, and its
        checkpoint speaks for a view of the log another process has moved past.

        What is cleared is what can never stand again for whoever is current:
        files of another log id (``/new`` started a new one), and files of an
        earlier generation of this log that no state of this generation or
        later names. A later generation is never touched.
        """
        session_key = checkpoint.session_key
        mine = _split_scope(checkpoint.scope)
        stored = [(path, self._read(path)) for path in self._checkpoint_files(session_key)]
        raw = self._read_state(session_key)
        if mine is not None and (
            _is_later(raw.get("scope") if raw is not None else None, mine)
            or any(other is not None and _is_later(other.scope, mine) for _path, other in stored)
        ):
            logger.info(
                "Not storing compaction checkpoint {} for {}: a later generation of this log is on disk",
                checkpoint.checkpoint_id,
                session_key,
            )
            return None
        path = self.path_for(checkpoint)
        stamped = dataclass_replace(checkpoint, written_at_ns=time.time_ns())
        try:
            atomic_replace(path, json.dumps(stamped.to_json(), ensure_ascii=False, indent=2))
        except OSError as exc:
            logger.warning("Could not store compaction checkpoint at {}: {}", path, exc)
            return None
        # Only a state of this generation can name a file this writer must
        # keep; a later one would have refused the write above.
        current = raw is not None and mine is not None and _split_scope(raw.get("scope")) == mine
        named = str(raw.get("checkpoint_id") or "") if current and raw is not None else ""
        for other_path, other in stored:
            if other_path == path or other is None or mine is None:
                continue
            theirs = _split_scope(other.scope)
            if theirs is None:
                continue
            if theirs[0] != mine[0] or (theirs[1] < mine[1] and other.checkpoint_id != named):
                try:
                    other_path.unlink(missing_ok=True)
                except OSError as exc:
                    # Windows refuses to delete a file another reader holds
                    # open. The new checkpoint is already written and readers
                    # match on scope, so a stale file left here is only clutter
                    # the next write clears; losing this turn's history to it
                    # would not be.
                    logger.info("Could not remove superseded checkpoint {}: {}", other_path, exc)
        return path

    def chain(self, session_key: str, *, scope: str | None = None) -> list[CompactionCheckpoint]:
        """Every stored generation for a session, oldest first.

        ``scope`` narrows it to one revision of the log. Two files of one
        generation are ordered by when they were written.
        """
        found = [cp for cp in (self._read(path) for path in self._checkpoint_files(session_key)) if cp is not None]
        if scope is not None:
            found = [cp for cp in found if cp.scope == scope]
        return sorted(found, key=lambda cp: (cp.generation, cp.written_at_ns, cp.checkpoint_id))

    def load(self, session_key: str, revision: "SessionRevision", *, message_count: int) -> CompactingState:
        """The state the last turn of this revision left, validated.

        ``message_count`` is the length of the log this turn is reading, which
        is the bound every stored ordinal has to fall under.
        """
        fresh = CompactingState(scope=revision.scope)
        mine = (revision.log_id, revision.generation)
        raw = self._read_state(session_key)
        if raw is not None and raw.get("session_key") == session_key and _is_later(raw.get("scope"), mine):
            return dataclass_replace(fresh, lagging=True)
        if raw is None and any(
            cp is not None and _is_later(cp.scope, mine)
            for cp in (self._read(path) for path in self._checkpoint_files(session_key))
        ):
            return dataclass_replace(fresh, lagging=True)
        if raw is None or raw.get("scope") != revision.scope or raw.get("session_key") != session_key:
            return fresh
        stored_count = raw.get("message_count")
        stored_count = stored_count if isinstance(stored_count, int) and stored_count >= 0 else 0
        pruned = ordinals_from_runs(raw.get("pruned"), limit=message_count)
        generation = raw.get("generation")
        generation = generation if isinstance(generation, int) and generation >= 0 else 0
        wanted = str(raw.get("checkpoint_id") or "")
        checkpoint = self._standing(session_key, revision, wanted, message_count) if wanted else None
        if checkpoint is not None:
            generation = max(generation, checkpoint.generation)
        stale = (
            runs_end(raw.get("pruned")) > message_count
            or (checkpoint.checkpoint_id if checkpoint is not None else "") != wanted
        )
        return CompactingState(
            scope=revision.scope,
            message_count=max(stored_count, message_count),
            generation=generation,
            pruned=pruned,
            checkpoint=checkpoint,
            stale=stale,
            lagging=message_count < stored_count,
        )

    def save(self, session_key: str, state: CompactingState) -> bool:
        """Write ``state``, keeping every pruned ordinal already on disk for its scope.

        Read and written under the file's lock, so two writers on one session
        add to each other's pruned set instead of replacing it: a body one of
        them rewrote must not come back because the other one wrote last.
        """
        path = self.folder(session_key) / STATE_FILE
        try:
            with write_transaction(path):
                pruned = set(state.pruned)
                message_count = state.message_count
                raw = self._read_state(session_key)
                mine = _split_scope(state.scope)
                if raw is not None and mine is not None and _is_later(raw.get("scope"), mine):
                    logger.info(
                        "Not storing compaction state for {}: a later generation of this log is on disk", session_key
                    )
                    return False
                if raw is not None and raw.get("scope") == state.scope and raw.get("session_key") == session_key:
                    stored_count = raw.get("message_count")
                    if isinstance(stored_count, int) and stored_count > message_count:
                        message_count = stored_count
                    pruned |= ordinals_from_runs(raw.get("pruned"), limit=max(message_count, 1))
                payload = {
                    "version": _STATE_VERSION,
                    "session_key": session_key,
                    "scope": state.scope,
                    "message_count": message_count,
                    "generation": state.generation,
                    "pruned": ordinal_runs(pruned),
                    "checkpoint_id": state.checkpoint.checkpoint_id if state.checkpoint is not None else None,
                }
                atomic_replace(path, json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError as exc:
            logger.warning("Could not store compaction state at {}: {}", path, exc)
            return False
        return True

    # ------------------------------------------------------------------

    def _checkpoint_files(self, session_key: str) -> list[Path]:
        folder = self.folder(session_key)
        if not folder.is_dir():
            return []
        return [path for path in sorted(folder.glob("*.json")) if path.name != STATE_FILE]

    def _standing(
        self,
        session_key: str,
        revision: "SessionRevision",
        wanted: str,
        message_count: int,
    ) -> CompactionCheckpoint | None:
        """The named checkpoint if it still qualifies, else the newest written that does."""

        def qualifies(checkpoint: CompactionCheckpoint) -> bool:
            return (
                checkpoint.session_key == session_key
                and checkpoint.scope == revision.scope
                and checkpoint.covers.hi < message_count
                and all(index < message_count for index in checkpoint.retired)
            )

        for path in sorted(self.folder(session_key).glob(f"*-{wanted}.json")):
            named = self._read(path, limit=message_count)
            if named is not None and named.checkpoint_id == wanted and qualifies(named):
                return named
        candidates = [
            cp
            for cp in (self._read(path, limit=message_count) for path in self._checkpoint_files(session_key))
            if cp is not None and qualifies(cp)
        ]
        if not candidates:
            return None
        chosen = max(candidates, key=lambda cp: (cp.generation, cp.written_at_ns, cp.checkpoint_id))
        logger.info(
            "Compaction checkpoint {} for {} no longer qualifies; {} stands instead",
            wanted,
            session_key,
            chosen.checkpoint_id,
        )
        return chosen

    def _read_state(self, session_key: str) -> dict[str, Any] | None:
        path = self.folder(session_key) / STATE_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable compaction state {}: {}", path, exc)
            return None
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _read(path: Path, *, limit: int | None = None) -> CompactionCheckpoint | None:
        """One checkpoint file, or ``None``.

        With ``limit``, a file naming an ordinal at or past it is ``None``
        without being expanded: it cannot stand for a log that short, and a
        reader that is merely behind should not pay for it.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            if limit is not None and runs_end(data.get("retired")) > limit:
                return None
            return CompactionCheckpoint.from_json(data)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Skipping unreadable compaction checkpoint {}: {}", path, exc)
            return None


def _split_scope(scope: Any) -> tuple[str, int] | None:
    """``(log_id, generation)`` of a revision scope, or ``None`` for anything else."""
    if not isinstance(scope, str):
        return None
    log_id, sep, generation = scope.rpartition("@")
    if not sep or not log_id or not generation.isdigit():
        return None
    return log_id, int(generation)


def _is_later(scope: Any, mine: tuple[str, int]) -> bool:
    """Whether ``scope`` is a later generation of the same log as ``mine``."""
    theirs = _split_scope(scope)
    return theirs is not None and theirs[0] == mine[0] and theirs[1] > mine[1]


__all__ = ["CHECKPOINT_DIR", "STATE_FILE", "CheckpointStore", "CompactingState"]
