"""Composing a table into the text an index can match against.

A table goes in as a table. `<table>` is the only shape that keeps all of what
a grid states -- which column a cell stood under, which cells are merged, which
row is the header -- and it is the shape a language model reads a table in,
which is what these chunks are for.

The flat form that used to live here was RAGFlow's ``__compose_table_content``:
one line per body row, each cell titled by its column (``Region: EU;Q1: 1.2M``).
It exists because a chunk boundary can render a grid meaningless -- cut one
below its first line and every row under the cut has lost its column names --
and a self-describing row survives that cut. The answer here is to not cut: a
table is one element and one unit to the chunker, so it is never split, and the
grid survives whole. What the flat form could never express is a merge. A
header standing over two columns had to be repeated across both, a cell running
down three rows was repeated or lost, and every format this reads -- Word,
PowerPoint, PDF -- states its merges exactly.

Here rather than inside one parser because every format with tables wants the
same answer: a Word table, a slide's table, and the rows a PDF's table finder
returns are three readers of one grid.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from html import escape

#: RAGFlow's cell taxonomy, ported pattern for pattern from
#: `deepdoc.parser.docx_parser`. Only one label carries a decision -- `Nu`, a
#: numeric cell -- but the rest have to keep their own labels, because the
#: decision is which label is the most common and lumping the others together
#: would let them outvote it. The Chinese date and quarter forms are written as
#: escapes so this file stays English source (AGENTS.md section 1.3), the way
#: `scripts/check_source_language.py` writes its own CJK ranges.
_CELL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), label)
    for pattern, label in (
        ("^(20|19)[0-9]{2}[\u5e74/-][0-9]{1,2}[\u6708/-][0-9]{1,2}\u65e5*$", "Dt"),
        ("^(20|19)[0-9]{2}\u5e74$", "Dt"),
        ("^(20|19)[0-9]{2}[\u5e74/-][0-9]{1,2}\u6708*$", "Dt"),
        ("^[0-9]{1,2}[\u6708/-][0-9]{1,2}\u65e5*$", "Dt"),
        ("^\u7b2c*[\u4e00\u4e8c\u4e09\u56db1-4]\u5b63\u5ea6$", "Dt"),
        ("^(20|19)[0-9]{2}\u5e74*[\u4e00\u4e8c\u4e09\u56db1-4]\u5b63\u5ea6$", "Dt"),
        ("^(20|19)[0-9]{2}[ABCDE]$", "DT"),
        ("^[0-9.,+%/ -]+$", "Nu"),
        (r"^[0-9A-Z/\._~-]+$", "Ca"),
        (r"^[A-Z]*[a-z' -]+$", "En"),
        (r"^[0-9.,+-]+[0-9A-Za-z/$\uffe5%<>\uff08\uff09()' -]+$", "NE"),
        ("^.{1}$", "Sg"),
    )
)


def cell_type(text: str) -> str:
    """Which kind of value a cell holds, in RAGFlow's labels.

    The prose fallback counts whitespace-separated words where RAGFlow counts
    tokenizer output. It cannot tell a person's name from other short prose
    (RAGFlow's `Nr`, which needs a POS tagger), and it under-counts a Chinese
    sentence, which carries no spaces. Neither changes a header decision: those
    labels differ from `Nu` either way, and `Nu` is the only label the caller
    tests for.
    """
    for pattern, label in _CELL_PATTERNS:
        if pattern.search(text):
            return label
    words = [word for word in text.split() if len(word) > 1]
    if len(words) > 3:
        return "Tx" if len(words) < 12 else "Lx"
    return "Ot"


def pad_grid(grid: list[list[str]]) -> list[list[str]]:
    """Square the grid off, so column `j` means the same thing in every row."""
    width = max((len(row) for row in grid), default=0)
    return [row + [""] * (width - len(row)) for row in grid]


@dataclass(frozen=True)
class Cell:
    """One cell of a grid, with what it spans.

    A plain string is a cell spanning one of each, which is why every caller
    that has no merges to report passes strings and never sees this.
    """

    text: str
    colspan: int = 1
    rowspan: int = 1


def _cells(row: "list[str | Cell]") -> list[Cell]:
    return [cell if isinstance(cell, Cell) else Cell(cell) for cell in row]


def html_table(grid: "list[list[str | Cell]]", caption: str = "") -> str:
    """One grid as an HTML table, its header rows marked.

    The shape a table keeps rather than one of the two it survives. Flattened
    to prose a cell loses the column it stood under; flattened by
    :func:`compose_table` it keeps the column name and loses the grid, which is
    the right trade for a format that has no cell boundaries to begin with and
    the wrong one for a PDF, where the boundaries are exactly what was
    measured. `<table>` keeps both, and it is the shape a language model reads
    a table in -- which is what these chunks are for.

    Which rows are headers is decided the way :func:`compose_table` decides it,
    off the same :func:`cell_type` vote, so a document's tables do not disagree
    about where their headers are depending on which path read them.

    A row may be given as plain strings or as :class:`Cell` values. The
    difference is merges: the flat form has to repeat a cell across the columns
    it straddles, because there is nowhere else to say so, and repeating it
    here would put the same value in two cells of a grid that has `colspan` to
    say it once. A caller that knows its merges passes cells; a caller reading
    a format that does not state them passes strings. Nothing squares the rows
    off: a row of three cells where one spans two columns is three cells.
    """
    rows = [_cells(row) for row in grid]
    rows = [row for row in rows if any(cell.text.strip() for cell in row)]
    if not rows:
        return ""

    header_rows = _header_rows([[cell.text for cell in row] for row in rows])
    out = ["<table>"]
    if caption.strip():
        out.append(f"<caption>{escape(caption.strip())}</caption>")
    for index, row in enumerate(rows):
        tag = "th" if index in header_rows else "td"
        built = []
        for cell in row:
            spans = ""
            if cell.colspan > 1:
                spans += f' colspan="{cell.colspan}"'
            if cell.rowspan > 1:
                spans += f' rowspan="{cell.rowspan}"'
            built.append(f"<{tag}{spans}>{escape(cell.text.strip())}</{tag}>")
        out.append(f"<tr>{''.join(built)}</tr>")
    out.append("</table>")
    return "\n".join(out)


def _header_rows(grid: list[list[str]]) -> list[int]:
    """Which rows of a grid read as headers rather than as body.

    The first always, and after it any row that breaks the body's dominant
    kind -- a rate table restating its columns per quarter carries headers
    further down, and RAGFlow finds them the same way.
    """
    if len(grid) < 2:
        return [0]
    body = Counter(cell_type(cell) for row in grid[1:] for cell in row)
    dominant = max(body.items(), key=lambda item: item[1])[0]
    header_rows = [0]
    if dominant == "Nu":
        for index, row in enumerate(grid[1:], start=1):
            types = Counter(cell_type(cell) for cell in row)
            if max(types.items(), key=lambda item: item[1])[0] != dominant:
                header_rows.append(index)
    return header_rows


__all__ = ["Cell", "cell_type", "html_table", "pad_grid"]
