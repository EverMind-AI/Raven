"""Session management for conversation history."""

import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from raven.utils.atomic_io import atomic_replace, locked_append
from raven.utils.helpers import ensure_dir, safe_filename


def new_chat_id(now: datetime | None = None) -> str:
    """Mint an opaque, sortable per-session chat_id: ``YYYYMMDD_HHMMSS_xxxxxx``.

    Sortable by value (timestamp prefix) and collision-safe (uuid suffix);
    channel-agnostic. Becomes the session key's chat_id segment and the JSONL
    filename stem.
    """
    ts = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class SessionResolution:
    """Outcome of resolving a user-supplied session id to a full key.

    ``status`` is one of ``"resolved"`` / ``"ambiguous"`` / ``"not_found"``.
    ``key`` carries the full ``channel:chat_id`` when resolved; ``candidates``
    carries the matching full keys when ambiguous. The no-match case is reported
    as ``not_found`` so each caller decides its own tail — the agent
    ``--session`` path mints ``cli:<value>``, while a read-only export errors.
    """

    status: str
    key: str | None = None
    candidates: tuple[str, ...] = ()


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    # ── Personalization state ─────────────────────────────────────────────────
    # Set when the agent asked a clarifying question and is waiting for the answer.
    # Structure: {"original_message": str, "question": str, "domain": str}
    # Cleared immediately after the user's answer is processed.
    pending_clarification: dict | None = field(default=None)
    # Messages already on disk; save() appends only past this index.
    _persisted_count: int = field(default=0, repr=False)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        self.record({"role": role, "content": content, **kwargs})

    def record(self, msg: dict[str, Any]) -> None:
        """Append a message dict, stamping a wall-clock timestamp.

        The single choke point for session writes — every persistence path
        (``add_message``, the agent loop's ``_save_turn``, clarification
        appends) must come through here so no message lands unstamped. A
        caller-set ``timestamp`` is preserved. Per-message ordering and
        turn grouping derive from append order and the ``role`` boundary,
        so no separate received_at / turn_id stamp is kept.
        """
        msg.setdefault("timestamp", datetime.now().isoformat())
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        out: list[dict[str, Any]] = []
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()

    def undo_last_turn(self, n: int = 1) -> int:
        """Drop the last ``n`` user-turn blocks from the unconsolidated tail.

        A turn starts at a ``role == "user"`` message and runs to the next
        user message (its assistant/tool followers inherit it). Only the
        unconsolidated tail (``messages[last_consolidated:]``) is eligible —
        content already summarized into MEMORY.md is never crossed. Returns
        the number of messages removed (0 when the tail has no user message).
        Persistence is the caller's job via ``SessionManager.save``.
        """
        if n < 1:
            return 0
        start = self.last_consolidated
        user_starts = [
            i for i in range(start, len(self.messages))
            if self.messages[i].get("role") == "user"
        ]
        if not user_starts:
            return 0
        cut_index = user_starts[-n] if n <= len(user_starts) else user_starts[0]
        removed = len(self.messages) - cut_index
        self.messages = self.messages[:cut_index]
        self.updated_at = datetime.now()
        return removed


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session: sessions/{channel}/{chat_id}.jsonl."""
        channel, _, chat_id = key.partition(":")
        return self.sessions_dir / safe_filename(channel) / f"{safe_filename(chat_id)}.jsonl"

    @staticmethod
    def key_from_path(path: Path) -> str:
        """Best-effort reverse of the nested filename encoding for a session
        file: channel is the parent directory, chat_id is the stem.

        The on-disk ``_type:metadata`` key is authoritative when present and
        wins over this; callers use it only as the fallback for metadata-less
        files. ``safe_filename`` is non-invertible, so any character it folds
        to ``_`` (``/``, ``:``, ...) is not recovered here.
        """
        return f"{path.parent.name}:{path.stem}"

    def resolve_key(self, value: str) -> SessionResolution:
        """Resolve a session id to a full ``channel:chat_id`` key across channels.

        Shared resolution core for the agent ``--session`` path and session
        export:

        - a value containing ':' is already a full key -> resolved;
        - exactly one exact chat_id match across channels -> resolved;
        - exactly one prefix match -> resolved;
        - more than one match -> ambiguous (candidate full keys);
        - no match -> not_found.

        The no-match tail is reported as ``not_found``; callers decide whether to
        mint (agent ``--session``) or error (read-only export).
        """
        if ":" in value:
            return SessionResolution("resolved", key=value)
        sessions = self.list_sessions(channel=None)
        exact = [s for s in sessions if s["key"].partition(":")[2] == value]
        matches = exact or [
            s for s in sessions if s["key"].partition(":")[2].startswith(value)
        ]
        if len(matches) > 1:
            return SessionResolution(
                "ambiguous", candidates=tuple(s["key"] for s in matches)
            )
        if matches:
            return SessionResolution("resolved", key=matches[0]["key"])
        return SessionResolution("not_found")

    def find_most_recent_chat_id(self, channel: str) -> str | None:
        """Return the chat_id of the most-recently-updated session on this
        channel, or None if no such session exists.

        Used by cron delivery at trigger time to auto-resolve where to
        forward ephemeral (cli / tui) reminders, so users don't need to
        know their own open_id / chat_id on the target channel.

        Reads each candidate file's metadata line (first line of the JSONL)
        to get the authoritative session key ``<channel>:<chat_id>`` and
        ``updated_at``; recency is decided by ``updated_at``, falling back
        to file mtime for files that lack it.
        """
        channel_dir = self.sessions_dir / safe_filename(channel)
        if not channel_dir.is_dir():
            return None

        best_chat_id: str | None = None
        best_updated = ""
        for p in channel_dir.glob("*.jsonl"):
            meta, _count = self._scan_file(p)
            if meta is None:
                continue
            key_val = meta.get("key", "")
            if ":" not in key_val:
                continue
            ch, chat_id = key_val.split(":", 1)
            if ch != channel or not chat_id:
                continue
            updated = meta.get("updated_at")
            if not isinstance(updated, str) or not updated:
                try:
                    updated = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
                except OSError:
                    continue
            if updated > best_updated:
                best_chat_id = chat_id
                best_updated = updated
        return best_chat_id

    @staticmethod
    def _scan_file(path: Path) -> tuple[dict[str, Any] | None, int]:
        """Single pass over a session file: return (last metadata record,
        message line count).

        One metadata record is appended per save, so the last reflects
        current state. Message lines are counted without keeping them in
        memory.
        """
        meta: dict[str, Any] | None = None
        count = 0
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get("_type") == "metadata":
                        meta = data
                    else:
                        count += 1
        except OSError:
            return None, 0
        return meta, count

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0
            pending_clarification = None

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Partial trailing line from a crashed append.
                        logger.debug("Skipping undecodable line in session {}", key)
                        continue

                    # Metadata records are appended per save; last one wins.
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                        pending_clarification = data.get("pending_clarification")
                    else:
                        messages.append(data)

            session = Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
                pending_clarification=pending_clarification,
            )
            session._persisted_count = len(messages)
            return session
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk.

        Appends a fresh metadata record plus the not-yet-persisted messages
        under a cross-process lock, so concurrent writers never lose each
        other's turns and a turn's messages stay contiguous. A shrunken
        message list (clear) rewrites the file atomically instead.
        """
        path = self._get_session_path(session.key)

        channel, _, chat_id = session.key.partition(":")
        reserved = {
            "source": None,
            "channel": channel,
            "chat_id": chat_id,
            "title": None,
            "parent_session_id": None,
        }
        session.metadata = {**reserved, **session.metadata}

        metadata_line = json.dumps({
            "_type": "metadata",
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "last_consolidated": session.last_consolidated,
            # Personalization: persist clarification wait-state across restarts
            "pending_clarification": session.pending_clarification,
        }, ensure_ascii=False)

        if len(session.messages) < session._persisted_count:
            lines = [metadata_line]
            lines += [json.dumps(m, ensure_ascii=False) for m in session.messages]
            atomic_replace(path, "".join(line + "\n" for line in lines))
        else:
            new_messages = session.messages[session._persisted_count:]
            lines = [metadata_line]
            lines += [json.dumps(m, ensure_ascii=False) for m in new_messages]
            locked_append(path, lines)

        session._persisted_count = len(session.messages)
        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def delete(self, key: str) -> bool:
        """Remove the session file and invalidate the cache entry.

        Returns True only if a file was actually removed; False if no file
        existed or the removal failed. Deleting an unknown key is a safe no-op.
        """
        path = self._get_session_path(key)
        self.invalidate(key)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                logger.warning("session.delete: failed to remove file for {}", key)
                return False
            return True
        return False

    def exists(self, key: str) -> bool:
        """Return True if the session has a file on disk (lazy sessions don't)."""
        return self._get_session_path(key).exists()

    def peek(self, key: str) -> "Session | None":
        """Return the cached session if present; else load from disk without caching.

        Callers that need read-only access to a session should use this instead
        of get_or_create, which would cache a fresh empty session for unknown keys.
        """
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        return self._load(key)

    def fork(self, source_key: str, *, title: str | None = None) -> "Session | None":
        """Fork ``source_key`` at its head into a new diverging child session.

        Full-copy semantics: the child is minted with a fresh chat_id on the
        source's channel, a deep copy of the source's messages, and
        ``parent_session_id`` set to the source key (the reserved lineage slot).
        The child inherits ``last_consolidated`` (so its active-context window
        matches the source at the fork point) and resets ``pending_clarification``
        (interaction wait-state is not history). The child is persisted eagerly.

        Returns the persisted child, or None when the source does not exist or
        has zero messages (a fork of an empty session has no value).
        """
        source = self.peek(source_key)
        if source is None or not source.messages:
            return None

        channel = source_key.partition(":")[0]
        child = Session(
            key=f"{channel}:{new_chat_id()}",
            messages=copy.deepcopy(source.messages),
            last_consolidated=source.last_consolidated,
        )
        if title is not None:
            child.metadata["title"] = title
        else:
            parent_title = (source.metadata or {}).get("title")
            if parent_title:
                child.metadata["title"] = f"{parent_title} (fork)"
        child.metadata["parent_session_id"] = source_key
        self.save(child)
        return child

    def flush(self, key: str) -> bool:
        """Save the cached session iff it has unpersisted messages.

        Uses the _persisted_count dirty check. Returns False only when a save
        was attempted and failed (the failure is swallowed); True otherwise,
        including the no-op cases (key not cached / no new messages).
        """
        cached = self._cache.get(key)
        if cached is None:
            return True
        if len(cached.messages) > cached._persisted_count:
            try:
                self.save(cached)
            except Exception:
                logger.warning("flush: failed to persist session {}", key)
                return False
        return True

    def list_sessions(self, channel: str | None = None) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by channel.

        Each entry carries: key, created_at, updated_at, path, message_count.
        Sorted by updated_at descending. Each file is read in a single pass.
        """
        sessions = []

        for path in self.sessions_dir.glob("*/*.jsonl"):
            if channel is not None and path.parent.name != channel:
                continue
            data, message_count = self._scan_file(path)
            if data is None:
                continue
            key = data.get("key") or self.key_from_path(path)
            sessions.append({
                "key": key,
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "path": str(path),
                "message_count": message_count,
                "metadata": data.get("metadata", {}),
            })

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
