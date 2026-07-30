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
_IMAGE_PLACEHOLDER = "[image x{count}]"
_TEXT_FIELD_BY_TYPE = {"text": "text", "input_text": "content"}
_IMAGE_TYPES = frozenset({"image_url", "input_image"})

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
# user-track extraction skips a memcell with no role=user sender outright.
_ENTRY_ROLE = {"user-md": "user", "memory-md": "assistant"}
_PREAMBLE = {"user-md": _USER_MD_PREAMBLE, "memory-md": _MEMORY_MD_PREAMBLE}


def split_memory_entries(raw: str) -> list[str]:
    """Split a Hermes memory file's raw text into stripped, non-blank entries."""
    return [e.strip() for e in raw.split(_ENTRY_DELIMITER) if e.strip()]


def resolve_hermes_home() -> Path:
    """Mirror Hermes' own resolution: HERMES_HOME, else the platform default.

    Only the resolved home is imported. Hermes also supports named profiles
    under ``<root>/profiles/<name>``, each with its own memories/ and skills/;
    importing every profile would need a cross-profile collision policy that no
    current use case calls for.
    """
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / "hermes"
    return Path.home() / ".hermes"


class HermesScanner:
    """Discovers and reads Hermes Agent local data for cold-start import."""

    platform = Platform.HERMES

    def __init__(self, hermes_home: Path | None = None) -> None:
        self._home = hermes_home if hermes_home is not None else resolve_hermes_home()

    async def scan(self) -> list[ScanResult]:
        if not self._home.is_dir():
            logger.info("hermes not installed at {}; nothing to import", self._home)
            return []
        return self._scan_memory_files()

    async def read(self, result: ScanResult) -> ImportSession:
        if result.kind is SourceKind.MEMORY_FILE:
            return self._read_memory_file(result)
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
            ImportMessage(role="user", content=preamble, timestamp=base_ms, sender_id="user"),
        ]
        for i, entry in enumerate(entries, start=1):
            messages.append(
                ImportMessage(
                    role=entry_role,
                    content=entry,
                    timestamp=base_ms + i,
                    sender_id="user",
                )
            )
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

    Hermes documents four content-part shapes: ``text`` (text under ``text``),
    ``input_text`` (text under ``content`` instead), ``image_url`` and
    ``input_image``. Neither real export sampled here contains list content at
    all -- the image-heavy session on that install had not ended, and only ended
    sessions are exportable -- so this path rests on the documented shapes rather
    than on observed data.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    texts: list[str] = []
    images = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        text_field = _TEXT_FIELD_BY_TYPE.get(part_type)
        if text_field is not None:
            text = (part.get(text_field) or "").strip()
            if text:
                texts.append(text)
        elif part_type in _IMAGE_TYPES:
            images += 1
        else:
            # Unrecognised part shape: recover any string text/content it
            # carries instead of discarding it, and never count it as an image.
            fallback = part.get("text") or part.get("content")
            if isinstance(fallback, str) and fallback.strip():
                texts.append(fallback.strip())
    if images:
        texts.append(_IMAGE_PLACEHOLDER.format(count=images))
    return "\n\n".join(texts)


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
                sender_id="user" if role == "user" else "assistant",
                tool_calls=tuple(tool_calls) if tool_calls else None,
                tool_call_id=raw.get("tool_call_id"),
            )
        )
    return out


# -- session export listing ------------------------------------------------

_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]+$")
_DRYRUN_HEADER_RE = re.compile(r"^Would export (\d+) session\(s\)")

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
    would report "0 conversations, done" and look like success, so the
    header count is cross-checked and an unrecognised header is an error
    rather than a zero. A header count above ``len(session_ids)`` is the
    documented 100-cap, not a parse error -- the caller decides what to do
    about it.
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
        if not stripped:
            continue
        token = stripped.split()[0]
        if _SESSION_ID_RE.match(token):
            ids.append(token)

    if expected > 0 and not ids:
        raise HermesExportError(f"hermes reported {expected} session(s) but no session id could be parsed")
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


async def _run_hermes_dry_run(args: Sequence[str]) -> str:
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

    A single unwindowed probe answers the question directly when hermes
    counts at most the 100 it is willing to print. Above that, the
    ``started_at`` axis is partitioned into half-open, minute-aligned
    windows (the finest hermes' own minute-truncated ``--after``/``--before``
    can express) and probed recursively, so a window this parser cannot
    resolve further contributes its listed ids plus a counted, logged
    remainder instead of either raising or silently dropping sessions.
    """
    active_runner = runner if runner is not None else _run_hermes_dry_run
    root = await _probe(active_runner, None, None)
    if root.expected <= _LISTING_CAP:
        return SessionListing(session_ids=root.session_ids, unlisted=0)

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
