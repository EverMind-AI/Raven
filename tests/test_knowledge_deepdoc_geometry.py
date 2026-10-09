"""Where a detected quadrilateral goes before anything reads it.

Between the detector and the recognizer sits arithmetic: four corners put in a
fixed order, clipped to the page, thrown away if they enclose nothing, sorted
into reading order, and warped flat. None of it needs a session -- and all of
it decides what the recognizer is handed, so an error here reads a page in the
wrong order or reads a sliver of one.

The classes load their models in ``__init__``, which is the whole reason this
was reachable only from tests that skip wherever the weights are not installed.
These methods touch no attribute a constructor sets, so the tests build the
objects without running it and stay honest about what they cover.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from raven.knowledge.parser.deepdoc import _ocr
from raven.knowledge.parser.deepdoc._ocr import OCR, TextDetector, TextRecognizer, transform


def _detector() -> TextDetector:
    """A detector with no session behind it. None of the geometry asks for one."""
    return object.__new__(TextDetector)


def _reader() -> OCR:
    return object.__new__(OCR)


def _quad(left: float, top: float, right: float, bottom: float) -> np.ndarray:
    return np.array([[left, top], [right, top], [right, bottom], [left, bottom]], dtype=np.float32)


# -- four corners, one order ---------------------------------------


def test_the_corners_come_back_top_left_first_going_clockwise() -> None:
    """Every later step indexes the corners rather than searching them: the
    crop reads 0-1 as the top edge and 0-3 as the left."""
    scrambled = np.array([[30, 20], [10, 20], [10, 5], [30, 5]], dtype=np.float32)

    ordered = _detector().order_points_clockwise(scrambled)

    assert ordered.tolist() == [[10, 5], [30, 5], [30, 20], [10, 20]]


def test_a_box_already_in_order_is_left_alone() -> None:
    box = _quad(10, 5, 30, 20)

    assert _detector().order_points_clockwise(box).tolist() == box.tolist()


def test_a_tilted_box_keeps_its_tilt() -> None:
    """A line scanned at an angle is a quadrilateral, not a rectangle, and
    ordering it must not square it off."""
    tilted = np.array([[12, 4], [34, 9], [32, 22], [10, 17]], dtype=np.float32)

    ordered = _detector().order_points_clockwise(tilted)

    assert ordered.tolist() == tilted.tolist()


# -- the page's own edges ------------------------------------------


def test_a_corner_past_the_page_is_pulled_back_inside() -> None:
    """The boxes are scaled up from a smaller map, so rounding can put one a
    pixel or two off the page. A crop taken there would raise."""
    box = _quad(-5, -2, 500, 400)

    clipped = _detector().clip_det_res(box.copy(), 100, 200)

    assert clipped.min() == 0
    assert clipped[:, 0].max() == 199, "the last column, not the width"
    assert clipped[:, 1].max() == 99


def test_clipping_leaves_a_box_that_is_already_inside() -> None:
    box = _quad(10, 5, 30, 20)

    assert _detector().clip_det_res(box.copy(), 100, 200).tolist() == box.tolist()


# -- what is not worth reading -------------------------------------


def test_a_box_too_thin_to_hold_a_glyph_is_dropped() -> None:
    """Three pixels of height is not a line of text; it is an artifact of the
    map, and sending it to the recognizer spends a model call on noise."""
    boxes = [_quad(10, 5, 60, 25), _quad(10, 40, 60, 42)]

    kept = _detector().filter_tag_det_res(boxes, (100, 200, 3))

    assert len(kept) == 1
    assert kept[0][0].tolist() == [10, 5]


def test_filtering_orders_and_clips_what_it_keeps() -> None:
    """One pass: the caller gets boxes that are in order and on the page."""
    scrambled = np.array([[260, 60], [10, 60], [10, 5], [260, 5]], dtype=np.float32)

    [kept] = _detector().filter_tag_det_res([scrambled], (100, 200, 3))

    assert kept[0].tolist() == [10, 5], "top-left first"
    assert kept[:, 0].max() == 199, "clipped to the page"


def test_a_list_of_corners_is_read_like_an_array() -> None:
    """What the postprocessor hands over is not always an ndarray."""
    boxes = [[[10, 5], [60, 5], [60, 25], [10, 25]]]

    kept = _detector().filter_tag_det_res(boxes, (100, 200, 3))

    assert len(kept) == 1


def test_clipping_alone_keeps_boxes_the_filter_would_drop() -> None:
    """The other pass, for a caller that has already decided what is a box --
    a table's cells, say, where a rule really is two pixels tall."""
    boxes = [_quad(10, 5, 60, 25), _quad(10, 40, 60, 42)]

    kept = _detector().filter_tag_det_res_only_clip(boxes, (100, 200, 3))

    assert len(kept) == 2


# -- reading order -------------------------------------------------


def test_boxes_are_ordered_down_the_page() -> None:
    top, middle, bottom = _quad(10, 5, 60, 25), _quad(10, 40, 60, 60), _quad(10, 80, 60, 100)

    ordered = _reader().sorted_boxes(np.array([bottom, top, middle]))

    assert [box[0][1] for box in ordered] == [5, 40, 80]


def test_two_boxes_on_one_line_are_ordered_left_to_right() -> None:
    """Two columns of a header sit at the same height to the eye and a few
    pixels apart in the map; reading order is still left, then right."""
    left, right = _quad(10, 7, 60, 25), _quad(120, 5, 180, 25)

    ordered = _reader().sorted_boxes(np.array([right, left]))

    assert [box[0][0] for box in ordered] == [10, 120]


def test_a_box_far_enough_down_is_a_new_line_however_far_left_it_sits() -> None:
    """Ten pixels is the whole of what separates 'beside' from 'below'."""
    first, second = _quad(120, 5, 180, 25), _quad(10, 40, 60, 60)

    ordered = _reader().sorted_boxes(np.array([second, first]))

    assert [box[0][1] for box in ordered] == [5, 40]


def test_one_box_sorts_to_itself() -> None:
    assert len(_reader().sorted_boxes(np.array([_quad(10, 5, 60, 25)]))) == 1


# -- the crop the recognizer reads ---------------------------------


def test_a_crop_comes_out_the_size_of_its_own_box() -> None:
    page = np.zeros((100, 200, 3), dtype=np.uint8)
    page[5:25, 10:60] = 255

    crop = _reader().get_rotate_crop_image(page, _quad(10, 5, 60, 25))

    assert crop.shape[:2] == (20, 50)
    assert crop.mean() > 200, "the ink, not the margin beside it"


def test_a_tilted_box_is_warped_upright() -> None:
    """The point of taking four corners rather than a rectangle: the strip the
    recognizer reads is straight even where the page was not."""
    page = np.zeros((100, 200, 3), dtype=np.uint8)
    tilted = np.array([[12, 10], [92, 20], [90, 40], [10, 30]], dtype=np.float32)
    cv2.fillPoly(page, np.array([tilted], dtype=np.int32), (255, 255, 255))

    crop = _reader().get_rotate_crop_image(page, tilted)

    height, width = crop.shape[:2]
    assert width > height * 3, "a flat strip, whatever angle it was found at"
    assert crop[height // 2, width // 2].mean() > 200


# -- what a session is handed, and what happens when it refuses ----


class _Stub:
    """A recognizer's input tensor, without the session behind it."""

    def __init__(self, width: object) -> None:
        self.shape = ["batch", 3, 48, width]


def _recognizer(width: object) -> TextRecognizer:
    reader = object.__new__(TextRecognizer)
    reader.rec_image_shape = [3, 48, 320]
    reader.input_tensor = _Stub(width)
    return reader


def test_a_crop_is_padded_out_to_the_batch_width() -> None:
    """A batch is one array, so every strip in it is the width of the widest;
    the narrow ones are padded rather than stretched, which would deform the
    glyphs the model was trained on."""
    crop = np.full((24, 60, 3), 255, dtype=np.uint8)

    ready = _recognizer("width").resize_norm_img(crop, 320 / 48)

    assert ready.shape == (3, 48, 320)
    assert ready[:, :, -1].max() == 0, "padding, not a stretched glyph"


def test_a_crop_wider_than_the_batch_is_cut_to_it() -> None:
    crop = np.full((24, 600, 3), 255, dtype=np.uint8)

    ready = _recognizer("width").resize_norm_img(crop, 2.0)

    assert ready.shape == (3, 48, 96)


def test_a_fixed_shape_model_gets_its_own_width_whatever_the_batch_asked() -> None:
    """A model compiled to one input size ignores the batch's ratio; sending
    it anything else raises inside onnxruntime."""
    crop = np.full((24, 60, 3), 255, dtype=np.uint8)

    ready = _recognizer(160).resize_norm_img(crop, 320 / 48)

    assert ready.shape == (3, 48, 160)


def test_the_operators_run_in_the_order_they_were_given() -> None:
    assert transform("a", [lambda data: data + "b", lambda data: data + "c"]) == "abc"


def test_an_operator_that_rejects_the_image_ends_the_chain() -> None:
    """A page too small to resize comes back as nothing, and the operators
    after it would read that as an image."""
    seen: list[str] = []

    assert transform("a", [lambda _: None, lambda data: seen.append("ran") or data]) is None
    assert seen == []


def test_no_operators_leaves_the_image_alone() -> None:
    assert transform("a") == "a"


def test_a_session_that_fails_once_is_asked_again(monkeypatch) -> None:
    """Indexing a long document is thousands of these calls, and onnxruntime
    can fail one under memory pressure that the next one survives."""
    monkeypatch.setattr(_ocr.time, "sleep", lambda _: None)
    attempts: list[int] = []

    class _Flaky:
        def run(self, _out, _inputs, _opts):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("busy")
            return ["ok"]

    assert _ocr._run_with_retry(_Flaky(), {}) == ["ok"]
    assert len(attempts) == 2


def test_a_session_that_keeps_failing_surfaces_the_failure(monkeypatch) -> None:
    """Retried briefly, then raised: a document that cannot be read has to
    say so rather than index as empty."""
    monkeypatch.setattr(_ocr.time, "sleep", lambda _: None)
    attempts: list[int] = []

    class _Broken:
        def run(self, _out, _inputs, _opts):
            attempts.append(1)
            raise RuntimeError("the graph is gone")

    with pytest.raises(RuntimeError, match="the graph is gone"):
        _ocr._run_with_retry(_Broken(), {})
    assert len(attempts) == 3, "tried, not tried forever"
