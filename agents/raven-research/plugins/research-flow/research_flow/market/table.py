"""A market table, read as what it is: companies, facts, and their pages.

The flow's deep-report clause already asks for a comparison table whose
citations are markdown links. This module reads one of those tables as a
**set of claims with their sources** - one entity per row, one fact per
cell, one page per fact - so the facts can be checked instead of read.

Two things it will not do.

**It will not guess a field's source.** A fact cell that carries a link
gives that fact its source. A fact cell that carries no link falls back
to the row's source column, and if the row has none the fact is left
unsourced - which the scan refuses, rather than borrowing some other
citation for it. A fact cell carrying two links has no *one* source, and
the scan refuses rather than picking one.

**It will not invent a company.** A row with no name is not a company,
and a row whose name cell is a bare link is a citation rather than a name.

The header names the fields, and the field name is normalised to
``snake_case`` so the same table reads the same way to the scan's own
criteria - which name fields like ``price`` and ``what_it_does`` without
caring that the report printed "Price" or "What it does".
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from raven.records.types import Provenance

from .ledger import DEFAULT_USE, MarketEntity

__all__ = [
    "MarketTable",
    "OpenedPage",
    "entities_from_table",
    "find_market_table",
    "normalise_field",
    "opened_pages",
]

_INLINE_LINK_RE = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<url>[^)\s]+)\)")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_DELIM_CELL_RE = re.compile(r"^:?-+:?$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")

#: The Chinese header words, written as escapes so this file holds no
#: non-English source (AGENTS.md section 1.3). Each is named for what it means.
_LAI_YUAN = "\u6765\u6e90"  # "source"
_CHU_CHU = "\u51fa\u5904"  # "provenance"
_GONG_SI = "\u516c\u53f8"  # "company"
_CHAN_SHANG = "\u5382\u5546"  # "vendor"
_CHAN_PIN = "\u4ea7\u54c1"  # "product"

#: Headers that name a column of sources rather than a fact about the company.
_SOURCE_HEADER_RE = re.compile(rf"source|read\s+from|evidence|{_LAI_YUAN}|{_CHU_CHU}", re.I)

#: Headers that name the company rather than a fact about it.
_NAME_HEADER_RE = re.compile(
    rf"company|vendor|provider|player|brand|{_GONG_SI}|{_CHAN_SHANG}|{_CHAN_PIN}", re.I
)


@dataclass(frozen=True)
class OpenedPage:
    """One page the run opened: when, and which reader served it."""

    url: str
    retrieved_at: float
    source: str


@dataclass(frozen=True)
class MarketTable:
    """A table read as a market: a header, rows, and where the sources are."""

    header: list[str]
    rows: list[list[str]]
    name_index: int
    field_indices: list[int]
    source_index: int | None = None


def normalise_field(name: str) -> str:
    """A header a criterion can name: ``"What it does"`` becomes ``"what_it_does"``."""
    text = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower())
    return text.strip("_")


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT_RE.split(inner)]


def _is_delimiter(line: str, ncols: int) -> bool:
    cells = _cells(line)
    return len(cells) == ncols and all(_DELIM_CELL_RE.fullmatch(c) for c in cells)


def _fenced(lines: Sequence[str]) -> list[bool]:
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


def _tables(lines: Sequence[str]) -> list[tuple[list[str], list[list[str]]]]:
    tables: list[tuple[list[str], list[list[str]]]] = []
    fenced = _fenced(lines)
    i = 0
    while i < len(lines) - 1:
        if fenced[i] or fenced[i + 1] or "|" not in lines[i]:
            i += 1
            continue
        header = _cells(lines[i])
        if len(header) < 2 or not _is_delimiter(lines[i + 1], len(header)):
            i += 1
            continue
        rows: list[list[str]] = []
        j = i + 2
        while j < len(lines) and not fenced[j] and "|" in lines[j]:
            rows.append(_cells(lines[j]))
            j += 1
        tables.append((header, rows))
        i = j
    return tables


def _links(cell: str) -> list[str]:
    return [m.group("url") for m in _INLINE_LINK_RE.finditer(cell or "")]


def _only_links(cell: str) -> bool:
    """Whether the cell is nothing but links - a citation, not a fact."""
    text = _INLINE_LINK_RE.sub("", cell or "").strip()
    return bool(_links(cell)) and not text


def find_market_table(markdown: str) -> MarketTable | None:
    """The first table in ``markdown`` that reads as a market.

    A market table names a company in one column, carries at least one
    fact in another, and cites a page somewhere - in a source column, or
    inside the fact cells themselves. A table without a name column is
    not a market table, however many rows it has.

    The name column is the first column whose cells are neither citations
    nor empty, unless the header says otherwise: a table that starts with
    a rank and then names the company is still a market table, and the
    rank is not a fact about the company.
    """
    for header, rows in _tables(markdown.split("\n")):
        if not rows:
            continue
        width = len(header)

        # A market table cites a page. A table with no citation anywhere is a
        # table of numbers and labels - a metrics table, a spec table - and
        # reading it as a market would invent companies out of row headers.
        if not any(_links(cell) for row in rows for cell in row):
            continue

        link_cols = [
            idx
            for idx in range(width)
            if sum(1 for r in rows if idx < len(r) and _only_links(r[idx])) / len(rows) >= 0.6
        ]
        name_index = -1
        for idx in range(width):
            if idx in link_cols:
                continue
            header_says = bool(_NAME_HEADER_RE.search(header[idx]))
            cells_say = (
                sum(1 for r in rows if idx < len(r) and r[idx] and not _only_links(r[idx])) / len(rows) >= 0.6
            )
            if header_says or (name_index < 0 and cells_say):
                name_index = idx
                if header_says:
                    break
        if name_index < 0:
            continue

        source_index: int | None = None
        # The header is the reliable signal: a column headed "Read from" is a
        # column of pages whether or not every row filled one in - and a row
        # that left it empty is exactly the row whose facts have no page.
        for idx in range(width):
            if idx != name_index and _SOURCE_HEADER_RE.search(header[idx]):
                source_index = idx
                break
        if source_index is None and len(link_cols) == 1:
            only = link_cols[0]
            if only != name_index:
                source_index = only

        field_indices = [idx for idx in range(width) if idx != name_index and idx != source_index]
        if not field_indices:
            continue

        return MarketTable(
            header=header,
            rows=rows,
            name_index=name_index,
            field_indices=field_indices,
            source_index=source_index,
        )
    return None


def opened_pages(
    rows: Sequence[Mapping[str, Any]],
    *,
    extractor_keys: Mapping[str, str] | None = None,
) -> dict[str, OpenedPage]:
    """The pages the run opened, from the run's own ledger.

    ``extractor_keys`` maps an extractor name to the sources-registry key
    of the vendor that served the page - ``jina-reader`` to ``jina``. The
    ledger names the backend that served the page; the registry is keyed by
    the vendor whose terms that backend carries. A backend with no registry
    key keeps its own name, and the scan refuses it: a reader whose terms
    have not been looked at cannot back a market scan.
    """
    keys = dict(extractor_keys or {})
    pages: dict[str, OpenedPage] = {}
    for row in rows:
        if row.get("op") != "fetch" or not row.get("ok"):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        extractor = str(row.get("extractor") or "").strip()
        source = keys.get(extractor, extractor or "web")
        pages[url] = OpenedPage(
            url=url,
            retrieved_at=float(row.get("ts") or time.time()),
            source=source,
        )
    return pages


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def entities_from_table(
    table: MarketTable,
    pages: Mapping[str, OpenedPage],
    *,
    use: str = DEFAULT_USE,
) -> list[MarketEntity]:
    """The entities a market table says, and the pages behind them.

    Raises ``MarketScanError`` when a fact cannot be traced to a page the
    run opened. It is the same refusal the scan makes for a hand-built
    ledger, and it is the right one: a table whose facts cannot be traced
    is a table that says things nobody checked.
    """
    from .scan import MarketScanError

    entities: list[MarketEntity] = []
    for row in table.rows:
        if table.name_index >= len(row):
            continue
        name = _INLINE_LINK_RE.sub("", row[table.name_index]).strip()
        if not name:
            continue

        row_sources: list[str] = []
        if table.source_index is not None and table.source_index < len(row):
            row_sources = _links(row[table.source_index])

        fields: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        for idx in table.field_indices:
            if idx >= len(row):
                continue
            cell = row[idx].strip()
            if not cell or _only_links(cell):
                continue
            field = normalise_field(table.header[idx])
            if not field:
                continue
            cell_links = _links(cell)
            if len(cell_links) == 1:
                url = cell_links[0]
            elif len(cell_links) > 1:
                raise MarketScanError(
                    f"{name}: the {field!r} cell cites {len(cell_links)} pages, so the fact "
                    "cannot be traced to one of them"
                )
            elif row_sources:
                url = row_sources[0]
            else:
                continue
            text = _INLINE_LINK_RE.sub("", cell).strip()
            if not text:
                continue
            fields[field] = text
            pending.append((field, url))

        if not fields:
            continue

        # The pages are resolved after the facts are read, so a fact that
        # named a page the run never opened is a refusal rather than a row
        # with a missing provenance.
        provenance: list[Provenance] = []
        for field, url in pending:
            page = pages.get(url)
            if page is None:
                raise MarketScanError(
                    f"{name}: the {field!r} fact cites {url}, which the run did not open"
                )
            provenance.append(
                Provenance(
                    field=field,
                    source_url=url,
                    source=page.source,
                    method="fetch",
                    retrieved_at=_iso(page.retrieved_at),
                )
            )

        entities.append(
            MarketEntity(
                key=normalise_field(name) or name.lower(),
                name=name,
                fields=fields,
                provenance=provenance,
                use=use,
            )
        )

    if not entities:
        raise MarketScanError("the table names no company with a fact behind it")
    return entities
