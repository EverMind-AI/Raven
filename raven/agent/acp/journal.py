"""Every frame of one ACP connection, in wire order, on disk.

The turn collector in :mod:`raven.agent.subagent.backends.acp_agent` records the
notifications routed to one session. That is what a reader of a delegated run
wants, and it is not the same thing as what crossed the wire. Four classes of
traffic never reached it, each invisible in a different way:

- **The agent's own requests.** ``session/request_permission`` is answered
  automatically and unattended (see :mod:`raven.agent.acp.permissions`), so
  nothing anywhere could say what raven approved on a sub-agent's behalf.
- **Raven's outbound frames.** Which session was resumed, whether a cancel was
  sent, what the prompt actually was.
- **Notifications for a session no longer attached.** The router drops them with
  a debug line; a late ``usage_update`` after a turn settles is the ordinary
  case, and there was no record it ever arrived.
- **stderr.** An adapter has been measured reporting a fatal provider error
  there while the protocol still answered ``stopReason: end_turn``.

A journal is per connection rather than per call because an ACP session is: one
process serves every session of one agent, and the ``initialize`` handshake that
opens it belongs to no single call. A call names the byte range it occupied
instead, so its own record still points at exactly its own traffic.

On disk rather than in memory because "complete" and "bounded" cannot both hold
for a list -- a connection lives as long as the raven process, and one turn's
frames can run to megabytes. Bounded here means a byte ceiling per connection,
and reaching it is written down (:data:`_TRUNCATED`) rather than left to look
like a connection that went quiet.

Nothing here may fail a run: every write is best-effort, and a journal that
cannot open its file downgrades to recording nothing rather than raising into
the read loop.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

from loguru import logger

_OFF = {"0", "false", "off", "no"}

MAX_BYTES = 64 * 1024 * 1024
"""Ceiling per connection. Generous because the file is the audit record and a
truncated one answers fewer questions than a large one; bounded because a
long-lived connection would otherwise grow without limit."""

KEEP_DAYS = 7
"""How many days of journals survive. The existing artifact store has no reclaim
policy at all and had grown to gigabytes on a developer machine, so this one
states a policy rather than inheriting that."""

_TRUNCATED = "truncated"
_DIR_NAME = "acp-frames"

FRAME = "acp_frame"
CALL = "acp_call"
MARKER = "acp_marker"
"""Record discriminators, in the shape a raven session transcript already uses.

The session log at ``sessions/<group>/<chat_id>.jsonl`` tags its header record
``_type: "metadata"`` and leaves message rows untagged, so a ``_type`` on every
frame record puts these files in the same grammar as the conversation they
belong to -- one reader, one time key -- without ever claiming a frame is a
message. It is not the same file: nothing feeds a frame log to
``SessionManager``, whose loader treats an unknown ``_type`` as a message rather
than skipping it."""


def enabled() -> bool:
    """On unless ``RAVEN_ACP_JOURNAL`` says otherwise.

    Deliberately not tied to ``[tracing].enabled``: the frames answer what raven
    approved on a sub-agent's behalf, which is a safety record rather than a
    performance trace, and turning off tracing should not silently retire it.
    """
    env = os.environ.get("RAVEN_ACP_JOURNAL")
    return env.strip().lower() not in _OFF if env is not None else True


def _safe(value: str) -> str:
    keep = [c if c.isalnum() or c in "._-" else "-" for c in value.strip()]
    return ("".join(keep).strip("-") or "agent")[:60]


def journal_root() -> Path:
    """Where connection journals live: ``<tracing state>/logs/acp-frames``."""
    from raven.tracing import config

    return config.state_dir() / "logs" / _DIR_NAME


def prune(root: Path, keep_days: int = KEEP_DAYS) -> None:
    """Drop journal day-directories older than ``keep_days``. Best-effort."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    try:
        days = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return
    for day in days:
        if day.name < cutoff:
            try:
                shutil.rmtree(day, ignore_errors=True)
            except OSError as exc:  # pragma: no cover - rmtree already swallows
                logger.debug("acp journal: could not prune {}: {}", day, exc)


class FrameJournal:
    """One connection's wire log. Append-only, newest last, never raises."""

    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES) -> None:
        self.path = path
        self._max_bytes = max_bytes
        self._offset = 0
        self._handle: TextIO | None = None
        self._closed = False
        self._full = False

    @property
    def offset(self) -> int:
        """Bytes written so far -- the cursor a call marks its range with."""
        return self._offset

    def _open(self) -> TextIO | None:
        if self._handle is not None or self._closed:
            return self._handle
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 0600 before the first byte, not after: the frames carry the whole
            # prompt and every tool result, so the window where the file exists
            # world-readable must not exist at all.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._handle = os.fdopen(fd, "a", buffering=1, encoding="utf-8")
        except OSError as exc:
            logger.warning("acp journal: {} could not be opened ({}); frames go unrecorded", self.path, exc)
            self._closed = True
            return None
        return self._handle

    def note(
        self,
        direction: str,
        *,
        frame: dict[str, Any] | None = None,
        text: str | None = None,
        session: str | None = None,
    ) -> None:
        """Record one frame, one stderr line, or one unparseable line.

        Called from the connection's read loop and from whichever task is
        sending, so it is synchronous and short: one ``json.dumps`` and one
        line-buffered write, which flushes on the newline so a killed process
        keeps everything up to its last complete line.
        """
        if self._closed or self._full:
            return
        # `timestamp`, naive and local, because that is what the session log and
        # `transcript.jsonl` beside it both use -- an audit file would rather
        # have UTC, but a reader that has to branch on two time formats is the
        # worse trade.
        record: dict[str, Any] = {"_type": FRAME, "timestamp": datetime.now().isoformat(), "dir": direction}
        if session:
            record["session"] = session
        if frame is not None:
            record["frame"] = frame
        if text is not None:
            record["text"] = text
        self._write(record)

    def bind(self, identity: dict[str, Any]) -> None:
        """Name whose call the frames that follow belong to.

        ACP carries no room for raven's own identity: ``session/new`` takes a cwd
        and ``session/prompt`` a session id, and the session id is the agent's to
        mint. So the only thing tying a frame to a raven instance is what the
        dispatcher knew when it opened the session, and this is where it is
        written down. Without it a journal -- which is per connection, and
        therefore spans conversations and instances -- can only be read by
        joining its session ids against spans or every meta.json on the host,
        and for a stateless agent, which never registers an instance row, that
        join has no other side.
        """
        if identity:
            self._write({"_type": CALL, "timestamp": datetime.now().isoformat(), **dict(identity)})

    def _write(self, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        except (TypeError, ValueError) as exc:  # pragma: no cover - default=str covers the wire shapes
            logger.debug("acp journal: unserialisable record dropped ({})", exc)
            return
        marker = json.dumps({"_type": MARKER, _TRUNCATED: self._max_bytes}, ensure_ascii=False) + "\n"
        size = len(line.encode("utf-8"))
        # The marker is budgeted before the frame is, so the file never exceeds
        # the ceiling it reports -- the same order the partial-reply notice uses,
        # and for the same reason: the one line saying the record stops here must
        # not be the part that does not fit.
        if self._offset + size > self._max_bytes - len(marker.encode("utf-8")):
            self._full = True
            self._append(marker)
            logger.warning(
                "acp journal: {} reached {} bytes; later frames are not recorded", self.path, self._max_bytes
            )
            return
        self._append(line)

    def _append(self, line: str) -> None:
        handle = self._open()
        if handle is None:
            return
        try:
            handle.write(line)
        except OSError as exc:
            logger.warning("acp journal: write to {} failed ({}); frames go unrecorded", self.path, exc)
            self._closed = True
            return
        self._offset += len(line.encode("utf-8"))

    def close(self) -> None:
        """Close the file. Idempotent; the offset stays readable afterwards."""
        self._closed = True
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def open_journal(agent: str, *, root: Path | None = None) -> FrameJournal | None:
    """A journal for one connection of ``agent``, or ``None`` when disabled.

    The file is named for the agent and the wall clock rather than the pid: a
    pid is recycled, and two connections of one agent in a single day are told
    apart by the time they were opened.
    """
    if not enabled():
        return None
    base = root if root is not None else journal_root()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prune(base)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S") + f"{time.monotonic_ns() % 1000000:06d}"
    return FrameJournal(base / day / f"{_safe(agent)}-{stamp}.jsonl")


__all__ = [
    "CALL",
    "FRAME",
    "MARKER",
    "KEEP_DAYS",
    "MAX_BYTES",
    "FrameJournal",
    "enabled",
    "journal_root",
    "open_journal",
    "prune",
]
