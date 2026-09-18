"""Composing a table into the text an index can match against.

RAGFlow's ``__compose_table_content``: header rows are not emitted on their
own, they are lifted onto each body row as ``header: cell``, so a row survives
being read without the grid around it. That is the property a chunk boundary
destroys otherwise -- cut a grid anywhere below its first line and every row
under the cut has lost its column names.

Here rather than inside one parser because every format with tables wants the
same composition and the same answer: a Word table, a slide's table, and the
rows a PDF's table finder returns are three readers of one grid.
"""

from __future__ import annotations

import re
from collections import Counter

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


def compose_table(grid: list[list[str]]) -> str:
    """One line per body row, each cell titled by the headers above it.

    RAGFlow's `__compose_table_content`, kept: header rows are not emitted on
    their own, they are lifted onto each body row as `header: cell`, cells are
    joined with `;`, and a table of fewer than two rows composes to nothing at
    all -- a single row has no header to compose against.

    A table whose body is mostly numeric can carry headers further down as
    well (a rate table restating its columns per quarter), and RAGFlow finds
    them by looking for rows that break the numeric pattern. That test is why
    :func:`cell_type` exists.

    Not kept: RAGFlow returns the rows as a list when the table has more than
    three columns and as one joined string otherwise, which is a chunking
    decision -- how much of a table lands in one retrievable piece. Here the
    chunker owns that, and this returns the text either way.
    """
    if len(grid) < 2:
        return ""

    body = Counter(cell_type(cell) for row in grid[1:] for cell in row)
    dominant = max(body.items(), key=lambda item: item[1])[0]

    header_rows = [0]
    if dominant == "Nu":
        for index, row in enumerate(grid[1:], start=1):
            types = Counter(cell_type(cell) for cell in row)
            if max(types.items(), key=lambda item: item[1])[0] != dominant:
                header_rows.append(index)

    lines: list[str] = []
    for index, row in enumerate(grid):
        if index in header_rows:
            continue
        above = _headers_above(header_rows, index)
        cells: list[str] = []
        for column, cell in enumerate(row):
            if not cell:
                continue
            titles: list[str] = []
            for offset in above:
                title = grid[index + offset][column].strip()
                if title and title not in titles:
                    titles.append(title)
            prefix = ",".join(titles)
            cells.append(f"{prefix}: {cell}" if prefix else cell)
        if cells:
            lines.append(";".join(cells))
    return "\n".join(lines)


def _headers_above(header_rows: list[int], index: int) -> list[int]:
    """Offsets of the header rows a body row answers to, nearest block only.

    Where several header rows stand apart, only the run of consecutive ones
    closest above the body row titles it -- an earlier block belongs to the
    rows that followed it, not to this one.
    """
    offsets = [row - index for row in header_rows if row < index]
    position = len(offsets) - 1
    while position > 0:
        if offsets[position] - offsets[position - 1] > 1:
            return offsets[position:]
        position -= 1
    return offsets


__all__ = ["cell_type", "compose_table", "pad_grid"]
