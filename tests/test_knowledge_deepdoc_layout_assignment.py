"""Attaching each text box to the region it sits in.

The layout model answers with regions; this is what happens next -- every text
box on the page is given the type of the region covering it, page furniture is
dropped, and a figure carrying no text still gets a box so the crop survives.

The model's answer is supplied here rather than inferred, which is the only
part that needs the weights. Everything below it is arithmetic and ordering,
and it is where the damage is quiet: a header read as a title puts running
furniture into the index on every page of the document.

`tests/test_knowledge_deepdoc_layout.py` covers the same class against the real
weights and skips without them, which is every CI run.
"""

from __future__ import annotations

import pytest
from PIL import Image

from raven.knowledge.parser.deepdoc._layout_recognizer import LayoutRecognizer


def region(kind: str, x0: float, top: float, x1: float, bottom: float, score: float = 0.9) -> dict:
    """One region, as the model answers it: a corner box and a class name."""
    return {"type": kind, "score": score, "bbox": [x0, top, x1, bottom]}


def text(content: str, x0: float, top: float, x1: float, bottom: float) -> dict:
    return {"text": content, "x0": x0, "top": top, "x1": x1, "bottom": bottom}


def pages(count: int = 1, size: tuple[int, int] = (300, 900)) -> list[Image.Image]:
    return [Image.new("RGB", size) for _ in range(count)]


@pytest.fixture
def recogniser(monkeypatch: pytest.MonkeyPatch):
    """A layout recogniser whose model answer is whatever a case supplies.

    `__init__` opens an onnx session; nothing below reaches it.
    """

    def build(answer: list[list[dict]]) -> LayoutRecognizer:
        from raven.knowledge.parser.deepdoc import _recognizer

        monkeypatch.setattr(_recognizer.Recognizer, "__call__", lambda self, images, thr=0.2, batch_size=16: answer)
        instance = object.__new__(LayoutRecognizer)
        instance.garbage_layouts = ["abandon"]  # type: ignore[attr-defined]
        instance.client = None  # type: ignore[attr-defined]
        instance.label_list = LayoutRecognizer.labels  # type: ignore[attr-defined]
        return instance

    return build


class TestLabels:
    def test_the_label_order_is_the_published_models_own(self) -> None:
        """Not RAGFlow's list, which starts with a background class: read
        against that one a title comes back as `_background_` and a page
        footer as a title."""
        assert LayoutRecognizer.labels[:3] == ["Title", "Text", "Abandon"]

    def test_page_furniture_is_one_class(self) -> None:
        assert "Abandon" in LayoutRecognizer.labels
        assert "Header" not in LayoutRecognizer.labels


class TestFilterGarbageLayouts:
    @staticmethod
    def _recogniser() -> LayoutRecognizer:
        instance = object.__new__(LayoutRecognizer)
        instance.garbage_layouts = ["abandon"]  # type: ignore[attr-defined]
        return instance

    def test_a_low_scoring_furniture_region_is_dropped(self) -> None:
        boxes = [{"type": "abandon", "score": 0.1}]

        assert self._recogniser()._filter_garbage_layouts(boxes) == []

    def test_a_confident_furniture_region_is_kept(self) -> None:
        boxes = [{"type": "abandon", "score": 0.9}]

        assert self._recogniser()._filter_garbage_layouts(boxes) == boxes

    def test_a_low_scoring_content_region_is_kept(self) -> None:
        """The gate is about furniture, not confidence in general: a faint
        table is still a table and dropping it loses the content."""
        boxes = [{"type": "table", "score": 0.1}]

        assert self._recogniser()._filter_garbage_layouts(boxes) == boxes

    def test_the_threshold_can_be_moved(self) -> None:
        boxes = [{"type": "abandon", "score": 0.5}]

        assert self._recogniser()._filter_garbage_layouts(boxes, score_thr=0.6) == []


class TestForward:
    def test_it_applies_the_same_furniture_gate_as_the_main_path(self, recogniser) -> None:
        """Without this the HTTP endpoint answers raw low-confidence furniture
        while the parser beside it does not."""
        answer = [[region("abandon", 0, 0, 10, 10, score=0.1), region("text", 0, 20, 10, 30)]]

        out = recogniser(answer).forward(pages())

        assert [b["type"] for b in out[0]] == ["text"]


class TestAssigningBoxesToRegions:
    def test_a_box_takes_the_type_of_the_region_over_it(self, recogniser) -> None:
        answer = [[region("table", 0, 0, 300, 300)]]
        boxes = [text("a value", 10, 10, 50, 40)]

        out, _layout = recogniser(answer)(pages(), [boxes], scale_factor=1)

        assert out[0]["layout_type"] == "table"
        assert out[0]["layoutno"].startswith("table-")

    def test_a_box_under_no_region_is_left_untyped(self, recogniser) -> None:
        answer = [[region("table", 0, 0, 10, 10)]]
        boxes = [text("far away", 200, 500, 280, 540)]

        out, _layout = recogniser(answer)(pages(), [boxes], scale_factor=1)

        assert out[0]["layout_type"] == ""

    def test_an_equation_is_filed_as_a_figure(self, recogniser) -> None:
        """One name downstream: the crop and the caption are handled the same
        way for both."""
        answer = [[region("equation", 0, 0, 300, 300)]]
        boxes = [text("e = mc^2", 10, 10, 50, 40)]

        out, _layout = recogniser(answer)(pages(), [boxes], scale_factor=1)

        assert out[0]["layout_type"] == "figure"

    def test_the_page_layout_comes_back_beside_the_boxes(self, recogniser) -> None:
        answer = [[region("text", 0, 0, 300, 300)]]

        _out, layout = recogniser(answer)(pages(), [[text("a", 10, 10, 50, 40)]], scale_factor=1)

        assert len(layout) == 1
        assert layout[0][0]["type"] == "text"

    def test_the_scale_factor_brings_the_regions_back_to_page_coordinates(self, recogniser) -> None:
        """The model reads a page rendered at three times its size."""
        answer = [[region("text", 0, 0, 300, 300)]]

        _out, layout = recogniser(answer)(pages(), [[text("a", 10, 10, 50, 40)]], scale_factor=3)

        assert layout[0][0]["x1"] == pytest.approx(100.0)

    def test_a_cid_artefact_is_dropped(self, recogniser) -> None:
        """`(cid:12)` is what a PDF with a broken font map yields instead of a
        glyph. It is not text and must not reach the index."""
        answer = [[region("text", 0, 0, 300, 300)]]
        boxes = [text("(cid:12)", 10, 10, 50, 40), text("real words", 10, 50, 50, 80)]

        out, _layout = recogniser(answer)(pages(), [boxes], scale_factor=1)

        assert [b["text"] for b in out] == ["real words"]


class TestDroppingFurniture:
    """Page furniture, and the branch here that no longer drops it.

    The published model answers one `Abandon` class for headers, footers and
    page numbers, and `garbage_layouts` names it. But the loop that walks the
    regions still asks for the older model's three names -- footer, header,
    reference -- so an `abandon` region is never matched to a text box and the
    drop beside it cannot be reached. `"abandon"` appears exactly once in this
    module: in that list.

    Not a leak into the index, though: `_pipeline._read` skips an `abandon`
    region when it builds elements, so the furniture is excluded a step later.
    What is pinned here is that this path no longer does it, which is worth
    knowing before someone relies on it or deletes the list as unused.
    """

    def test_a_repeated_footer_reaches_the_output_today(self, recogniser) -> None:
        answer = [[region("abandon", 0, 800, 300, 880)] for _ in range(2)]
        per_page = [[text("Page footer", 10, 810, 100, 850)] for _ in range(2)]

        out, _layout = recogniser(answer)(pages(2), per_page, scale_factor=1)

        assert [b["text"] for b in out] == ["Page footer", "Page footer"]
        assert all(b["layout_type"] == "" for b in out), "never matched to the abandon region"

    def test_content_is_never_dropped(self, recogniser) -> None:
        answer = [
            [region("abandon", 0, 800, 300, 880), region("text", 0, 0, 300, 100)],
            [region("abandon", 0, 800, 300, 880), region("text", 0, 0, 300, 100)],
        ]
        per_page = [[text("Page footer", 10, 810, 100, 850), text(f"body {n}", 10, 10, 100, 50)] for n in range(2)]

        out, _layout = recogniser(answer)(pages(2), per_page, scale_factor=1)

        assert {"body 0", "body 1"} <= {b["text"] for b in out}

    @pytest.mark.xfail(
        reason="findLayout asks for footer/header/reference, so the published model's 'abandon' is never matched",
        strict=True,
    )
    def test_this_path_no_longer_drops_a_repeated_footer(self, recogniser) -> None:
        """What `garbage_layouts` is for, pinned as dead.

        `_pipeline._read` drops the region a step later, so nothing is lost
        today. This says the drop here is unreachable, so that a change which
        makes it reachable again is a visible one.
        """
        answer = [[region("abandon", 0, 800, 300, 880)] for _ in range(2)]
        per_page = [[text("Page footer", 10, 810, 100, 850)] for _ in range(2)]

        out, _layout = recogniser(answer)(pages(2), per_page, scale_factor=1)

        assert out == []

    def test_keeping_the_furniture_is_possible(self, recogniser) -> None:
        """`drop=False` is what the page viewer asks for: it draws the whole
        page, furniture included."""
        answer = [[region("abandon", 0, 800, 300, 880)] for _ in range(2)]
        per_page = [[text("Page footer", 10, 810, 100, 850)] for _ in range(2)]

        out, _layout = recogniser(answer)(pages(2), per_page, scale_factor=1, drop=False)

        assert len(out) == 2


class TestTextlessRegions:
    def test_a_figure_with_no_text_still_gets_a_box(self, recogniser) -> None:
        """Otherwise the crop is never taken and the figure is lost: there is
        no text box to hang it on."""
        answer = [[region("figure", 0, 0, 300, 300)]]

        out, _layout = recogniser(answer)(pages(), [[]], scale_factor=1)

        assert len(out) == 1
        assert out[0]["layout_type"] == "figure"
        assert out[0]["text"] == ""
        assert out[0]["layoutno"] == "figure-0"

    def test_a_figure_that_did_have_text_gets_no_second_box(self, recogniser) -> None:
        answer = [[region("figure", 0, 0, 300, 300)]]
        boxes = [text("a caption inside", 10, 10, 50, 40)]

        out, _layout = recogniser(answer)(pages(), [boxes], scale_factor=1)

        assert len(out) == 1

    def test_figures_and_equations_are_numbered_in_their_own_namespaces(self, recogniser) -> None:
        """A combined index under one prefix would collide with the tags
        `findLayout` assigns and merge two unrelated regions."""
        answer = [[region("figure", 0, 0, 100, 100), region("equation", 0, 200, 100, 300)]]

        out, _layout = recogniser(answer)(pages(), [[]], scale_factor=1)

        assert sorted(b["layoutno"] for b in out) == ["equation-0", "figure-0"]


#: A second detection, scored under the 0.08 this branch forces, so it is
#: filtered out and changes no assertion. Present because `np.squeeze` flattens
#: a one-detection block into a single row, which the branch cannot index -- a
#: shape the graph never answers, since it emits one per anchor.
_IGNORED_DETECTION = [900.0, 900.0, 910.0, 910.0, 0.0, 0.0]


class TestYolov10Variant:
    """The letterboxing variant.

    It fits the page inside the graph's square input rather than stretching it
    to fill it, so the aspect ratio survives, and records the padding it added
    so the boxes can be put back on the page afterwards. Getting the padding
    subtraction wrong moves every region by the width of the grey border.
    """

    @staticmethod
    def _variant(input_shape: tuple[int, int] = (64, 64)):
        from raven.knowledge.parser.deepdoc._layout_recognizer import LayoutRecognizer4YOLOv10

        instance = object.__new__(LayoutRecognizer4YOLOv10)
        instance.input_names = ["images"]
        instance.input_shape = input_shape
        instance.label_list = LayoutRecognizer4YOLOv10.labels
        instance.center = True
        return instance

    def test_a_page_is_fitted_inside_the_graphs_input(self) -> None:
        import numpy as np

        out = self._variant().preprocess([np.zeros((100, 200, 3), dtype=np.uint8)])

        assert out[0]["images"].shape == (1, 3, 64, 64)

    def test_the_padding_it_added_is_recorded(self) -> None:
        """A 2:1 page in a square input is padded top and bottom, and that
        offset has to come back off the boxes."""
        import numpy as np

        out = self._variant().preprocess([np.zeros((100, 200, 3), dtype=np.uint8)])

        _wx, _wy, dw, dh = out[0]["scale_factor"]
        assert dw == pytest.approx(0.0)
        assert dh > 0, "padded on the short axis"

    def test_a_square_page_needs_no_padding(self) -> None:
        import numpy as np

        out = self._variant().preprocess([np.zeros((100, 100, 3), dtype=np.uint8)])

        _wx, _wy, dw, dh = out[0]["scale_factor"]
        assert (dw, dh) == (pytest.approx(0.0), pytest.approx(0.0))

    def test_pixels_arrive_scaled_to_the_unit_range(self) -> None:
        import numpy as np

        out = self._variant().preprocess([np.full((64, 64, 3), 255, dtype=np.uint8)])

        assert out[0]["images"].max() == pytest.approx(1.0)

    def test_a_detection_comes_back_with_the_padding_taken_off(self) -> None:
        import numpy as np

        answer = np.array([[[10.0, 20.0, 30.0, 40.0, 0.9, 0.0], _IGNORED_DETECTION]], dtype=np.float32)

        found = self._variant().postprocess(answer, {"scale_factor": [2.0, 2.0, 5.0, 10.0]}, thr=0.5)

        assert len(found) == 1
        assert found[0]["bbox"] == [10.0, 20.0, 50.0, 60.0], "padding off, then scaled"

    def test_a_faint_detection_is_dropped(self) -> None:
        import numpy as np

        answer = np.array([[[10.0, 20.0, 30.0, 40.0, 0.01, 0.0], _IGNORED_DETECTION]], dtype=np.float32)

        assert self._variant().postprocess(answer, {"scale_factor": [1.0, 1.0, 0.0, 0.0]}, thr=0.5) == []

    def test_two_readings_of_one_region_are_thinned(self) -> None:
        import numpy as np

        answer = np.array([[[10.0, 20.0, 30.0, 40.0, 0.9, 0.0], [11.0, 21.0, 31.0, 41.0, 0.6, 0.0]]], dtype=np.float32)

        found = self._variant().postprocess(answer, {"scale_factor": [1.0, 1.0, 0.0, 0.0]}, thr=0.5)

        assert len(found) == 1
        assert found[0]["score"] == pytest.approx(0.9)

    def test_two_kinds_that_overlap_both_survive(self) -> None:
        import numpy as np

        answer = np.array([[[10.0, 20.0, 30.0, 40.0, 0.9, 0.0], [10.0, 20.0, 30.0, 40.0, 0.9, 3.0]]], dtype=np.float32)

        found = self._variant().postprocess(answer, {"scale_factor": [1.0, 1.0, 0.0, 0.0]}, thr=0.5)

        assert {b["type"] for b in found} == {"title", "figure"}
