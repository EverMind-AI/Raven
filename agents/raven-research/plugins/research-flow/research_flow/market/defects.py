"""The mechanical defects a market scan's report may not carry.

Four of them, and they are the four the repository's own
``scripts/research_report_audit.py`` calls out, because those are the ones
that decided the 2026-09-04 head-to-head:

1. **An estimate inside a quantitative column.** A number the report itself
   calls an approximation, next to numbers it measured. The reader cannot
   tell which is which, and a ranking built on the column cannot either.
2. **A row the report marks as never opened.** The disclosure is honest
   and the table is half-built at the same time.
3. **A table with more compared columns than a row can be read across.**
4. **A ranking that contradicts its own totals.** A market scan has no
   ordering to defend and no score to add up, so a column that looks like
   either is a defect in this report whatever the numbers say.

**Why this exists beside the audit and not inside it.** The audit is
repo-level and stdlib-only on purpose: it has to run against a competitor's
report on a machine with no raven checkout on the path. The flow plugin is
shipped separately from the repo's scripts, so it cannot import the audit
either. So the two checkers live apart, and ``tests/test_agents_research_flow_market_gates.py``
**pins them together**: it runs the repo's audit and this module over the same
adversarial reports and asserts they find the same defects. A marker added to
one and not the other fails that test rather than silently widening one
checker's blind spot.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "MARKER_GLOSSARY",
    "report_defects",
    "table_defects",
]

#: The most columns a reader can carry across one row. The same number the
#: audit uses, and for the same reason: past it the cells become slivers and
#: the row-to-row comparison the table exists for stops working.
MAX_COMPARED_COLUMNS = 8

#: A fraction of a column's cells that must carry a digit for the column to
#: count as quantitative.
QUANTITATIVE_SHARE = 0.6

#: The Chinese markers, written as escapes so this file holds no non-English
#: source (AGENTS.md section 1.3). Each is named for what it means, so the
#: marker table below reads in English.
_YUE = "\u7ea6"  # "approximately", written before a number
_GU = "\u4f30"  # "estimate"; the two-character form adds \u8ba1
_GU_ESTIMATE = "\u4f30\u8ba1"  # "estimate", the two-character form
_WEI_QU = "\u672a\u53d6\u5f97"  # "not obtained"
_WEI_ZHUA = "\u672a\u6293\u53d6"  # "not fetched"
_WEI_YAN = "\u672a\u9a8c\u8bc1"  # "unverified"
_TUI_DAO = "\u63a8\u5bfc"  # "derived", as a report marks a computed quantity

#: Markers that say a number is not a measurement. The tuple is
#: ``(name, pattern, hard)``; ``hard`` marks the ones that fail the gate,
#: and the approximate-notation ones that do not, because a source's own
#: "~4x" is the source's word and not a retrieval failure.
ESTIMATE_MARKERS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("(est.)", re.compile(rf"[\uff08(]\s*(?:est\.?|{_GU}|{_GU_ESTIMATE})\s*[\uff09)]", re.I), True),
    ("est.", re.compile(r"\best\.(?![a-z])", re.I), True),
    ("estimated", re.compile(r"\bestimat(?:e|ed|ion)\b", re.I), True),
    ("approx", re.compile(r"\bapprox(?:\.|imately)?\b", re.I), True),
    ("approximately+N", re.compile(rf"{_YUE}\s*\d"), True),
    ("not fetched", re.compile(rf"not\s+fetched|{_WEI_QU}|{_WEI_ZHUA}", re.I), True),
    ("unverified", re.compile(rf"\bunverified\b|{_WEI_YAN}", re.I), True),
    ("~N", re.compile(r"~\s*\d"), False),
    ("\u2248", re.compile("\u2248"), False),
)

MARKER_GLOSSARY: dict[str, str] = {
    "(est.)": "estimate",
    "est.": "estimate",
    "estimated": "estimate",
    "approx": "approximately",
    "approximately+N": "approximately",
    "not fetched": "not fetched",
    "unverified": "unverified",
    "~N": "approximately",
    "\u2248": "approximately",
}

#: The derivation marker. A cell carrying its own derivation is exempt from
#: the approximate-notation findings, because showing its inputs is the
#: contract being honoured - an estimate has none.
DERIVED_MARKERS = re.compile(rf"{_TUI_DAO}|derived|computed|\bcalc\b", re.I)

#: A cell that is nothing but bare URLs. A source column is one fixed cost
#: the reader never compares across rows, so it is held out of the width
#: count; a cell that carries a value *and* its source is a compared column.
_URL_ONLY_CELL_RE = re.compile(r"^<?https?://\S+>?(?:\s+<?https?://\S+>?)*$")

#: An inline markdown link, collapsed to its own text before a width is
#: measured - because that is what the reader sees where the destination is
#: hidden behind it.
_INLINE_LINK_RE = re.compile(r"\[(?P<text>[^\]]*)\]\([^)]*\)")

#: A header that names a column holding a summed score. Only a hint: the
#: column is found by arithmetic, and this decides ties.
_TOTAL_HEADER_HINT = re.compile(r"total|sum\b|score", re.I)

_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_DELIM_CELL_RE = re.compile(r"^:?-+:?$")

#: A fenced code block's boundary line.
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT_RE.split(inner)]


def _is_row(line: str) -> bool:
    return bool(line.strip()) and bool(_CELL_SPLIT_RE.search(line))


def _is_delimiter(line: str, ncols: int) -> bool:
    cells = _cells(line)
    return len(cells) == ncols and all(_DELIM_CELL_RE.fullmatch(c) for c in cells)


def _fenced(lines: list[str]) -> list[bool]:
    inside: tuple[str, int] | None = None
    marks: list[bool] = []
    for line in lines:
        m = _FENCE_RE.match(line)
        if m is None:
            marks.append(inside is not None)
            continue
        marker, info = m.group("marker"), m.group("info")
        char, length = marker[0], len(marker)
        if inside is None:
            if char == "`" and "`" in info:
                marks.append(False)
                continue
            inside = (char, length)
            marks.append(True)
            continue
        open_char, open_length = inside
        if char == open_char and length >= open_length and not info.strip():
            inside = None
        marks.append(True)
    return marks


def _rendered(cell: str) -> str:
    return _INLINE_LINK_RE.sub(lambda m: m.group("text"), cell).strip()


def _number(cell: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", cell.replace(",", ""))
    return float(m.group()) if m else None


def _tables(lines: list[str]) -> list[tuple[list[str], list[list[str]]]]:
    """Every markdown table, as ``(header, rows)``."""
    tables: list[tuple[list[str], list[list[str]]]] = []
    fenced = _fenced(lines)
    i = 0
    while i < len(lines) - 1:
        if fenced[i] or fenced[i + 1] or not _is_row(lines[i]):
            i += 1
            continue
        header = _cells(lines[i])
        if not _is_delimiter(lines[i + 1], len(header)):
            i += 1
            continue
        rows: list[list[str]] = []
        j = i + 2
        while j < len(lines) and not fenced[j] and _is_row(lines[j]):
            rows.append(_cells(lines[j]))
            j += 1
        tables.append((header, rows))
        i = j
    return tables


def _quantitative_columns(header: list[str], rows: list[list[str]]) -> list[int]:
    out: list[int] = []
    for idx in range(len(header)):
        vals = [r[idx] for r in rows if idx < len(r) and r[idx]]
        if not vals:
            continue
        with_digit = sum(1 for v in vals if re.search(r"\d", v))
        if with_digit / len(vals) >= QUANTITATIVE_SHARE:
            out.append(idx)
    return out


def _link_columns(header: list[str], rows: list[list[str]]) -> list[int]:
    out: list[int] = []
    for idx in range(len(header)):
        vals = [r[idx] for r in rows if idx < len(r) and r[idx].strip()]
        if not vals:
            continue
        if sum(1 for v in vals if _URL_ONLY_CELL_RE.match(_rendered(v))) / len(vals) >= 0.6:
            out.append(idx)
    return out


def _rank_column(header: list[str], rows: list[list[str]]) -> int | None:
    for idx in range(len(header)):
        vals = [_number(r[idx]) for r in rows if idx < len(r) and r[idx]]
        if len(vals) < 2 or any(v is None or not float(v).is_integer() for v in vals):
            continue
        ints = [int(v) for v in vals if v is not None]
        if ints[0] in (0, 1) and ints == list(range(ints[0], ints[0] + len(ints))):
            return idx
    return None


def _total_column(
    header: list[str],
    rows: list[list[str]],
    quant: list[int],
    rank_idx: int | None,
) -> int | None:
    best: tuple[int, float] | None = None
    for pos, idx in enumerate(quant):
        run: list[int] = []
        for other in reversed(quant[:pos]):
            if other == rank_idx or (run and other != run[-1] - 1):
                break
            run.append(other)
        if len(run) < 2:
            continue
        agree = 0
        rows_seen = 0
        for row in rows:
            if idx >= len(row) or any(c >= len(row) for c in run):
                continue
            total, parts = _number(row[idx]), [_number(row[c]) for c in run]
            if total is None or any(p is None for p in parts):
                continue
            rows_seen += 1
            if abs(total - sum(p for p in parts if p is not None)) < 1e-9:
                agree += 1
        if rows_seen and agree / rows_seen >= 0.6:
            score = agree / rows_seen + (0.5 if _TOTAL_HEADER_HINT.search(header[idx]) else 0.0)
            if best is None or score > best[1]:
                best = (idx, score)
    if best is not None:
        return best[0]
    named = [
        idx
        for idx in quant
        if idx != rank_idx and idx < len(header) and _TOTAL_HEADER_HINT.search(header[idx])
    ]
    return named[-1] if named else None


def _rows_out_of_order(values: list[float]) -> int:
    """How many rows would have to move for the column to be non-increasing."""
    if not values:
        return 0
    longest = [1] * len(values)
    for i, value in enumerate(values):
        for j in range(i):
            if values[j] >= value:
                longest[i] = max(longest[i], longest[j] + 1)
    return len(values) - max(longest)


def table_defects(report: str) -> list[dict[str, Any]]:
    """The mechanical defects in one report's tables."""
    findings: list[dict[str, Any]] = []
    for header, rows in _tables(report.split("\n")):
        width = len(header)
        quant = _quantitative_columns(header, rows)

        # 1. an estimate inside a quantitative column
        label_idx = 1 if width > 1 else 0
        for row in rows:
            for idx in quant:
                if idx >= len(row) or not row[idx]:
                    continue
                cell = row[idx]
                derived = bool(DERIVED_MARKERS.search(cell))
                for name, rx, hard in ESTIMATE_MARKERS:
                    if not rx.search(cell):
                        continue
                    if derived and not hard:
                        break
                    findings.append(
                        {
                            "kind": "estimate_in_quantitative_column",
                            "detail": (
                                f"row {row[label_idx] if label_idx < len(row) else '?'!r} "
                                f"column {header[idx]!r}: {cell!r} ({name})"
                            ),
                            "hard": hard,
                        }
                    )
                    break

        # 2. a row the report marks as never opened
        unfetched = re.compile(rf"not\s+fetched|{_WEI_QU}|{_WEI_ZHUA}", re.I)
        for row in rows:
            if any(unfetched.search(c) for c in row):
                findings.append(
                    {
                        "kind": "unfetched_table_row",
                        "detail": f"row {row[label_idx] if label_idx < len(row) else '?'!r} is marked not fetched",
                        "hard": False,
                    }
                )

        # 3. too many compared columns
        links = set(_link_columns(header, rows))
        compared = [i for i in range(width) if i not in links]
        if len(compared) > MAX_COMPARED_COLUMNS:
            findings.append(
                {
                    "kind": "too_many_compared_columns",
                    "detail": f"{len(compared)} compared column(s), {len(links)} link column(s)",
                    "hard": False,
                }
            )

        # 4. a ranking that contradicts its own totals
        rank_idx = _rank_column(header, rows)
        total_idx = _total_column(header, rows, quant, rank_idx)
        if rank_idx is not None and total_idx is not None and rank_idx != total_idx:
            seq: list[tuple[float, float, str]] = []
            for row in rows:
                if max(rank_idx, total_idx) >= len(row):
                    continue
                rank, total = _number(row[rank_idx]), _number(row[total_idx])
                if rank is None or total is None:
                    continue
                seq.append((rank, total, row[label_idx] if label_idx < len(row) else "?"))
            if len(seq) >= 4:
                seq.sort(key=lambda x: x[0])
                totals = [total for _rank, total, _label in seq]
                tolerance = max(1, len(seq) // 10)
                if _rows_out_of_order(totals) <= tolerance:
                    rising = [(a, b) for a, b in zip(seq, seq[1:]) if b[1] > a[1]]
                    for (r_prev, t_prev, l_prev), (r_now, t_now, l_now) in rising:
                        findings.append(
                            {
                                "kind": "rank_contradicts_total",
                                "detail": (
                                    f"#{int(r_now)} {l_now!r} totals {t_now:g} but ranks below "
                                    f"#{int(r_prev)} {l_prev!r} at {t_prev:g}"
                                ),
                                "hard": True,
                            }
                        )
    return findings


def report_defects(report: str) -> list[dict[str, Any]]:
    """Every mechanical defect in one report."""
    return table_defects(report)
