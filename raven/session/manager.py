"""Session management for conversation history."""

import copy
import hashlib
import inspect
import json
import os
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from raven.contracts.context import SessionRevision
from raven.session.title import TITLE_STORAGE_MAX, collapse_to_line
from raven.session.turn_shape import TakeBack, TakeBackStep
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

# Key prefix of an Imported Session (raven/trajectory/imported.py). Those
# live under the raven home, never in this store: a save under this channel is
# refused, so nothing that resolves a key to a file here can give an imported
# session a writable twin that pickers, sweeps and delivery targets see.
IMPORTED_CHANNEL = "imported"

# Metadata keys the loop stamps on a session with the message index a cut turn
# starts at: one stopped at the model's output ceiling, one its own budget ended
# (the tool cap, the wall clock or a stalled tool). ACP reads them to refine a
# prompt's ``end_turn``; clearing the session's history forgets them.
OUTPUT_LIMIT_TURN_AT = "output_limit_turn_at"
BUDGET_STOP_TURN_AT = "budget_stop_turn_at"
TURN_MARKERS = (OUTPUT_LIMIT_TURN_AT, BUDGET_STOP_TURN_AT)


#: The metadata a fork carries over from its parent: the model and provider it
#: was switched onto, and the reasoning effort picked for it. The effort key is
#: ``raven.agent.session_effort.METADATA_KEY``, spelt here rather than imported
#: because the session layer does not import the agent's; a test holds the two
#: spellings together. A value that is null -- a pick cleared, which a metadata
#: patch leaves as null rather than removing -- is not carried: the fork follows
#: the default, as its parent does.
FORK_CARRIES: tuple[str, ...] = ("model", "provider", "reasoning_effort")


class SessionWriterConflictError(RuntimeError):
    """A session file changed under a copy that still holds it.

    The store serves one writer per transcript. A second one -- two terminals
    continuing the same session, or two processes pointed at one home -- is a
    misuse: its rows would have to be merged or silently dropped, and neither
    is what the people using it meant. The save refuses instead, and the copy
    has to be reloaded before it can write again.
    """


def _refuse_imported(key: str) -> None:
    if key.partition(":")[0] == IMPORTED_CHANNEL:
        raise ValueError(f"{key!r} is an imported session; the session store does not keep it")


# How many sessions a manager keeps loaded once nothing else holds them. Soft:
# a session with unsaved state is never dropped from the cache, and one a caller
# still holds stays reachable through ``SessionManager._held`` whatever this says.
# A change made through a reference to a session that has already left the cache,
# then released unsaved, is lost, as it would be in a fresh process.
_CACHE_LIMIT = 64

# How many listing scans one manager keeps. A session picker asks for the same
# transcripts on every poll, and a transcript only changes when a turn is
# appended, so a rescan while (mtime_ns, size) still matches is wasted work.
# Past this many distinct transcripts the LRU churns and a file still being
# polled can be read again; a miss costs the scan, never a stale result.
_SCAN_CACHE_LIMIT = 2048


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
        # utf-8-sig, as every reader of a transcript: a byte order mark in front of the metadata line
        # would leave it unparsed, and a file claiming no key goes to a session of any channel with
        # the same chat id.
        with open(path, encoding="utf-8-sig") as f:
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


def _declares(method: Any, name: str) -> bool:
    """Whether ``method`` can be handed the keyword ``name``: it declares it or takes
    ``**kwargs``. An observer written against the paper as it stood is called the way it
    was written, and so is one whose signature cannot be read -- a keyword it never
    declared would fail the call, and the store would skip it."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


_STAMP_TAIL_BYTES = 4096
"""How much of a transcript's end the stamp digests: one read, not a scan of the file.

A rewrite of the same length changes the tail, which is what makes the anchor
catch a second writer the size and clock cannot. A row rewritten in the middle
alone leaves it alone, so the pair is a cheap tell, not a checksum of the file.
"""


def _tail_stamp(handle: Any, size: int) -> str:
    """A short digest of the last ``_STAMP_TAIL_BYTES`` of a transcript.

    The window is measured in bytes, so a handle opened for text is read through
    its raw buffer: counting characters would land the two digests on different
    windows for anything the file does not spell in ASCII.
    """
    raw = getattr(handle, "buffer", handle)
    raw.seek(max(0, size - _STAMP_TAIL_BYTES))
    return hashlib.blake2b(raw.read(_STAMP_TAIL_BYTES), digest_size=16).hexdigest()


def _stamp(path: Path) -> tuple[int, int, str] | None:
    """A transcript's (size, mtime_ns, tail digest), or None when it is not there.

    What a saved copy compares itself against to know whether anybody else has
    written to the file since it last read or wrote it. The size and the clock
    are not enough on their own: ``st_mtime_ns`` counts in the filesystem's own
    ticks, which can be as coarse as a millisecond, so a rewrite of the same
    length lands in the tick the copy already recorded and both halves compare
    equal. The tail digest is the content anchor that tells those apart.
    """
    try:
        with open(path, "rb") as handle:
            st = os.fstat(handle.fileno())
            return (st.st_size, st.st_mtime_ns, _tail_stamp(handle, st.st_size))
    except OSError:
        return None


@contextmanager
def _writing_as(session: "Session | None", path: Path) -> Iterator[tuple[int, int, str] | None]:
    """Hold the write lock for a write ``session`` makes, and settle its stamp after it.

    The stamp says the file holds nothing this copy has not read or written, and
    every save trusts it: a stamp that still matches skips the re-read. Moved past
    bytes the copy never took in, it hides them from every later save -- another
    writer's rows, so the conflict is never raised, or another client's metadata
    record, so the fold never runs and the next save writes over it.

    So the stamp moves here and nowhere else after a write, and on evidence rather
    than on a caller's word. This yields the file's stamp as the lock found it; on
    a clean exit the copy's stamp moves past the write only when the copy's own
    stamp still names that file. A writer that adds a record and reads nothing
    needs no more than this: a file someone else changed leaves its stamp behind,
    and the next save settles the difference. A writer that did read what changed
    and took it in says so with :func:`_took_in` before it writes, which is what
    ``SessionManager.save`` does after its conflict check and its fold.

    ``read_session_file`` sets the first stamp, taken before the read, so a write
    landing during the read leaves the copy behind the file rather than ahead.
    ``SessionManager.append_metadata_patch`` writes for a key, not as a copy, and
    moves no copy's stamp. A writer of message rows goes through ``save``, which
    keeps the accounts :class:`Session` lists.
    """
    with write_transaction(path):
        before = _stamp(path)
        yield before
        if session is not None and session._file_stamp == before:
            session._file_stamp = _stamp(path)


def _took_in(
    session: "Session",
    before: tuple[int, int, str] | None,
    written: tuple[dict[str, Any] | None, int] | None,
) -> None:
    """Record, inside :func:`_writing_as`, that ``session`` now holds the file as ``before`` names it.

    For a writer that read what changed since its stamp and took it in -- ``save``,
    whose conflict check raised on moved rows and whose fold took in the metadata
    record. ``written`` is that read: the file's last metadata record and message
    count, or None when there was nothing to read (the stamp had not moved, or the
    file is not there). It is checked again here, so a call that skipped the read,
    or one made past rows another writer added, raises instead of moving the stamp
    past what this copy never read.
    """
    if before is not None and before != session._file_stamp:
        if written is None or _message_rows_moved(session, *written):
            raise RuntimeError(
                f"session {session.key!r}: its stamp cannot move past a file this copy has not read and taken in"
            )
    session._file_stamp = before


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
    # -- What this copy knows about its file --
    # Several accounts, because each answers a different question and folding two
    # together makes one of them wrong. ``read_session_file`` sets them on load and
    # ``SessionManager.save`` -- the only writer of message rows -- brings every one
    # up to date in one place after its write; a cut or an in-place replacement
    # marks them stale so that save rewrites. A writer of another record kind (a
    # metadata patch, a turn_meta) touches none of them; a copy's stamp moves only
    # inside ``_writing_as``, on that copy's own write.
    #
    # - ``_persisted_count`` indexes ``messages``, where the next append starts; it
    #   counts the reader's synthetic repairs, which sit in the list.
    # - ``_persisted_file_count`` counts the file's message rows, which never hold
    #   those repairs; a save compares it with the file's own count.
    # - ``_persisted_revision`` is the (log_id, generation) the file carried: a
    #   second writer's undo followed by an append of the same length keeps the
    #   count and moves this.
    # - ``_persisted_truncations`` and ``_refile_rows`` say the file still holds
    #   rows this copy cut or replaced; either makes the next save a rewrite.
    # - ``_persisted_metadata`` is what a fold measures this copy's changes against.
    # - ``_file_stamp`` is the cheap test of whether the file changed at all, made
    #   before any of the above is read back from disk.
    #
    # The turn keeps two of its own (``live["persisted"]`` and ``live["filed_ids"]``
    # in ``turn_path``): where its incremental writes stopped, and which rows the
    # current attempt appended -- the set a rerun takes off the file.
    #
    # Messages already on disk; save() appends only past this index.
    _persisted_count: int = field(default=0, repr=False)
    # Message rows the file held as this copy last read or wrote it. The
    # reader's synthetic repair is not one of them, so this is what the file's
    # own message count must still match before a save may proceed.
    _persisted_file_count: int = field(default=0, repr=False)
    # ``truncations`` when this copy last read or wrote the file. A cut since then
    # leaves the file holding the messages that were cut, which only a rewrite
    # drops, however far the list has grown back.
    _persisted_truncations: int = field(default=0, repr=False)
    # A row was rewritten after it reached disk (the image window turned a
    # picture into a note). An append would leave the stale row in the
    # transcript, so the next save rewrites the file; the save clears this.
    _refile_rows: bool = field(default=False, repr=False)
    # What each filed row's content was when it was written, by identifier, so
    # the writer can tell a rewrite from the transforms it makes itself. Pruned
    # to the rows the copy still holds.
    _filed_contents: dict[str, str] = field(default_factory=dict, repr=False)
    # What a filed inbound row holds now, by identifier: the words the user
    # typed when the row is text, the blocks and placeholders it was filed as
    # when the question carried a picture. The hooks' rewrite is a view of the
    # request, not of the record: a row already filed that is prepared again
    # must be restored to the form the file holds, not to the string behind it.
    _inbound_filed: dict[str, Any] = field(default_factory=dict, repr=False)
    # What the transcript looked like the last time this copy read or wrote it,
    # as (size, mtime_ns, tail digest). save() compares it to notice a second
    # writer before appending or rewriting.
    _file_stamp: tuple[int, int, str] | None = field(default=None, repr=False)
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
        # The append boundary cannot stay past rows this copy no longer holds:
        # a later message would land behind it and look persisted already.
        self._persisted_count = min(self._persisted_count, cut_index)
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


def _move_past(session: Session, *used: tuple[str, int] | None) -> None:
    """Move the generation past every ``used`` scope of this log id.

    For a save whose file will no longer hold what those scopes named: each was
    already handed out by this copy for messages at ordinals that now hold
    something else, and a scope must never name two different messages.
    """
    for revision in used:
        if revision is not None and revision[0] == session.log_id and session.log_generation <= revision[1]:
            session.log_generation = revision[1] + 1


def _filed_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows a save writes: every message but the repair the reader derived.

    The synthetic results of :func:`repair_dangling_tool_calls` are not part of
    what happened, and a rewrite (an undo, a clear, a fork) would otherwise
    turn one into a tool result no tool produced. They stay in memory, where
    the next model call needs the history to be provider-valid, and the next
    read derives them from the file again.
    """
    return [message for message in messages if not message.get(SYNTHETIC_INTERRUPTED_KEY)]


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


_INTERRUPTED_RESULT = "[interrupted] this call never returned"

SYNTHETIC_INTERRUPTED_KEY = "_synthetic_interrupted"
"""Marks the result :func:`repair_dangling_tool_calls` invents on read.

The file never holds such a row: it is a reader's repair, re-derived on every
read, and a rewrite would turn it into a tool result no tool produced. The
graceful-cancel path builds its own ``[interrupted]`` rows without this mark,
because those are the turn's real record of what happened."""

_TURN_META_KEYS = frozenset(
    {"replied_via_tool", "observers", "reasoning_ms", "duration_ms", "context_used", "context_parts"}
)
"""The keys a ``turn_meta`` record may restate on a row already written.

The record exists for stamps that land after the row reached disk. Anything
else in it is malformed or from a newer writer, and applying it would let a
patch rewrite message content the format never lets it touch."""


def repair_dangling_tool_calls(messages: list[dict], *, mark_synthetic: bool = False) -> list[dict]:
    """Give every tool call that never got a result an ``[interrupted]`` one.

    An incrementally written transcript can end mid-call: the assistant message
    is on disk and the process is gone before the tool answered. A strict
    provider rejects a history whose ``tool_calls`` have no matching tool
    message, so the repair happens on the way in rather than on the way out --
    the file keeps saying what actually happened. Each result is filed directly
    after the call that opened it, and after any results already filed for it,
    so a later restart reloads the same provider-valid order.

    ``mark_synthetic`` tags the inserted rows with
    :data:`SYNTHETIC_INTERRUPTED_KEY`. The reader passes it, so a later save
    knows not to write the repair it derived; the cancel path does not, because
    the rows it builds are what that turn actually leaves behind.
    """
    answered = {str(message.get("tool_call_id") or "") for message in messages if message.get("role") == "tool"}
    repairs: list[tuple[int, list[dict]]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        pending = []
        for call in message.get("tool_calls") or []:
            call_id = str(call.get("id") or "") if isinstance(call, dict) else str(getattr(call, "id", "") or "")
            if not call_id or call_id in answered:
                continue
            function = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
            name = function.get("name") if isinstance(function, dict) else getattr(function, "name", None)
            synthetic = {
                "role": "tool",
                "tool_call_id": call_id,
                "name": str(name or "tool"),
                "content": _INTERRUPTED_RESULT,
            }
            if mark_synthetic:
                synthetic[SYNTHETIC_INTERRUPTED_KEY] = True
            pending.append(synthetic)
        if pending:
            repairs.append((index, pending))
    if not repairs:
        return list(messages)
    out = list(messages)
    for index, pending in reversed(repairs):
        at = index + 1
        while at < len(out) and out[at].get("role") == "tool":
            at += 1
        out[at:at] = pending
    return out


def read_session_file(path: Path, key: str) -> Session | None:
    """The session stored in the transcript at ``path``, read as ``key``.

    None when the file is absent or cannot be read. Public because a transcript
    kept outside the session store -- an Imported Session's -- is read by
    the same rules: metadata records are appended per save and the last one
    wins.

    The filing key on a stored row is a detail of the format, not of the
    conversation: the rows handed to a model or a front end never carry it. A
    file that ends on a call with no result is repaired here, so every reader
    gets a provider-valid history; the derived result is never written back.
    """
    if not path.exists():
        return None

    try:
        messages = []
        patches: list[dict] = []
        metadata = {}
        created_at = None
        updated_at = None
        last_consolidated = 0
        pending_clarification = None
        revision: tuple[str, int, str | None] | None = None
        created_raw = None

        # utf-8-sig, as the session list reads it: a byte order mark in front of the metadata line
        # would leave it unparsed, and the session would open without its title or archive flag.
        with open(path, encoding="utf-8-sig") as f:
            # Of the file this read, taken before reading it: a write that lands
            # while the file is read (or replaces it) leaves the stamp behind the
            # file, so the next save notices it changed rather than writing over
            # a revision this copy never saw.
            st = os.fstat(f.fileno())
            file_stamp = (st.st_size, st.st_mtime_ns, _tail_stamp(f, st.st_size))
            f.seek(0)
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

                # A turn's late stamps on rows already written: applied by the
                # row's own identifier rather than its position, which the
                # window can move under the turn that stamped it.
                if data.get("_type") == "turn_meta":
                    patches.extend(data.get("patches") or [])
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

        if patches:
            positions = {str(row.get("_row_id")): index for index, row in enumerate(messages) if row.get("_row_id")}
            for patch in patches:
                index = positions.get(str(patch.get("row_id")))
                keys = patch.get("keys")
                if index is not None and isinstance(keys, dict):
                    messages[index].update({key: value for key, value in keys.items() if key in _TURN_META_KEYS})
        for row in messages:
            row.pop("_row_id", None)
        file_count = len(messages)
        messages = repair_dangling_tool_calls(messages, mark_synthetic=True)
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
        session._persisted_file_count = file_count
        session._file_stamp = file_stamp
        session._persisted_metadata = dict(metadata)
        return session
    except Exception as e:
        logger.warning("Failed to load session {}: {}", key, e)
        return None


def _message_rows_moved(session: Session, last: dict[str, Any] | None, count: int) -> bool:
    """Whether the file's message rows are no longer the ones this copy last wrote.

    A metadata-only record appended since leaves the count and the revision
    alone; anything else is a second writer.
    """
    disk = None if last is None else _stored_revision(last)
    disk_scope = None if disk is None else disk[:2]
    return count != session._persisted_file_count or (last is not None and disk_scope != session._persisted_revision)


def append_turn_meta(session_path: Path, patches: list[dict], *, session: Session | None = None) -> None:
    """Restate keys on rows already written, by row id, under the file lock.

    The turn's rows are appended as it produces them; the stamps filed at its
    end -- the reply's provenance, the hook observers -- arrive later. One record
    per turn carries them, and the reader applies it in file order. ``session``
    is the copy that wrote them. Its stamp moves past this record only when
    nothing else was written since (``_writing_as``): the record folds nothing
    in, so another client's metadata record has to stay ahead of the stamp for
    the save that does.
    """
    if not patches:
        return
    payload = json.dumps({"_type": "turn_meta", "patches": patches}, ensure_ascii=False)
    with _writing_as(session, session_path) as before:
        if session is not None and before != session._file_stamp:
            # A message write since is a second writer, and filing into its rows
            # would be this copy's first write past them. A metadata-only record
            # is not, and the append goes ahead.
            last, count, *_rest = SessionManager._scan_file(session_path)
            if _message_rows_moved(session, last, count):
                return
        locked_append(session_path, [payload])


def _cut_since_persisted(session: Session) -> bool:
    return session.truncations != session._persisted_truncations


def _is_clean(session: Session) -> bool:
    if len(session.messages) != session._persisted_count or _cut_since_persisted(session) or session._refile_rows:
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
        # path -> (st_mtime_ns, st_size, scan result), so a poll that finds an
        # unchanged transcript does not read it again. See _scan_listed.
        # Not thread-safe: every caller runs synchronously on the event-loop
        # thread today, and the get/move_to_end/evict sequence assumes that.
        self._scan_cache: OrderedDict[Path, tuple[int, int, tuple[dict[str, Any] | None, int, str, str, str]]] = (
            OrderedDict()
        )

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
            meta, count, _last, _first, _preview = self._scan_listed(p)
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
        memory. A line that is another record kind (``turn_meta`` today) is
        not a message and is not counted: the count is what the picker shows
        and what a save compares against the file's own message count.

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

        A turn a tool answered is the exception: the tool is what delivers a
        reply to the reader, so such a turn answered whatever the model ended it
        on, and it stays the activity the session is ordered by. Its marker's
        row carries the live turn's verdict. Which turns go is read from one
        place (``TakeBack`` in ``raven/session/turn_shape.py``), the one the ACP
        replay and the live turn read it from, so the surfaces cannot come to
        disagree about which turn is taken back.

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
        before_runtime_turn: tuple[str, str] = ("", "")
        take_back = TakeBack()
        try:
            # utf-8-sig: a Windows editor that saves UTF-8 with a byte order mark puts it in front of the
            # metadata line, which would then fail to parse and take the session off the list.
            with path.open(encoding="utf-8-sig") as f:
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
                        continue
                    if isinstance(data, dict) and data.get("_type"):
                        # A record kind rather than a message: turn_meta, and
                        # whatever the format adds next. Counting one here made
                        # the listing's count disagree with the transcript, and
                        # made a save compare a count that could never match.
                        continue
                    count += 1
                    if isinstance(data, dict):
                        step = take_back.read(data)
                        if step is TakeBackStep.OPENS:
                            before_runtime_turn = (latest_preview, last_ts)
                        elif step is TakeBackStep.TAKES_BACK:
                            latest_preview, last_ts = before_runtime_turn
                        role = data.get("role")
                        if role == "user" and not first_user:
                            first_user = _message_text(data.get("content"))
                        if role in {"user", "assistant"} and data.get("silent") is not True:
                            text = _message_text(data.get("content"))
                            if text:
                                latest_preview = text
                            ts = data.get("timestamp")
                            if isinstance(ts, str) and ts > last_ts:
                                last_ts = ts
        except OSError:
            return None, 0, "", "", ""
        return meta, count, last_ts, first_user, latest_preview

    def _scan_listed(self, path: Path) -> tuple[dict[str, Any] | None, int, str, str, str]:
        """``_scan_file`` for a reader that only lists: a transcript that is not UTF-8 (one saved in a
        Windows code page) is left out as an unreadable one is, rather than its decoding error escaping
        the whole scan.

        Only for such a reader. A save reading it that way would take the file for an empty one and
        append UTF-8 lines no reader can open, so on the way to a write the error still stops it.

        Results are cached per path, keyed on ``(st_mtime_ns, st_size)`` taken
        before the read, so an unchanged transcript is parsed once and a hit
        returns the tuple the filling scan produced. Writes made through this
        manager drop the entry explicitly (``save``, ``delete``, and
        ``append_metadata_patch``); a write from another process is noticed by
        the stamp. Above ``_SCAN_CACHE_LIMIT`` distinct transcripts the LRU
        starts evicting entries that are still being polled, so an unchanged
        file can be scanned again -- that costs a read, never a stale result.
        """
        try:
            stat = path.stat()
        except OSError:
            return None, 0, "", "", ""
        cached = self._scan_cache.get(path)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            self._scan_cache.move_to_end(path)
            return cached[2]
        try:
            result = self._scan_file(path)
        except UnicodeDecodeError:
            result = (None, 0, "", "", "")
        # Stamped before the read: a turn appended during it makes the next
        # poll see a newer mtime and scan again, which is the safe direction.
        self._scan_cache[path] = (stat.st_mtime_ns, stat.st_size, result)
        self._scan_cache.move_to_end(path)
        if len(self._scan_cache) > _SCAN_CACHE_LIMIT:
            self._scan_cache.popitem(last=False)
        return result

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
        # The scan is the merge basis and the append lands it, so the same
        # cross-process transaction the saves hold spans both: a record that
        # lands in between would otherwise be rolled back by this merged copy
        # of the older one -- its log_generation, its last_consolidated, its
        # metadata keys.
        with write_transaction(path):
            last, _count, _last_ts, _first, _preview = self._scan_file(path)
            if last is None:
                return False
            merged = {**(last.get("metadata") or {}), **patch}
            locked_append(path, [json.dumps({**last, "metadata": merged}, ensure_ascii=False)])
        # The cached scan describes the file before this record. Drop it here
        # rather than trust the stamp, which can miss a same-size write on a
        # coarse clock.
        self._scan_cache.pop(path, None)
        cached = self._live(key)
        if cached is not None:
            # Blind to the patched keys: a caller often sets them on its own copy
            # first, and the patch has just put them on disk.
            unchanged = _unchanged_but_for(cached, patch.keys())
            cached.metadata.update(patch)
            # These keys are on disk now, so they are no longer this copy's
            # unsaved opinion: left out of the baseline the patch reads as a
            # local change on every later save and is asserted again over
            # whatever anybody wrote in between.
            cached._persisted_metadata.update(patch)
            # The cached copy's stamp is left where it was: this write is made for
            # the key, not by the copy. The file also carries the record this
            # merged into, and a stale stamp is what makes the next save re-read
            # it. Clean stays clean; a copy with changes of its own keeps them
            # unsaved.
            if unchanged:
                cached._clean_fingerprint = _fingerprint(cached)
        return True

    def save(self, session: Session) -> None:
        """Save a session to disk.

        Appends a fresh metadata record plus the not-yet-persisted messages
        under a cross-process lock. A message list cut since this copy last
        read or wrote the file (``clear``, ``undo_last_turn``) rewrites it
        atomically instead, however far the list has grown back: the cut
        messages are still in the file, and an append would leave them there.

        The store serves one writer per transcript. A file that has gained or
        lost message rows since this copy read it raises
        :class:`SessionWriterConflictError` instead of merging the other writer's
        rows in or writing over them. A metadata-only record appended since
        (``append_metadata_patch``) is not a second writer: it is folded under
        this copy's changed keys and the save goes on.

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

        # The read and the write are one transaction: the metadata record this
        # folds under can otherwise be written between them and lost anyway.
        with _writing_as(session, path) as before:
            written = self._record_written_since(session, path, before)
            if written is not None:
                self._refuse_second_writer(session, written)
            last = None if written is None else written[0]
            session.metadata = self._metadata_to_write(session, last)
            _took_in(session, before, written)
            rewrite = (
                len(session.messages) < session._persisted_count
                or _cut_since_persisted(session)
                or session._refile_rows
            )
            disk = None if last is None else _stored_revision(last)
            disk_scope = None if disk is None else disk[:2]
            messages: list[dict[str, Any]] = []
            if rewrite:
                # The file is about to hold exactly this copy's list, so neither
                # the scope it last saw nor the one on disk may name it.
                _move_past(session, session._persisted_revision, disk_scope)
                messages = _filed_rows(session.messages)
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
                lines += [json.dumps(m, ensure_ascii=False) for m in messages]
                atomic_replace(path, "".join(line + "\n" for line in lines))
                # The file now holds these rows, so this copy's list holds them
                # too; otherwise the next append would be written past a prefix
                # it never read. The repair is applied once, after a call still
                # open on the file can meet its result in this copy's tail.
                session.messages = repair_dangling_tool_calls(messages, mark_synthetic=True)
                session._persisted_file_count = len(messages)
            else:
                new_messages = _filed_rows(session.messages[session._persisted_count :])
                lines = [metadata_line]
                lines += [json.dumps(m, ensure_ascii=False) for m in new_messages]
                locked_append(path, lines)
                session._persisted_file_count += len(new_messages)
            session._persisted_metadata = dict(session.metadata)
            session._persisted_revision = (session.log_id, session.log_generation)

        # This process just wrote the transcript, so the listing cache is stale
        # by construction. The stamp alone is not enough: a same-size rewrite
        # can land in the same mtime tick (observed on NTFS), and serving the
        # previous listing is the unsafe direction.
        self._scan_cache.pop(path, None)
        session._refile_rows = False
        session._persisted_count = len(session.messages)
        session._persisted_truncations = session.truncations
        session._clean_fingerprint = _fingerprint(session)
        self._remember(session)

    def _refuse_second_writer(self, session: Session, written: tuple[dict[str, Any] | None, int]) -> None:
        """Refuse a save when the file's message rows are not the ones this copy last wrote.

        A metadata-only record appended since (an archive flag, the episodic
        cursor) leaves the rows and the revision alone and is folded in by
        ``_metadata_to_write``. Anything else means a second writer is running
        the same session, and merging its rows or writing over them would
        silently lose one side's turn.
        """
        last, count = written
        if not _message_rows_moved(session, last, count):
            return
        # Dropped before the raise, because the message below tells the caller to
        # reload and nothing else would: the copy stays cached otherwise, and
        # every later save of it is refused for the rest of the process's life.
        # The next ``get_or_create`` reads the transcript the other writer left.
        self._forget(session)
        raise SessionWriterConflictError(
            f"session {session.key!r} changed on disk since this copy loaded it: "
            "another process is writing this session. This copy is stale: the next read "
            "of the session returns the transcript the other writer left."
        )

    def _forget(self, session: Session) -> None:
        """Drop ``session`` from the cache and the held map, and not a copy that replaced it.

        A refused copy can be refused again -- a turn keeps it until its closing
        save -- after the next load has cached a fresh copy under the same key.
        Dropping by key evicted that one, and the load after it made a second
        copy beside the first, each refusing the other's saves.
        """
        if self._cache.get(session.key) is session:
            self._cache.pop(session.key, None)
        if self._held.get(session.key) is session:
            self._held.pop(session.key, None)

    def drop_filed_rows(self, session: Session, row_ids: Collection[str]) -> bool:
        """Take the rows filed under ``row_ids`` off the transcript.

        The failed attempt filed its rows as it ran, so a rerun that only reset
        the in-memory log left its research in the transcript and appended the
        rerun's answer behind the wreckage. The cut is by identifier, not by
        position: the window can drop a row from the turn record, and a cut by
        position would take a neighbour with it. The file is rewritten, which is
        what makes the cut land however far the in-memory list has grown back.

        True when the file no longer holds any of the rows; False when the cut
        could not be written, so the caller does not go on to append an answer
        over it.
        """
        original = session.messages
        original_truncations = session.truncations
        original_count = session._persisted_count
        kept = [row for row in original if str(row.get("_row_id") or "") not in row_ids]
        if len(kept) == len(original):
            return True
        session.messages = kept
        session.truncations += 1
        try:
            self.save(session)
        except Exception:
            session.messages = original
            session.truncations = original_truncations
            session._persisted_count = original_count
            logger.exception("could not drop the filed rows from {}; the caller must not append over them", session.key)
            return False
        return True

    def _record_written_since(
        self, session: "Session", path: Path, stamp: tuple[int, int, str] | None
    ) -> tuple[dict[str, Any] | None, int] | None:
        """The file's last metadata record and message count, if anybody wrote to it since this copy last read or wrote it.

        ``stamp`` is the file's stamp as the write lock found it. None while the
        file is byte for byte what this copy last read or wrote, which is every
        save in the ordinary case of one writer -- so a conversation pays for the
        scan only when somebody else has actually written to it.
        """
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
        with the removal outcome, and the directory the session lived in when the
        observer declares ``session_dir``; an observer that raises is logged and
        skipped (paper: contracts/session_events.py), so none can change this verdict.
        """
        path = self.session_path(key)
        # Resolved while the transcript is still there to find: a session another process
        # filed under its project's slug is found only through it, and an observer that
        # asked once it was gone would be told this process's own group instead.
        directory = self.session_dir(key)
        self.invalidate(key)
        # A removed file is never hit (its stat fails), but a later session
        # written at the same path must start from a miss rather than from a
        # stale entry that happened to share its stamp.
        self._scan_cache.pop(path, None)
        removed = False
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError:
                logger.warning("session.delete: failed to remove file for {}", key)
        for observer in self._delete_observers:
            try:
                if _declares(observer.on_session_deleted, "session_dir"):
                    observer.on_session_deleted(key, removed, session_dir=directory)
                else:
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
            # The parent's file may end mid-call. The child is served from this
            # process's cache as well as from its file, and a strict provider
            # rejects the unrepaired call, so the copy it is cached as is
            # repaired here; the save below leaves the synthetic results out of
            # the file, which derives them again on read.
            messages=repair_dangling_tool_calls(
                [copy.deepcopy(m) for m in _filed_rows(source.messages)], mark_synthetic=True
            ),
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
        # parent's model, and at the reasoning effort picked for it. The caller
        # re-points the live binding, but that lives in memory only -- without
        # carrying the record too, the fork drops to the default the first time
        # it is resumed in a new process.
        for slot in FORK_CARRIES:
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
        unpersisted). A :class:`SessionWriterConflictError` is raised rather than
        swallowed: the copy has been dropped with what it had not saved, which a
        False would report as a write that may still land.
        """
        cached = self._live(key)
        if cached is None:
            return True
        if len(cached.messages) > cached._persisted_count or _cut_since_persisted(cached):
            try:
                self.save(cached)
            except SessionWriterConflictError:
                raise
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
            data, message_count, last_ts, first_user, latest_preview = self._scan_listed(path)
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
            # The scan cache returns the same parsed record on every hit, so the
            # caller gets its own copy of the one mutable field rather than a
            # handle into the cache.
            metadata = data.get("metadata", {})
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
                    "metadata": dict(metadata) if isinstance(metadata, dict) else metadata,
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
