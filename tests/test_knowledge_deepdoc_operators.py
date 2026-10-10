"""The transforms a rendered page passes through before the model sees it.

Resize, normalise, reorder the axes, pad to a stride. Pure arithmetic over
arrays, so none of it needs the weights -- and all of it is the kind of ported
code where a mistake is silent: the wrong mean still produces a tensor of the
right shape, and the model answers it a little worse.

The shapes are deliberately small and the numbers round, so each case says what
the transform does rather than merely that it ran.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from raven.knowledge.parser.deepdoc._operators import (
    DetResizeForTest,
    KeepKeys,
    LinearResize,
    NormalizeImage,
    PadStride,
    Permute,
    StandardizeImage,
    ToCHWImage,
    _as_array,
    nms,
    preprocess,
)


def page(height: int = 20, width: int = 30, value: int = 255) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


class TestAsArray:
    def test_a_pil_image_becomes_an_array(self) -> None:
        out = _as_array(Image.new("RGB", (4, 3)))

        assert isinstance(out, np.ndarray)
        assert out.shape == (3, 4, 3), "PIL is width-first, arrays are height-first"

    def test_an_array_is_handed_back_unchanged(self) -> None:
        given = page()

        assert _as_array(given) is given


class TestStandardizeImage:
    def test_it_scales_to_the_unit_range_then_subtracts_the_mean(self) -> None:
        out, _ = StandardizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])(page(value=255).astype(np.float32), {})

        assert out == pytest.approx(np.ones((20, 30, 3)))

    def test_without_scaling_the_raw_values_are_standardised(self) -> None:
        out, _ = StandardizeImage(mean=[0.0] * 3, std=[1.0] * 3, is_scale=False)(page(value=7).astype(np.float32), {})

        assert out == pytest.approx(np.full((20, 30, 3), 7.0))

    def test_norm_type_none_only_scales(self) -> None:
        """The mean and std are still constructed; what changes is whether they
        are applied. A page that skipped the scaling too would arrive at 255."""
        out, _ = StandardizeImage(mean=[9.0] * 3, std=[9.0] * 3, norm_type="none")(
            page(value=255).astype(np.float32), {}
        )

        assert out == pytest.approx(np.ones((20, 30, 3)))

    def test_the_info_dictionary_rides_through_untouched(self) -> None:
        info = {"scale_factor": np.array([1.0, 1.0])}

        _, out = StandardizeImage(mean=[0.0] * 3, std=[1.0] * 3)(page().astype(np.float32), info)

        assert out is info


class TestNormalizeImage:
    """Unlike `StandardizeImage` beside it, this one defaults to channel-first:
    the recognition graph takes its input that way, so the statistics are
    shaped (3, 1, 1) and an HWC page will not broadcast against them."""

    @staticmethod
    def _chw(value: int, height: int = 4, width: int = 6) -> np.ndarray:
        return np.full((3, height, width), value, dtype=np.uint8)

    def test_it_scales_and_standardises(self) -> None:
        op = NormalizeImage(scale=1.0 / 255.0, mean=[0.5] * 3, std=[0.5] * 3)

        out = op({"image": self._chw(255)})

        assert out["image"] == pytest.approx(np.ones((3, 4, 6)))

    def test_it_defaults_to_the_imagenet_statistics(self) -> None:
        """The published weights were trained against these; a build that
        quietly used zeros would read every page slightly washed out."""
        out = NormalizeImage()({"image": self._chw(0)})

        assert out["image"].min() < 0, "subtracting a positive mean from black"

    def test_the_last_axis_holds_the_channels_when_asked(self) -> None:
        op = NormalizeImage(order="hwc", scale=1.0 / 255.0, mean=[0.5] * 3, std=[0.5] * 3)

        out = op({"image": page(value=255)})

        assert out["image"] == pytest.approx(np.ones((20, 30, 3)))

    def test_a_scale_written_as_a_fraction_is_read_as_one(self) -> None:
        """The operator tables are JSON, where this arrives as the string
        "1/255" rather than a number."""
        op = NormalizeImage(scale="1/255", mean=[0.0] * 3, std=[1.0] * 3)

        out = op({"image": self._chw(255)})

        assert out["image"] == pytest.approx(np.ones((3, 4, 6)))

    def test_a_scale_written_as_a_decimal_string_is_read_as_one(self) -> None:
        op = NormalizeImage(scale="0.5", mean=[0.0] * 3, std=[1.0] * 3)

        out = op({"image": self._chw(2)})

        assert out["image"] == pytest.approx(np.ones((3, 4, 6)))


class TestToCHWImage:
    def test_the_channel_axis_moves_to_the_front(self) -> None:
        out = ToCHWImage()({"image": page(height=4, width=6)})

        assert out["image"].shape == (3, 4, 6)

    def test_the_pixels_are_the_same_ones(self) -> None:
        given = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)

        out = ToCHWImage()({"image": given.copy()})

        assert out["image"][0, 1, 2] == given[1, 2, 0]


class TestKeepKeys:
    def test_only_the_named_keys_come_back_and_in_order(self) -> None:
        out = KeepKeys(keep_keys=["b", "a"])({"a": 1, "b": 2, "c": 3})

        assert out == [2, 1]

    def test_an_empty_selection_answers_with_nothing(self) -> None:
        assert KeepKeys(keep_keys=[])({"a": 1}) == []


class TestLinearResize:
    def test_a_single_int_target_is_taken_as_a_square(self) -> None:
        assert LinearResize(64).target_size == [64, 64]

    def test_without_keeping_the_ratio_the_page_lands_on_the_target(self) -> None:
        out, info = LinearResize([10, 20], keep_ratio=False)(page(height=40, width=40), {})

        assert out.shape[:2] == (10, 20)
        assert info["im_shape"] == pytest.approx(np.array([10.0, 20.0]))

    def test_keeping_the_ratio_scales_both_axes_alike(self) -> None:
        out, info = LinearResize([10, 10], keep_ratio=True)(page(height=40, width=20), {})

        assert info["scale_factor"][0] == pytest.approx(info["scale_factor"][1])
        assert max(out.shape[:2]) <= 10

    def test_the_scale_factor_records_what_the_resize_did(self) -> None:
        """The boxes come back in the resized frame; this is what puts them
        back on the page."""
        _, info = LinearResize([10, 20], keep_ratio=False)(page(height=40, width=40), {})

        assert info["scale_factor"] == pytest.approx(np.array([0.25, 0.5]))

    def test_a_target_of_zero_is_refused(self) -> None:
        with pytest.raises(AssertionError):
            LinearResize([0, 10], keep_ratio=False)(page(), {})


class TestPermute:
    def test_the_channel_axis_moves_to_the_front(self) -> None:
        out, _ = Permute()(page(height=4, width=6), {})

        assert out.shape == (3, 4, 6)


class TestPadStride:
    def test_a_page_is_padded_up_to_a_multiple_of_the_stride(self) -> None:
        """The graph's convolutions need the input a multiple of 32; the pad is
        on the right and bottom so the boxes keep their coordinates."""
        given = np.ones((3, 10, 20), dtype=np.float32)

        out, _ = PadStride(stride=32)(given, {})

        assert out.shape == (3, 32, 32)

    def test_the_original_pixels_keep_their_place(self) -> None:
        given = np.ones((3, 10, 20), dtype=np.float32)

        out, _ = PadStride(stride=32)(given, {})

        assert out[:, :10, :20] == pytest.approx(1.0)
        assert out[:, 10:, :] == pytest.approx(0.0), "padded with zeros, after the image"

    def test_a_stride_of_zero_leaves_the_page_alone(self) -> None:
        given = np.ones((3, 10, 20), dtype=np.float32)

        out, _ = PadStride(stride=0)(given, {})

        assert out.shape == given.shape

    def test_a_page_already_on_the_stride_is_not_grown(self) -> None:
        given = np.ones((3, 32, 64), dtype=np.float32)

        out, _ = PadStride(stride=32)(given, {})

        assert out.shape == (3, 32, 64)


class TestDetResizeForTest:
    def test_a_fixed_target_shape_is_honoured(self) -> None:
        out = DetResizeForTest(image_shape=[32, 64])({"image": page(height=10, width=20)})

        assert out["image"].shape[:2] == (32, 64)

    def test_the_ratio_comes_back_for_putting_boxes_home(self) -> None:
        out = DetResizeForTest(image_shape=[32, 64])({"image": page(height=10, width=20)})

        assert "shape" in out
        assert len(out["shape"]) == 4, "height, width, and the two ratios"

    def test_a_limit_side_resize_keeps_the_page_within_the_limit(self) -> None:
        out = DetResizeForTest(limit_side_len=64, limit_type="max")({"image": page(height=200, width=100)})

        assert max(out["image"].shape[:2]) <= 64 + 32, "within the limit, rounded up to the stride"

    def test_a_resize_long_keeps_the_longest_side_near_its_target(self) -> None:
        out = DetResizeForTest(resize_long=96)({"image": page(height=200, width=100)})

        assert max(out["image"].shape[:2]) == pytest.approx(96, abs=32)


class TestPreprocess:
    def test_the_operators_run_in_order_and_the_info_accumulates(self) -> None:
        ops = [
            LinearResize([16, 16], keep_ratio=False),
            StandardizeImage(mean=[0.0] * 3, std=[1.0] * 3),
            Permute(),
            PadStride(stride=32),
        ]

        out, info = preprocess(page(height=40, width=40), ops)

        assert out.shape == (3, 32, 32)
        assert info["scale_factor"] == pytest.approx(np.array([0.4, 0.4]))

    def test_the_original_shape_is_recorded_before_anything_runs(self) -> None:
        _, info = preprocess(page(height=40, width=60), [])

        assert info["im_shape"] == pytest.approx(np.array([40.0, 60.0]))


class TestNms:
    def test_two_detections_of_one_thing_keep_the_better(self) -> None:
        boxes = np.array([[0.0, 0.0, 10.0, 10.0], [0.5, 0.5, 10.5, 10.5]])
        scores = np.array([0.9, 0.6])

        assert nms(boxes, scores, iou_thresh=0.3) == [0]

    def test_detections_that_do_not_overlap_both_survive(self) -> None:
        boxes = np.array([[0.0, 0.0, 10.0, 10.0], [100.0, 100.0, 110.0, 110.0]])
        scores = np.array([0.9, 0.6])

        assert sorted(nms(boxes, scores, iou_thresh=0.3)) == [0, 1]

    def test_the_order_is_by_score_rather_than_by_position(self) -> None:
        boxes = np.array([[0.0, 0.0, 10.0, 10.0], [100.0, 100.0, 110.0, 110.0]])
        scores = np.array([0.1, 0.9])

        assert nms(boxes, scores, iou_thresh=0.3)[0] == 1

    def test_a_single_detection_is_kept(self) -> None:
        assert nms(np.array([[0.0, 0.0, 10.0, 10.0]]), np.array([0.9]), iou_thresh=0.3) == [0]
