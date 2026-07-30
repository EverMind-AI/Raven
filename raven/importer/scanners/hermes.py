"""Hermes Agent scanner -- memory files and conversations.

Conversation listing drives the ``hermes`` CLI's own
``sessions export --dry-run`` rather than reading Hermes' SQLite store
directly, because that schema is internal and versioned. Only ended sessions
are ever candidates there (Hermes' own prune-candidate filter always
requires ``ended_at IS NOT NULL``), so a currently live session is never
listed -- that gap is a property of the data source, not data loss by this
module.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from raven.importer.types import ImportMessage, ImportSession, Platform, ScanResult, SourceKind
from raven.utils.text import is_cjk

_MEMORY_SOURCES = (("user-md", "USER.md"), ("memory-md", "MEMORY.md"))

_CONTENT_TRUNCATE_LIMIT = 10_000
_ACCEPTED_ROLES = frozenset({"user", "assistant", "tool"})
_NON_TEXT_PLACEHOLDER = "[media x{count}]"

# Mirrors agent/message_content.py upstream. A text part can carry its text
# under either key -- run_agent reads "text" for input_text as well as for text,
# while the canonical flattener tries "content" next -- so reading only one of
# them drops real prose, and drops the whole message once nothing is left.
_TEXT_KEYS = ("text", "content")
_NON_TEXT_TYPES = frozenset({"image", "image_url", "input_image", "audio", "input_audio"})

# Hermes' own parser splits on the newline-wrapped section sign specifically so
# that an entry whose own text contains a bare "§" is not torn in half.
_ENTRY_DELIMITER = "\n§\n"

# ``(zh, en)`` pairs. The preamble is itself extracted, so it is phrased as a
# fact that is true on its own ("the user worked in Hermes") rather than as an
# instruction, which would surface later as a stray directive.
_USER_MD_PREAMBLE = (
    "以下是我在 Hermes AI 助手中积累的个人档案，共 {count} 条。",
    "These are the personal profile facts I accumulated in the Hermes AI assistant, {count} in total.",
)
_MEMORY_MD_PREAMBLE = (
    "我之前用 Hermes AI 助手工作，下面是它当时记录下来的事实，共 {count} 条。",
    "I worked with the Hermes AI assistant before; below are the facts it recorded, {count} in total.",
)
# MEMORY.md is the assistant's own notes, written in its first person, so the
# entries are assistant turns. The user preamble is not decoration: EverOS'
# user-track extraction skips any memcell whose role is not "user" outright.
_ENTRY_ROLE = {"user-md": "user", "memory-md": "assistant"}
_PREAMBLE = {"user-md": _USER_MD_PREAMBLE, "memory-md": _MEMORY_MD_PREAMBLE}


def split_memory_entries(raw: str) -> list[str]:
    """Split a Hermes memory file's raw text into stripped, non-blank entries."""
    return [e.strip() for e in raw.split(_ENTRY_DELIMITER) if e.strip()]


_DEFAULT_PROFILE_NAME = "default"


def resolve_hermes_home() -> Path:
    """Mirror Hermes' own resolution: HERMES_HOME, else the active profile
    under the platform-default root, else the root itself.

    Hermes stores named profiles under ``<root>/profiles/<name>``, each with
    its own memories/ and skills/ (``hermes_cli/profiles.py``). When
    ``HERMES_HOME`` is unset and a non-default profile is sticky-active
    (``<root>/active_profile``), reading the root would import that user's
    *default* profile while their actual data sits in the active one --
    upstream treats this as a bug serious enough to warn loudly about rather
    than a silent, equally-valid fallback (``hermes_constants.py``,
    ``_warn_profile_fallback_once``).
    """
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    root = _platform_default_hermes_home()
    active = _read_active_profile(root)
    if active:
        return root / "profiles" / active
    return root


def _platform_default_hermes_home() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _read_active_profile(root: Path) -> str:
    """Return the sticky active profile name, or "" when it is the default.

    Mirrors ``get_active_profile`` + ``normalize_profile_name``
    (``hermes_cli/profiles.py``): the file's content is trimmed, and both a
    missing/empty file and a value that case-insensitively reads "default"
    mean the root itself, not a named profile directory.
    """
    try:
        raw = (root / "active_profile").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not raw or raw.casefold() == _DEFAULT_PROFILE_NAME:
        return ""
    return raw.lower()


class HermesScanner:
    """Discovers and reads Hermes Agent local data for cold-start import."""

    platform = Platform.HERMES

    def __init__(self, hermes_home: Path | None = None, run_cli: DryRunRunner | None = None) -> None:
        self._home = hermes_home if hermes_home is not None else resolve_hermes_home()
        self._run_cli = run_cli if run_cli is not None else _run_hermes_cli
        self.partial_failure: Exception | None = None
        """Set when scan() returned an incomplete result on purpose -- read by
        scan_all so the reason reaches the user rather than only the log."""

    async def scan(self) -> list[ScanResult]:
        if not self._home.is_dir():
            logger.info("hermes not installed at {}; nothing to import", self._home)
            return []
        memory_files = self._scan_memory_files()
        try:
            conversations = await self._scan_conversations()
        except HermesExportError as exc:
            # The memory files are already in hand and never needed the CLI, so
            # a missing or failing hermes binary must not take them down with
            # the conversations. The failure still has to reach the user, which
            # is what partial_failure is for.
            logger.warning("hermes conversations cannot be enumerated: {}", exc)
            self.partial_failure = exc
            return memory_files
        return memory_files + conversations

    async def read(self, result: ScanResult) -> ImportSession:
        if result.kind is SourceKind.MEMORY_FILE:
            return self._read_memory_file(result)
        if result.kind is SourceKind.CONVERSATION:
            return await self._read_conversation(result)
        raise ValueError(f"unsupported scan result kind: {result.kind}")

    # -- scan ---------------------------------------------------------------

    def _scan_memory_files(self) -> list[ScanResult]:
        out: list[ScanResult] = []
        for source_key, filename in _MEMORY_SOURCES:
            path = self._home / "memories" / filename
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(
                ScanResult(
                    source_key=source_key,
                    platform=Platform.HERMES,
                    kind=SourceKind.MEMORY_FILE,
                    file_paths=(path,),
                    estimated_size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
        return out

    # -- read -----------------------------------------------------------------

    def _read_memory_file(self, result: ScanResult) -> ImportSession:
        session_id = f"import-hermes-{result.source_key}"
        (path,) = result.file_paths
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return ImportSession(session_id=session_id, messages=())

        entries = split_memory_entries(raw)
        if not entries:
            return ImportSession(session_id=session_id, messages=())

        base_ms = int(result.mtime * 1000)
        # Sample the whole file rather than is_cjk's 200-char default: these
        # files are a short list of atomic facts, so the first entry's language
        # does not represent the rest. ClaudeCodeScanner keeps the default
        # because it reads much larger aggregated bodies.
        cjk = is_cjk(raw, sample=len(raw))
        preamble = _PREAMBLE[result.source_key][0 if cjk else 1].format(count=len(entries))
        entry_role = _ENTRY_ROLE[result.source_key]

        messages = [
            ImportMessage(role="user", content=preamble, timestamp=base_ms),
        ]
        for i, entry in enumerate(entries, start=1):
            messages.append(
                ImportMessage(
                    role=entry_role,
                    content=entry,
                    timestamp=base_ms + i,
                )
            )
        return ImportSession(session_id=session_id, messages=tuple(messages))

    # -- conversations --------------------------------------------------------

    async def _scan_conversations(self) -> list[ScanResult]:
        try:
            listing = await list_exportable_sessions(runner=self._run_cli)
        except HermesExportError:
            raise
        except OSError as exc:
            raise HermesExportError(f"cannot run the hermes CLI ({exc}); conversations cannot be imported") from exc
        if listing.unlisted:
            logger.warning(
                "hermes has {} session(s) that could not be enumerated; they will not be imported",
                listing.unlisted,
            )
        return [
            ScanResult(
                source_key=sid,
                platform=Platform.HERMES,
                kind=SourceKind.CONVERSATION,
                file_paths=(),
                estimated_size=0,
                mtime=0.0,
            )
            for sid in listing.session_ids
        ]

    async def _read_conversation(self, result: ScanResult) -> ImportSession:
        session_id = f"import-hermes-{result.source_key}"
        args = [*_EXPORT_ARGS, "--session-id", result.source_key, "-"]
        try:
            raw = await self._run_cli(args)
        except HermesExportError:
            raise
        except OSError as exc:
            raise HermesExportError(f"cannot run the hermes CLI ({exc}); session not imported") from exc
        messages: list[ImportMessage] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            messages.extend(hermes_session_to_messages(_parse_exported_line(stripped, raw, result.source_key)))
        return ImportSession(session_id=session_id, messages=tuple(messages))


# -- exported session conversion -------------------------------------------


def strip_images(content: Any) -> str:
    """Flatten multimodal content to text, replacing images with a marker.

    ImportMessage.content is a str and the EverOS adapter reduces list content
    to its text parts anyway, so an inlined base64 image would be carried the
    whole way only to be discarded. Carrying it is not free either: a data URI
    runs past the importer's own 30,000-character batch limit
    (``orchestrator._BATCH_CHAR_LIMIT``) on its own, so it would be posted as a
    batch of one sized to the image.

    No exportable session on the machine this was written against carries list
    content at all, because the image-heavy one had not ended, so the part
    handling follows agent/message_content.py upstream rather than observed data.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    texts: list[str] = []
    non_text = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in _NON_TEXT_TYPES:
            non_text += 1
            continue
        # Text parts and unrecognised ones are treated alike: take whichever
        # text key is populated. Guessing at an unknown shape's meaning risks
        # discarding prose, while counting it as an image would invent one.
        text = _first_text(part)
        if text:
            texts.append(text)
    if non_text:
        texts.append(_NON_TEXT_PLACEHOLDER.format(count=non_text))
    return "\n\n".join(texts)


def _first_text(part: dict[str, Any]) -> str:
    for key in _TEXT_KEYS:
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _truncate(text: str) -> str:
    if len(text) <= _CONTENT_TRUNCATE_LIMIT:
        return text
    return text[:_CONTENT_TRUNCATE_LIMIT] + "..."


def hermes_session_to_messages(session: dict[str, Any]) -> list[ImportMessage]:
    """Map one exported Hermes session onto ImportMessage.

    ``system_prompt`` lives at session level and is Hermes' own prompt, not user
    content; EverOS has no system role either. ``session_meta`` is not an
    accepted role. ``reasoning`` / ``reasoning_content`` are chain-of-thought,
    dropped for the same reason ClaudeCodeScanner drops thinking blocks.

    ``active`` and ``compacted`` need no filtering here: Hermes' own exporter
    already calls ``get_messages(session_id, include_inactive=False)``, so
    rewound and compaction-archived messages never reach this payload.
    """
    out: list[ImportMessage] = []
    for raw in session.get("messages") or []:
        role = raw.get("role")
        if role not in _ACCEPTED_ROLES:
            continue
        ts = raw.get("timestamp")
        if not isinstance(ts, (int, float)) or ts <= 0:
            continue
        content = _truncate(strip_images(raw.get("content")))
        tool_calls = raw.get("tool_calls") or None
        if not content and not tool_calls:
            continue
        out.append(
            ImportMessage(
                role=role,
                content=content,
                timestamp=int(ts * 1000),
                tool_calls=tuple(tool_calls) if tool_calls else None,
                tool_call_id=raw.get("tool_call_id"),
            )
        )
    return out


def _parse_exported_line(line: str, raw: str, session_id: str) -> dict[str, Any]:
    """Decode one line of ``sessions export --format jsonl`` output.

    Hermes can print a failure to stdout and still exit 0 (``hermes_cli/
    main.py``), so a bad line here is a hermes-side error, not malformed JSON
    from a healthy export. ``json.JSONDecodeError``'s own message ("Expecting
    value: line 1 column 1 (char 0)") names neither the session nor what
    hermes actually said, so both are added here.
    """
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        first_line = raw.splitlines()[0].strip() if raw.strip() else ""
        raise HermesExportError(
            f"hermes export for session {session_id!r} did not return valid JSON; hermes said: {first_line!r}"
        ) from exc


# -- session export listing ------------------------------------------------

_DRYRUN_HEADER_RE = re.compile(r"^Would export (\d+) session\(s\)")

# The listing's own row format is f"  {id}  {source}" (hermes_cli/main.py), so
# the id is the first token of an indented line. Matching a shape instead would
# silently drop whole classes of session: cron ids are `cron_<job>_<stamp>` and
# ACP ids are bare uuid4, neither of which looks like the timestamped default.
_TRUNCATION_MARKER = "..."

# The trailing "-" writes the export to stdout instead of a file, same as the
# dry-run listing's own "-" argument, so the single DryRunRunner seam serves
# both call shapes.
_EXPORT_ARGS = ("sessions", "export", "--format", "jsonl")

# hermes truncates the printed listing at this many rows even when the
# header count is higher (hermes_cli/main.py: `candidates[:100]`).
_LISTING_CAP = 100

_BOUND_FMT = "%Y-%m-%d %H:%M"

# `--after`/`--before` truncate to the minute, so any window narrower than
# this cannot be expressed and is treated as a leaf.
_MIN_WINDOW = timedelta(minutes=1)

# The partition's left edge, and the one bound that cannot be a judgment call:
# a session starting before it falls outside every window and is dropped with
# nothing to notice the loss. A recent-looking floor would be cheaper by a
# handful of probes and wrong the first time a clock skew or a migrated store
# produced an older started_at, so the epoch it is -- the empty halves left of
# real data return on their first probe without recursing.
_PARTITION_FLOOR = datetime(1970, 1, 1)


class HermesExportError(RuntimeError):
    """The hermes CLI produced output this parser does not recognise."""


@dataclass(frozen=True)
class DryRunListing:
    """One parsed ``sessions export --dry-run`` invocation."""

    expected: int
    session_ids: tuple[str, ...]


@dataclass(frozen=True)
class SessionListing:
    """The exportable session ids assembled from one or more dry-run probes."""

    session_ids: tuple[str, ...]
    unlisted: int


# A probe: the argument list after the ``hermes`` executable, returning
# stdout. Real callers spawn a subprocess; tests inject a fake in-memory one.
DryRunRunner = Callable[[Sequence[str]], Awaitable[str]]


def parse_dry_run_listing(text: str) -> DryRunListing:
    """Extract session ids from ``sessions export --dry-run`` output.

    The output is written for humans and upstream can change it at any time.
    The dangerous failure is not a crash but a silent empty result, which
    would report "0 conversations, done" and look like success, so the header
    count is kept and an unrecognised header is an error rather than a zero.
    Every caller must reconcile ``expected`` against the ids it received: a
    shortfall is the documented 100-cap or a row this parser did not recognise,
    and both mean sessions that will not be imported.
    """
    lines = text.splitlines()
    header = None
    expected = 0
    for line in lines:
        matched = _DRYRUN_HEADER_RE.match(line.strip())
        if matched is not None:
            header, expected = line, int(matched.group(1))
            break
    if header is None:
        raise HermesExportError("unrecognised hermes export header; refusing to assume there are no sessions")

    ids: list[str] = []
    for line in lines:
        if line is header or not line.startswith(" "):
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(_TRUNCATION_MARKER):
            continue
        ids.append(stripped.split()[0])

    if expected > 0 and not ids:
        raise HermesExportError(f"hermes reported {expected} session(s) but no session id could be parsed")
    if len(ids) > expected:
        # hermes prints at most the number it counted, so a surplus means a row
        # was read as a session that is not one. Exporting it would fail per
        # session, but raising here says which listing is no longer understood.
        raise HermesExportError(
            f"hermes reported {expected} session(s) but {len(ids)} listing rows parsed as ids; "
            "the listing format is no longer understood"
        )
    return DryRunListing(expected=expected, session_ids=tuple(ids))


def _format_bound(dt: datetime) -> str:
    return dt.strftime(_BOUND_FMT)


def _ceil_to_minute(dt: datetime) -> datetime:
    truncated = dt.replace(second=0, microsecond=0)
    return truncated if truncated == dt else truncated + timedelta(minutes=1)


def _midpoint_minute(start: datetime, end: datetime) -> datetime:
    whole_minutes = (end - start) // timedelta(minutes=1)
    return start + timedelta(minutes=whole_minutes // 2)


def _dry_run_args(start: datetime | None, end: datetime | None) -> list[str]:
    args = ["sessions", "export", "--dry-run", "--min-messages", "1"]
    if start is not None:
        args += ["--after", _format_bound(start)]
    if end is not None:
        args += ["--before", _format_bound(end)]
    args.append("-")
    return args


async def _probe(runner: DryRunRunner, start: datetime | None, end: datetime | None) -> DryRunListing:
    stdout = await runner(_dry_run_args(start, end))
    return parse_dry_run_listing(stdout)


async def _collect_window(runner: DryRunRunner, start: datetime, end: datetime) -> tuple[tuple[str, ...], int]:
    listing = await _probe(runner, start, end)
    if listing.expected <= _LISTING_CAP:
        return listing.session_ids, 0
    if end - start <= _MIN_WINDOW:
        unlisted = listing.expected - len(listing.session_ids)
        logger.warning(
            "hermes window {}..{} has {} sessions, past the {} the dry-run listing caps at; {} will not be imported",
            _format_bound(start),
            _format_bound(end),
            listing.expected,
            _LISTING_CAP,
            unlisted,
        )
        return listing.session_ids, unlisted
    mid = _midpoint_minute(start, end)
    left_ids, left_unlisted = await _collect_window(runner, start, mid)
    right_ids, right_unlisted = await _collect_window(runner, mid, end)
    return left_ids + right_ids, left_unlisted + right_unlisted


async def _run_hermes_cli(args: Sequence[str]) -> str:
    """Invoke the ``hermes`` binary with ``args`` and return its stdout.

    Despite the historical name of its call sites' ``DryRunRunner`` type, this
    is the one seam both the dry-run listing probe and the real
    ``sessions export`` (no ``--dry-run``) go through -- see ``_read_conversation``.
    """
    hermes_path = shutil.which("hermes")
    if hermes_path is None:
        raise HermesExportError("hermes executable not found on PATH")
    proc = await asyncio.create_subprocess_exec(
        hermes_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HermesExportError(
            f"hermes exited with code {proc.returncode}: {stderr.decode('utf-8', 'replace').strip()}"
        )
    return stdout.decode("utf-8", errors="replace")


async def list_exportable_sessions(*, runner: DryRunRunner | None = None) -> SessionListing:
    """Find every Hermes session id that ``sessions export`` can produce.

    A single unwindowed probe answers the question directly when hermes counts
    at most the 100 it is willing to print. Above that, the ``started_at`` axis
    is partitioned into half-open, minute-aligned windows and probed
    recursively, so a window this parser cannot resolve further contributes its
    listed ids plus a counted, logged remainder instead of either raising or
    silently dropping sessions.

    Both paths reconcile against the header count, including the short one every
    install with fewer than a hundred sessions takes: a row the parser did not
    recognise is invisible there otherwise, and reporting fewer conversations
    than hermes counted while claiming success is the failure this whole
    enumeration exists to avoid.
    """
    active_runner = runner if runner is not None else _run_hermes_cli
    root = await _probe(active_runner, None, None)
    if root.expected <= _LISTING_CAP:
        ids = tuple(dict.fromkeys(root.session_ids))
        return SessionListing(session_ids=ids, unlisted=_uncovered(root.expected, len(ids), 0))

    ceiling = _ceil_to_minute(datetime.now()) + timedelta(minutes=1)
    session_ids, unlisted = await _collect_window(active_runner, _PARTITION_FLOOR, ceiling)
    unique = tuple(dict.fromkeys(session_ids))
    return SessionListing(session_ids=unique, unlisted=unlisted + _uncovered(root.expected, len(unique), unlisted))


def _uncovered(expected: int, collected: int, unlisted: int) -> int:
    """Reconcile the windows against the count the root probe reported.

    Arithmetic is the only thing that can catch a coverage bug here -- a
    partition that misses a range returns a perfectly well-formed short list.

    The race runs both ways, so neither direction is treated as an error: a
    session ending mid-scan joins the candidate set, and resuming one leaves it
    again, because Hermes clears ``ended_at`` on resume (``reopen_session``) and
    only ended sessions are candidates. Counting rather than raising also keeps
    a genuine coverage bug from failing an import outright, at the cost of
    making the two causes indistinguishable from here.
    """
    missing = expected - collected - unlisted
    if missing <= 0:
        return 0
    logger.warning(
        "hermes reported {} exportable session(s) but windowed probing accounted for {}; {} unaccounted for",
        expected,
        collected + unlisted,
        missing,
    )
    return missing


__all__ = [
    "DryRunListing",
    "DryRunRunner",
    "HermesExportError",
    "HermesScanner",
    "SessionListing",
    "hermes_session_to_messages",
    "list_exportable_sessions",
    "parse_dry_run_listing",
    "resolve_hermes_home",
    "split_memory_entries",
    "strip_images",
]
