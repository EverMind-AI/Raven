"""Redaction history: the user's keep/replace decisions on suspected Review Items.

A bug report review asks the user, per suspected token value, whether to keep
it or replace it with ``[REDACTED:user-confirmed]``. This module remembers
those answers on disk so the next review can replay them instead of asking
again. It is independent of any CLI or UI, so the terminal flows and a web
report flow share one store and one set of rules.

Storage is ``<raven home>/redaction_history.json`` (mode 0600), keyed by the
sha256 of the token value. Only the hash and a masked sample are kept: a value
the user chose to replace is a confirmed secret and must not be written to
disk again in plaintext. Replacing a replayed value still happens in memory,
through the review flow, from the token the residual scan found.

Rules the callers rely on:

- :meth:`RedactionHistory.resolve` decides an item -- or its whole linked
  group -- only when every member has a recorded, agreeing decision;
  confirmed-sensitive items are always left to the user.
- Every human decision gets a fresh ``revision``. A replay is counted only
  against the revision it read, so a stale replay never overrides a newer
  human answer and never revives a forgotten record.
- Writes run under the :mod:`raven.utils.atomic_io` write lock but do their
  own read, backup and replace: an unreadable file is backed up with an
  exclusive create before it is replaced, and every file-system failure
  raises :class:`HistoryStorageError` with the file and no temp files left
  behind. A result is returned only after it was saved.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from raven.home import raven_home
from raven.trajectory.redact import _mask
from raven.trajectory.review import (
    ACTION_KEPT,
    ACTION_REDACTED,
    KIND_SUSPECTED,
    ReviewDecision,
    ReviewItem,
)
from raven.utils.atomic_io import write_transaction
from raven.utils.portable_lock import LockTimeoutError

HISTORY_FILENAME = "redaction_history.json"
SCHEMA_VERSION = 1
ENV_SWITCH = "RAVEN_BUGREPORT_REDACTION_HISTORY"
SOURCES = frozenset({"cli", "browser", "web"})

_ACTIONS = frozenset({ACTION_KEPT, ACTION_REDACTED})
_OFF_VALUES = frozenset({"off", "0", "false"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{16}$")
_ID_PREFIX = re.compile(r"^[0-9a-f]{12,64}$")
_MAX_BACKUP_SUFFIX = 99

T = TypeVar("T")


class HistoryStorageError(Exception):
    """Reading or saving the redaction history failed at the file-system level."""


@dataclass(frozen=True)
class RememberedDecision:
    """One remembered decision on one token value."""

    digest: str
    id: str
    action: str
    category: str
    masked: str
    decided_at: str
    source: str
    revision: str
    applied: int
    last_applied_at: str | None


@dataclass(frozen=True)
class HistorySnapshot:
    """The whole history as read; ``corrupt`` explains a file read as empty."""

    entries: list[RememberedDecision]
    corrupt: str | None


@dataclass(frozen=True)
class HistoryResolution:
    """What :meth:`RedactionHistory.resolve` decided for a set of review items."""

    auto: list[ReviewDecision]
    remaining: list[ReviewItem]
    replayed: dict[str, RememberedDecision]
    hints: dict[str, RememberedDecision]
    corrupt: str | None


@dataclass(frozen=True)
class RememberResult:
    """What :meth:`RedactionHistory.remember` saved."""

    recorded: int
    replayed: int
    stale: int
    backup: Path | None = None


@dataclass(frozen=True)
class ForgetResult:
    """What :meth:`RedactionHistory.forget` removed, or why it removed nothing."""

    removed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    backup: Path | None = None


@dataclass(frozen=True)
class ClearResult:
    """What :meth:`RedactionHistory.clear` removed."""

    removed: int
    backup: Path | None = None


@dataclass
class _Parsed:
    entries: dict[str, RememberedDecision] = field(default_factory=dict)
    corrupt: str | None = None
    unsupported: bool = False


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """False when ``RAVEN_BUGREPORT_REDACTION_HISTORY`` is off, 0 or false."""
    env = os.environ if environ is None else environ
    return env.get(ENV_SWITCH, "").strip().lower() not in _OFF_VALUES


def default_path() -> Path:
    return raven_home() / HISTORY_FILENAME


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_revision() -> str:
    return secrets.token_hex(8)


def _backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _reason(exc: OSError) -> str:
    return exc.strerror or exc.__class__.__name__


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _entry_from_raw(digest: str, raw: Any) -> RememberedDecision | None:
    if not _DIGEST.match(digest) or not isinstance(raw, dict):
        return None
    action = raw.get("action")
    revision = raw.get("revision")
    applied = raw.get("applied")
    last_applied_at = raw.get("last_applied_at")
    if not isinstance(action, str) or action not in _ACTIONS:
        return None
    if not all(_non_empty_str(raw.get(name)) for name in ("category", "masked", "decided_at", "source")):
        return None
    if not isinstance(revision, str) or not _REVISION.match(revision):
        return None
    if not _is_int(applied) or applied < 0:
        return None
    if last_applied_at is not None and not _non_empty_str(last_applied_at):
        return None
    return RememberedDecision(
        digest=digest,
        id=digest[:12],
        action=action,
        category=raw["category"],
        masked=raw["masked"],
        decided_at=raw["decided_at"],
        source=raw["source"],
        revision=revision,
        applied=applied,
        last_applied_at=last_applied_at,
    )


def _parse(raw: bytes) -> _Parsed:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _Parsed(corrupt="not valid UTF-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return _Parsed(corrupt=f"not valid JSON (line {exc.lineno})")
    if not isinstance(document, dict):
        return _Parsed(corrupt="unexpected top-level structure")
    version = document.get("schema_version")
    if _is_int(version) and version > SCHEMA_VERSION:
        return _Parsed(corrupt=f"written by a newer raven (schema_version {version})", unsupported=True)
    if not _is_int(version) or version != SCHEMA_VERSION:
        return _Parsed(corrupt="unsupported schema_version")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, dict):
        return _Parsed(corrupt="entries is not an object")
    entries: dict[str, RememberedDecision] = {}
    for digest, raw_entry in raw_entries.items():
        entry = _entry_from_raw(digest, raw_entry) if isinstance(digest, str) else None
        if entry is None:
            label = digest[:12] if isinstance(digest, str) and digest else "?"
            return _Parsed(corrupt=f"malformed entry {label}")
        entries[digest] = entry
    return _Parsed(entries=entries)


def _serialize(entries: Mapping[str, RememberedDecision]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "entries": {
            digest: {
                "action": entry.action,
                "category": entry.category,
                "masked": entry.masked,
                "decided_at": entry.decided_at,
                "source": entry.source,
                "revision": entry.revision,
                "applied": entry.applied,
                "last_applied_at": entry.last_applied_at,
            }
            for digest, entry in entries.items()
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _manual_entry(token: str, action: str, category: str, source: str, now: str) -> RememberedDecision:
    digest = _digest(token)
    return RememberedDecision(
        digest=digest,
        id=digest[:12],
        action=action,
        category=category,
        masked=_mask(token),
        decided_at=now,
        source=source,
        revision=_new_revision(),
        applied=0,
        last_applied_at=None,
    )


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"unknown decision source {source!r}")


def _components(suspected: Mapping[str, ReviewItem]) -> list[list[str]]:
    """Connected groups over ``linked``.

    Linked pairs are value containments, so A and B can each sit inside C
    without being linked to each other; all three must still share one
    decision, which is why groups are components rather than direct pairs.
    """
    seen: set[str] = set()
    groups: list[list[str]] = []
    for start in suspected:
        if start in seen:
            continue
        seen.add(start)
        group = [start]
        queue = [start]
        while queue:
            current = queue.pop()
            for neighbour in suspected[current].linked:
                if neighbour in suspected and neighbour not in seen:
                    seen.add(neighbour)
                    group.append(neighbour)
                    queue.append(neighbour)
        groups.append(group)
    return groups


class RedactionHistory:
    """The on-disk redaction history at one path (default: the raven home)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()

    def _read_raw(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HistoryStorageError(f"cannot read {self.path}: {_reason(exc)}") from exc

    def _load(self) -> _Parsed:
        raw = self._read_raw()
        return _Parsed() if raw is None else _parse(raw)

    def read(self) -> HistorySnapshot:
        parsed = self._load()
        entries = sorted(parsed.entries.values(), key=lambda e: (e.decided_at, e.digest), reverse=True)
        return HistorySnapshot(entries=entries, corrupt=parsed.corrupt)

    def lookup(self, token: str) -> RememberedDecision | None:
        if not token:
            return None
        return self._load().entries.get(_digest(token))

    def resolve(self, items: Sequence[ReviewItem]) -> HistoryResolution:
        parsed = self._load()
        suspected = {item.id: item for item in items if item.kind == KIND_SUSPECTED and item.token}
        replayed: dict[str, RememberedDecision] = {}
        hints: dict[str, RememberedDecision] = {}
        for group in _components(suspected):
            records = {item_id: parsed.entries.get(_digest(suspected[item_id].token)) for item_id in group}
            known = {item_id: record for item_id, record in records.items() if record is not None}
            if len(known) == len(group) and len({record.action for record in known.values()}) == 1:
                replayed.update(known)
            else:
                hints.update(known)
        auto = [ReviewDecision(item.id, replayed[item.id].action) for item in items if item.id in replayed]
        remaining = [item for item in items if item.id not in replayed]
        return HistoryResolution(auto=auto, remaining=remaining, replayed=replayed, hints=hints, corrupt=parsed.corrupt)

    def _backup(self, raw: bytes) -> Path:
        base = f"{self.path.name}.corrupt-{_backup_stamp()}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for suffix in range(_MAX_BACKUP_SUFFIX + 1):
            candidate = self.path.parent / (base if suffix == 0 else f"{base}-{suffix}")
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                raise HistoryStorageError(f"cannot back up the unreadable {self.path.name}: {_reason(exc)}") from exc
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                candidate.unlink(missing_ok=True)
                raise HistoryStorageError(f"cannot back up the unreadable {self.path.name}: {_reason(exc)}") from exc
            return candidate
        raise HistoryStorageError(f"cannot back up the unreadable {self.path.name}: too many backups named {base}")

    def _replace(self, entries: Mapping[str, RememberedDecision]) -> None:
        text = _serialize(entries)
        try:
            fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        except OSError as exc:
            raise HistoryStorageError(f"cannot save {self.path}: {_reason(exc)}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException as exc:
            Path(tmp).unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise HistoryStorageError(f"cannot save {self.path}: {_reason(exc)}") from exc
            raise

    def _transact(
        self, mutate: Callable[[dict[str, RememberedDecision], bool], tuple[T, bool]]
    ) -> tuple[T, Path | None]:
        """Run one locked read-modify-write; ``mutate`` only edits the dict.

        The read happens inside the lock so a decision made by another
        process since the caller's own read is the one ``mutate`` sees.
        """
        try:
            with write_transaction(self.path):
                raw = self._read_raw()
                parsed = _Parsed() if raw is None else _parse(raw)
                if parsed.unsupported:
                    raise HistoryStorageError(f"{self.path} was {parsed.corrupt}; refusing to overwrite it")
                entries = dict(parsed.entries)
                result, changed = mutate(entries, parsed.corrupt is not None)
                if not changed:
                    return result, None
                backup = self._backup(raw) if parsed.corrupt is not None and raw is not None else None
                try:
                    self._replace(entries)
                except BaseException:
                    if backup is not None:
                        # The original still holds the same bytes, so the copy is
                        # only litter once the save has failed.
                        try:
                            backup.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
                return result, backup
        except LockTimeoutError as exc:
            raise HistoryStorageError(f"cannot save {self.path}: its write lock could not be acquired") from exc
        except OSError as exc:
            raise HistoryStorageError(f"cannot save {self.path}: {_reason(exc)}") from exc

    def record(self, token: str, action: str, *, category: str, source: str) -> RememberedDecision:
        """Save one human decision, replacing any earlier record of the value."""
        if not token:
            raise ValueError("a token is required")
        if action not in _ACTIONS:
            raise ValueError(f"invalid action {action!r}")
        if not category:
            raise ValueError("a category is required")
        _check_source(source)

        def _mutate(entries: dict[str, RememberedDecision], _corrupt: bool) -> tuple[RememberedDecision, bool]:
            entry = _manual_entry(token, action, category, source, _now())
            entries[entry.digest] = entry
            return entry, True

        entry, _backup = self._transact(_mutate)
        return entry

    def remember(
        self,
        items: Sequence[ReviewItem],
        decisions: Sequence[ReviewDecision],
        *,
        source: str,
        replayed: Mapping[str, RememberedDecision] | None = None,
    ) -> RememberResult:
        """Save a review's suspected-item decisions in one transaction.

        Items found in ``replayed`` with the replayed action only count a
        reuse, and only while the record still carries the revision that was
        read; everything else is a human decision and replaces the record.
        """
        _check_source(source)
        snapshots = dict(replayed or {})
        actions = {decision.item_id: decision.action for decision in decisions}
        work = [
            (item, actions[item.id])
            for item in items
            if item.kind == KIND_SUSPECTED and item.token and actions.get(item.id) in _ACTIONS
        ]
        if not work:
            return RememberResult(recorded=0, replayed=0, stale=0)

        def _mutate(entries: dict[str, RememberedDecision], _corrupt: bool) -> tuple[RememberResult, bool]:
            now = _now()
            recorded = reused = stale = 0
            for item, action in work:
                digest = _digest(item.token)
                snapshot = snapshots.get(item.id)
                if snapshot is not None and snapshot.digest == digest and snapshot.action == action:
                    current = entries.get(digest)
                    if current is not None and current.revision == snapshot.revision:
                        entries[digest] = replace(current, applied=current.applied + 1, last_applied_at=now)
                        reused += 1
                    else:
                        stale += 1
                    continue
                entries[digest] = _manual_entry(item.token, action, item.category, source, now)
                recorded += 1
            return RememberResult(recorded=recorded, replayed=reused, stale=stale), recorded + reused > 0

        result, backup = self._transact(_mutate)
        return replace(result, backup=backup)

    def forget(self, ids: Iterable[str]) -> ForgetResult:
        """Remove records by id (12 to 64 hex digits), all or nothing."""
        wanted = list(ids)
        if not wanted:
            return ForgetResult()

        def _mutate(entries: dict[str, RememberedDecision], _corrupt: bool) -> tuple[ForgetResult, bool]:
            unknown: list[str] = []
            ambiguous: list[str] = []
            targets: list[str] = []
            for raw_id in wanted:
                normalized = raw_id.strip().lower()
                if not _ID_PREFIX.match(normalized):
                    unknown.append(raw_id)
                    continue
                matches = [digest for digest in entries if digest.startswith(normalized)]
                if not matches:
                    unknown.append(raw_id)
                elif len(matches) > 1:
                    ambiguous.append(raw_id)
                elif matches[0] not in targets:
                    targets.append(matches[0])
            if unknown or ambiguous:
                return ForgetResult(unknown=unknown, ambiguous=ambiguous), False
            for digest in targets:
                del entries[digest]
            return ForgetResult(removed=[digest[:12] for digest in targets]), bool(targets)

        result, backup = self._transact(_mutate)
        return replace(result, backup=backup)

    def clear(self) -> ClearResult:
        """Remove every record; an unreadable file is backed up and reset."""

        def _mutate(entries: dict[str, RememberedDecision], corrupt: bool) -> tuple[ClearResult, bool]:
            removed = len(entries)
            entries.clear()
            return ClearResult(removed=removed), removed > 0 or corrupt

        result, backup = self._transact(_mutate)
        return replace(result, backup=backup)


__all__ = [
    "ENV_SWITCH",
    "HISTORY_FILENAME",
    "SCHEMA_VERSION",
    "SOURCES",
    "ClearResult",
    "ForgetResult",
    "HistoryResolution",
    "HistorySnapshot",
    "HistoryStorageError",
    "RedactionHistory",
    "RememberResult",
    "RememberedDecision",
    "default_path",
    "enabled",
]
