"""One page, from a render to a list of elements.

The pipeline is the part that decides things: whether a page has a usable text
layer or has to be recognised, which region each line belongs to, whether a
table is read as a grid or as prose, and what is dropped. The models are
supplied here rather than loaded, so all of that is reachable without the
weights -- which is the only way it is reachable on CI.

What the page objects are is deliberately thin: `render` is the one function
that touches PyMuPDF, and every case below hands the render in.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from raven.knowledge.parser import LayoutType
from raven.knowledge.parser.deepdoc import _pipeline


def text(content: str, x0: float, top: float, x1: float, bottom: float) -> dict:
    return {"text": content, "x0": x0, "top": top, "x1": x1, "bottom": bottom}


def region(kind: str, x0: float, top: float, x1: float, bottom: float) -> dict:
    return {"type": kind, "x0": x0, "top": top, "x1": x1, "bottom": bottom}


def image(height: int = 400, width: int = 300) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


class TestWithin:
    """Membership by the middle of a box, not by containment.

    A region's box comes from a model looking at pixels and a line's from the
    glyphs; the two agree on where a paragraph is and not on its exact edge.
    """

    def test_a_box_inside_a_region_is_within_it(self) -> None:
        assert _pipeline._within(text("a", 10, 10, 20, 20), region("text", 0, 0, 100, 100))

    def test_a_box_outside_is_not(self) -> None:
        assert not _pipeline._within(text("a", 200, 200, 210, 210), region("text", 0, 0, 100, 100))

    def test_a_line_sticking_out_of_the_region_still_belongs(self) -> None:
        """The case containment would lose: the line runs past the region's
        right edge, but its middle is still inside."""
        assert _pipeline._within(text("a", 90, 10, 110, 20), region("text", 0, 0, 100, 100))

    def test_a_box_mostly_outside_does_not_belong(self) -> None:
        assert not _pipeline._within(text("a", 90, 10, 150, 20), region("text", 0, 0, 100, 100))


class TestTextOf:
    def test_it_joins_the_lines_inside_a_region(self) -> None:
        boxes = [text("hello", 10, 10, 50, 20), text("world", 10, 30, 50, 40)]

        assert _pipeline._text_of(region("text", 0, 0, 100, 100), boxes) == "hello world"

    def test_lines_outside_the_region_are_left_out(self) -> None:
        boxes = [text("inside", 10, 10, 50, 20), text("outside", 500, 500, 550, 520)]

        assert _pipeline._text_of(region("text", 0, 0, 100, 100), boxes) == "inside"

    def test_a_region_with_no_lines_is_empty(self) -> None:
        assert _pipeline._text_of(region("text", 0, 0, 100, 100), []) == ""


class TestTextBoxes:
    """Whether the page is read or recognised, decided by how much text the
    page's own layer carries."""

    def test_a_page_with_a_text_layer_is_read_rather_than_recognised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layer = [text("a" * 200, 10, 10, 100, 20)]
        monkeypatch.setattr(_pipeline, "_from_text_layer", lambda *_a: layer)

        found, recognised = _pipeline.text_boxes(SimpleNamespace(), image(), 1)

        assert found == layer
        assert recognised is False

    def test_a_page_with_almost_no_text_falls_through_to_recognition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A scanned page often carries a few stray glyphs rather than none;
        the threshold is what stops those standing in for the page."""
        monkeypatch.setattr(_pipeline, "_from_text_layer", lambda *_a: [text("x", 0, 0, 5, 5)])
        monkeypatch.setattr(_pipeline, "_from_ocr", lambda *_a: [text("recognised", 0, 0, 50, 10)])

        found, recognised = _pipeline.text_boxes(SimpleNamespace(), image(), 1)

        assert [b["text"] for b in found] == ["recognised"]
        assert recognised is True

    def test_a_page_with_no_text_at_all_is_recognised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_pipeline, "_from_text_layer", lambda *_a: [])
        monkeypatch.setattr(_pipeline, "_from_ocr", lambda *_a: [text("recognised", 0, 0, 50, 10)])

        _found, recognised = _pipeline.text_boxes(SimpleNamespace(), image(), 1)

        assert recognised is True


class TestRegions:
    def test_the_page_number_is_stamped_on_every_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        found = [region("text", 0, 0, 100, 100)]
        monkeypatch.setattr(_pipeline, "_layout_model", lambda: lambda *_a, **_kw: ([], [found]))

        out = _pipeline.regions(SimpleNamespace(), 7, image=image(), boxes=[])

        assert out[0]["page_number"] == 7

    def test_the_tagged_boxes_are_written_back_in_place(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller keeps the list it passed; the layout pass tags it with
        the region each line fell into."""
        tagged = [text("a", 0, 0, 10, 10) | {"layout_type": "text"}]
        monkeypatch.setattr(_pipeline, "_layout_model", lambda: lambda *_a, **_kw: (tagged, [[]]))
        boxes: list[dict] = [text("a", 0, 0, 10, 10)]

        _pipeline.regions(SimpleNamespace(), 1, image=image(), boxes=boxes)

        assert boxes[0]["layout_type"] == "text"

    def test_a_page_the_model_found_nothing_on_has_no_regions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_pipeline, "_layout_model", lambda: lambda *_a, **_kw: ([], []))

        assert _pipeline.regions(SimpleNamespace(), 1, image=image(), boxes=[]) == []


class TestRead:
    """The whole of one page, with every model supplied."""

    @staticmethod
    def _install(
        monkeypatch: pytest.MonkeyPatch,
        *,
        boxes: list[dict],
        found: list[dict],
        recognised: bool = False,
        tables: dict[int, str] | None = None,
    ) -> None:
        monkeypatch.setattr(_pipeline, "render", lambda _page: image())
        monkeypatch.setattr(_pipeline, "text_boxes", lambda *_a: (list(boxes), recognised))
        monkeypatch.setattr(_pipeline, "regions", lambda *_a, **_kw: found)
        monkeypatch.setattr(_pipeline, "_table_text", lambda *_a: tables or {})

    def test_a_region_becomes_an_element_carrying_its_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(
            monkeypatch,
            boxes=[text("a paragraph", 10, 10, 90, 20)],
            found=[region("text", 0, 0, 100, 100)],
        )

        out = _pipeline.elements(SimpleNamespace(), 1)

        assert out is not None
        assert [e.text for e in out] == ["a paragraph"]
        assert out[0].layout is LayoutType.TEXT

    def test_a_title_becomes_a_heading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(
            monkeypatch,
            boxes=[text("A Title", 10, 10, 90, 20)],
            found=[region("title", 0, 0, 100, 100)],
        )

        out = _pipeline.elements(SimpleNamespace(), 1)

        assert out is not None
        assert out[0].layout is LayoutType.HEADING
        assert out[0].heading_level == 1

    def test_page_furniture_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The clearest thing the layout model buys: a running footer repeats
        on every page, and a search matching it answers with the furniture of
        the document rather than the document."""
        self._install(
            monkeypatch,
            boxes=[text("Page 3 of 40", 10, 10, 90, 20)],
            found=[region("abandon", 0, 0, 100, 100)],
        )

        assert _pipeline.elements(SimpleNamespace(), 1) is None

    def test_a_table_uses_the_structure_model_when_it_read_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(
            monkeypatch,
            boxes=[text("a value", 10, 10, 90, 20)],
            found=[region("table", 0, 0, 100, 100)],
            tables={0: "<table><tr><td>a value</td></tr></table>"},
        )

        out = _pipeline.elements(SimpleNamespace(), 1)

        assert out is not None
        assert out[0].text.startswith("<table>")

    def test_a_table_falls_back_to_its_own_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prose is worse than a grid and much better than dropping the region,
        which is a table missing with nothing to say it was there."""
        self._install(
            monkeypatch,
            boxes=[text("a value", 10, 10, 90, 20)],
            found=[region("table", 0, 0, 100, 100)],
            tables={},
        )

        out = _pipeline.elements(SimpleNamespace(), 1)

        assert out is not None
        assert out[0].text == "a value"

    def test_an_empty_region_is_left_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, boxes=[], found=[region("text", 0, 0, 100, 100)])

        assert _pipeline.elements(SimpleNamespace(), 1) is None

    def test_a_figure_with_no_text_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The one region worth keeping empty: the crop is the content."""
        self._install(
            monkeypatch,
            boxes=[text("elsewhere", 500, 500, 550, 520)],
            found=[region("figure", 0, 0, 100, 100)],
        )

        out = _pipeline.elements(SimpleNamespace(), 1)

        assert out is not None
        assert out[0].layout is LayoutType.FIGURE
        assert out[0].text == ""

    def test_a_page_the_models_found_no_regions_on_answers_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`None` means "do what you would have done without any of this"."""
        self._install(monkeypatch, boxes=[text("a", 10, 10, 90, 20)], found=[])

        assert _pipeline.elements(SimpleNamespace(), 1) is None

    def test_a_recognised_page_is_refused_when_recognition_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller's own fallback -- the vision model -- reads it instead."""
        self._install(
            monkeypatch,
            boxes=[text("a", 10, 10, 90, 20)],
            found=[region("text", 0, 0, 100, 100)],
            recognised=True,
        )

        assert _pipeline.elements(SimpleNamespace(), 1, ocr=False) is None

    def test_a_recognised_page_is_read_when_recognition_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(
            monkeypatch,
            boxes=[text("scanned words", 10, 10, 90, 20)],
            found=[region("text", 0, 0, 100, 100)],
            recognised=True,
        )

        out = _pipeline.elements(SimpleNamespace(), 1)

        assert out is not None and out[0].text == "scanned words"

    def test_the_bounding_box_comes_from_the_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(
            monkeypatch,
            boxes=[text("a", 1.0, 2.0, 3.0, 4.0)],
            found=[region("text", 1.0, 2.0, 3.0, 4.0)],
        )

        out = _pipeline.elements(SimpleNamespace(), 1)

        assert out is not None
        assert (out[0].bbox.x0, out[0].bbox.top, out[0].bbox.x1, out[0].bbox.bottom) == (1.0, 2.0, 3.0, 4.0)


class TestOnePageFailingIsNotADocumentFailing:
    def test_a_model_raising_on_one_page_answers_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_pipeline, "render", lambda _page: (_ for _ in ()).throw(RuntimeError("a bad page")))

        assert _pipeline.elements(SimpleNamespace(), 1) is None

    def test_missing_weights_are_raised_rather_than_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A page that cannot be read is one page; an install with no models is
        every page, and the caller is told so once rather than silently given
        a document of nothing."""
        from raven.knowledge.parser.deepdoc._onnx import ModelsMissingError

        monkeypatch.setattr(_pipeline, "render", lambda _page: (_ for _ in ()).throw(ModelsMissingError("no weights")))

        with pytest.raises(ModelsMissingError):
            _pipeline.elements(SimpleNamespace(), 1)


class TestAvailability:
    def test_usable_follows_the_layout_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_pipeline, "available", lambda *names: names == ("layout",))

        assert _pipeline.usable() is True

    def test_readable_follows_the_recognition_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_pipeline, "available", lambda *names: names == ("det", "rec"))

        assert _pipeline.readable() is True
        assert _pipeline.usable() is False
