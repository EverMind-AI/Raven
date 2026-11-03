"""Session management for conversation history."""

import copy
import json
import os
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from raven.contracts.context import SessionRevision
from raven.session.title import TITLE_STORAGE_MAX, collapse_to_line
from raven.session.turn_shape import replied_via_message_tool
from raven.utils.atomic_io import atomic_replace, locked_append, write_transaction
from raven.utils.paths import ensure_dir, safe_filename, safe_path_segment

# Channel for subagent transcripts. Defined here, not in the subagent package,
# because this module has to know which sessions to keep out of an unfiltered
# list and must not import upward to learn it.
#
# The filter exists before any writer does, and that ordering is the point:
# a session store that starts collecting `sub:` files
# before anything knows to exclude them puts machine-generated transcripts in
# front of every human-facing picker.
SUBAGENT_CHANNEL = "sub"

# Key prefix of an Imported Conversation (raven/trajectory/imported.py). Those
# live under the raven home, never in this store: a save under this channel is
# refused, so nothing that resolves a key to a file here can give an imported
# conversation a writable twin that pickers, sweeps and delivery targets see.
IMPORTED_CHANNEL = "imported"

# Metadata keys the loop stamps on a session with the message index a cut turn
# starts at: one stopped at the model's output ceiling, one its own budget ended
# (the tool cap, the wall clock or a stalled tool). ACP reads them to refine a
# prompt's ``end_turn``; clearing the session's history forgets them.
OUTPUT_LIMIT_TURN_AT = "output_limit_turn_at"
BUDGET_STOP_TURN_AT = "budget_stop_turn_at"
TURN_MARKERS = (OUTPUT_LIMIT_TURN_AT, BUDGET_STOP_TURN_AT)


def _refuse_imported(key: str) -> None:
    if key.partition(":")[0] == IMPORTED_CHANNEL:
        raise ValueError(f"{key!r} is an imported conversation; the session store does not keep it")


# How many sessions a manager keeps loaded once nothing else holds them. Soft:
# a session with unsaved state is never dropped from the cache, and one a caller
# still holds stays reachable through ``SessionManager._held`` whatever this says.
# A change made through a reference to a session that has already left the cache,
# then released unsaved, is lost, as it would be in a fresh process.
_CACHE_LIMIT = 64


def _message_text(content: Any, cap: int = 120) -> str:
    """Flatten a message's content to one line of plain text (capped).

    Content is either a string or a list of blocks whose text blocks carry
    a ``text`` field; anything else contributes nothing.
    """
    if isinstance(content, list):
        content = " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") in (None, "text")
        )
    text = " ".join(str(content or "").split())
    return text[:cap]


def _stored_key(path: Path) -> str | None:
    """The session key a transcript claims, or None when it claims none.

    Reads the leading metadata record only. ``session_path`` asks this of files
    it is deciding between, so it runs on the way to opening one rather than
    after, and the whole transcript is the wrong amount to read to learn its
    first line. A file whose head is missing, unreadable, or keyless answers
    None: absent is not the same as belonging to somebody else, and the caller
    treats the two differently.
    """
    try:
        with open(path, encoding="utf-8") as f:
            head = f.readline()
    except OSError:
        return None
    try:
        data = json.loads(head)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("_type") != "metadata":
        return None
    stored = data.get("key")
    return stored if isinstance(stored, str) and stored else None


def new_chat_id(now: datetime | None = None) -> str:
    """Mint an opaque, sortable per-session chat_id: ``YYYYMMDD_HHMMSS_xxxxxx``.

    Sortable by value (timestamp prefix) and collision-safe (uuid suffix);
    channel-agnostic. Becomes the session key's chat_id segment and the JSONL
    filename stem.
    """
    ts = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:6]}"


_AUTO_TITLE_MAX_CHARS = 40


def derive_title(content: Any) -> str | None:
    """Derive an auto-title from message content: first non-empty line,
    whitespace collapsed, truncated to 40 characters. None when the
    content is not a usable string (e.g. structured multimodal parts).

    Public because a sub-agent instance is named by the same rule: an instance
    the reader started themselves has no dispatch to take a name from, and one
    named differently from a conversation would read as a different kind of
    thing on a screen that shows both."""
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    collapsed = " ".join(stripped.splitlines()[0].split())
    return collapsed[:_AUTO_TITLE_MAX_CHARS] or None


def _first_user_auto_title(messages: list[dict[str, Any]]) -> str | None:
    for m in messages:
        if m.get("role") == "user":
            return derive_title(m.get("content"))
    return None


_FORK_SUFFIX = " (fork)"


def _fork_title(parent_title: str) -> str:
    """Name a fork after its parent, short enough for ``set_title`` to accept.

    Nobody typed this name, so the refusal ``set_title`` gives an over-long
    human title is the wrong tail here: there is no author to hand the text back
    to, and the fork would land nameless -- the very hole that inheriting the
    parent's name was added to close. The parent's part gives way instead, and
    only for a parent already within the suffix's width of the storage ceiling.
    The suffix is what makes this a name for the fork rather than for what it was
    forked from, so it is the part kept whole.
    """
    collapsed = collapse_to_line(parent_title)
    room = TITLE_STORAGE_MAX - len(_FORK_SUFFIX)
    if len(collapsed) > room:
        logger.debug("fork: parent title cut to {} chars to leave room for the fork suffix", room)
        collapsed = collapsed[:room].rstrip()
    return collapsed + _FORK_SUFFIX


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


def _stamp(path: Path) -> tuple[int, int] | None:
    """A transcript's (size, mtime_ns), or None when it is not there.

    What a saved copy compares itself against to know whether anybody else has
    written to the file since it last read or wrote it.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


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
    # Bumped by every truncation. A background reader that sliced this list and
    # then awaited cannot tell from the indices alone that they now point at
    # other messages: a cut below a cursor leaves that cursor's value alone,
    # and messages arriving afterwards can take the length back past where it
    # was. In process only -- a task does not outlive the process it ran in.
    truncations: int = field(default=0, repr=False)
    log_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    """Which logical log ``messages`` is. A clear (``/new``) mints a new one."""
    log_generation: int = 0
    """Bumped by every mutation that makes an ordinal name a different message.

    ``session.messages`` is append-only *within* a generation, which is what
    lets a ``[[ref ...]]`` name a message by its index. An undo breaks that:
    the list shrinks and later messages take indices that older refs already
    named. Folding this counter into the scope makes a ref minted before an
    undo resolve to nothing rather than to whatever took its place.
    """
    log_started_at: str | None = None
    """When the clear that minted ``log_id`` happened; None for an id minted
    with the session or on loading a file that had none. It is what decides
    which of two writers' ids survives a save: the most recent clear wins."""
    # ── Personalization state ─────────────────────────────────────────────────
    # Set when the agent asked a clarifying question and is waiting for the answer.
    # Structure: {"original_message": str, "question": str, "domain": str}
    # Cleared immediately after the user's answer is processed.
    pending_clarification: dict | None = field(default=None)
    # Messages already on disk; save() appends only past this index.
    _persisted_count: int = field(default=0, repr=False)
    # ``truncations`` when this copy last read or wrote the file. A cut since then
    # leaves the file holding the messages that were cut, which only a rewrite
    # drops, however far the list has grown back.
    _persisted_truncations: int = field(default=0, repr=False)
    # What the transcript looked like the last time this copy read or wrote it,
    # as (size, mtime_ns). save() compares it to decide whether anybody else
    # has written since, and only then re-reads the record to merge under.
    _file_stamp: tuple[int, int] | None = field(default=None, repr=False)
    # The metadata as this copy last read it from the file or wrote it there.
    # What ``metadata`` says that this does not is what this copy changed, and
    # only that may outrank a record written since -- the snapshot itself is
    # every key the file held at load, most of which this copy never meant.
    _persisted_metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    # (log_id, log_generation) as this copy last read it from the file or wrote
    # it there; None while the file has never held one this copy saw.
    _persisted_revision: tuple[str, int] | None = field(default=None, repr=False)
    # ``_fingerprint`` of this session as last read, written or minted. While it
    # and the message count still match, nothing is unsaved and the session may
    # leave the cache. None where one could not be made, which never matches.
    _clean_fingerprint: str | None = field(default=None, repr=False)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        self.record({"role": role, "content": content, **kwargs})

    def set_title(self, title: str) -> None:
        """Set a human-given title.

        Clears the ``title_auto`` marker, which is what keeps a generated title
        from replacing this one later -- ``set_generated_title`` declines against
        a title no marker calls machine-made. Every rename path must come through
        here, not assign metadata directly. The marker no longer decides what a
        fork inherits: a fork carries its parent's name whatever made it.

        Collapsed to one line -- a metadata record is one JSON line and every
        surface renders a title on one row, so an embedded newline has nowhere
        to go. Past ``TITLE_STORAGE_MAX`` the title is refused rather than
        truncated: a person typed this, and quietly storing the first 200
        characters hands them back a fragment they never wrote with nothing
        saying why. How a stored title *fits* a row is the front end's business.
        """
        cleaned = collapse_to_line(title)
        if len(cleaned) > TITLE_STORAGE_MAX:
            raise ValueError(f"session title is {len(cleaned)} characters; the maximum is {TITLE_STORAGE_MAX}")
        self.metadata["title"] = cleaned
        self.metadata.pop("title_auto", None)

    def set_generated_title(self, title: str) -> bool:
        """Record a machine-made title. False when it was declined.

        Stamped ``title_auto`` like the one ``save`` derives: both are machine
        titles, and the marker is what lets a later *generation* replace them,
        where a human title is declined below. A rename does not consult it --
        ``set_title`` overwrites either kind and clears the marker on the way
        through. Nor does it say anything about what a fork inherits; a fork
        carries its parent's name whatever made it, and takes no marker with it.

        Declined when a person has already named the session: the call that
        produced this ran concurrently with the turn, so a rename typed while it
        was in flight is the newer intent and has to win.
        """
        if self.metadata.get("title") and not self.metadata.get("title_auto"):
            return False
        cleaned = collapse_to_line(title)
        if not cleaned or len(cleaned) > TITLE_STORAGE_MAX:
            return False
        self.metadata["title"] = cleaned
        self.metadata["title_auto"] = True
        return True

    def record(self, msg: dict[str, Any]) -> None:
        """Append a message dict, stamping a wall-clock timestamp.

        The single choke point for session writes — every persistence path
        (``add_message``, the agent loop's ``_save_turn``, clarification
        appends) must come through here so no message lands unstamped. A
        caller-set ``timestamp`` is preserved. Per-message ordering and
        turn grouping derive from append order and the ``role`` boundary;
        the only per-turn stamp is the ``trace_id`` the agent loop puts on a
        turn's opening user message while tracing is on, which lets a
        selection of turns find each turn's messages in the record.
        """
        msg.setdefault("timestamp", datetime.now().isoformat())
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @property
    def revision(self) -> SessionRevision:
        """Identity and high-water mark of this log view, the scope refs into it carry."""
        return SessionRevision(log_id=self.log_id, generation=self.log_generation)

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated :]
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
        # The episodic cursor indexes into ``messages``; left at its old value
        # it would swallow every turn written after the clear until the count
        # climbed past it. Stated as zero rather than dropped, per the removal
        # rule in ``SessionManager._metadata_to_write``: a key this copy merely
        # omits comes back on the next save.
        self.metadata["annotated_through"] = 0
        self._forget_turn_markers(0)
        self.truncations += 1
        self.log_id = uuid.uuid4().hex
        self.log_generation = 0
        self.log_started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        self.updated_at = datetime.now()

    def _forget_turn_markers(self, from_index: int) -> None:
        """Clear the turn markers whose turn starts at ``from_index`` or later.

        The loop stamps a cut turn with the index its rows start at
        (:data:`TURN_MARKERS`) and ACP reports a
        prompt as cut when one sits at or past where that prompt began. Once the
        messages from ``from_index`` are gone, the next prompt begins there, and
        a command that writes no marker of its own would be reported cut by a
        turn that no longer exists. Cleared as None rather than dropped, per the
        removal rule in ``SessionManager._metadata_to_write``.
        """
        for key in TURN_MARKERS:
            at = self.metadata.get(key)
            if isinstance(at, int) and at >= from_index:
                self.metadata[key] = None

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
        user_starts = [i for i in range(start, len(self.messages)) if self.messages[i].get("role") == "user"]
        if not user_starts:
            return 0
        cut_index = user_starts[-n] if n <= len(user_starts) else user_starts[0]
        removed = len(self.messages) - cut_index
        self.messages = self.messages[:cut_index]
        cursor = self.metadata.get("annotated_through")
        if isinstance(cursor, int) and cursor > cut_index:
            self.metadata["annotated_through"] = cut_index
        self._forget_turn_markers(cut_index)
        self.truncations += 1
        self.log_generation += 1
        self.updated_at = datetime.now()
        return removed


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _fingerprint(session: Session, *, leaving: Collection[str] = ()) -> str | None:
    """What a save writes besides the messages, as one comparable string; None where it cannot be made.

    A string rather than a copy: ``_persisted_metadata`` is a shallow copy, so a
    nested value changed in place would read the same on both sides of it. A dict
    whose keys mix types is something a save writes but ``sort_keys`` cannot order,
    and such a session reads as unsaved here rather than failing a save or the eviction
    loop. ``leaving`` names metadata keys to leave out.
    """
    try:
        metadata = {k: v for k, v in session.metadata.items() if k not in leaving} if leaving else session.metadata
        return _canonical([metadata, session.last_consolidated, session.pending_clarification])
    except (TypeError, ValueError, RecursionError):
        return None


def _saved_time(value: Any) -> datetime | None:
    """A timestamp a save wrote, as the naive local time ``datetime.now()`` gives.

    None where it is missing or unreadable. Unreadable is not fatal: a hand-edited
    stamp must not make the whole session fail to load.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        # Converting can fail too: a stamp near either end of the calendar leaves
        # it once moved to local time.
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except (ValueError, OverflowError, OSError):
        return None


def _stored_revision(record: dict[str, Any]) -> tuple[str, int, str | None] | None:
    """The (log_id, log_generation, log_started_at) a metadata record holds; None when it holds no valid one."""
    log_id = record.get("log_id")
    generation = record.get("log_generation")
    try:
        revision = SessionRevision(log_id, generation)
    except ValueError:
        return None
    started = record.get("log_started_at")
    return revision.log_id, revision.generation, started if isinstance(started, str) and started else None


def _started(value: str | None) -> datetime:
    """A ``log_started_at`` as an aware UTC time; the earliest time when missing or unreadable.

    Written in UTC; a naive value is read as local time.
    """
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _merge_revision(session: Session, disk: tuple[str, int, str | None] | None) -> None:
    """Settle ``session``'s revision against the one another writer left on disk.

    ``disk`` is that revision, or None when nobody wrote one since this copy
    last read or wrote the file. The log id follows the most recent clear -- an
    id minted with the session or on load never outranks one already on disk --
    and on a shared id the generation is the larger of the two. Disagreement is
    resolved, never raised: a save that fails loses the turn it was saving.
    """
    if disk is None:
        return
    disk_id, disk_generation, disk_started = disk
    if disk_id != session.log_id:
        if _started(session.log_started_at) > _started(disk_started):
            return
        session.log_id, session.log_started_at = disk_id, disk_started
        session.log_generation = disk_generation
    else:
        session.log_generation = max(disk_generation, session.log_generation)


def _move_past(session: Session, *used: tuple[str, int] | None) -> None:
    """Move the generation past every ``used`` scope of this log id.

    For a save whose file will no longer hold what those scopes named: each was
    already handed out -- by this copy or by another writer -- for messages at
    ordinals that now hold something else, and a scope must never name two
    different messages.
    """
    for revision in used:
        if revision is not None and revision[0] == session.log_id and session.log_generation <= revision[1]:
            session.log_generation = revision[1] + 1


def _normalized(messages: list[dict[str, Any]]) -> list[Any]:
    """Messages as a reader of the file would get them back."""
    return [json.loads(json.dumps(m, ensure_ascii=False)) for m in messages]


def _legacy_log_id(key: str, created_at: Any, message_count: int) -> str:
    """The log id of a file that stores none.

    Derived from what identifies that file's log rather than drawn at random, so
    every reader of the same unsaved legacy file names it the same way: a ref
    minted against one loaded copy still resolves against another, and two
    processes that load it converge on one id.
    """
    if not isinstance(created_at, str) or not created_at:
        return uuid.uuid4().hex
    return uuid.uuid5(uuid.NAMESPACE_URL, f"raven-session-log:{key}:{created_at}:{message_count}").hex


def read_session_file(path: Path, key: str) -> Session | None:
    """The session stored in the transcript at ``path``, read as ``key``.

    None when the file is absent or cannot be read. Public because a transcript
    kept outside the session store -- an Imported Conversation's -- is read by
    the same rules: metadata records are appended per save and the last one
    wins.
    """
    if not path.exists():
        return None

    try:
        messages = []
        metadata = {}
        created_at = None
        updated_at = None
        last_consolidated = 0
        pending_clarification = None
        revision: tuple[str, int, str | None] | None = None
        created_raw = None

        with open(path, encoding="utf-8") as f:
            # Of the file this read, taken before reading it: a write that lands
            # while the file is read (or replaces it) leaves the stamp behind the
            # file, so the next save re-reads and merges rather than writing over
            # a revision this copy never saw.
            st = os.fstat(f.fileno())
            file_stamp = (st.st_size, st.st_mtime_ns)
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
                    updated_at = data.get("updated_at")
                    last_consolidated = data.get("last_consolidated", 0)
                    pending_clarification = data.get("pending_clarification")
                    revision = _stored_revision(data)
                    created_raw = data.get("created_at")
                else:
                    messages.append(data)

        session = Session(
            key=key,
            messages=messages,
            created_at=created_at or datetime.now(),
            updated_at=_saved_time(updated_at) or datetime.now(),
            metadata=metadata,
            last_consolidated=last_consolidated,
            pending_clarification=pending_clarification,
        )
        # A file written before revisions existed, or whose latest record holds
        # an unreadable one, gets a derived id that the next save writes. An
        # older valid record is not reused: the latest record is the one that
        # speaks for the file.
        if revision is not None:
            session.log_id, session.log_generation, session.log_started_at = revision
            session._persisted_revision = revision[:2]
        else:
            session.log_id = _legacy_log_id(key, created_raw, len(messages))
        session._persisted_count = len(messages)
        session._file_stamp = file_stamp
        session._persisted_metadata = dict(metadata)
        return session
    except Exception as e:
        logger.warning("Failed to load session {}: {}", key, e)
        return None


def _cut_since_persisted(session: Session) -> bool:
    return session.truncations != session._persisted_truncations


def _is_clean(session: Session) -> bool:
    if len(session.messages) != session._persisted_count or _cut_since_persisted(session):
        return False
    current = _fingerprint(session)
    # None is "could not tell", never "unchanged": two failures must not read as a match.
    return current is not None and session._clean_fingerprint == current


def _unchanged_but_for(session: Session, keys: Collection[str]) -> bool:
    """Whether what a save writes besides the messages is as last read or written.

    The metadata under ``keys`` is left out on both sides.
    """
    if session._clean_fingerprint is None:
        return False
    metadata, *rest = json.loads(session._clean_fingerprint)
    current = _fingerprint(session, leaving=keys)
    return current is not None and current == _canonical([{k: v for k, v in metadata.items() if k not in keys}, *rest])


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path, project_slug: str | None = None, project_dir: Path | None = None):
        """
        Args:
            workspace: Agent home; sessions live under ``<workspace>/sessions``.
            project_slug: Groups this process's sessions under one directory
                (``raven tui`` / ``raven agent`` pass the slugged launch
                directory, so a project's conversations stay together). Leave
                unset on the gateway, where the grouping directory is the
                channel name instead -- one daemon serves every project, so a
                launch directory would say nothing about a conversation.
            project_dir: The unslugged directory behind ``project_slug``,
                stamped into each new session's metadata. The slug is lossy, so
                this is what identifies the project; the directory name is only
                a bucket.
        """
        self.workspace = workspace
        self.project_slug = project_slug
        self.project_dir = project_dir
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self._cache: OrderedDict[str, Session] = OrderedDict()
        # Every session handed out and still referenced somewhere, so one that
        # left ``_cache`` while a turn held it comes back as that same object
        # rather than as a second copy loaded from disk.
        self._held: weakref.WeakValueDictionary[str, Session] = weakref.WeakValueDictionary()
        self._delete_observers: tuple = ()

    def set_delete_observers(self, observers: tuple) -> None:
        """Replace the whole deletion-observer tuple. A START-phase verb owned
        by the resident host: attach at service start, detach with ``()`` at
        stop; assembly never calls it (paper: contracts/session_events.py)."""
        self._delete_observers = tuple(observers)

    def _group_dir(self, key: str) -> Path:
        """The directory grouping this session: project slug, or channel.

        An empty channel falls back to ``_`` rather than the empty string, which
        would collapse to ``sessions_dir`` itself -- putting the transcript at
        ``sessions/.jsonl``, where the ``*/*.jsonl`` scans cannot see it.
        """
        channel = key.partition(":")[0]
        return self.sessions_dir / (self.project_slug or safe_path_segment(channel) or "_")

    def session_path(self, key: str) -> Path:
        """The file path for a session: ``sessions/<group>/<chat_id>.jsonl``.

        A session opened before this process's grouping applied -- a transcript
        still under its channel directory, from before project grouping, or
        from a run started in a different directory -- keeps its existing file.
        Without that, resuming by id would silently open an empty session next
        to the real transcript rather than continuing it.

        The search runs whatever this process groups by, gateway included. Its
        group is the channel name, so a conversation started by a terminal
        entrypoint sits under a slug it never looks in -- yet ``list_sessions``
        scans every group, so the gateway offers that conversation and then
        could not open it. Worse than the empty transcript that produced: the
        write verbs resolve through here too, so a rename or a turn from the
        page filed a second transcript under the channel and split the
        conversation across two files.

        A candidate is adopted only once its own metadata says it is this
        session. The glob matches on the chat_id alone, and the chat_id does not
        name a channel: ``cli:<id>`` and ``tui:<id>`` are two conversations, and
        taking the first file with the right stem handed one of them the other's
        transcript to append to. The stored key is what tells them apart. A file
        that carries no key is adopted regardless -- it predates the field, and
        the stem is the only identity it has.
        """
        chat_id = safe_filename(key.partition(":")[2])
        path = self._group_dir(key) / f"{chat_id}.jsonl"
        if not path.exists():
            for existing in sorted(self.sessions_dir.glob(f"*/{chat_id}.jsonl")):
                stored = _stored_key(existing)
                if stored is None or stored == key:
                    return existing
        return path

    def session_dir(self, key: str) -> Path:
        """This session's metadata directory, beside its transcript.

        ``sessions/<group>/<chat_id>/`` holds what belongs to the conversation
        but not in its message log -- the sub-agent call history today. Named
        the same as the transcript minus the suffix, so the pair sits together
        and neither the ``*.jsonl`` globs nor this directory sees the other.

        The group comes from :meth:`session_path` so a pre-grouping
        transcript's metadata lands beside the transcript rather than under the
        slug this process would otherwise pick.

        The name is re-derived with ``safe_path_segment`` rather than taken off
        the transcript's stem: ``safe_filename`` leaves ``.`` and ``..`` intact
        because a suffix always follows it, which is not true here.
        ``with_suffix("")`` on ``<group>/...jsonl`` yields ``<group>/..`` --
        pointing out of the group directory -- and on ``<group>/..jsonl`` it
        raises ``ValueError``.
        """
        chat_id = safe_path_segment(key.partition(":")[2]) or "_"
        return self.session_path(key).parent / chat_id

    @staticmethod
    def key_from_path(path: Path) -> str:
        """Best-effort reverse of the nested filename encoding for a session file.

        The on-disk ``_type:metadata`` key is authoritative when present and
        wins over this; callers use it only as the fallback for metadata-less
        files. ``safe_filename`` is non-invertible, so any character it folds
        to ``_`` (``/``, ``:``, ...) is not recovered here.

        The parent directory names the channel only under channel grouping. A
        project slug always begins with ``-`` (the leading path separator) and
        a channel name never does, so a slug parent is recognisable -- and
        there the channel is simply not knowable from the path. Reporting
        ``unknown`` rather than the slug matters because callers compare the
        channel half against real channel names: a slug would look like a
        channel that no session key can ever match.
        """
        parent = path.parent.name
        return f"{'unknown' if parent.startswith('-') else parent}:{path.stem}"

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
        matches = exact or [s for s in sessions if s["key"].partition(":")[2].startswith(value)]
        if len(matches) > 1:
            return SessionResolution("ambiguous", candidates=tuple(s["key"] for s in matches))
        if matches:
            return SessionResolution("resolved", key=matches[0]["key"])
        return SessionResolution("not_found")

    def find_most_recent_chat_id(
        self,
        channel: str,
        *,
        this_project_only: bool = False,
        include_archived: bool = True,
    ) -> str | None:
        """Return the chat_id of the most-recently-updated session on this
        channel, or None if no such session exists.

        Used by cron delivery at trigger time to auto-resolve where to
        forward ephemeral (cli / tui) reminders, so users don't need to
        know their own open_id / chat_id on the target channel.

        Reads each candidate file's metadata line (first line of the JSONL)
        to get the authoritative session key ``<channel>:<chat_id>`` and
        ``updated_at``; recency is decided by ``updated_at``, falling back
        to file mtime for files that lack it.

        Sessions with messages take priority: a freshly minted zero-message
        session (``sessions create``) must not hijack delivery away from the
        user's real last conversation. Empty sessions are only considered
        when the channel has no session with messages at all.

        ``this_project_only`` narrows the scan, which is what resuming wants:
        ``--continue`` in one checkout must not reopen a conversation started in
        another, and the glob fallback would then append this project's turns to
        a transcript filed under that one. Delivery callers want the opposite --
        the gateway forwards to whichever session is live regardless of where it
        was started -- so the wide scan stays the default. Without a
        ``project_slug`` there is no narrower scan to make and the flag is a
        no-op.

        Narrowed still means two directories, not one: this project's group, and
        the channel-named group holding sessions written before grouping
        existed. Those carry no project attribution, and ``session_path``
        already lets any project adopt one, so excluding them here would strand
        every pre-upgrade conversation with no way to reach it from ``-c``.

        ``include_archived`` defaults to True because delivery callers still
        need the latest live destination even when it is hidden from session
        pickers. Resume callers set it to False so archiving remains a durable
        opt-out from automatic reopening.
        """
        best_chat_id: str | None = None
        best_updated = ""
        best_empty_chat_id: str | None = None
        best_empty_updated = ""
        # Either way the directory name is never trusted to name the channel:
        # under project grouping a `cli` session lives under a slugged launch
        # path, so the authoritative channel is the one in each file's metadata
        # (re-checked below).
        if this_project_only and self.project_slug:
            groups = {self.project_slug, safe_path_segment(channel) or "_"}
            candidates = (p for g in groups for p in (self.sessions_dir / g).glob("*.jsonl"))
        else:
            candidates = self.sessions_dir.glob("*/*.jsonl")
        for p in candidates:
            meta, count, _last, _first, _preview = self._scan_file(p)
            if meta is None:
                continue
            key_val = meta.get("key", "")
            if ":" not in key_val:
                continue
            ch, chat_id = key_val.split(":", 1)
            if ch != channel or not chat_id:
                continue
            if not include_archived and (meta.get("metadata") or {}).get("archived"):
                continue
            if this_project_only and self.project_dir is not None:
                # The slug is lossy, so one group can hold two projects
                # (`/srv/a_b` and `/srv/a/b`). Where the real launch directory
                # was recorded, use it; sessions predating that field have no
                # attribution to contradict and stay eligible.
                origin = (meta.get("metadata") or {}).get("project_dir")
                if origin is not None and origin != str(self.project_dir):
                    continue
            updated = meta.get("updated_at")
            if not isinstance(updated, str) or not updated:
                try:
                    updated = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
                except OSError:
                    continue
            if count > 0:
                if updated > best_updated:
                    best_chat_id = chat_id
                    best_updated = updated
            elif updated > best_empty_updated:
                best_empty_chat_id = chat_id
                best_empty_updated = updated
        return best_chat_id if best_chat_id is not None else best_empty_chat_id

    @staticmethod
    def _scan_file(path: Path) -> tuple[dict[str, Any] | None, int, str, str, str]:
        """Single pass over a session file: return (last metadata record,
        message line count, last user message timestamp, first user message
        text, latest conversational preview).

        One metadata record is appended per save, so the last reflects
        current state. Message lines are counted without keeping them in
        memory.

        The timestamp tracked is the newest readable conversational message:
        ``user`` or ``assistant``. Runtime-origin and delegated entries are
        deliberately included because they add visible content to a Session
        even when nobody typed at that moment. Tool and system records do not
        move the picker on their own. The metadata record's ``updated_at``
        cannot serve either -- it only moves when a save writes metadata and
        can lag the transcript by a whole turn.

        A ``silent`` assistant row is left out of the timestamp and the preview
        alike: the model chose to say nothing, so its marker is neither
        activity to order by nor a line to show. When the runtime opened the
        turn it closes (a user row carrying ``origin``), the whole turn is left
        out: that row is prose for the model rather than conversation, and the
        tool calls the run made on the way delivered nothing either. A person's
        own message still counts when the model answers it with silence.

        A turn the message tool answered is the exception: the tool is what
        delivers a reply to the reader, so such a turn answered whatever the
        model ended it on, and it stays the activity the session is ordered by.
        Its marker's row carries the live turn's verdict, read from one place
        (``raven/session/turn_shape.py``), the same one the ACP replay reads the
        turn with, so the two surfaces cannot come to disagree about which turn
        answered.

        The first user message is what session pickers title an untitled
        session with, so its text (flattened to one line, capped) rides along.
        The latest non-empty user or assistant message is a separate display
        preview: identity and recent content have different lifecycles and must
        not be overloaded into one field.
        """
        meta: dict[str, Any] | None = None
        count = 0
        last_ts = ""
        first_user = ""
        latest_preview = ""
        # The preview and the clock as they stood before the runtime-opened turn now under way, for a
        # turn that ends silent to be taken back whole.
        before_runtime_turn: tuple[str, str] | None = None
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
                        if isinstance(data, dict):
                            role = data.get("role")
                            if role == "user":
                                # Only a turn the runtime opened can be taken back: a person's message
                                # counts even when the model answers it with silence.
                                before_runtime_turn = (latest_preview, last_ts) if data.get("origin") else None
                                if not first_user:
                                    first_user = _message_text(data.get("content"))
                            if role in {"user", "assistant"}:
                                if data.get("silent") is not True:
                                    text = _message_text(data.get("content"))
                                    if text:
                                        latest_preview = text
                                    ts = data.get("timestamp")
                                    if isinstance(ts, str) and ts > last_ts:
                                        last_ts = ts
                                elif before_runtime_turn is not None and not replied_via_message_tool(data):
                                    latest_preview, last_ts = before_runtime_turn
        except OSError:
            return None, 0, "", "", ""
        return meta, count, last_ts, first_user, latest_preview

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        session = self._live(key)
        if session is None:
            session = self._load(key)
            if session is None:
                session = Session(key=key)
            # The group directory is a lossy label -- two projects whose paths
            # differ only in punctuation slug the same way. Record the real
            # directory so anything that needs the project's identity reads it from
            # here rather than from the directory name. Set once, on the session
            # that introduced it; a later run elsewhere must not rewrite history.
            if self.project_dir is not None and not session.metadata.get("project_dir"):
                session.metadata["project_dir"] = str(self.project_dir)
            # After the stamp, which this manager derives: a session dropped with
            # it unsaved gets the same stamp back when it is loaded again.
            session._clean_fingerprint = _fingerprint(session)
        self._remember(session)
        return session

    def _live(self, key: str) -> Session | None:
        """The session already in memory under ``key``, cached or still held elsewhere."""
        session = self._cache.get(key)
        if session is None:
            session = self._held.get(key)
        return session

    def _remember(self, session: Session) -> None:
        """Make ``session`` the most recently used, and drop clean ones past the limit."""
        self._cache[session.key] = session
        self._cache.move_to_end(session.key)
        self._held[session.key] = session
        if len(self._cache) <= _CACHE_LIMIT:
            return
        for key in list(self._cache)[:-1]:
            cached = self._cache.get(key)
            if cached is not None and _is_clean(cached):
                self._cache.pop(key, None)
                if len(self._cache) <= _CACHE_LIMIT:
                    return

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        return read_session_file(self.session_path(key), key)

    def append_metadata_patch(self, key: str, patch: dict[str, Any]) -> bool:
        """Fold ``patch`` into a session's metadata by appending one record.

        The last metadata record wins on load, so appending is enough -- and
        the transcript is never read, which is what lets a housekeeping pass
        touch hundreds of sessions without loading any of them. Merging into
        the record on disk is also what keeps two clients from undoing each
        other: whoever writes second keeps the other's keys, which a whole
        ``save`` of one client's copy of the metadata cannot do.

        False when there was nothing to append to -- no transcript, or one with
        no metadata record -- which a caller reporting whether a flag reached
        the disk has to tell apart from a write that happened.
        """
        path = self.session_path(key)
        if not path.is_file():
            return False
        last, _count, _last_ts, _first, _preview = self._scan_file(path)
        if last is None:
            return False
        merged = {**(last.get("metadata") or {}), **patch}
        locked_append(path, [json.dumps({**last, "metadata": merged}, ensure_ascii=False)])
        cached = self._live(key)
        if cached is not None:
            # Blind to the patched keys: a caller often sets them on its own copy
            # first, and the patch has just put them on disk.
            unchanged = _unchanged_but_for(cached, patch.keys())
            cached.metadata.update(patch)
            # These keys are on disk now, so they are no longer this copy's
            # unsaved opinion: left out of the baseline the patch reads as a
            # local change on every later save and is asserted again over
            # whatever anybody wrote in between. ``_file_stamp`` deliberately
            # stays stale -- the file also carries the record this merged into,
            # and a stale stamp is what makes the next save re-read it.
            cached._persisted_metadata.update(patch)
            # Clean stays clean; a copy with changes of its own keeps them unsaved.
            if unchanged:
                cached._clean_fingerprint = _fingerprint(cached)
        return True

    def save(self, session: Session) -> None:
        """Save a session to disk.

        Appends a fresh metadata record plus the not-yet-persisted messages
        under a cross-process lock, so concurrent writers never lose each
        other's turns and a turn's messages stay contiguous. A message list
        cut since this copy last read or wrote the file (``clear``,
        ``undo_last_turn``) rewrites it atomically instead, however far the
        list has grown back: the cut messages are still in the file, and an
        append would leave them there.

        An untitled session is auto-named here from its first user message
        (first line, collapsed whitespace, capped at 40 chars); a title set
        by the user is never overwritten. Forked children are excluded — their
        first user message names the fork point's ancestor, not the fork. They
        are not left nameless by that: ``fork`` gives the child its parent's
        name with ``(fork)`` after it, which is a name for the fork rather than
        for what it was forked from.
        """
        _refuse_imported(session.key)
        path = self.session_path(session.key)

        if not session.metadata.get("title") and not session.metadata.get("parent_session_id"):
            auto_title = _first_user_auto_title(session.messages)
            if auto_title:
                session.metadata["title"] = auto_title
                session.metadata["title_auto"] = True

        channel, _, chat_id = session.key.partition(":")
        reserved = {
            "source": None,
            "channel": channel,
            "chat_id": chat_id,
            "title": None,
            "parent_session_id": None,
        }
        session.metadata = {**reserved, **session.metadata}

        # The read and the write are one transaction: the record this merges
        # under can otherwise be written between them and lost anyway.
        with write_transaction(path):
            written = self._record_written_since(session, path)
            last = None if written is None else written[0]
            session.metadata = self._metadata_to_write(session, last)
            rewrite = len(session.messages) < session._persisted_count or _cut_since_persisted(session)
            disk = None if last is None else _stored_revision(last)
            disk_scope = None if disk is None else disk[:2]
            before = (session.log_id, session.log_generation)
            _merge_revision(session, disk)
            merged: list[dict[str, Any]] | None = None
            if rewrite:
                # The file is about to hold exactly this copy's list, so neither
                # the scope it last saw nor the one on disk may name it.
                _move_past(session, session._persisted_revision, disk_scope)
            elif written is not None and (
                written[1] != session._persisted_count or disk_scope != session._persisted_revision
            ):
                # Somebody else changed the messages since this copy last saw
                # the file. The append lands after theirs, so the live list is
                # re-read to match the file. Refs are minted only into history
                # already persisted, so the scope survives as long as every
                # persisted ordinal still names the same message, and is retired
                # otherwise. Where the persisted part agrees, this copy's own
                # entries are kept by identity, which is what lets a turn find
                # its own messages again after the merge.
                on_disk = read_session_file(path, session.key)
                count = session._persisted_count
                kept = on_disk is not None and len(on_disk.messages) >= count
                kept = kept and _normalized(session.messages[:count]) == _normalized(on_disk.messages[:count])
                if on_disk is not None:
                    head = session.messages[:count] if kept else on_disk.messages[:count]
                    merged = head + on_disk.messages[count:] + session.messages[count:]
                if not kept and (session.log_id, session.log_generation) == before:
                    _move_past(session, before, disk_scope)
                # Indices into this copy's unsaved tail move when the other
                # writer's messages land ahead of it.
                moved = on_disk is not None and len(on_disk.messages) > count and len(session.messages) > count
                if merged is None or not kept or moved:
                    session.truncations += 1
            metadata_line = json.dumps(
                {
                    "_type": "metadata",
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated,
                    "log_id": session.log_id,
                    "log_generation": session.log_generation,
                    "log_started_at": session.log_started_at,
                    # Personalization: persist clarification wait-state across restarts
                    "pending_clarification": session.pending_clarification,
                },
                ensure_ascii=False,
            )

            if rewrite:
                lines = [metadata_line]
                lines += [json.dumps(m, ensure_ascii=False) for m in session.messages]
                atomic_replace(path, "".join(line + "\n" for line in lines))
            else:
                new_messages = session.messages[session._persisted_count :]
                lines = [metadata_line]
                lines += [json.dumps(m, ensure_ascii=False) for m in new_messages]
                locked_append(path, lines)
            if merged is not None:
                session.messages = merged
            session._file_stamp = _stamp(path)
            session._persisted_metadata = dict(session.metadata)
            session._persisted_revision = (session.log_id, session.log_generation)

        session._persisted_count = len(session.messages)
        session._persisted_truncations = session.truncations
        session._clean_fingerprint = _fingerprint(session)
        self._remember(session)

    def _record_written_since(self, session: "Session", path: Path) -> tuple[dict[str, Any] | None, int] | None:
        """The file's last metadata record and message count, if anybody wrote to it since this copy last read or wrote it.

        None while the file is byte for byte what this copy last read or wrote,
        which is every save in the ordinary case of one writer -- so a
        conversation pays for the scan only when somebody else has actually
        written to it.
        """
        stamp = _stamp(path)
        if stamp is None or stamp == session._file_stamp:
            return None
        last, count, *_rest = self._scan_file(path)
        return last, count

    def _metadata_to_write(self, session: "Session", last: dict[str, Any] | None) -> dict[str, Any]:
        """What this copy changed, over the record on disk, over what it loaded.

        A save rewrites the whole metadata record, so on its own it speaks for
        every key its own copy happens to hold: a flag another client wrote
        after this copy was loaded -- archived, most visibly -- was gone the
        next time anything here saved, and the conversation came back.

        Which is why only what this copy *changed* may outrank the record. A
        session's ``metadata`` is the whole snapshot it loaded, not the keys it
        meant, so overlaying it whole trades one stale-write bug for a narrower
        one: restore persists ``archived: False``, a second client loads that,
        the first archives again, and the second's next ordinary turn writes
        its stale False back over the newer True. Comparing against what this
        copy last read or wrote is what tells the two apart.

        **Removal is written, never left unsaid.** Nothing here can tell a key
        this copy dropped from one it never had, so every remover states a
        false value instead (``session.pin``, :meth:`Session.set_title`, the
        output-limit stamp). A new remover that omits a key instead will find
        it resurrected.

        ``last`` is the record somebody else wrote since this copy last read or
        wrote the file (:meth:`_record_written_since`); None leaves this copy's
        metadata as it is.
        """
        if last is None:
            return session.metadata
        base = session._persisted_metadata
        changed = {k: v for k, v in session.metadata.items() if k not in base or base[k] != v}
        # Base under the record: a key this copy read and did not touch still
        # belongs in what it writes, and the record on disk has the say on it.
        return {**base, **(last.get("metadata") or {}), **changed}

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache, and from the held map behind it."""
        self._cache.pop(key, None)
        self._held.pop(key, None)

    def delete(self, key: str) -> bool:
        """Remove the session file and invalidate the cache and held-map entries.

        Returns True only if a file was actually removed; False if no file
        existed or the removal failed. Deleting an unknown key is a safe no-op.
        Attached observers hear every delete request after the store has acted,
        with the removal outcome; an observer that raises is logged and skipped
        (paper: contracts/session_events.py), so none can change this verdict.
        """
        path = self.session_path(key)
        self.invalidate(key)
        removed = False
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError:
                logger.warning("session.delete: failed to remove file for {}", key)
        for observer in self._delete_observers:
            try:
                observer.on_session_deleted(key, removed)
            except Exception:
                logger.exception("session.delete: observer failed for {}; the deletion stands", key)
        return removed

    def exists(self, key: str) -> bool:
        """Return True if the session has a file on disk (lazy sessions don't)."""
        return self.session_path(key).exists()

    def peek(self, key: str) -> "Session | None":
        """Return the session in memory if present, cached or held elsewhere; else load it from disk without caching.

        Callers that need read-only access to a session should use this instead
        of get_or_create, which would cache a fresh empty session for unknown keys.
        """
        cached = self._live(key)
        if cached is not None:
            return cached
        return self._load(key)

    def fork(self, source_key: str, *, title: str | None = None) -> "Session | None":
        """Fork ``source_key`` at its head into a new diverging child session.

        Full-copy semantics: the child is minted with a fresh chat_id on the
        source's channel, a deep copy of the source's messages, and
        ``parent_session_id`` set to the source key (the reserved lineage slot).
        The child inherits ``last_consolidated`` (so its active-context window
        matches the source at the fork point), starts annotated through its
        inherited history (see below), and resets ``pending_clarification``
        (interaction wait-state is not history). The child is persisted eagerly.

        Whatever the source is called, the child is called ``<title> (fork)``
        -- typed by hand, derived by ``save`` from the first user message, or
        generated. Inheritance used to be restricted to human titles, which
        crossed with ``save``'s rule that a fork is never auto-named to leave
        the forks of auto-named sessions with no title at all; the front ends
        then headed them with a placeholder, so every fork of an unnamed
        conversation read the same. The reason a fork is not auto-named stands
        and is a different one: its first user message names the fork point's
        ancestor, not the fork. The parent's name, marked as the fork it is,
        does name the fork.

        The child's title carries no ``title_auto`` marker, whatever the
        source's said. It is the fork's own name from here: a rename still
        wins, and nothing regenerates it behind the reader's back.

        The derived name goes in through ``set_title`` like every other name, so
        it obeys the one line and the one ceiling that rule states. Where a human
        title too long for the record is refused, this one is not: a parent named
        right up to the ceiling gives up its tail so the fork keeps a name
        (``_fork_title``).

        Returns the persisted child, or None when the source does not exist or
        has zero messages (a fork of an empty session has no value).
        """
        _refuse_imported(source_key)
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
                child.set_title(_fork_title(parent_title))
        child.metadata["parent_session_id"] = source_key
        # The fork starts annotated. Its inherited history is the parent's and
        # is already in the workspace log if the parent annotated it, and a
        # session's past is never back-filled -- the after-turn step seeds the
        # same way, at the length before the first turn it sees. Without this
        # the cursor reads 0, and ``/new`` as a fresh fork's first action hands
        # every inherited message to the annotator a second time.
        child.metadata["annotated_through"] = len(child.messages)
        # A fork continues its parent's conversation, so it continues on its
        # parent's model. The caller re-points the live binding, but that lives
        # in memory only -- without carrying the record too, the fork drops to
        # the default the first time it is resumed in a new process.
        for slot in ("model", "provider"):
            inherited = (source.metadata or {}).get(slot)
            if inherited:
                child.metadata[slot] = inherited
        self.save(child)
        return child

    def flush(self, key: str) -> bool:
        """Save the session in memory, cached or held elsewhere, iff it has unpersisted messages.

        Unpersisted means messages past ``_persisted_count``, or a cut the file
        has not seen yet, such as one whose own save failed. Returns False only
        when a save was attempted and failed (the failure is swallowed); True
        otherwise, including the no-op cases (key not in memory / nothing
        unpersisted).
        """
        cached = self._live(key)
        if cached is None:
            return True
        if len(cached.messages) > cached._persisted_count or _cut_since_persisted(cached):
            try:
                self.save(cached)
            except Exception:
                logger.warning("flush: failed to persist session {}", key)
                return False
        return True

    def list_sessions(
        self,
        channel: str | None = None,
        *,
        channels: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by one or several channels.

        Each entry carries: key, created_at, updated_at, last_message_at,
        path, message_count, first_user_message, last_message_preview. Ordered
        by the metadata ``updated_at`` stamp descending. Each file is read in a
        single pass, including when several channels are requested.

        Subagent transcripts are stored as sessions so they can be read with the
        same machinery, but they are steps inside somebody else's turn rather
        than conversations -- listing them would bury the real ones. Ask for the
        channel by name to get them. ``channel`` preserves the original
        single-channel API; ``channels`` is the multi-channel picker path and
        cannot be combined with it.
        """
        if channel is not None and channels is not None:
            raise ValueError("channel and channels are mutually exclusive")
        selected_channels = set(channels) if channels is not None else None
        sessions = []

        for path in self.sessions_dir.glob("*/*.jsonl"):
            data, message_count, last_ts, first_user, latest_preview = self._scan_file(path)
            if data is None:
                continue
            key = data.get("key") or self.key_from_path(path)
            key_channel = key.partition(":")[0]
            # Filter on the key, not the parent directory: under project
            # grouping the directory is a slugged launch path, so it no longer
            # names the channel. That applies to the subagent exclusion too --
            # keyed off the directory it would quietly stop excluding anything
            # the moment a session landed under a slugged group instead.
            if channel is None and selected_channels is None and key_channel == SUBAGENT_CHANNEL:
                continue
            if channel is not None and key_channel != channel:
                continue
            if selected_channels is not None and key_channel not in selected_channels:
                continue
            sessions.append(
                {
                    "key": key,
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    # The newest visible conversation entry. Runtime-origin
                    # and delegated rows count because they change the Session,
                    # unless the turn they opened ended silent.
                    "last_message_at": last_ts,
                    "path": str(path),
                    "message_count": message_count,
                    "metadata": data.get("metadata", {}),
                    "first_user_message": first_user,
                    "last_message_preview": latest_preview,
                }
            )

        # Ordered by ``updated_at``, unchanged. ``last_message_at`` is
        # carried on each entry but deliberately not sorted on here: three
        # callers take ``[0]`` off this list to decide *where a message gets
        # delivered* -- the gateway's heartbeat target, the sentinel's nudge
        # target, and `raven sessions list`, whose table renders ``updated_at``
        # in its own Updated column. Re-ordering for a picker's benefit would
        # silently re-route those. A surface that wants latest conversational
        # activity sorts on the field itself; ``session.list`` does.
        return sorted(sessions, key=lambda x: x.get("updated_at") or "", reverse=True)
