"""One direct chat's on-disk record, and the handoff it owes the main agent.

A direct-chat turn is deliberately absent from the session transcript (the
whole point is to keep those exchanges out of the main agent's context), so
this directory is the only evidence it happened:

    <session_dir>/subagents/direct/<agent>/<handle>/
    |-- messages.json         resume state (raven/agent/subagent/instance_state.py)
    `-- <call_id>/            one turn: prompt.md, out.md, meta.json

Same shape as ``spawn/<call_id>/`` on purpose, so both delegation paths are
inspectable the same way and the handoff can name a turn's input and output
file without inventing a second convention.

A file name is never derived from a sub-agent's output, only from ids raven
mints itself -- the same invariant ``raven/agent/subagent/history.py`` states.
The handoff block relies on it: the block is prepended to user text, and it is
safe to leave unwrapped only because every byte in it is raven's own.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger

from raven.agent.subagent.activity import persisted_output
from raven.agent.subagent.history import add_turn_to_instance_log, make_call_id
from raven.utils.helpers import safe_path_segment

_DIRECT_DIRNAME = "direct"


def direct_root(session_dir: Path, agent: str, handle: str) -> Path:
    """Where one instance's direct chat lives. Not created here."""
    return Path(session_dir) / "subagents" / _DIRECT_DIRNAME / safe_path_segment(agent) / safe_path_segment(handle)


class DirectTurnMeta(NamedTuple):
    """One direct turn, as the handoff block needs to describe it."""

    agent: str
    handle: str
    call_id: str
    directory: Path
    started_at_ms: int
    ended_at_ms: int | None
    status: str


class DirectChatCreation(NamedTuple):
    """One instance the user made by hand, as the handoff block describes it.

    Carries no directory: nothing is written for a creation but the registry
    row, because the record directories are made per turn and no turn has run
    (see ``SubagentManager.create_instance``).
    """

    agent: str
    handle: str
    created_at_ms: int


class DirectChatError(RuntimeError):
    """A failed direct-chat turn, carrying that turn's ``DirectTurnMeta``.

    The per-session handoff list (what tells the main agent a direct chat
    happened) only ever learns about a turn from ``SubagentManager.chat``'s
    return value. A bare-raised backend exception would strand a failed
    turn's meta with nothing able to reach it, so the handoff would silently
    omit it forever. Raised chained (``from exc``) so the original
    exception's traceback still survives as ``__cause__``.
    """

    def __init__(self, meta: DirectTurnMeta) -> None:
        super().__init__(f"Direct chat turn {meta.call_id} failed (record at {meta.directory})")
        self.meta = meta


class DirectChatRecord:
    """One direct turn's record directory.

    ``open`` writes the prompt before the sub-agent is dispatched, so a turn the
    user abandons with Esc -- or one killed by a gateway restart -- still leaves
    what was asked. ``finish`` records the outcome, failures included.

    Every method swallows its own I/O errors, for the same reason
    ``SpawnRecord`` does: this is an audit trail, and losing it must never take
    down the turn it describes.
    """

    def __init__(
        self,
        directory: Path,
        *,
        agent: str,
        handle: str,
        started_at_ms: int,
        session_dir: Path | None = None,
        task: str | None = None,
    ) -> None:
        self.dir = directory
        self.agent = agent
        self.handle = handle
        self.started_at_ms = started_at_ms
        # Kept for the instance log: a direct turn is one turn of the same
        # conversation a spawn call started, and it lands in the same file.
        self.session_dir = session_dir
        self.task = task
        # What `finish` logged, for a caller that also has to hand this call's
        # conversation to everos. Empty until `finish` runs, and stays empty if
        # it returns early -- there is nothing to prime with in that case either.
        self.turn: list[dict[str, Any]] = []

    @classmethod
    def open(
        cls,
        session_dir: Path,
        *,
        agent: str,
        handle: str,
        task_id: str,
        task: str,
    ) -> "DirectChatRecord":
        started = int(time.time() * 1000)
        record = cls(
            direct_root(session_dir, agent, handle) / make_call_id(task_id),
            agent=agent,
            handle=handle,
            started_at_ms=started,
            session_dir=Path(session_dir),
            task=task,
        )
        try:
            record.dir.mkdir(parents=True, exist_ok=True)
            (record.dir / "prompt.md").write_text(task, encoding="utf-8")
            record._write_meta(
                {
                    "call_id": record.dir.name,
                    "agent": agent,
                    "handle": handle,
                    "status": "running",
                    "started_at_ms": started,
                }
            )
        except OSError as exc:
            logger.warning("Direct chat [{}] history could not be opened at {}: {}", task_id, record.dir, exc)
        return record

    def finish(self, *, status: str, output: str | None = None, error: str | None = None, activity: Any = None) -> None:
        try:
            if not self.dir.is_dir():
                return
            # The whole answer rather than the capped value handed back, for the
            # reason ``SpawnRecord.finish`` writes it that way: this directory is
            # the only evidence a direct turn happened.
            if (whole := persisted_output(activity, output)) is not None:
                (self.dir / "out.md").write_text(whole, encoding="utf-8")
            if error is not None:
                (self.dir / "error.md").write_text(error, encoding="utf-8")
            meta = self._read_meta()
            meta.update(status=status, ended_at_ms=int(time.time() * 1000))
            if activity is not None:
                meta.update(getattr(activity, "as_meta", dict)() or {})
                transcript = getattr(activity, "transcript", None)
                if isinstance(transcript, list) and transcript:
                    (self.dir / "transcript.jsonl").write_text(
                        "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in transcript),
                        encoding="utf-8",
                    )
            self._write_meta(meta)
        except OSError as exc:
            logger.warning("Direct chat history at {} could not be finished: {}", self.dir, exc)
        self.turn = add_turn_to_instance_log(
            self.session_dir,
            meta={**self._read_meta(), "agent": self.agent, "handle": self.handle},
            prompt=self.task,
            output=output,
            error=error,
            activity=activity,
            kind="direct",
        )

    def meta(self) -> DirectTurnMeta:
        """This turn as the handoff describes it, read back from disk."""
        raw = self._read_meta()
        return DirectTurnMeta(
            agent=self.agent,
            handle=self.handle,
            call_id=self.dir.name,
            directory=self.dir,
            started_at_ms=int(raw.get("started_at_ms") or self.started_at_ms),
            ended_at_ms=raw.get("ended_at_ms"),
            status=str(raw.get("status") or "running"),
        )

    def _read_meta(self) -> dict[str, Any]:
        try:
            return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# "activity" rather than "chats": the block also reports an instance the user
# created and has not spoken to, which under the narrower wording reads as a
# contradiction to the model acting on it.
_HANDOFF_HEADER = "[subagent direct chat activity since your last turn]"


def _utc(ms: int) -> str:
    """Epoch ms as UTC ISO-8601. The block is read by a model, which cannot map
    an epoch integer onto the user's sense of "just now"."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


_Entry = DirectTurnMeta | DirectChatCreation


class DirectChatHandoff:
    """The direct-chat activity a session owes its main agent, and its block.

    Held by the runtime rather than the client: the runtime is what wrote the
    records, so it is what knows which ones the main agent has not been told
    about, and the list survives a client restart. A client reads only the
    count, to draw a hint.

    An entry is appended as each direct turn lands, not on mode exit -- the user
    may enter and leave an instance repeatedly, or go straight back to the main
    conversation without leaving at all.

    An instance the user creates is an entry in its own right, not merely a line
    on a turn that follows it: the case worth reporting is precisely the one
    where nothing has been said to it yet, which no turn would carry.

    The rendered block is prepended straight to the user's own text, with no
    ``wrap_untrusted`` around it (contrast sub-agent tool output elsewhere in
    this codebase). That is safe only because every byte here is raven-minted,
    never sub-agent output: agent names come from config, handles from the
    registry, call ids from ``make_call_id``, and path segments are sanitized
    through ``safe_path_segment``. A user-created instance keeps that true: its
    handle is minted by ``mint_handle``, never typed. A future field that echoes
    a sub-agent's own reply text -- or a user-supplied handle -- would break this
    invariant and must not be added here.
    """

    def __init__(self) -> None:
        self._pending: dict[str, list[_Entry]] = {}

    def record(self, session_key: str, meta: DirectTurnMeta) -> None:
        self._pending.setdefault(session_key, []).append(meta)

    def record_created(self, session_key: str, creation: DirectChatCreation) -> None:
        self._pending.setdefault(session_key, []).append(creation)

    def pending_count(self, session_key: str) -> int:
        return len(self._pending.get(session_key, ()))

    def take(self, session_key: str) -> str | None:
        """The rendered block, clearing the list. ``None`` when nothing is pending.

        Take-and-clear is what stops one segment being reported twice. The empty
        case returns before touching anything: this runs on every ordinary user
        turn, so it has to be free when there is nothing to say.
        """
        entries = self._pending.pop(session_key, None)
        if not entries:
            return None
        return self._render(entries)

    def _render(self, entries: list["_Entry"]) -> str:
        grouped: dict[tuple[str, str], list[_Entry]] = {}
        for entry in entries:
            grouped.setdefault((entry.agent, entry.handle), []).append(entry)

        lines = [_HANDOFF_HEADER]
        for (agent, handle), group in grouped.items():
            lines.append(f"{agent} / {handle}")
            turns = [e for e in group if isinstance(e, DirectTurnMeta)]
            for creation in (e for e in group if isinstance(e, DirectChatCreation)):
                # Said explicitly, because an instance sitting unused is what the
                # main agent can act on and an absent turn line does not say it.
                unused = "" if turns else ", no turns yet"
                lines.append(f"  created by the user at {_utc(creation.created_at_ms)}{unused}")
            if not turns:
                # No turn has run, so no record directory exists to name. The
                # same reason the missing-record branch below exists.
                continue
            lines.append(f"  {self._span(turns)}")
            root = turns[0].directory.parent
            lines.append(f"  {root}{os.sep}")
            for turn in turns:
                if not turn.directory.is_dir():
                    # The record was never written (a full or read-only disk when
                    # the turn opened) -- naming prompt.md/out.md here would hand
                    # the main agent paths to files that don't exist.
                    lines.append(f"    {turn.call_id} (record unavailable)")
                    continue
                files = "{prompt.md,out.md}" if turn.ended_at_ms is not None else "prompt.md"
                lines.append(f"    {turn.call_id}/{files}")
        return "\n".join(lines)

    @staticmethod
    def _span(turns: list[DirectTurnMeta]) -> str:
        count = f"{len(turns)} turn{'s' if len(turns) != 1 else ''},"
        start = _utc(turns[0].started_at_ms)
        unlanded = [t for t in turns if t.ended_at_ms is None]
        last_end = max((t.ended_at_ms for t in turns if t.ended_at_ms is not None), default=None)
        if last_end is None:
            return f"{count} {start} (running at handoff time)"
        span = f"{count} {start} -> {_utc(last_end)}"
        return f"{span} ({len(unlanded)} running at handoff time)" if unlanded else span


__all__ = [
    "DirectChatCreation",
    "DirectChatError",
    "DirectChatHandoff",
    "DirectChatRecord",
    "DirectTurnMeta",
    "direct_root",
]
