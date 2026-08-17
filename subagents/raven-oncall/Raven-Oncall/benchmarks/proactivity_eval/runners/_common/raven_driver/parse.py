"""Parsers for ``raven`` CLI subcommand output.

The raven CLI prints Rich-formatted tables to stdout (box-drawing chars,
padded columns). These functions extract the {field: value} pairs without
pulling Rich in as a runtime dep.
"""

from __future__ import annotations

import re

_BOX_CHARS = "┌┐└┘├┤┬┴┼─│"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def parse_two_column_table(stdout: str) -> dict[str, str]:
    """Parse a Rich 2-column table (field | value) into a dict.

    The raven CLI uses this layout for sentinel tick / status output.
    Rows that wrap across multiple lines get joined back together. Any
    line without exactly one ``│`` column separator (after the outer
    borders) is skipped — that lets us tolerate title rows, blank
    separator lines, and ANSI noise.

    Returns the dict in insertion order. Multi-line values are joined
    with a single space. Empty values become "".
    """
    text = _strip_ansi(stdout)
    rows: list[tuple[str, str]] = []
    last_key: str | None = None

    for raw in text.splitlines():
        # A typical row looks like:  │ field          │ value …     │
        # The outer pipes flank the 2 inner cells.
        line = raw.strip()
        if not line or line[0] not in _BOX_CHARS:
            continue
        # Box-only lines (├─┼─┤) have no real content; skip.
        if all(c in _BOX_CHARS + " " for c in line):
            continue
        # Strip outer pipes + horizontal padding, then split on the
        # remaining "│". Expect exactly 2 cells.
        inner = line.strip("│").strip()
        cells = [c.strip() for c in inner.split("│")]
        if len(cells) != 2:
            continue
        field, value = cells
        if field:
            rows.append((field, value))
            last_key = field
        elif last_key is not None and value:
            # Continuation of the previous value (Rich wrapped a long string).
            prev_field, prev_val = rows[-1]
            joined = (prev_val + " " + value).strip()
            rows[-1] = (prev_field, joined)

    return dict(rows)


def parse_bool(s: str | None) -> bool | None:
    """Parse 'True'/'False'/'true'/'false'/'1'/'0' from a stringy field."""
    if s is None:
        return None
    s = s.strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def parse_float(s: str | None) -> float | None:
    if s is None or s.strip() == "" or s.strip() == "-":
        return None
    try:
        return float(s.strip())
    except ValueError:
        return None
