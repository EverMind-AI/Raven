"""Shared text utilities: frontmatter, timestamps, CJK detection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import yaml

CJK_RE = re.compile(r"[一-鿿]")
_FM_CLOSE_RE = re.compile(r"^---\s*$", re.MULTILINE)


def is_cjk(text: str, sample: int = 200) -> bool:
    """Return True if *text* (first *sample* chars) contains CJK ideographs."""
    return bool(CJK_RE.search(text[:sample]))


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter delimited by ``---`` lines.

    Returns ``(metadata_dict, body_after_frontmatter)``.
    Returns ``({}, original_text)`` when no valid frontmatter is found.
    """
    if not text.startswith("---"):
        return {}, text
    m = _FM_CLOSE_RE.search(text, pos=4)
    if m is None:
        return {}, text
    fm_str = text[3 : m.start()].strip()
    body = text[m.end() :].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_str)
        return (fm if isinstance(fm, dict) else {}), body
    except yaml.YAMLError:
        return {}, text


def parse_iso_ts_ms(raw: Any) -> int | None:
    """Parse a timestamp value to millisecond epoch.

    Accepts ISO 8601 strings, epoch-millisecond ints, and epoch-second
    floats.  Returns ``None`` on unparseable input.
    """
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 1e12 else int(raw * 1000)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


__all__ = [
    "CJK_RE",
    "is_cjk",
    "parse_frontmatter",
    "parse_iso_ts_ms",
]
