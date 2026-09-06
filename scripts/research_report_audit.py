#!/usr/bin/env python3
"""Mechanical quality audit of one deep-research report.

Reads a report as it was delivered - the markdown body plus, when the run wrote one, the
process appendix ``process_appendix.render`` emits - and reports only what a parser can
settle without judgement: which template sections are present, what the trail recorded,
how deep the fetches went, which table cells carry an estimate, whether a ranking agrees
with its own totals, and whether prose leaked in front of the first heading.

Why a script rather than a rubric: the 2026-09-04 head-to-head against a competitor's top
tier lost four dimensions to defects of exactly this kind - an order-of-magnitude number
that entered a scored table as an estimate, a convention table with unfetched rows, a rank
that disagreed with its own total column, and a run whose citations were never opened. Each
is mechanically detectable, so each should be found by a machine before a reader spends an
hour scoring the report. What stays human is everything the audit deliberately does not
score: mechanism depth, insight, whether a recommendation is right.

The audit is vendor-agnostic on purpose. A competitor's report has no appendix and no
ledger, so every trail field degrades to "not recorded" rather than failing - the
estimate, ranking, section and preamble checks read the body alone and work on any
markdown report. That is what makes a head-to-head comparable: both sides are measured by
the same parser.

Usage::

    python3 scripts/research_report_audit.py REPORT.md [REPORT.md ...]
    python3 scripts/research_report_audit.py --json REPORT.md
    python3 scripts/research_report_audit.py --strict REPORT.md   # exit 1 on a hard finding

Hard findings, the three that decided the head-to-head, are the ``--strict`` gate:
an estimate marker inside a quantitative table column, a ranking that contradicts its
totals, and a citation the run never opened.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ``_THIN_PAGE_CHARS`` in the flow's process_appendix. Kept in sync by eye rather than
# imported: this script must run against a competitor's report on a machine with no
# raven checkout on the path, so it stays stdlib-only and copies the one constant.
THIN_PAGE_CHARS = 400

#: The three sections the deep report template requires, in order.
REQUIRED_SECTIONS = ("Answer", "Findings", "Limitations")

#: Markers that say a number is not a measurement, each with whether it is a hard finding.
#: The split is between a report declaring its own number an estimate - the defect that
#: mis-ranked two candidates by an order of magnitude on 2026-09-04 - and approximate
#: notation a source itself uses ("~4x compression"), which is the paper's word and not a
#: retrieval failure. Both are reported; only the first fails ``--strict``.
#:
#: The non-ASCII forms are written as codepoint escapes with their meaning named, because
#: the reports this reads are not all in English - the product renders zh whenever the
#: host config sets a language - and a marker is input data the parser must recognise
#: rather than text a reader of this file needs. See MARKER_GLOSSARY below.
_YUE = "\u7ea6"  # "approximately", written before a number
_GU = "\u4f30"  # "estimate"; the two-character form adds \u8ba1
_WEI_QU = "\u672a\u53d6\u5f97"  # "not obtained"
_WEI_ZHUA = "\u672a\u6293\u53d6"  # "not fetched"
_WEI_YAN = "\u672a\u9a8c\u8bc1"  # "unverified"
_TUI_DAO = "\u63a8\u5bfc"  # "derived", as a report marks a computed quantity
_PAREN_L, _PAREN_R = "\uff08", "\uff09"  # full-width parentheses

#: What each escape above says, for a reader who does not read the script's regexes.
MARKER_GLOSSARY: dict[str, str] = {
    _YUE: "approximately",
    _GU: "estimate",
    _WEI_QU: "not obtained",
    _WEI_ZHUA: "not fetched",
    _WEI_YAN: "unverified",
    _TUI_DAO: "derived",
}

_UNFETCHED_PATTERN = rf"not\s+fetched|{_WEI_QU}|{_WEI_ZHUA}"

ESTIMATE_MARKERS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("(est.)", re.compile(rf"[{_PAREN_L}(]\s*(?:est\.?|{_GU}|{_GU}\u8ba1)\s*[){_PAREN_R}]", re.I), True),
    ("est.", re.compile(r"\best\.(?![a-z])", re.I), True),
    ("estimated", re.compile(r"\bestimat(?:e|ed|ion)\b", re.I), True),
    ("approx", re.compile(r"\bapprox(?:\.|imately)?\b", re.I), True),
    ("approximately+N", re.compile(rf"{_YUE}\s*\d"), True),
    ("not fetched", re.compile(_UNFETCHED_PATTERN, re.I), True),
    ("unverified", re.compile(rf"\bunverified\b|{_WEI_YAN}", re.I), True),
    ("~N", re.compile(r"~\s*\d"), False),
    ("\u2248", re.compile("\u2248"), False),
)

DERIVED_MARKERS = re.compile(rf"{_TUI_DAO}|derived|computed|\bcalc\b", re.I)
"""A cell carrying its own derivation. Exempt from the approximate-notation findings: the
flow asks the model to compute what the sources leave implicit and to mark it as derived,
so ``≈5.2 (derived: 4,853/931)`` is the contract being honoured, not a missing fetch. An
explicitly self-declared estimate is still reported - a derivation shows its inputs, an
estimate has none."""

#: The fewest rows that can carry an order at all. Below this every sequence is one row
#: away from sorted, so the question cannot be asked.
_MIN_RANKED_ROWS = 4

#: A header that names a column holding a summed score. Only a hint: the column is found
#: by arithmetic below, and this decides ties. Deliberately not a table of translations -
#: a report in a language nobody listed would lose the check that matters most.
_TOTAL_HEADER_HINT = re.compile(r"total|sum\b|score", re.I)

_TRAIL_RE = re.compile(
    r"\*\*Research trail\*\*\s*[-—]\s*(?P<searches>\d+)\s+searches\s*"
    r"\((?P<unique>\d+)\s+unique query strings\),\s*(?P<pages>\d+)\s+pages read"
    r"(?:\s*\((?P<thin>\d+)\s+returned almost nothing\))?"
    r"(?:,\s*(?P<minutes>\d+)m of research)?"
    r"(?:,\s*reviewer:\s*(?P<reviewer>[a-z_ ]+?)(?:\s*\(|,|$))?",
    re.MULTILINE,
)
_UNREVIEWED_RE = re.compile(r"^>[^*]*\*\*This answer shipped unreviewed\*\*\s*[-—]\s*(?P<why>.+?)\.?\s*$")
_NEVER_SURFACED_RE = re.compile(r">[^0-9]*(?P<n>\d+) of (?P<of>\d+) link\(s\) cited above appear nowhere in this run")
_UNOPENED_RE = re.compile(r">[^0-9]*(?P<n>\d+) of (?P<of>\d+) link\(s\) cited above were returned by a search")
_ALL_OPENED_RE = re.compile(r">[^0-9]*All (?P<n>\d+) cited link")
_FENCE_TAG_RE = re.compile(r"(?P<n>\d+) (?:tool-result fence tag|further reference\(s\) are)")
_SCHEMELESS_RE = re.compile(r"names (?P<n>\d+) source\(s\) without a URL scheme")
_READ_NO_CITE_RE = re.compile(r">[^0-9]*(?P<n>\d+) page\(s\) were read but the answer cites no")
_NOTHING_CITED_RE = re.compile(r">\s*No links were cited above, so there was nothing to check")
_PAGE_RE = re.compile(r"^-\s+(?P<url>https?://\S+)\s+\((?P<chars>[\d,]+)\s+chars\)\s*$")
#: The producer caps each listing at 40 entries and writes this line for the rest.
_MORE_RE = re.compile(r"^-\s+(?:…|\.\.\.)\s*and (?P<n>\d+) more\s*$")
_DETAILS_RE = re.compile(r"<details><summary>(?P<name>[^<]+)</summary>")
_THIN_NOTE_RE = re.compile(r"^-\s+\((?P<n>\d+) fetch\(es\) returned almost nothing\)")
_FAILED_NOTE_RE = re.compile(r"^-\s+\((?P<n>\d+) page\(s\) could not be retrieved\)")
_APPENDIX_START_RE = re.compile(r"^(?:>[^*]*\*\*This answer shipped unreviewed|\*\*Research trail\*\*)")


@dataclass
class Table:
    """One markdown table, split into a header and body rows."""

    line_no: int
    header: list[str]
    rows: list[list[str]]

    @property
    def width(self) -> int:
        return len(self.header)


@dataclass
class Finding:
    """One machine-detected defect.

    ``hard`` marks the three classes that decided the 2026-09-04 head-to-head; ``--strict``
    fails on those alone, so a soft finding can be reported without blocking a run.
    """

    kind: str
    detail: str
    hard: bool = False


@dataclass
class Audit:
    """Everything the parser could settle about one report."""

    path: str
    chars: int
    preamble: str = ""
    sections: list[str] = field(default_factory=list)
    extra_sections: list[str] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    trail: dict[str, Any] = field(default_factory=dict)
    citations: dict[str, Any] = field(default_factory=dict)
    pages: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "chars": self.chars,
            "preamble": self.preamble,
            "sections": self.sections,
            "extra_sections": self.extra_sections,
            "tables": self.tables,
            "trail": self.trail,
            "citations": self.citations,
            "pages": self.pages,
            "findings": [{"kind": f.kind, "detail": f.detail, "hard": f.hard} for f in self.findings],
            "hard_findings": sum(1 for f in self.findings if f.hard),
        }


#: A fenced code block's boundary line, per GFM: up to three leading spaces, then a run
#: of at least three backticks or tildes, then an optional info string.
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")

#: An unescaped cell separator. A ``\|`` inside a cell is content, not a boundary.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

#: One cell of a delimiter row: ``---``, ``:--``, ``--:``, ``:-:``.
_DELIM_CELL_RE = re.compile(r"^:?-+:?$")


def _cells(line: str) -> list[str]:
    r"""Split one markdown table row into stripped cells.

    Outer pipes are optional in GFM, so they are removed when present rather than
    required; the split is on unescaped pipes only, and a ``\|`` is unescaped back to
    content afterwards. A row written without outer pipes and a row written with them
    yield the same cells, which is what lets one parser read two vendors' tables.
    """
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT_RE.split(inner)]


def _is_row(line: str) -> bool:
    """True for a line that could be a table row: non-blank, with an unescaped pipe."""
    return bool(line.strip()) and bool(_CELL_SPLIT_RE.search(line))


def _is_delimiter(line: str, ncols: int) -> bool:
    """True for a ``|---|:--:|`` row of exactly ``ncols`` cells.

    The column count is part of the test because it is what keeps prose out: GFM itself
    requires the delimiter to match its header, and a sentence containing a dash and a
    pipe is otherwise a table header waiting to happen.
    """
    cells = _cells(line)
    return len(cells) == ncols and all(_DELIM_CELL_RE.fullmatch(c) for c in cells)


def _fenced(lines: list[str]) -> list[bool]:
    """Per line, whether it sits inside a fenced code block.

    A report explaining its own conventions shows a table in a fence, and a parser that
    read those rows as data would report findings against an example. The fence's own
    boundary lines count as inside, so a table cannot start on one.

    The opener is remembered, not just counted, because GFM closes a fence only on the
    same marker character at no less than the opening length and with nothing after it.
    A toggle on any fence-shaped line re-enters the document at the inner fence of a
    four-backtick block wrapping a three-backtick example - which is how a convention
    table shown as an example became report data and a hard finding.
    """
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
            # An info string may not contain a backtick on a backtick fence: that shape
            # is inline code in a paragraph, not a code block.
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


def parse_tables(lines: list[str]) -> list[Table]:
    """Every markdown table in the body, header and rows separated.

    A table is a row-shaped line followed by a delimiter row of the same width;
    anything after it that is still row-shaped is a body row. Rows whose cell count
    differs from the header's are kept as they are - a ragged table is itself worth
    reporting - so the column readers below index defensively.
    """
    tables: list[Table] = []
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
        tables.append(Table(line_no=i + 1, header=header, rows=rows))
        i = j
    return tables


def quantitative_columns(table: Table) -> list[int]:
    """Column indices whose body cells are mostly numbers.

    The estimate check is scoped to these: an ``(est.)`` in a prose column is a caveat the
    reader can weigh, while the same marker in a size or score column is a number the
    report went on to rank with.
    """
    out: list[int] = []
    for idx in range(table.width):
        vals = [r[idx] for r in table.rows if idx < len(r) and r[idx]]
        if not vals:
            continue
        with_digit = sum(1 for v in vals if re.search(r"\d", v))
        if with_digit / len(vals) >= 0.6:
            out.append(idx)
    return out


def _number(cell: str) -> float | None:
    """The first plain number in a cell, or ``None``."""
    m = re.search(r"-?\d+(?:\.\d+)?", cell.replace(",", ""))
    return float(m.group()) if m else None


def rank_column(table: Table) -> int | None:
    """The column holding the ranking, found by its shape rather than its header.

    A rank runs 1, 2, 3 ... down the table, and that is true of a report in any language,
    which a list of header words is not. The run must be unbroken, ascending and begin at
    the top, so neither a criterion column that climbs nor a column of years is mistaken
    for one.
    """
    for idx in range(table.width):
        vals = [_number(r[idx]) for r in table.rows if idx < len(r) and r[idx]]
        if len(vals) < 2 or any(v is None or not float(v).is_integer() for v in vals):
            continue
        ints = [int(v) for v in vals if v is not None]
        # Starting at 1 (or 0) is what separates a rank from any other ascending run of
        # integers. `Year | North | South | Total` for 2024-2026 is an unbroken ascending
        # run in the first column, and reading it as a rank turns a yearly breakdown into
        # hard findings; a first-column escape for excerpts is not worth that.
        if ints[0] in (0, 1) and ints == list(range(ints[0], ints[0] + len(ints))):
            return idx
    return None


def total_column(table: Table, quant: list[int], rank_idx: int | None) -> int | None:
    """The column whose value is the sum of the criterion columns beside it.

    Verified rather than trusted: the candidate must equal the sum of the unbroken run of
    numeric columns that ends just before it, on most rows. That is what a scored table
    is - criteria, then their total - and it holds whatever the header says or which
    language it says it in. A header hint only breaks a tie between two columns that both
    add up, which is rare and always ambiguous.
    """
    best: tuple[int, float] | None = None
    for pos, idx in enumerate(quant):
        run = []
        for other in reversed(quant[:pos]):
            if other == rank_idx or (run and other != run[-1] - 1):
                break
            run.append(other)
        if len(run) < 2:
            continue
        agree = 0
        rows = 0
        for row in table.rows:
            if idx >= len(row) or any(c >= len(row) for c in run):
                continue
            total, parts = _number(row[idx]), [_number(row[c]) for c in run]
            if total is None or any(p is None for p in parts):
                continue
            rows += 1
            if abs(total - sum(p for p in parts if p is not None)) < 1e-9:
                agree += 1
        if rows and agree / rows >= 0.6:
            score = agree / rows + (0.5 if _TOTAL_HEADER_HINT.search(table.header[idx]) else 0.0)
            if best is None or score > best[1]:
                best = (idx, score)
    if best is not None:
        return best[0]
    named = [
        idx for idx in quant if idx != rank_idx and idx < table.width and _TOTAL_HEADER_HINT.search(table.header[idx])
    ]
    return named[-1] if named else None


def check_estimates(tables: list[Table]) -> list[Finding]:
    """Estimate markers inside quantitative table columns.

    Scoped to columns that are mostly numbers, because that is where an estimate stops
    being a caveat and becomes an input: the number gets scored, summed and ranked with the
    measured ones beside it, and nothing downstream can tell them apart.
    """
    findings: list[Finding] = []
    for t in tables:
        quant = sorted(set(quantitative_columns(t)))
        if not quant:
            continue
        label_idx = 1 if t.width > 1 else 0
        for row in t.rows:
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
                    label = row[label_idx] if label_idx < len(row) else "?"
                    col = t.header[idx] if idx < t.width else f"col{idx}"
                    findings.append(
                        Finding(
                            kind="estimate_in_quantitative_column",
                            detail=f"table@L{t.line_no} row {label!r} column {col!r}: {cell!r} ({name})",
                            hard=hard,
                        )
                    )
                    break
    return findings


def check_unfetched_cells(tables: list[Table]) -> list[Finding]:
    """Table rows the report itself marks as never opened.

    Scoped to whole cells rather than quantitative columns, because the column this lands
    in is usually prose: a convention table's "benchmarks used" cell reading "(paper; not
    fetched)" is an honest disclosure and a half-built table at the same time. The
    2026-09-04 head-to-head lost the convention-table dimension to three such rows, one of
    which then carried a wrong comparability verdict, so the count belongs in the audit
    even though the disclosure is correct behaviour.
    """
    findings: list[Finding] = []
    unfetched = re.compile(_UNFETCHED_PATTERN, re.I)
    for t in tables:
        label_idx = 1 if t.width > 1 else 0
        hits = [(r[label_idx] if label_idx < len(r) else "?") for r in t.rows if any(unfetched.search(c) for c in r)]
        if hits:
            findings.append(
                Finding(
                    kind="unfetched_table_row",
                    detail=f"table@L{t.line_no}: {len(hits)} row(s) marked not fetched: " + ", ".join(hits[:5]),
                )
            )
    return findings


def rows_out_of_order(values: list[float]) -> int:
    """How many rows would have to move for the column to be non-increasing.

    The length of the sequence minus its longest non-increasing subsequence: 0 for a table
    already sorted by its total, 1 for a table with a single misplaced candidate, and a
    large share of the table for an order that carries no information.

    This is the question the finding asks, so it is the question the gate should ask too.
    Two measures were tried first and each failed the same way on a short table. A share
    of adjacent pairs cannot express "one exception" when four rows make three pairs. A
    rank correlation over every pair then failed on ties: `10, 10, 11, 9` has one row out
    of place, but its two tied leaders sit in the denominator and its one displaced row
    costs two pairs, so the correlation reads 0.17 and the exception was suppressed.
    Counting displaced rows is insensitive to both.
    """
    if not values:
        return 0
    longest = [1] * len(values)
    for i, value in enumerate(values):
        for j in range(i):
            if values[j] >= value:
                longest[i] = max(longest[i], longest[j] + 1)
    return len(values) - max(longest)


def _order_tolerance(rows: int) -> int:
    """How many displaced rows still read as a ranking with exceptions.

    One, or a tenth of a long table: a twenty-row shortlist with two rows out of place is
    still a shortlist somebody sorted, while a four-row table with two is not a ranking.
    """
    return max(1, rows // 10)


def check_ranking(tables: list[Table]) -> list[Finding]:
    """Rankings that contradict their own total column.

    Two conditions, and the second is what keeps an ordinary table out of it. The first is
    that the table has a rank and a verified total. The second is that the table is
    visibly ordered by that total already, because a numbered list whose totals simply
    vary is not a ranking, and calling its every rise a contradiction would fire on any
    indexed breakdown.

    Ordering is measured as the number of rows that would have to move for the totals to
    fall - one, or a tenth of a long table. That is the same question the finding asks, and
    unlike a share of adjacent pairs or a rank correlation it does not lose a single
    misplaced candidate on a short table or behind tied totals.

    What is reported is therefore the exception inside an order: this table is sorted by
    its total except here. The fix is either a re-sort or one sentence under the table
    saying why the order departs from the sum.
    """
    findings: list[Finding] = []
    for t in tables:
        rank_idx = rank_column(t)
        total_idx = total_column(t, sorted(set(quantitative_columns(t))), rank_idx)
        if rank_idx is None or total_idx is None or rank_idx == total_idx:
            continue
        seq: list[tuple[float, float, str]] = []
        label_idx = rank_idx + 1 if rank_idx + 1 < t.width else rank_idx
        for row in t.rows:
            if max(rank_idx, total_idx) >= len(row):
                continue
            rank, total = _number(row[rank_idx]), _number(row[total_idx])
            if rank is None or total is None:
                continue
            seq.append((rank, total, row[label_idx] if label_idx < len(row) else "?"))
        if len(seq) < _MIN_RANKED_ROWS:
            continue
        seq.sort(key=lambda x: x[0])
        totals = [total for _rank, total, _label in seq]
        if rows_out_of_order(totals) > _order_tolerance(len(totals)):
            continue
        rising = [(a, b) for a, b in zip(seq, seq[1:]) if b[1] > a[1]]
        for (r_prev, t_prev, l_prev), (r_now, t_now, l_now) in rising:
            findings.append(
                Finding(
                    kind="rank_contradicts_total",
                    detail=(
                        f"table@L{t.line_no}: #{int(r_now)} {l_now!r} totals {t_now:g} "
                        f"but ranks below #{int(r_prev)} {l_prev!r} at {t_prev:g}"
                    ),
                    hard=True,
                )
            )
    return findings


def parse_trail(lines: list[str]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``(trail, citations, pages)`` read off the process appendix.

    Every field is absent rather than zero when the appendix does not carry it: a report
    with no appendix has an unknown fetch depth, which is a different statement from a
    report that fetched nothing, and a head-to-head that conflated the two would credit a
    competitor for a check it never ran.

    The two listings are read per ``<details>`` block rather than by pattern alone,
    because both are capped at forty entries and both end with the same "and N more"
    line. Read globally, the queries' cap would be attributed to the pages'.
    """
    trail: dict[str, Any] = {}
    citations: dict[str, Any] = {}
    page_chars: list[int] = []
    page_urls: list[str] = []
    pages: dict[str, Any] = {}

    text = "\n".join(lines)
    if m := _TRAIL_RE.search(text):
        trail["searches"] = int(m.group("searches"))
        trail["unique_queries"] = int(m.group("unique"))
        trail["pages_read"] = int(m.group("pages"))
        trail["thin_pages"] = int(m.group("thin") or 0)
        trail["research_minutes"] = int(m.group("minutes")) if m.group("minutes") else None
        trail["reviewer"] = (m.group("reviewer") or "").strip() or None

    section: str | None = None
    for raw in lines:
        line = raw.strip()
        if m := _DETAILS_RE.search(line):
            section = m.group("name").strip().lower()
            continue
        if line.startswith("</details>"):
            section = None
            continue

        if m := _UNREVIEWED_RE.match(line):
            trail["unreviewed"] = m.group("why").strip()
        if m := _NEVER_SURFACED_RE.search(line):
            citations["cited"] = int(m.group("of"))
            citations["never_surfaced"] = int(m.group("n"))
        if m := _UNOPENED_RE.search(line):
            citations["cited"] = int(m.group("of"))
            citations["unopened"] = int(m.group("n"))
        if m := _ALL_OPENED_RE.search(line):
            citations["cited"] = int(m.group("n"))
            citations["all_opened"] = True
        # The producer's two no-link outcomes. Both mean the grounding check did not
        # run at all, which is a stronger statement than any count of bad citations -
        # and the one an audit must not render as an absence of citation defects.
        if m := _READ_NO_CITE_RE.search(line):
            citations["cited"] = 0
            citations["read_but_cited_nothing"] = int(m.group("n"))
        if _NOTHING_CITED_RE.search(line):
            citations["cited"] = 0
            citations.setdefault("read_but_cited_nothing", 0)
        if m := _FENCE_TAG_RE.search(line):
            citations["fence_tags"] = int(m.group("n"))
        if m := _SCHEMELESS_RE.search(line):
            citations["schemeless"] = int(m.group("n"))

        if section == "pages read":
            if m := _PAGE_RE.match(line):
                page_urls.append(m.group("url"))
                page_chars.append(int(m.group("chars").replace(",", "")))
            elif m := _THIN_NOTE_RE.match(line):
                pages["thin_unlisted"] = int(m.group("n"))
            elif m := _FAILED_NOTE_RE.match(line):
                pages["failed"] = int(m.group("n"))
            elif m := _MORE_RE.match(line):
                pages["unlisted"] = int(m.group("n"))
        elif section == "queries run":
            if m := _MORE_RE.match(line):
                trail["queries_unlisted"] = int(m.group("n"))

    if page_chars:
        pages["listed"] = len(page_chars)
        pages["chars_min"] = min(page_chars)
        pages["chars_median"] = int(statistics.median(page_chars))
        pages["chars_max"] = max(page_chars)
        pages["below_1k"] = sum(1 for c in page_chars if c < 1000)
        pages["below_3k"] = sum(1 for c in page_chars if c < 3000)
        pages["urls"] = page_urls
        # Two ways to learn the listing was cut, and the head's own count is the one
        # that survives a producer that stops writing the marker: "pages read" counts
        # the thin stubs too, so the substantive total is that minus the thin note.
        if pages.get("unlisted"):
            pages["truncated"] = True
            pages["substantive_total"] = pages["listed"] + pages["unlisted"]
        elif trail.get("pages_read") is not None:
            thin = pages.get("thin_unlisted", trail.get("thin_pages") or 0)
            total = trail["pages_read"] - thin
            if total > pages["listed"]:
                pages["truncated"] = True
                pages["substantive_total"] = total
    return trail, citations, pages


def audit(path: Path) -> Audit:
    """Audit one report file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    result = Audit(path=str(path), chars=len(text))

    # Prose in front of the first heading. The template says the reply begins at
    # `## Answer`, and what lands here instead is the model answering its own harness -
    # a revision note, a bounce acknowledgement, "here is the corrected report". It ships
    # as the first thing the reader sees, so it is a delivery defect rather than a style
    # one, and it is invisible to every other check.
    body_start = next((i for i, ln in enumerate(lines) if ln.startswith("#")), len(lines))
    preamble = "\n".join(lines[:body_start]).strip()
    if preamble:
        result.preamble = preamble
        first = preamble.splitlines()[0][:160]
        result.findings.append(
            Finding(kind="preamble_before_first_heading", detail=f"{len(preamble)} chars, opens {first!r}")
        )

    # Section coverage. The appendix is fenced off first: its own `##`-free block sits
    # after the body, but a competitor's report may have any heading shape at all.
    appendix_at = next((i for i, ln in enumerate(lines) if _APPENDIX_START_RE.match(ln.strip())), len(lines))
    body = lines[:appendix_at]
    heads = [ln[3:].strip() for ln in body if ln.startswith("## ")]
    result.sections = heads
    missing = [s for s in REQUIRED_SECTIONS if not any(h.lower().startswith(s.lower()) for h in heads)]
    if missing:
        result.findings.append(Finding(kind="missing_template_section", detail=", ".join(missing)))
    result.extra_sections = [h for h in heads if not any(h.lower().startswith(s.lower()) for s in REQUIRED_SECTIONS)]
    if result.extra_sections:
        result.findings.append(Finding(kind="extra_h2_heading", detail=", ".join(result.extra_sections[:6])))

    tables = parse_tables(body)
    result.tables = [{"line": t.line_no, "width": t.width, "rows": len(t.rows), "header": t.header} for t in tables]
    for t in tables:
        ragged = [i for i, r in enumerate(t.rows) if len(r) != t.width]
        if ragged:
            result.findings.append(
                Finding(kind="ragged_table", detail=f"table@L{t.line_no}: {len(ragged)} row(s) off {t.width} columns")
            )

    result.findings += check_estimates(tables)
    result.findings += check_ranking(tables)
    result.findings += check_unfetched_cells(tables)

    trail, citations, pages = parse_trail(lines)
    result.trail, result.citations, result.pages = trail, citations, pages

    if not trail:
        result.findings.append(Finding(kind="no_research_trail", detail="no process appendix in this file"))
    if trail.get("unreviewed"):
        result.findings.append(Finding(kind="shipped_unreviewed", detail=trail["unreviewed"]))
    if citations.get("never_surfaced"):
        result.findings.append(
            Finding(
                kind="citation_never_surfaced",
                detail=f"{citations['never_surfaced']} of {citations.get('cited', '?')} cited links",
                hard=True,
            )
        )
    if citations.get("unopened"):
        result.findings.append(
            Finding(
                kind="citation_never_opened",
                detail=f"{citations['unopened']} of {citations.get('cited', '?')} cited links",
                hard=True,
            )
        )
    if citations.get("schemeless"):
        result.findings.append(Finding(kind="citation_without_scheme", detail=f"{citations['schemeless']} source(s)"))
    if citations.get("fence_tags"):
        result.findings.append(Finding(kind="citation_is_fence_tag", detail=f"{citations['fence_tags']} reference(s)"))
    # Hard, and hard for a different reason than a bad citation: the grounding check did
    # not run. Nothing in the report is traceable, and the appendix says so in a sentence
    # whose absence of a count reads, scanned, like a clean bill.
    if citations.get("cited") == 0 and "read_but_cited_nothing" in citations:
        read = citations["read_but_cited_nothing"]
        where = f"{read} page(s) were read and none is cited" if read else "no page was opened and none is cited"
        result.findings.append(Finding(kind="answer_cites_nothing", detail=where, hard=True))
    if pages.get("truncated"):
        # The producer lists forty pages and counts the rest. A median over the listed
        # prefix is a median over whichever fetches came first, so it is reported as
        # what it is rather than as the run's depth - and it does not become a verdict.
        result.findings.append(
            Finding(
                kind="fetch_depth_from_a_truncated_list",
                detail=(
                    f"{pages['listed']} of {pages['substantive_total']} substantive pages are listed; "
                    f"the prefix median is {pages['chars_median']:,} chars, which is not the run's depth"
                ),
            )
        )
    elif pages.get("chars_median") is not None and pages["chars_median"] < 3000:
        result.findings.append(
            Finding(
                kind="shallow_fetch_depth",
                detail=(
                    f"median {pages['chars_median']:,} chars over {pages['listed']} listed pages; "
                    f"{pages['below_3k']} under 3k, {pages['below_1k']} under 1k"
                ),
            )
        )
    return result


def _render(result: Audit) -> str:
    """The human summary: what was measured, then what is wrong with it."""
    out: list[str] = [f"== {result.path} ({result.chars:,} chars)"]

    t = result.trail
    if t:
        parts = [
            f"{t.get('searches', '?')} searches ({t.get('unique_queries', '?')} unique)",
            f"{t.get('pages_read', '?')} pages read",
        ]
        if t.get("thin_pages"):
            parts.append(f"{t['thin_pages']} thin")
        if t.get("research_minutes") is not None:
            parts.append(f"{t['research_minutes']}m")
        parts.append(f"reviewer: {t.get('reviewer') or 'not recorded'}")
        out.append("   trail:      " + ", ".join(parts))
    else:
        out.append("   trail:      not recorded (no process appendix)")

    p = result.pages
    if p.get("listed"):
        scope = (
            f"the first {p['listed']} of {p['substantive_total']} pages listed"
            if p.get("truncated")
            else f"{p['listed']} listed pages"
        )
        out.append(
            f"   fetch depth: median {p['chars_median']:,} chars "
            f"(min {p['chars_min']:,}, max {p['chars_max']:,}) over {scope}"
        )
    c = result.citations
    if c:
        out.append("   citations:  " + ", ".join(f"{k}={v}" for k, v in sorted(c.items()) if k != "urls"))
    out.append(f"   sections:   {', '.join(result.sections) if result.sections else 'none'}")
    if result.tables:
        widths = ", ".join(f"L{tb['line']}:{tb['width']}col×{tb['rows']}row" for tb in result.tables)
        out.append(f"   tables:     {widths}")

    hard = [f for f in result.findings if f.hard]
    soft = [f for f in result.findings if not f.hard]
    out.append(f"   findings:   {len(hard)} hard, {len(soft)} soft")
    for f in hard:
        out.append(f"     ! {f.kind}: {f.detail}")
    for f in soft:
        out.append(f"     - {f.kind}: {f.detail}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mechanical quality audit of a deep-research report.")
    parser.add_argument("reports", nargs="+", type=Path, help="report markdown file(s)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable findings")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any hard finding is present")
    args = parser.parse_args(argv)

    results = []
    for path in args.reports:
        if not path.is_file():
            print(f"no such report: {path}", file=sys.stderr)
            return 2
        results.append(audit(path))

    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2, ensure_ascii=False))
    else:
        print("\n\n".join(_render(r) for r in results))

    if args.strict and any(f.hard for r in results for f in r.findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
