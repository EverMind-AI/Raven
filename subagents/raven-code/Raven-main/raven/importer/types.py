"""Cold-start import data types and Scanner protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class Platform(StrEnum):
    """Supported source platforms for cold-start import."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    KIMICODE = "kimicode"
    HERMES = "hermes"
    OPENCLAW = "openclaw"


class SourceKind(StrEnum):
    """What kind of data a scan result represents."""

    MEMORY_FILE = "memory_file"
    CONVERSATION = "conversation"


class Tier(StrEnum):
    """User-facing import scope choice.

    MEMORY_FILES imports only memory/config files (fast).
    FULL imports memory files plus conversation history (slow).
    """

    MEMORY_FILES = "memory_files"
    FULL = "full"


@dataclass(frozen=True)
class ImportMessage:
    """One message in a cold-start import session."""

    role: str
    content: str
    timestamp: int
    sender_id: str
    tool_calls: tuple[dict[str, Any], ...] | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ImportSession:
    """A complete importable unit ready for store()."""

    session_id: str
    messages: tuple[ImportMessage, ...] = ()


@dataclass(frozen=True)
class ScanResult:
    """One importable unit discovered by a Scanner."""

    source_key: str
    platform: Platform
    kind: SourceKind
    file_paths: tuple[Path, ...]
    estimated_size: int
    mtime: float


@runtime_checkable
class Scanner(Protocol):
    """Discovers and reads importable units for one platform."""

    platform: Platform

    async def scan(self) -> list[ScanResult]:
        """Discover all importable units -- no tier filtering."""
        ...

    async def read(self, result: ScanResult) -> ImportSession:
        """Load one discovered unit into an ImportSession."""
        ...


def filter_by_tier(results: list[ScanResult], tier: Tier) -> list[ScanResult]:
    """Filter scan results by the user's chosen import tier."""
    if tier == Tier.FULL:
        return results
    return [r for r in results if r.kind == SourceKind.MEMORY_FILE]


__all__ = [
    "ImportMessage",
    "ImportSession",
    "Platform",
    "ScanResult",
    "Scanner",
    "SourceKind",
    "Tier",
    "filter_by_tier",
]
