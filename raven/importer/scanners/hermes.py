"""Hermes Agent scanner -- memory files and conversations."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from raven.importer.types import ImportSession, Platform, ScanResult, SourceKind

_MEMORY_SOURCES = (("user-md", "USER.md"), ("memory-md", "MEMORY.md"))


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
        raise NotImplementedError

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


__all__ = ["HermesScanner", "resolve_hermes_home"]
