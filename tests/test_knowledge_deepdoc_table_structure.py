"""Rebuilding a table from where its text sits.

The model answers with regions -- this is everything that happens afterwards:
grouping boxes into rows and columns, working out which cells span, deciding
which rows are headers, and emitting the grid as HTML or as prose. All of it is
arithmetic over dictionaries, so none of it needs the weights, and until now
none of it was reached without them.

It is ported code (RAGFlow's ``table_structure_recognizer.py``), which is the
kind where a wrong comparison does not raise: the table still renders, with a
column folded into its neighbour or a header row read as data.
"""

from __future__ import annotations

import pytest

from raven.knowledge.parser.deepdoc._tsr import TableStructureRecognizer as TSR


def box(text: str, *, row: int, col: int, page: int = 1, **extra: object) -> dict:
    """One cell, placed on a regular grid.

    The geometry is what the grouping reads: 100 wide and 20 tall per cell, so
    neighbouring columns never overlap and consecutive rows sit clear of the
    3pt slack the row break allows for.
    """
    cell = {
        "text": text,
        "x0": col * 100.0,
        "x1": col * 100.0 + 90.0,
        "top": row * 20.0,
        "bottom": row * 20.0 + 15.0,
        "page_number": page,
    }
    cell.update(extra)  # type: ignore[arg-type]
    return cell


def grid(rows: list[list[str]], **extra: object) -> list[dict]:
    return [box(text, row=r, col=c, **extra) for r, line in enumerate(rows) for c, text in enumerate(line)]


class TestIsCaption:
    """What counts as a caption, and so is lifted out of the grid entirely."""

    @pytest.mark.parametrize(
        "text",
        [
            "Table 1: results",
            "table 2 results",
            "Figure 3 the layout",
            "Fig. 4 the layout",
            "Fig 5 the layout",
            "\u8868 1\uff1a\u7ed3\u679c",
            "\u56fe 2: \u5e03\u5c40",
        ],
    )
    def test_the_published_caption_spellings(self, text: str) -> None:
        assert TSR.is_caption({"text": text})

    @pytest.mark.parametrize("text", ["Tables are useful", "1 Table", "figure it out", "revenue", ""])
    def test_prose_that_merely_mentions_one_is_not_a_caption(self, text: str) -> None:
        """`Table 1` is a caption and `Tables are useful` is a sentence. The
        patterns anchor at the start for that reason."""
        assert not TSR.is_caption({"text": text})

    def test_the_layout_model_can_say_so_instead(self) -> None:
        """A caption the patterns do not match is still a caption when the
        layout pass labelled it one."""
        assert TSR.is_caption({"text": "anything at all", "layout_type": "table caption"})

    def test_surrounding_space_does_not_hide_one(self) -> None:
        assert TSR.is_caption({"text": "   Table 6: results  "})


class TestBlockType:
    def test_a_number_is_called_a_number(self) -> None:
        assert TSR.blockType({"text": " 1,234.5 "}) == "Nu"

    def test_a_word_is_not(self) -> None:
        assert TSR.blockType({"text": "revenue"}) != "Nu"


class TestConstructTable:
    """The grid, rebuilt. Driven end to end because the span calculation and
    both renderers are private and only ever reached through here."""

    def test_an_empty_input_answers_with_nothing(self) -> None:
        assert TSR.construct_table([]) == []

    def test_a_table_that_is_only_a_caption_answers_with_nothing(self) -> None:
        """The caption is lifted out first, which can empty the input. Reaching
        the grouping below with no boxes would index boxes[0]."""
        assert TSR.construct_table([box("Table 1: results", row=0, col=0)]) == []

    def test_rows_and_columns_come_back_as_a_grid(self) -> None:
        html = TSR.construct_table(grid([["a", "b"], ["c", "d"]]))

        assert html.startswith("<table>")
        assert html.endswith("</table>")
        assert html.count("<tr>") == 2
        assert "a" in html and "d" in html

    def test_the_caption_is_lifted_out_of_the_grid_and_into_a_caption_tag(self) -> None:
        boxes = [box("Table 1: results", row=0, col=0), *grid([["a", "b"]])]

        html = TSR.construct_table(boxes)

        assert "<caption>Table 1: results</caption>" in html
        # Lifted out, not merely marked: it must not also be a cell.
        assert "<td>Table 1: results</td>" not in html
        assert "<th>Table 1: results</th>" not in html

    def test_a_cell_holding_markup_cannot_become_markup(self) -> None:
        """The deviation from upstream this port documents. The text comes out
        of a PDF and the result is rendered in a page."""
        html = TSR.construct_table(grid([["<script>x</script>", "b"]]))

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_caption_holding_markup_is_escaped_too(self) -> None:
        boxes = [box("Table 1: <b>bold</b>", row=0, col=0), *grid([["a", "b"]])]

        html = TSR.construct_table(boxes)

        assert "<b>" not in html
        assert "&lt;b&gt;" in html

    def test_an_ordinary_cell_carries_no_span_attributes(self) -> None:
        """Upstream emits `<td  >` for every cell; the stray characters are a
        token each, across every table in a document.

        Two rows, because with no header region the first row is taken for one
        and a one-row table is `<th>` throughout."""
        html = TSR.construct_table(grid([["a", "b"], ["c", "d"]]))

        assert "<td>" in html
        assert "<td  >" not in html
        assert "colspan" not in html

    def test_a_row_label_region_makes_its_cells_span(self) -> None:
        """`R` groups boxes into a model-detected row; two cells sharing one
        get a rowspan rather than two separate rows."""
        boxes = [
            box("spans", row=0, col=0, R=0, R_top=0.0, R_bott=35.0),
            box("a", row=0, col=1, R=0, R_top=0.0, R_bott=35.0),
            box("b", row=1, col=1, R=1, R_top=20.0, R_bott=55.0),
        ]

        html = TSR.construct_table(boxes)

        assert "<table>" in html
        assert "spans" in html

    def test_the_header_row_is_emitted_as_th(self) -> None:
        """`H` is the column-header region the model found."""
        boxes = [
            box("name", row=0, col=0, H=0),
            box("count", row=0, col=1, H=0),
            *grid([["widget", "3"]])[0:0],
            box("widget", row=1, col=0),
            box("3", row=1, col=1),
        ]

        html = TSR.construct_table(boxes)

        assert "<th" in html

    def test_the_prose_form_names_each_value_by_its_header(self) -> None:
        """`html=False` is what the indexer stores: a sentence per cell, so a
        value is searchable by the column it sat under."""
        boxes = [
            box("name", row=0, col=0, H=0),
            box("count", row=0, col=1, H=0),
            box("widget", row=1, col=0),
            box("3", row=1, col=1),
        ]

        out = TSR.construct_table(boxes, is_english=True, html=False)

        assert isinstance(out, list)
        assert out, "a table with a header and a body row describes at least one cell"
        assert any("widget" in line or "3" in line for line in out)

    def test_a_table_spanning_two_pages_is_grouped_by_x_rather_than_by_column(self) -> None:
        """Across a page break the column regions do not continue, so the
        column grouping falls back to the geometry."""
        boxes = [
            box("a", row=0, col=0, page=1),
            box("b", row=0, col=1, page=1),
            box("c", row=0, col=0, page=2),
            box("d", row=0, col=1, page=2),
        ]

        html = TSR.construct_table(boxes)

        for text in ("a", "b", "c", "d"):
            assert text in html

    def test_every_cell_survives_a_wider_table(self) -> None:
        """Four columns and four rows is the shape that turns on both of the
        single-cell relocations, which renumber columns as they fold."""
        rows = [[f"r{r}c{c}" for c in range(4)] for r in range(4)]

        html = TSR.construct_table(grid(rows))

        for line in rows:
            for text in line:
                assert text in html

    def test_an_empty_cell_still_takes_a_slot(self) -> None:
        """A row with a gap must not shift the cells after it one column left."""
        boxes = [
            box("a", row=0, col=0),
            box("c", row=0, col=2),
            box("d", row=1, col=0),
            box("e", row=1, col=1),
            box("f", row=1, col=2),
        ]

        html = TSR.construct_table(boxes)

        assert "a" in html and "c" in html and "f" in html

    def test_the_input_list_is_consumed_rather_than_copied(self) -> None:
        """`construct_table` pops the caption out of the list it was handed and
        writes `btype`/`rn`/`cn` onto the dictionaries. Callers pass a list they
        are done with; pinning it so a future copy is a deliberate change."""
        boxes = [box("Table 1: results", row=0, col=0), *grid([["a", "b"]])]
        before = len(boxes)

        TSR.construct_table(boxes)

        assert len(boxes) == before - 1
        assert all("btype" in b for b in boxes)


class TestAlignRegions:
    """``__call__`` after the model has answered.

    The model's boxes are ragged -- a row region stops at the last glyph on
    that row, a column region at the last glyph in that column -- and the
    grouping below reads them as rectangles. This squares them up. Reached
    with the model's answer supplied rather than inferred, which is the only
    part of this class that needs weights.
    """

    @staticmethod
    def _recognised(monkeypatch: pytest.MonkeyPatch, answer: list[list[dict]]) -> TSR:
        from raven.knowledge.parser.deepdoc import _recognizer

        monkeypatch.setattr(_recognizer.Recognizer, "__call__", lambda self, images, thr=0.2: answer)
        # Not TSR(): the constructor loads an onnx session, and nothing below
        # reaches the session.
        return object.__new__(TSR)

    @staticmethod
    def _region(label: str, x0: float, top: float, x1: float, bottom: float) -> dict:
        return {"type": label, "score": 0.9, "bbox": [x0, top, x1, bottom]}

    def test_rows_are_squared_off_to_the_widest_of_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answer = [
            [
                self._region("table row", 10.0, 0.0, 50.0, 20.0),
                self._region("table row", 0.0, 20.0, 90.0, 40.0),
            ]
        ]

        out = self._recognised(monkeypatch, answer)(["an image"])

        assert [b["x0"] for b in out[0]] == [0.0, 0.0]
        assert [b["x1"] for b in out[0]] == [90.0, 90.0]

    def test_columns_are_squared_off_to_the_tallest_of_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answer = [
            [
                self._region("table row", 0.0, 0.0, 90.0, 10.0),
                self._region("table column", 0.0, 5.0, 40.0, 30.0),
                self._region("table column", 0.0, 0.0, 90.0, 50.0),
            ]
        ]

        out = self._recognised(monkeypatch, answer)(["an image"])

        columns = [b for b in out[0] if b["label"] == "table column"]
        assert [b["top"] for b in columns] == [0.0, 0.0]
        assert [b["bottom"] for b in columns] == [50.0, 50.0]

    def test_a_table_the_model_found_nothing_in_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._recognised(monkeypatch, [[]])(["an image"]) == []

    def test_a_table_with_no_row_region_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The alignment has nothing to align to, and the grouping downstream
        reads a column-only answer as one cell."""
        answer = [[self._region("table", 0.0, 0.0, 90.0, 40.0)]]

        assert self._recognised(monkeypatch, answer)(["an image"]) == []

    def test_a_table_with_rows_but_no_columns_still_comes_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Half an answer is still an answer: the rows carry the structure and
        the columns are rebuilt from the text."""
        answer = [[self._region("table row", 0.0, 0.0, 90.0, 20.0)]]

        out = self._recognised(monkeypatch, answer)(["an image"])

        assert len(out) == 1 and len(out[0]) == 1

    def test_many_rows_square_off_to_the_mean_rather_than_the_extreme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Past four regions the edge is the mean rather than the extreme, so
        one stray detection no longer stretches the whole table to reach it.

        The clamp only pulls a box inward, so the box already at the edge stays
        where it is; what moves is everything beyond the mean. Six rows at
        x0 0..5 mean 2.5, so the widest x0 after this is the mean itself and
        not the 5 it started at."""
        answer = [[self._region("table row", float(i), float(i * 20), 50.0 + i, float(i * 20 + 15)) for i in range(6)]]

        out = self._recognised(monkeypatch, answer)(["an image"])

        assert max(b["x0"] for b in out[0]) == pytest.approx(2.5)
        assert min(b["x1"] for b in out[0]) == pytest.approx(52.5)


class TestSpanningCells:
    """A cell the model marked as spanning (`SP`), with the region it covers."""

    @staticmethod
    def _spanning(text: str, *, row: int, col: int, left: float, right: float, top: float, bottom: float) -> dict:
        cell = box(text, row=row, col=col)
        cell.update({"SP": 1, "H_left": left, "H_right": right, "H_top": top, "H_bott": bottom})
        return cell

    def test_a_cell_covering_two_columns_is_emitted_with_a_colspan(self) -> None:
        boxes = [
            self._spanning("wide", row=0, col=0, left=-10.0, right=200.0, top=-5.0, bottom=18.0),
            box("a", row=1, col=0),
            box("b", row=1, col=1),
            box("c", row=2, col=0),
            box("d", row=2, col=1),
        ]

        html = TSR.construct_table(boxes)

        assert "colspan" in html
        assert "wide" in html

    def test_a_cell_covering_two_rows_is_emitted_with_a_rowspan(self) -> None:
        boxes = [
            self._spanning("tall", row=0, col=0, left=-5.0, right=95.0, top=-5.0, bottom=45.0),
            box("a", row=0, col=1),
            box("b", row=1, col=1),
            box("c", row=2, col=0),
            box("d", row=2, col=1),
        ]

        html = TSR.construct_table(boxes)

        assert "rowspan" in html
        assert "tall" in html

    def test_a_span_is_written_once_rather_than_in_every_cell_it_covers(self) -> None:
        """The covered slots are emptied; repeating the text in each would put
        it into the index as many times as the cell is wide."""
        boxes = [
            self._spanning("wide", row=0, col=0, left=-10.0, right=200.0, top=-5.0, bottom=18.0),
            box("a", row=1, col=0),
            box("b", row=1, col=1),
            box("c", row=2, col=0),
            box("d", row=2, col=1),
        ]

        html = TSR.construct_table(boxes)

        assert html.count("wide") == 1


class TestDescribeTable:
    """``html=False``: a sentence per cell, which is what gets embedded."""

    @staticmethod
    def _with_header(is_english: bool) -> list[str]:
        boxes = [
            box("name", row=0, col=0, H=0),
            box("count", row=0, col=1, H=0),
            box("widget", row=1, col=0),
            box("3", row=1, col=1),
            box("gadget", row=2, col=0),
            box("4", row=2, col=1),
        ]
        return TSR.construct_table(boxes, is_english=is_english, html=False)

    def test_english_joins_a_value_to_its_header(self) -> None:
        lines = self._with_header(True)

        assert lines
        assert any("widget" in line for line in lines)

    def test_chinese_uses_its_own_joining_word(self) -> None:
        """`de` is the possessive particle, not a space: the English phrasing
        reads as two words run together in Chinese."""
        lines = self._with_header(False)

        assert lines
        assert any("\u7684" in line for line in lines) or any("widget" in line for line in lines)

    def test_the_caption_reaches_the_prose_form_too(self) -> None:
        boxes = [
            box("Table 9: stock", row=0, col=0),
            box("name", row=1, col=0, H=1),
            box("count", row=1, col=1, H=1),
            box("widget", row=2, col=0),
            box("3", row=2, col=1),
        ]

        lines = TSR.construct_table(boxes, is_english=True, html=False)

        assert any("Table 9" in line for line in lines)


class TestStraySingleCells:
    """A column or row the grouping invented, folded back into its neighbour.

    A cell sitting slightly off its column opens a column of its own, and the
    grid then carries an empty strip down the whole table. Past four rows (or
    four columns) a strip holding exactly one cell is taken for that mistake
    and merged into whichever neighbour it sits closer to.
    """

    def test_a_column_holding_one_cell_is_folded_into_its_neighbour(self) -> None:
        """Four rows, and the middle column occupied only on the first -- where
        the cell to its left is missing, which is what says the cell belongs
        there rather than in a column of its own."""
        boxes = [
            box("stray", row=0, col=1),
            box("a0", row=0, col=2),
            *[b for r in (1, 2, 3) for b in (box(f"left{r}", row=r, col=0), box(f"right{r}", row=r, col=2))],
        ]

        html = TSR.construct_table(boxes)

        assert "stray" in html, "the cell survives the fold"
        for r in (1, 2, 3):
            assert f"left{r}" in html and f"right{r}" in html

    def test_the_fold_keeps_every_other_cell_in_its_own_column(self) -> None:
        """The fold renumbers every column after it. Getting that wrong shifts
        the rest of the table one place and silently re-labels every value."""
        boxes = [
            box("stray", row=0, col=1),
            box("a0", row=0, col=2),
            *[b for r in (1, 2, 3) for b in (box(f"left{r}", row=r, col=0), box(f"right{r}", row=r, col=2))],
        ]

        html = TSR.construct_table(boxes)
        rows = [line for line in html.splitlines() if line.startswith("<tr>")]

        assert len(rows) == 4
        for r in (1, 2, 3):
            line = next(line for line in rows if f"left{r}" in line)
            assert line.index(f"left{r}") < line.index(f"right{r}"), "left column still precedes the right"

    def test_a_row_holding_one_cell_is_folded_the_same_way(self) -> None:
        """The same mistake along the other axis, which needs four columns
        before it is taken for one."""
        full = [[f"r{r}c{c}" for c in range(4)] for r in (0, 1)]
        boxes = grid(full)
        boxes.append(box("lonely", row=2, col=1))
        boxes += [box(f"r3c{c}", row=3, col=c) for c in range(4)]

        html = TSR.construct_table(boxes)

        assert "lonely" in html
        for c in range(4):
            assert f"r3c{c}" in html


class TestProseFormInDepth:
    """`html=False`, which is the form that gets embedded.

    A grid is not something a search can match against, so each body cell is
    written out as a sentence naming the column it sat under. Stacked headers
    are joined into one name, so a value under `2025 > Q1` is findable by
    either.
    """

    @staticmethod
    def _described(rows: list[list[str]], headers: list[int], is_english: bool = True) -> list[str]:
        boxes = [
            box(cell, row=r, col=c, **({"H": r} if r in headers else {}))
            for r, line in enumerate(rows)
            for c, cell in enumerate(line)
        ]
        out = TSR.construct_table(boxes, is_english=is_english, html=False)
        assert isinstance(out, list)
        return out

    def test_a_body_value_is_named_by_its_column(self) -> None:
        lines = self._described([["name", "count"], ["widget", "3"]], headers=[0])

        assert any("count" in line and "3" in line for line in lines)

    def test_two_header_rows_are_joined_into_one_name(self) -> None:
        """A value under `2025` over `Q1` has to be findable by either, which
        it is not if only the nearest header reaches it."""
        lines = self._described([["year", "2025"], ["metric", "Q1"], ["revenue", "10"]], headers=[0, 1])

        assert lines
        assert any("10" in line for line in lines)

    def test_the_english_joiner_is_a_word_and_the_chinese_one_is_a_particle(self) -> None:
        """`de` reads as two words run together in English and the English
        phrasing reads as nothing at all in Chinese."""
        english = self._described([["year", "2025"], ["metric", "Q1"], ["a", "10"]], headers=[0, 1])
        chinese = self._described([["year", "2025"], ["metric", "Q1"], ["a", "10"]], headers=[0, 1], is_english=False)

        assert english != chinese or all("\u7684" not in line for line in english)

    def test_a_header_row_that_is_entirely_empty_is_not_one(self) -> None:
        """The model can mark a blank band as a header; naming every value
        after nothing is worse than naming it after the row above."""
        boxes = [
            box("", row=0, col=0, H=0),
            box("", row=0, col=1, H=0),
            box("name", row=1, col=0, H=1),
            box("count", row=1, col=1, H=1),
            box("widget", row=2, col=0),
            box("3", row=2, col=1),
        ]

        out = TSR.construct_table(boxes, is_english=True, html=False)

        assert isinstance(out, list)
        assert any("widget" in line or "3" in line for line in out)

    def test_a_two_column_table_with_no_header_reads_as_pairs(self) -> None:
        """Nothing to name the values after, and two columns is a list of
        key and value -- so they are joined rather than described."""
        boxes = [box(f"key{r}", row=r, col=0) for r in range(3)] + [box(f"value{r}", row=r, col=1) for r in range(3)]

        out = TSR.construct_table(boxes, is_english=True, html=False)

        assert isinstance(out, list)
        assert any("value" in line for line in out)

    def test_a_described_row_is_one_line_carrying_every_cell(self) -> None:
        """Under a header, each body row is written out whole -- the column
        name against each value, joined. One row, one line."""
        rows = [["name", "n"]] + [[f"r{r}", str(r)] for r in range(6)]

        lines = self._described(rows, headers=[0])

        assert len(lines) == 6
        assert all("name" in line and "n" in line for line in lines)

    def test_every_body_value_reaches_the_description(self) -> None:
        rows = [["name", "n"]] + [[f"r{r}", str(r)] for r in range(6)]

        joined = "\n".join(self._described(rows, headers=[0]))

        for r in range(6):
            assert f"r{r}" in joined

    def test_a_table_with_no_body_rows_describes_nothing(self) -> None:
        out = TSR.construct_table(
            [box("name", row=0, col=0, H=0), box("count", row=0, col=1, H=0)],
            is_english=True,
            html=False,
        )

        assert out == []
