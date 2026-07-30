"""Hermes Agent scanner -- memory files and conversations."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from raven.importer.types import ImportMessage, ImportSession, Platform, ScanResult, SourceKind
from raven.utils.text import is_cjk

_MEMORY_SOURCES = (("user-md", "USER.md"), ("memory-md", "MEMORY.md"))

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

        entries = [e.strip() for e in raw.split(_ENTRY_DELIMITER)]
        entries = [e for e in entries if e]
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


__all__ = ["HermesScanner", "resolve_hermes_home"]
