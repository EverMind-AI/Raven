"""Shared text utilities: frontmatter, timestamps, CJK detection, display width."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import yaml

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_FM_OPEN_RE = re.compile(r"\A---[ \t]*\r?\n")
_FM_CLOSE_RE = re.compile(r"^---[ \t]*(?:\r?\n|$)", re.MULTILINE)
_YAML_MAX_CHARS = 16 * 1024
_YAML_MAX_DEPTH = 32


class _FrontmatterLoader(yaml.SafeLoader):
    """Bound composition before untrusted nesting or aliases expand into values."""

    _depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases are not accepted in frontmatter")
        if self._depth >= _YAML_MAX_DEPTH:
            raise yaml.YAMLError("frontmatter nesting is too deep")
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _parse_frontmatter_block(block: str, *, line_fallback: bool) -> dict[str, Any] | None:
    lines = {}
    if line_fallback:
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip():
                lines[key.strip()] = value.strip().strip("\"'")
    if len(block) > _YAML_MAX_CHARS:
        return lines if line_fallback else None
    try:
        loader = _FrontmatterLoader(block)
        try:
            parsed = loader.get_single_data()
        finally:
            loader.dispose()
    except Exception:  # noqa: BLE001 - YAML also raises ValueError and RecursionError
        return lines if line_fallback else None
    if not isinstance(parsed, dict):
        return lines if line_fallback else {}
    for key in ("name", "description") if line_fallback else ():
        if key in parsed and not isinstance(parsed[key], str):
            parsed[key] = lines.get(key, "")
    return parsed


def is_cjk(text: str, sample: int = 200) -> bool:
    """Return True if *text* (first *sample* chars) contains CJK ideographs."""
    return bool(CJK_RE.search(text[:sample]))


def display_width(text: str) -> int:
    """Terminal columns *text* occupies: 2 for a wide glyph, 1 for the rest.

    A length in code points is not comparable across scripts, and code that
    compares one against a single threshold silently means two different things
    to a Chinese and an English writer: "hello" is five code points and one
    word, while five CJK code points ("what can you do" in Chinese) are a whole question. Columns
    are the unit that reads the same in both, since a CJK ideograph occupies the
    room of about two latin letters on screen and carries about that much more.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    opening = _FM_OPEN_RE.match(text)
    if opening is None:
        return None
    closing = _FM_CLOSE_RE.search(text, pos=opening.end())
    if closing is None:
        return None
    return text[opening.end() : closing.start()].strip(), text[closing.end() :]


def parse_frontmatter(
    text: str, *, line_fallback: bool = False, preserve_body_whitespace: bool = False
) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter delimited by ``---`` lines.

    Returns ``(metadata_dict, body_after_frontmatter)``.
    Returns ``({}, original_text)`` when no complete frontmatter is found.
    Invalid YAML, aliases, blocks over 16,384 characters, and nesting beyond
    32 nodes retain the original text by default, so memory imports lose no
    prose. SKILL readers opt into the legacy line fallback, including textual
    names/descriptions and non-mapping headers. Body whitespace can be retained
    for callers that store the original Markdown.
    Valid YAML retains its scalar coercions and inline-comment semantics.
    """
    split = _split_frontmatter(text)
    if split is None:
        return {}, text
    block, body = split
    metadata = _parse_frontmatter_block(block, line_fallback=line_fallback)
    if metadata is None:
        return {}, text
    return metadata, body if preserve_body_whitespace else body.lstrip("\n")


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
    "display_width",
    "is_cjk",
    "parse_frontmatter",
    "parse_iso_ts_ms",
]
