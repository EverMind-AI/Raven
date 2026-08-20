"""A table comes out as the source printed it, under the caption that names it.

The finder's cell grid is neither what a reader calls "Table 2" -- that takes
in the caption above and the note below -- nor reliably one table, so these pin
the splitting: side by side, stacked, and the strategies tried in between.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.services.ingest import tables as tables_mod
from raven.ppt.services.ingest.tables import extract_tables

fitz = pytest.importorskip("fitz", reason="render extra (pymupdf) not installed")


class _Pixmap:
    width = 640
    height = 320

    def save(self, path: str) -> None:
        Path(path).write_bytes(b"rendered table")


@pytest.fixture(autouse=True)
def _stub_pixels(monkeypatch: pytest.MonkeyPatch):
    """These tests are about regions, not pixels: the render is stubbed so a
    fake page needs no real content."""
    monkeypatch.setattr(tables_mod, "autocrop_border", lambda _path: (640, 320))


def test_a_later_strategy_answers_where_the_default_found_nothing_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paper that rules its tables horizontally only defeats the default
    finder, so the looser strategies are tried in order -- and only where the
    page drew rules, which is what keeps them from finding text in columns."""

    class Table:
        def __init__(self, rows: int, cols: int, bbox) -> None:
            self.row_count = rows
            self.col_count = cols
            self.bbox = bbox

        def extract(self):
            return [[f"cell-{row}-{col}" for col in range(self.col_count)] for row in range(self.row_count)]

        def to_markdown(self, *, clean: bool):
            assert clean
            return "| Method | Accuracy | Latency |\n|---|---:|---:|\n| Proposed | 83.7 | 11.4 |"

    class Finder:
        def __init__(self, found) -> None:
            self.tables = found

    class Page:
        rect = fitz.Rect(0, 0, 612, 792)

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def find_tables(self, **strategy):
            self.calls.append(strategy)
            if not strategy:
                return Finder([Table(1, 2, (72, 80, 360, 120))])  # too few rows
            if strategy.get("horizontal_strategy") == "lines":
                return Finder([Table(4, 3, (72, 120, 522, 288))])
            return Finder([])

        def get_pixmap(self, **_kwargs):
            return _Pixmap()

    monkeypatch.setattr(
        tables_mod,
        "_horizontal_rules",
        lambda bbox, _drawings: [(bbox.x0, bbox.y0, bbox.x1), (bbox.x0, bbox.y1, bbox.x1)],
    )
    page = Page()

    extracted = extract_tables(
        page,
        tmp_path / "paper_p001",
        page_text="Table 1. Benchmark results",
        text_blocks=[(72, 90, 400, 110, "Table 1. Benchmark results")],
    )

    assert len(extracted) == 1
    assert extracted[0].source_label == "Table 1"
    assert extracted[0].markdown.startswith("| Method")
    assert extracted[0].path.is_file()
    assert len(page.calls) == 3


def test_a_page_that_never_says_table_is_not_searched(tmp_path: Path) -> None:
    class Page:
        rect = fitz.Rect(0, 0, 612, 792)

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def find_tables(self, **strategy):
            self.calls.append(strategy)
            return None

    page = Page()
    assert extract_tables(page, tmp_path / "plain_p001", page_text="No table here") == []
    assert page.calls == []


class _SideBySidePage:
    """One region holding two tables, in the shape an IEEE two-column page prints."""

    rect = fitz.Rect(0, 0, 612, 792)

    class Table:
        row_count = 4
        col_count = 6
        bbox = (50, 120, 562, 280)

        def extract(self):
            return [[f"cell-{row}-{column}" for column in range(self.col_count)] for row in range(self.row_count)]

        def to_markdown(self, *, clean: bool):
            assert clean
            return "| left | value | unit | right | value | unit |\n|---|---:|---|---|---:|---|"

    class Finder:
        tables = [None]

    def find_tables(self, **_strategy):
        finder = self.Finder()
        finder.tables = [self.Table()]
        return finder

    def get_drawings(self):
        return []

    def get_text(self, _kind: str, *, clip, sort: bool):
        assert sort
        return "left benchmark values" if clip.x0 < 300 else "right benchmark values"

    def get_pixmap(self, **_kwargs):
        return _Pixmap()


_SIDE_BY_SIDE_BLOCKS = [
    (50, 85, 250, 110, "TABLE I\nLEFT RESULTS"),
    (325, 85, 562, 110, "Table 2 Results"),
    (50, 286, 285, 302, "Results in bold are best."),
    (325, 286, 562, 302, "Higher is better."),
]


def _side_by_side_rules(bbox, _drawings):
    if bbox.width > 300:
        return [(50, 120, 562), (50, 280, 562)]
    if bbox.x1 <= 300:
        return [(50, 120, 285), (50, 280, 285)]
    return [(325, 120, 562), (325, 280, 562)]


def test_two_tables_printed_side_by_side_split_on_their_captions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merged into one region by the finder; their captions sit in a row over
    them, and the midpoint between the captions is where the region divides.
    Each half re-reads its own text -- handing both the merged markdown would
    make one of them a misquote."""
    monkeypatch.setattr(tables_mod, "_horizontal_rules", _side_by_side_rules)

    extracted = extract_tables(
        _SideBySidePage(),
        tmp_path / "paper_p001",
        page_text="TABLE I\nLEFT RESULTS\nTable 2 Results",
        text_blocks=_SIDE_BY_SIDE_BLOCKS,
    )

    assert [table.source_label for table in extracted] == ["Table I", "Table 2"]
    assert extracted[0].bbox.x1 < extracted[1].bbox.x0
    assert [table.markdown for table in extracted] == ["left benchmark values", "right benchmark values"]


def test_a_side_by_side_neighbour_does_not_push_the_second_region_down_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clip limits exist so two *stacked* tables do not overrun each other.
    Applied to a row they pushed the second table's region below the first
    one's floor, so its image began 200pt under the table it names."""
    monkeypatch.setattr(tables_mod, "_horizontal_rules", _side_by_side_rules)

    extracted = extract_tables(
        _SideBySidePage(),
        tmp_path / "paper_p001",
        page_text="TABLE I\nLEFT RESULTS\nTable 2 Results",
        text_blocks=_SIDE_BY_SIDE_BLOCKS,
    )

    for table in extracted:
        assert table.bbox.y0 <= 90, table.bbox
        assert table.bbox.y1 >= 300, table.bbox


def test_the_note_under_a_table_is_part_of_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Results in bold are best" is how the table says what its numbers mean;
    cropped away, the slide shows bold numbers and no key."""
    monkeypatch.setattr(tables_mod, "_horizontal_rules", _side_by_side_rules)

    extracted = extract_tables(
        _SideBySidePage(),
        tmp_path / "paper_p001",
        page_text="TABLE I\nLEFT RESULTS\nTable 2 Results",
        text_blocks=_SIDE_BY_SIDE_BLOCKS,
    )

    # 302 is the bottom of the footnote line; the padding takes it further.
    assert all(table.bbox.y1 >= 302 for table in extracted)


def test_a_table_the_page_never_captions_is_not_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caption is the only thing that separates a table from a page's own
    column layout, so a grid without one is not a table anybody can cite."""
    monkeypatch.setattr(tables_mod, "_horizontal_rules", _side_by_side_rules)

    extracted = extract_tables(
        _SideBySidePage(),
        tmp_path / "paper_p001",
        page_text="Table 9 is discussed elsewhere",
        text_blocks=[(50, 600, 562, 620, "Table 9 shows the ablation.")],
    )

    assert extracted == []
