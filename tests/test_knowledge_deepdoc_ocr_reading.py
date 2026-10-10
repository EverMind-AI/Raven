"""Reading a scanned page: detect the lines, crop them, recognise them.

The detector and the recogniser are the two graphs; everything between them is
here -- putting the quadrilaterals into reading order, warping each one flat,
and throwing away what was read too faintly to trust. The graphs are supplied
rather than loaded, so this runs without the weights.

`tests/test_knowledge_deepdoc_ocr.py` drives the same class with the real
models and skips without them, which is every CI run. What is asserted here is
the part that does not depend on what a model happens to answer.
"""

from __future__ import annotations

import numpy as np
import pytest

from raven.knowledge.parser.deepdoc._ocr import OCR


def quad(x0: float, top: float, x1: float, bottom: float) -> np.ndarray:
    """A detection, as the detector answers it: four corners, clockwise from
    the top left."""
    return np.array([[x0, top], [x1, top], [x1, bottom], [x0, bottom]], dtype=np.float32)


def page(height: int = 200, width: int = 300) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


class _Detector:
    """The detector answers one array of quadrilaterals, not a list of them."""

    def __init__(self, boxes) -> None:
        self.boxes = None if boxes is None else np.asarray(boxes, dtype=np.float32)
        self.seen: list[np.ndarray] = []

    def __call__(self, image):
        self.seen.append(image)
        return self.boxes, 0.0


class _Recogniser:
    """Answers one (text, score) per crop, in the order they arrive."""

    def __init__(self, answers: list[tuple[str, float]]) -> None:
        self.answers = answers
        self.batches: list[list] = []

    def __call__(self, crops):
        self.batches.append(crops)
        return self.answers[: len(crops)] or [("", 0.0)] * len(crops), 0.0


def reader(*, boxes=None, answers: list[tuple[str, float]] | None = None, drop_score: float = 0.5) -> OCR:
    """An OCR with both graphs supplied.

    `__init__` opens two onnx sessions; nothing below reaches either.
    """
    instance = object.__new__(OCR)
    instance.text_detector = [_Detector(boxes)]  # type: ignore[attr-defined]
    instance.text_recognizer = [_Recogniser(answers or [])]  # type: ignore[attr-defined]
    instance.drop_score = drop_score  # type: ignore[attr-defined]
    instance.crop_image_res_index = 0  # type: ignore[attr-defined]
    return instance


class TestSortedBoxes:
    """Reading order: down the page, then across each line."""

    def test_lines_come_back_top_to_bottom(self) -> None:
        out = reader().sorted_boxes(np.asarray([quad(0, 100, 50, 120), quad(0, 0, 50, 20)]))

        assert [b[0][1] for b in out] == [0, 100]

    def test_words_on_one_line_come_back_left_to_right(self) -> None:
        """Within ten points of each other counts as the same line, which is
        what keeps a line of words from being read as a column."""
        out = reader().sorted_boxes(np.asarray([quad(100, 2, 150, 20), quad(0, 0, 50, 18)]))

        assert [b[0][0] for b in out] == [0, 100]

    def test_a_gap_wider_than_a_line_is_two_lines(self) -> None:
        out = reader().sorted_boxes(np.asarray([quad(100, 0, 150, 18), quad(0, 50, 50, 68)]))

        assert [b[0][1] for b in out] == [0, 50]

    def test_a_single_detection_comes_back_alone(self) -> None:
        assert len(reader().sorted_boxes(np.asarray([quad(0, 0, 50, 20)]))) == 1


class TestRotateCrop:
    """One quadrilateral, warped flat into an upright strip.

    The detector answers four corners rather than a rectangle, because a line
    on a page scanned at an angle is not one; the recogniser reads left to
    right, so the quad is projected before it is read.
    """

    def test_a_crop_has_the_measured_size_of_its_quad(self) -> None:
        out = reader().get_rotate_crop_image(page(), quad(10, 20, 110, 50))

        assert out.shape[1] == pytest.approx(100, abs=1)
        assert out.shape[0] == pytest.approx(30, abs=1)

    def test_a_quad_of_the_wrong_shape_is_refused(self) -> None:
        with pytest.raises(AssertionError):
            reader().get_rotate_crop_image(page(), np.array([[0, 0], [1, 1]], dtype=np.float32))

    def test_a_tall_strip_is_offered_to_the_recogniser_three_ways(self) -> None:
        """A crop half again as tall as it is wide is either a column of
        vertical text or a line rotated by the scan, and nothing in the shape
        says which. So all three orientations are read and the one the
        recogniser is most confident about wins."""
        instance = reader(answers=[("x", 0.1)])

        instance.get_rotate_crop_image(page(), quad(10, 10, 30, 150))

        assert len(instance.text_recognizer[0].batches) == 3

    def test_the_orientation_the_recogniser_prefers_is_the_one_kept(self) -> None:
        upright = reader(answers=[("x", 0.1)])
        turned = reader(answers=[("x", 0.1)])
        # Second call -- the clockwise turn -- reads best.
        scores = iter([0.1, 0.9, 0.1])

        class _Rising:
            def __init__(self) -> None:
                self.batches: list[list] = []

            def __call__(self, crops):
                self.batches.append(crops)
                return [("x", next(scores))], 0.0

        turned.text_recognizer[0] = _Rising()

        kept_upright = upright.get_rotate_crop_image(page(), quad(10, 10, 30, 150))
        kept_turned = turned.get_rotate_crop_image(page(), quad(10, 10, 30, 150))

        assert kept_upright.shape[0] > kept_upright.shape[1], "no rotation beat the original"
        assert kept_turned.shape[0] < kept_turned.shape[1], "the turn the recogniser preferred"

    def test_a_wide_crop_is_read_as_it_is(self) -> None:
        """Only a tall crop is ambiguous; a line that is wider than it is tall
        is already the way the recogniser reads."""
        instance = reader(answers=[("x", 0.9)])

        instance.get_rotate_crop_image(page(), quad(10, 10, 110, 40))

        assert instance.text_recognizer[0].batches == [], "no orientation trial"


class TestDetect:
    def test_it_answers_a_box_per_detection_with_no_text_yet(self) -> None:
        found = list(reader(boxes=[quad(0, 0, 50, 20), quad(0, 50, 50, 70)]).detect(page()))

        assert len(found) == 2
        assert all(text == ("", 0) for _box, text in found)

    def test_a_page_that_is_not_there_is_not_read(self) -> None:
        assert reader(boxes=[]).detect(None) is None

    def test_a_page_the_detector_found_nothing_on_answers_nothing(self) -> None:
        assert reader(boxes=None).detect(page()) is None

    def test_the_boxes_come_back_in_reading_order(self) -> None:
        found = list(reader(boxes=[quad(0, 100, 50, 120), quad(0, 0, 50, 20)]).detect(page()))

        assert [b[0][1] for b, _ in found] == [0, 100]


class TestRecognize:
    def test_a_confident_reading_comes_back(self) -> None:
        out = reader(answers=[("hello", 0.9)]).recognize(page(), quad(0, 0, 50, 20))

        assert out == "hello"

    def test_a_faint_reading_is_thrown_away(self) -> None:
        """Below the threshold the recogniser is guessing, and a guess in the
        index is worse than a gap."""
        out = reader(answers=[("he11o", 0.1)]).recognize(page(), quad(0, 0, 50, 20))

        assert out == ""

    def test_a_batch_is_read_in_one_call(self) -> None:
        instance = reader(answers=[("a", 0.9), ("b", 0.9)])

        out = instance.recognize_batch([page(20, 50), page(20, 50)])

        assert out == ["a", "b"]
        assert len(instance.text_recognizer[0].batches) == 1, "one call, not one per crop"

    def test_a_faint_item_in_a_batch_is_blanked_without_dropping_the_rest(self) -> None:
        """Blanked rather than removed: the caller pairs these back with the
        boxes they came from, by position."""
        out = reader(answers=[("a", 0.9), ("junk", 0.1), ("c", 0.9)]).recognize_batch([page(20, 50) for _ in range(3)])

        assert out == ["a", "", "c"]

    def test_the_scored_form_keeps_the_score_of_a_blanked_item(self) -> None:
        """The HTTP adapter picks between rotations by score, so it needs the
        number even where the text was dropped."""
        out = reader(answers=[("junk", 0.1)]).recognize_batch_with_score([page(20, 50)])

        assert out == [("", pytest.approx(0.1))]

    def test_the_scored_form_keeps_a_confident_reading_whole(self) -> None:
        out = reader(answers=[("hello", 0.9)]).recognize_batch_with_score([page(20, 50)])

        assert out == [("hello", pytest.approx(0.9))]


class TestReadingAWholePage:
    def test_every_confident_line_comes_back_with_its_box(self) -> None:
        instance = reader(
            boxes=[quad(0, 0, 50, 20), quad(0, 50, 50, 70)],
            answers=[("first", 0.9), ("second", 0.9)],
        )

        out = instance(page())

        assert [text for _box, (text, _score) in out] == ["first", "second"]
        assert all(isinstance(box, list) for box, _ in out), "boxes answered as plain lists"

    def test_a_faint_line_is_left_out_entirely(self) -> None:
        instance = reader(
            boxes=[quad(0, 0, 50, 20), quad(0, 50, 50, 70)],
            answers=[("good", 0.9), ("junk", 0.1)],
        )

        out = instance(page())

        assert [text for _box, (text, _score) in out] == ["good"]

    def test_a_page_that_is_not_there_answers_nothing(self) -> None:
        out, _rec, timings = reader(boxes=[])(None)

        assert out is None
        assert timings["all"] == 0

    def test_a_page_with_no_detections_answers_nothing(self) -> None:
        out, _rec, timings = reader(boxes=None)(page())

        assert out is None
        assert "det" in timings

    def test_the_crops_reach_the_recogniser_in_reading_order(self) -> None:
        instance = reader(
            boxes=[quad(0, 100, 50, 120), quad(0, 0, 50, 20)],
            answers=[("top", 0.9), ("bottom", 0.9)],
        )

        out = instance(page())

        assert [text for _box, (text, _score) in out] == ["top", "bottom"]

    def test_the_page_handed_in_is_not_modified(self) -> None:
        """It is copied before the crops are taken; the caller still wants the
        page for the layout pass."""
        original = page()
        before = original.copy()

        reader(boxes=[quad(0, 0, 50, 20)], answers=[("a", 0.9)])(original)

        assert np.array_equal(original, before)


class _Tensor:
    """The graph's declared input: a name and a shape with a free width."""

    name = "x"
    shape = [-1, 3, 48, -1]


def _recogniser(answers: list[tuple[str, float]], *, batch: int = 16) -> object:
    """A TextRecognizer with its graph and its label decoder supplied."""
    from raven.knowledge.parser.deepdoc._ocr import TextRecognizer

    instance = object.__new__(TextRecognizer)
    instance.rec_image_shape = [3, 48, 320]  # type: ignore[attr-defined]
    instance.rec_batch_num = batch  # type: ignore[attr-defined]
    instance.input_tensor = _Tensor()  # type: ignore[attr-defined]
    instance.batches: list = []  # type: ignore[attr-defined]

    def predictor_run(_names, feed, _options=None):
        instance.batches.append(feed[_Tensor.name])  # type: ignore[attr-defined]
        return [np.zeros((len(feed[_Tensor.name]), 1, 1), dtype=np.float32)]

    instance.predictor = type("P", (), {"run": staticmethod(predictor_run)})()  # type: ignore[attr-defined]
    served = iter(answers)
    instance.postprocess_op = lambda preds: [next(served) for _ in range(len(preds))]  # type: ignore[attr-defined]
    return instance


class TestNormalisingAStrip:
    """Each crop, stretched to the height the graph wants and padded to a
    common width so a batch is one tensor."""

    def test_a_strip_comes_back_at_the_graphs_height(self) -> None:
        out = _recogniser([]).resize_norm_img(page(24, 100), max_wh_ratio=320 / 48)

        assert out.shape[0] == 3, "channels first"
        assert out.shape[1] == 48

    def test_the_pixels_are_centred_on_zero(self) -> None:
        """Scaled to the unit range then shifted to [-1, 1], which is what the
        graph was trained on."""
        out = _recogniser([]).resize_norm_img(np.full((24, 100, 3), 255, np.uint8), max_wh_ratio=320 / 48)

        assert out.max() == pytest.approx(1.0)

    def test_a_short_strip_is_padded_rather_than_stretched(self) -> None:
        """Stretching a short line to the batch width would distort letters
        the recogniser reads by shape."""
        out = _recogniser([]).resize_norm_img(page(48, 48), max_wh_ratio=320 / 48)

        assert out[:, :, -1] == pytest.approx(0.0), "padded, not filled"

    def test_a_channel_count_the_graph_did_not_ask_for_is_refused(self) -> None:
        with pytest.raises(AssertionError):
            _recogniser([]).resize_norm_img(np.zeros((24, 100, 1), np.uint8), max_wh_ratio=320 / 48)


class TestRecognisingABatch:
    def test_every_strip_comes_back_against_its_own_crop(self) -> None:
        """The crops are sorted by shape before they are read, so the answers
        come back in a different order than they went in and have to be put
        back. Getting this wrong puts each line's text on its neighbour."""
        crops = [page(20, 200), page(20, 20), page(20, 100)]
        # Read narrowest first, so the answers are served in that order.
        instance = _recogniser([("narrowest", 0.9), ("middle", 0.9), ("widest", 0.9)])

        out, _elapsed = instance(crops)

        assert [text for text, _score in out] == ["widest", "narrowest", "middle"], "each crop keeps its own"

    def test_a_long_list_is_read_in_batches(self) -> None:
        crops = [page(20, 100) for _ in range(5)]
        instance = _recogniser([("a", 0.9)] * 5, batch=2)

        instance(crops)

        assert [len(b) for b in instance.batches] == [2, 2, 1]  # type: ignore[attr-defined]

    def test_an_empty_list_reads_nothing(self) -> None:
        out, _elapsed = _recogniser([])([])

        assert out == []


def _detector() -> object:
    from raven.knowledge.parser.deepdoc._ocr import TextDetector

    return object.__new__(TextDetector)


class TestOrderingCorners:
    def test_corners_come_back_clockwise_from_the_top_left(self) -> None:
        """The detector's four points arrive in whatever order the contour was
        walked; the crop assumes top-left, top-right, bottom-right, bottom-left."""
        scrambled = np.array([[10, 50], [0, 0], [10, 0], [0, 50]], dtype=np.float32)

        out = _detector().order_points_clockwise(scrambled)

        assert out[0].tolist() == [0, 0]
        assert out[2].tolist() == [10, 50]


class TestClippingToThePage:
    def test_a_corner_past_the_edge_is_pulled_back(self) -> None:
        points = np.array([[-5, -5], [500, 10], [500, 400], [-5, 400]], dtype=np.float32)

        out = _detector().clip_det_res(points, img_height=200, img_width=300)

        assert out[:, 0].min() == 0 and out[:, 0].max() == 299
        assert out[:, 1].min() == 0 and out[:, 1].max() == 199

    def test_a_box_inside_the_page_is_left_alone(self) -> None:
        points = np.array([[10, 10], [50, 10], [50, 30], [10, 30]], dtype=np.float32)

        out = _detector().clip_det_res(points.copy(), img_height=200, img_width=300)

        assert out.tolist() == points.tolist()


class TestFilteringDetections:
    def test_a_sliver_is_dropped(self) -> None:
        """A detection three points wide carries no readable glyph; feeding it
        to the recogniser spends a batch slot on noise."""
        boxes = [np.array([[0, 0], [2, 0], [2, 20], [0, 20]], dtype=np.float32)]

        assert len(_detector().filter_tag_det_res(boxes, (200, 300))) == 0

    def test_a_readable_box_survives(self) -> None:
        boxes = [np.array([[0, 0], [80, 0], [80, 20], [0, 20]], dtype=np.float32)]

        assert len(_detector().filter_tag_det_res(boxes, (200, 300))) == 1

    def test_a_box_given_as_a_list_is_accepted(self) -> None:
        boxes = [[[0, 0], [80, 0], [80, 20], [0, 20]]]

        assert len(_detector().filter_tag_det_res(boxes, (200, 300))) == 1

    def test_the_clip_only_form_keeps_every_box(self) -> None:
        """Used where the caller has already decided what is worth reading."""
        boxes = [np.array([[0, 0], [2, 0], [2, 20], [0, 20]], dtype=np.float32)]

        assert len(_detector().filter_tag_det_res_only_clip(boxes, (200, 300))) == 1
