"""Proactivity preferences reader — bridges Personalizer output to NudgePolicy.

Personalizer.post_learn (``raven/agent/personalizer.py``) writes
proactivity-related facts to the ``## Proactivity Preferences`` section of
MEMORY.md (facts tagged with ``category=proactivity``). This module reads
that section and surfaces a structured ``PersonalizedOverrides`` object
that NudgePolicy consumes to personalize its limits.

Design constraints:

- **Tighten-only**: overrides can only narrow the permissive window, never
  widen it. A user preference that would *loosen* NudgePolicy defaults is
  ignored. Rationale: a noisy extraction that mis-infers "never disturb"
  should at worst make the agent quieter, never spammier.
- **No LLM calls**: Reader is pure regex over the markdown section. Keeps
  per-tick cost at zero.
- **MVP scope**: only ``quiet_hours`` is supported. Additional fields
  (``work_hours_silence``, ``min_priority_filter``, ...) can be added
  without breaking the schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class PersonalizedOverrides:
    """Tightened-only deltas NudgePolicy applies on top of its static config.

    ``quiet_hours`` widens the static quiet-hour window (e.g. config 22-07
    combined with user pref 20-09 → effective 20-09). A user pref that
    would narrow the window is rejected at apply time.
    """

    quiet_hours: tuple[int, int] | None = None

    def is_empty(self) -> bool:
        return self.quiet_hours is None


_SECTION_HEADER_RE = re.compile(
    r"^##\s*Proactivity\s+Preferences\s*$", re.IGNORECASE | re.MULTILINE
)
# Matches "HH:MM-HH:MM" / "HH-HH" / "22:00-07:00" anywhere in a fact line.
_QUIET_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2})(?::\d{2})?\s*[-–—~～至到]\s*(?P<end>\d{1,2})(?::\d{2})?"
)
_QUIET_KEYWORDS = ("quiet hour", "安静时段", "勿扰", "不打扰", "免打扰", "dnd",
                   "do not disturb", "no proactive", "安静时间")


class ProactivityPreferencesReader:
    """Parse ``## Proactivity Preferences`` section out of MEMORY.md.

    The reader is defensive: unknown formats, missing section, or conflicting
    multiple facts all fall back to ``PersonalizedOverrides()`` (empty).
    """

    def __init__(self, memory_file: Path | str | None = None,
                 read_fn=None) -> None:
        """Initialize with either a path or a read callable.

        ``read_fn`` (e.g. ``memory_store.read_long_term``) takes precedence
        when supplied — lets us reuse MemoryStore's read path without coupling.
        """
        self._memory_file = Path(memory_file) if memory_file else None
        self._read_fn = read_fn

    def read(self) -> PersonalizedOverrides:
        """Return the current overrides. Never raises — empty on any failure."""
        try:
            raw = self._read_text()
        except Exception as exc:
            logger.warning("ProactivityPreferencesReader read failed: {}", exc)
            return PersonalizedOverrides()
        if not raw:
            return PersonalizedOverrides()

        section = self._extract_section(raw)
        if not section:
            return PersonalizedOverrides()
        quiet = self._parse_quiet_hours(section)
        return PersonalizedOverrides(quiet_hours=quiet)

    # ------------------------------------------------------------------
    # Internals

    def _read_text(self) -> str:
        if self._read_fn is not None:
            return self._read_fn() or ""
        if self._memory_file is None:
            return ""
        if not self._memory_file.exists():
            return ""
        return self._memory_file.read_text(encoding="utf-8")

    @staticmethod
    def _extract_section(raw: str) -> str:
        """Body between '## Proactivity Preferences' and the next '## '
        header (or EOF)."""
        m = _SECTION_HEADER_RE.search(raw)
        if not m:
            return ""
        start = m.end()
        next_header = re.search(r"^##\s", raw[start:], re.MULTILINE)
        end = start + next_header.start() if next_header else len(raw)
        return raw[start:end].strip()

    @staticmethod
    def _parse_quiet_hours(section: str) -> tuple[int, int] | None:
        """Pick the first fact line that mentions a quiet-hours keyword and
        parse its HH range. Returns (start_hour, end_hour) or None."""
        for line in section.splitlines():
            line_l = line.lower().strip()
            if not line_l:
                continue
            if not any(k in line_l for k in _QUIET_KEYWORDS):
                continue
            m = _QUIET_RANGE_RE.search(line)
            if not m:
                continue
            try:
                start = int(m.group("start")) % 24
                end = int(m.group("end")) % 24
            except ValueError:
                continue
            return (start, end)
        return None


__all__ = ["PersonalizedOverrides", "ProactivityPreferencesReader"]
