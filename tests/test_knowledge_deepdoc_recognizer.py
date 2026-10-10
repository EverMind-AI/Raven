"""The geometry every deepdoc pass rests on: sorting, overlap, and nearness.

`Recognizer` is the base of the layout and table models, and all of this is
static: given boxes it answers which sit on a line together, which overlap and
by how much, and which of a set is nearest. The models above it only supply
the boxes.

None of it needs the weights, and that is the point of testing it here -- it
was reachable only from the cases that skip without them, which is every
machine but a developer's. It is also ported code, where a wrong comparison
reorders a page rather than raising: the text still comes out, in the wrong
order, which no exception ever reports.
"""

from __future__ import annotations

import pytest

from raven.knowledge.parser.deepdoc._recognizer import Recognizer


def box(x0: float, top: float, x1: float, bottom: float, **extra: object) -> dict:
    cell = {"x0": x0, "top": top, "x1": x1, "bottom": bottom}
    cell.update(extra)  # type: ignore[arg-type]
    return cell


class TestSortYFirstly:
    """Reading order down the page, then across it."""

    def test_boxes_on_separate_lines_sort_by_line(self) -> None:
        lower = box(0, 100, 10, 110, text="second")
        upper = box(50, 0, 60, 10, text="first")

        out = Recognizer.sort_Y_firstly([lower, upper], threshold=5)

        assert [b["text"] for b in out] == ["first", "second"]

    def test_boxes_within_the_threshold_count_as_one_line_and_sort_across(self) -> None:
        """Two glyphs on the same printed line never share a top to the pixel.
        The threshold is what makes them one line rather than two."""
        right = box(50, 2, 60, 12, text="b")
        left = box(0, 0, 10, 10, text="a")

        out = Recognizer.sort_Y_firstly([right, left], threshold=5)

        assert [b["text"] for b in out] == ["a", "b"]

    def test_a_gap_wider_than_the_threshold_is_two_lines(self) -> None:
        """The same pair, with the threshold too small to join them: now the
        lower-but-leftmost box sorts second."""
        right = box(50, 2, 60, 12, text="b")
        left = box(0, 0, 10, 10, text="a")

        out = Recognizer.sort_Y_firstly([right, left], threshold=1)

        assert [b["text"] for b in out] == ["a", "b"]

    def test_an_empty_list_sorts_to_an_empty_list(self) -> None:
        assert Recognizer.sort_Y_firstly([], threshold=5) == []


class TestSortXFirstly:
    def test_columns_sort_left_to_right(self) -> None:
        right = box(100, 0, 110, 10, text="b")
        left = box(0, 50, 10, 60, text="a")

        out = Recognizer.sort_X_firstly([right, left], threshold=5)

        assert [b["text"] for b in out] == ["a", "b"]

    def test_within_the_threshold_the_higher_box_comes_first(self) -> None:
        lower = box(2, 100, 12, 110, text="b")
        upper = box(0, 0, 10, 10, text="a")

        out = Recognizer.sort_X_firstly([lower, upper], threshold=5)

        assert [b["text"] for b in out] == ["a", "b"]


class TestSortByRegion:
    """The same two orders, but with the model's own row and column regions
    overriding the geometry where it supplied them."""

    def test_a_row_region_outranks_the_coordinates(self) -> None:
        """Two boxes the geometry would separate, which the model says are one
        row. `R` wins, which is what keeps a row together when one cell sits
        lower than the rest of its line."""
        late = box(0, 100, 10, 110, R=0, text="first")
        early = box(0, 0, 10, 10, R=1, text="second")

        out = Recognizer.sort_R_firstly([late, early], thr=5)

        assert [b["text"] for b in out] == ["first", "second"]

    def test_within_one_row_region_the_order_is_left_to_right(self) -> None:
        right = box(100, 0, 110, 10, R=0, text="b")
        left = box(0, 0, 10, 10, R=0, text="a")

        out = Recognizer.sort_R_firstly([right, left], thr=5)

        assert [b["text"] for b in out] == ["a", "b"]

    def test_boxes_without_a_region_keep_the_geometric_order(self) -> None:
        """A page the model found no rows on still has to come out in order."""
        lower = box(0, 100, 10, 110, text="second")
        upper = box(0, 0, 10, 10, text="first")

        out = Recognizer.sort_R_firstly([lower, upper], thr=5)

        assert [b["text"] for b in out] == ["first", "second"]

    def test_a_column_region_outranks_the_coordinates(self) -> None:
        late = box(100, 0, 110, 10, C=0, text="first")
        early = box(0, 0, 10, 10, C=1, text="second")

        out = Recognizer.sort_C_firstly([late, early], thr=5)

        assert [b["text"] for b in out] == ["first", "second"]

    def test_within_one_column_region_the_order_is_top_down(self) -> None:
        lower = box(0, 100, 10, 110, C=0, text="b")
        upper = box(0, 0, 10, 10, C=0, text="a")

        out = Recognizer.sort_C_firstly([lower, upper], thr=5)

        assert [b["text"] for b in out] == ["a", "b"]


class TestOverlappedArea:
    """How much of `a` lies under `b`, as a fraction of `a` unless told not to."""

    def test_boxes_that_miss_horizontally_do_not_overlap(self) -> None:
        assert Recognizer.overlapped_area(box(0, 0, 10, 10), box(20, 0, 30, 10)) == 0

    def test_boxes_that_miss_vertically_do_not_overlap(self) -> None:
        assert Recognizer.overlapped_area(box(0, 0, 10, 10), box(0, 20, 10, 30)) == 0

    def test_a_box_inside_another_is_wholly_overlapped(self) -> None:
        assert Recognizer.overlapped_area(box(2, 2, 8, 8), box(0, 0, 10, 10)) == pytest.approx(1.0)

    def test_a_quarter_overlap_reads_as_a_quarter(self) -> None:
        assert Recognizer.overlapped_area(box(0, 0, 2, 2), box(1, 1, 3, 3)) == pytest.approx(0.25)

    def test_without_the_ratio_it_answers_in_square_points(self) -> None:
        """The absolute form is what `layouts_cleanup` compares between two
        candidate regions, where the fractions are of different denominators."""
        assert Recognizer.overlapped_area(box(0, 0, 2, 2), box(1, 1, 3, 3), ratio=False) == pytest.approx(1.0)

    def test_a_box_of_no_area_overlaps_nothing(self) -> None:
        """A zero-width box divides by zero in the ratio. It arrives from a
        detection that collapsed, and the answer is no overlap rather than a
        raise in the middle of a page."""
        assert Recognizer.overlapped_area(box(5, 0, 5, 10), box(0, 0, 10, 10)) == 0

    def test_the_fraction_is_of_the_first_box(self) -> None:
        """Asymmetric on purpose: a small box inside a large one is wholly
        covered, while the large one is barely covered by the small."""
        small, large = box(0, 0, 2, 2), box(0, 0, 10, 10)

        assert Recognizer.overlapped_area(small, large) == pytest.approx(1.0)
        assert Recognizer.overlapped_area(large, small) == pytest.approx(0.04)


class TestLayoutsCleanup:
    """Two detections of the same thing, reduced to one."""

    def test_the_higher_scoring_of_two_overlapping_regions_wins(self) -> None:
        layouts = [
            box(0, 0, 10, 10, type="table", score=0.6),
            box(0, 0, 10, 10, type="table", score=0.9),
        ]

        out = Recognizer.layouts_cleanup([], layouts)

        assert len(out) == 1
        assert out[0]["score"] == 0.9

    def test_regions_of_different_kinds_both_survive(self) -> None:
        """A caption drawn over a table is not a duplicate of it."""
        layouts = [
            box(0, 0, 10, 10, type="table", score=0.9),
            box(0, 0, 10, 10, type="figure", score=0.8),
        ]

        assert len(Recognizer.layouts_cleanup([], layouts)) == 2

    def test_regions_that_barely_touch_both_survive(self) -> None:
        """Below the threshold they are two things that happen to abut, not one
        thing found twice."""
        layouts = [
            box(0, 0, 10, 10, type="table", score=0.9),
            box(9, 9, 20, 20, type="table", score=0.8),
        ]

        assert len(Recognizer.layouts_cleanup([], layouts)) == 2

    def test_without_scores_the_region_covering_more_text_wins(self) -> None:
        """An unscored duplicate is settled by which one the page's own text
        boxes actually sit inside."""
        layouts = [
            box(0, 0, 10, 10, type="table"),
            box(0, 0, 100, 100, type="table"),
        ]
        text = [box(20, 20, 80, 80)]

        out = Recognizer.layouts_cleanup(text, layouts)

        assert len(out) == 1
        assert out[0]["x1"] == 100, "the region the text is in"

    def test_a_single_region_is_left_alone(self) -> None:
        layouts = [box(0, 0, 10, 10, type="table", score=0.9)]

        assert Recognizer.layouts_cleanup([], layouts) == layouts

    def test_an_empty_page_cleans_up_to_nothing(self) -> None:
        assert Recognizer.layouts_cleanup([], []) == []


class TestFindOverlapped:
    """Which of a y-sorted list a box sits in, found by halving rather than
    walking -- the list is every text box on the page."""

    @staticmethod
    def _sorted_boxes() -> list[dict]:
        return [box(0, n * 20, 100, n * 20 + 10) for n in range(10)]

    def test_it_finds_the_box_the_target_sits_in(self) -> None:
        boxes = self._sorted_boxes()

        assert Recognizer.find_overlapped(box(0, 60, 100, 70), boxes) == 3

    def test_the_naive_walk_agrees_with_the_halving(self) -> None:
        """Same answer by the other route, which is what says the bisection
        above did not simply land somewhere plausible."""
        boxes = self._sorted_boxes()
        target = box(0, 60, 100, 70)

        assert Recognizer.find_overlapped(target, boxes) == Recognizer.find_overlapped(target, boxes, naive=True)

    def test_a_box_above_everything_overlaps_nothing(self) -> None:
        assert Recognizer.find_overlapped(box(0, -100, 100, -90), self._sorted_boxes()) is None

    def test_a_box_below_everything_overlaps_nothing(self) -> None:
        assert Recognizer.find_overlapped(box(0, 1000, 100, 1010), self._sorted_boxes()) is None

    def test_an_empty_list_has_nothing_to_find(self) -> None:
        assert Recognizer.find_overlapped(box(0, 0, 10, 10), []) is None

    def test_the_most_overlapped_wins_when_a_box_straddles_two(self) -> None:
        boxes = [box(0, 0, 100, 10), box(0, 10, 100, 60)]

        assert Recognizer.find_overlapped(box(0, 8, 100, 60), boxes) == 1


class TestFindHorizontallyTightestFit:
    """Which column a cell belongs to, among the columns of its own table."""

    def test_the_nearest_by_edge_wins(self) -> None:
        boxes = [box(0, 0, 10, 10, layoutno="table-0"), box(90, 0, 100, 10, layoutno="table-0")]

        assert Recognizer.find_horizontally_tightest_fit(box(88, 2, 98, 8, layoutno="table-0"), boxes) == 1

    def test_a_candidate_from_another_table_is_refused(self) -> None:
        boxes = [box(0, 0, 10, 10, layoutno="table-1")]

        assert Recognizer.find_horizontally_tightest_fit(box(0, 0, 10, 10, layoutno="table-0"), boxes) is None

    def test_a_candidate_sharing_no_line_is_refused(self) -> None:
        """`layoutno` resets per page, so the same string names a different
        table further down. Vertical extent is what tells them apart -- without
        this a column from another page wins on x alone.
        """
        boxes = [box(0, 500, 10, 510, layoutno="table-0")]

        assert Recognizer.find_horizontally_tightest_fit(box(0, 0, 10, 10, layoutno="table-0"), boxes) is None

    def test_an_empty_candidate_list_fits_nothing(self) -> None:
        assert Recognizer.find_horizontally_tightest_fit(box(0, 0, 10, 10), []) is None


class TestFindOverlappedWithThreshold:
    def test_an_overlap_under_the_threshold_is_no_match(self) -> None:
        boxes = [box(9, 9, 20, 20)]

        assert Recognizer.find_overlapped_with_threshold(box(0, 0, 10, 10), boxes, thr=0.5) is None

    def test_an_overlap_over_the_threshold_matches(self) -> None:
        boxes = [box(0, 0, 10, 10)]

        assert Recognizer.find_overlapped_with_threshold(box(1, 1, 9, 9), boxes, thr=0.5) == 0

    def test_the_best_of_several_wins(self) -> None:
        boxes = [box(0, 0, 20, 20), box(0, 0, 11, 11)]

        assert Recognizer.find_overlapped_with_threshold(box(0, 0, 10, 10), boxes, thr=0.1) == 1

    def test_an_empty_candidate_list_matches_nothing(self) -> None:
        assert Recognizer.find_overlapped_with_threshold(box(0, 0, 10, 10), []) is None


LABELS = ["Table", "Figure", "Text"]


def _stub(*, input_names: list[str], input_shape: tuple[int, int] = (64, 64)) -> Recognizer:
    """A recognizer with its graph's shape but no graph.

    `__init__` opens an onnx session, and none of the pre/post-processing below
    reaches it: what those read is the input names the graph declared and the
    label list the caller passed.
    """
    recogniser = object.__new__(Recognizer)
    recogniser.input_names = input_names  # type: ignore[attr-defined]
    recogniser.input_shape = input_shape  # type: ignore[attr-defined]
    recogniser.label_list = LABELS  # type: ignore[attr-defined]
    return recogniser


class TestPreprocess:
    """A rendered page, turned into the tensor the graph declared."""

    def test_the_direct_path_scales_to_the_graphs_input_shape(self) -> None:
        import numpy as np

        page = np.zeros((200, 300, 3), dtype=np.uint8)

        inputs = _stub(input_names=["images"], input_shape=(64, 96)).preprocess([page])

        assert len(inputs) == 1
        assert inputs[0]["images"].shape == (1, 3, 64, 96), "batch, channels, height, width"

    def test_pixels_arrive_scaled_to_the_unit_range(self) -> None:
        import numpy as np

        page = np.full((10, 10, 3), 255, dtype=np.uint8)

        inputs = _stub(input_names=["images"], input_shape=(8, 8)).preprocess([page])

        assert inputs[0]["images"].max() == pytest.approx(1.0)

    def test_the_scale_factor_records_what_the_resize_cost(self) -> None:
        """The boxes come back in the resized frame and are multiplied by this
        to land on the page again."""
        import numpy as np

        page = np.zeros((200, 300, 3), dtype=np.uint8)

        inputs = _stub(input_names=["images"], input_shape=(100, 150)).preprocess([page])

        assert inputs[0]["scale_factor"] == [pytest.approx(2.0), pytest.approx(2.0)]

    def test_a_page_is_not_swapped_to_bgr(self) -> None:
        """Upstream reads pages through cv2, whose arrays are BGR, and swaps
        here. This tree renders through PyMuPDF, whose arrays are RGB, so the
        same swap would feed the model blue text on a yellow ground -- which it
        answers, a little worse, saying nothing."""
        import numpy as np

        page = np.zeros((8, 8, 3), dtype=np.uint8)
        page[:, :, 0] = 255  # red in RGB

        inputs = _stub(input_names=["images"], input_shape=(8, 8)).preprocess([page])

        channels = inputs[0]["images"][0]
        assert channels[0].max() == pytest.approx(1.0), "the red channel stays first"
        assert channels[2].max() == pytest.approx(0.0)

    def test_every_page_in_the_batch_comes_back(self) -> None:
        import numpy as np

        pages = [np.zeros((20, 20, 3), dtype=np.uint8) for _ in range(3)]

        assert len(_stub(input_names=["images"], input_shape=(8, 8)).preprocess(pages)) == 3


class TestPostprocessDocLayoutYolo:
    """The shape the published weights actually answer in: one row per query,
    already in corner form. Upstream has only the other two, so its YOLO branch
    transposes this one into nonsense and indexes the labels with a query
    number -- detected here by shape, because the graph states neither."""

    @staticmethod
    def _answer(rows: list[list[float]]):
        import numpy as np

        return np.array([rows], dtype=np.float32)

    def test_a_detection_comes_back_scaled_onto_the_page(self) -> None:
        boxes = self._answer([[0.0, 0.0, 10.0, 20.0, 0.9, 0.0]])

        found = _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [2.0, 3.0]}, thr=0.5)

        assert len(found) == 1
        assert found[0]["type"] == "table"
        assert found[0]["bbox"] == [0.0, 0.0, 20.0, 60.0]

    def test_a_detection_under_the_threshold_is_dropped(self) -> None:
        boxes = self._answer([[0.0, 0.0, 10.0, 20.0, 0.1, 0.0]])

        assert _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5) == []

    def test_a_class_the_label_list_does_not_have_is_dropped(self) -> None:
        """The graph is free to answer a class this build has no name for;
        indexing the list with it would raise mid-page."""
        boxes = self._answer([[0.0, 0.0, 10.0, 20.0, 0.9, 99.0]])

        assert _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5) == []

    def test_the_label_is_lowercased(self) -> None:
        boxes = self._answer([[0.0, 0.0, 1.0, 1.0, 0.9, 1.0]])

        found = _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5)

        assert found[0]["type"] == "figure"


class TestPostprocessPaddleDetection:
    """The graph that takes a `scale_factor` input: one row per detection as
    [class, score, x0, y0, x1, y1]."""

    @staticmethod
    def _answer(rows: list[list[float]]):
        import numpy as np

        return np.array(rows, dtype=np.float32)

    def test_a_detection_comes_back_named(self) -> None:
        boxes = self._answer([[0.0, 0.9, 1.0, 2.0, 3.0, 4.0]])

        found = _stub(input_names=["image", "scale_factor"]).postprocess(boxes, {}, thr=0.5)

        assert found == [{"type": "table", "bbox": [1.0, 2.0, 3.0, 4.0], "score": pytest.approx(0.9)}]

    def test_a_detection_under_the_threshold_is_dropped(self) -> None:
        boxes = self._answer([[0.0, 0.2, 1.0, 2.0, 3.0, 4.0]])

        assert _stub(input_names=["image", "scale_factor"]).postprocess(boxes, {}, thr=0.5) == []

    def test_a_class_past_the_label_list_is_dropped(self) -> None:
        boxes = self._answer([[9.0, 0.9, 1.0, 2.0, 3.0, 4.0]])

        assert _stub(input_names=["image", "scale_factor"]).postprocess(boxes, {}, thr=0.5) == []


#: A second query, scored below every threshold these cases use, so it is
#: filtered out and changes no assertion. Present because `np.squeeze` flattens
#: a one-query block into a single row, which this branch then cannot index --
#: a shape the graph never answers, since it emits one query per anchor.
_IGNORED = [1000.0, 1000.0, 1.0, 1.0, 0.0, 0.0, 0.0]


class TestPostprocessTransposedYolo:
    """The third shape: a transposed block of boxes and per-class scores, in
    centre-and-size form, with overlapping detections still to be thinned."""

    @staticmethod
    def _answer(queries: list[list[float]]):
        """``queries`` are rows of [cx, cy, w, h, *class_scores]; the graph
        answers them transposed, with a leading batch axis."""
        import numpy as np

        return np.array([np.array(queries, dtype=np.float32).T])

    def test_a_detection_comes_back_in_corner_form(self) -> None:
        boxes = self._answer([[10.0, 20.0, 4.0, 6.0, 0.9, 0.0, 0.0], _IGNORED])

        found = _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5)

        assert len(found) == 1
        assert found[0]["type"] == "table"
        assert found[0]["bbox"] == [8.0, 17.0, 12.0, 23.0], "centre 10,20 of size 4x6"

    def test_everything_under_the_threshold_leaves_nothing(self) -> None:
        boxes = self._answer([[10.0, 20.0, 4.0, 6.0, 0.1, 0.0, 0.0], _IGNORED])

        assert _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5) == []

    def test_the_class_with_the_highest_score_names_the_box(self) -> None:
        boxes = self._answer([[10.0, 20.0, 4.0, 6.0, 0.1, 0.2, 0.95], _IGNORED])

        found = _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5)

        assert found[0]["type"] == "text"

    def test_two_detections_of_one_thing_are_thinned_to_the_better(self) -> None:
        """The graph answers a query per anchor, so a single table arrives many
        times over. Without the overlap filter the page carries each copy."""
        boxes = self._answer(
            [
                [10.0, 20.0, 4.0, 6.0, 0.9, 0.0, 0.0],
                [10.1, 20.1, 4.0, 6.0, 0.6, 0.0, 0.0],
            ]
        )

        found = _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5)

        assert len(found) == 1
        assert found[0]["score"] == pytest.approx(0.9), "the better of the two"

    def test_two_different_things_that_overlap_both_survive(self) -> None:
        """The filter runs per class: a caption drawn over a table must not
        thin the table away."""
        boxes = self._answer(
            [
                [10.0, 20.0, 4.0, 6.0, 0.9, 0.0, 0.0],
                [10.0, 20.0, 4.0, 6.0, 0.0, 0.9, 0.0],
            ]
        )

        found = _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [1.0, 1.0]}, thr=0.5)

        assert {b["type"] for b in found} == {"table", "figure"}

    def test_the_scale_factor_puts_the_boxes_back_on_the_page(self) -> None:
        boxes = self._answer([[10.0, 20.0, 4.0, 6.0, 0.9, 0.0, 0.0], _IGNORED])

        found = _stub(input_names=["images"]).postprocess(boxes, {"scale_factor": [2.0, 3.0]}, thr=0.5)

        assert found[0]["bbox"] == [16.0, 51.0, 24.0, 69.0]


class TestCreateInputs:
    """The PaddleDetection shape, where a batch is one padded tensor."""

    @staticmethod
    def _info(height: float, width: float) -> dict:
        import numpy as np

        return {
            "im_shape": np.array([height, width], dtype="float32"),
            "scale_factor": np.array([1.0, 1.0], dtype="float32"),
        }

    def test_a_single_page_is_wrapped_without_padding(self) -> None:
        import numpy as np

        page = np.zeros((3, 10, 20), dtype=np.float32)

        inputs = _stub(input_names=["image", "scale_factor"]).create_inputs([page], [self._info(10, 20)])

        assert inputs["image"].shape == (1, 3, 10, 20)
        assert set(inputs) == {"image", "im_shape", "scale_factor"}

    def test_a_batch_is_padded_up_to_its_largest_page(self) -> None:
        """One tensor per batch, so the shorter pages are padded rather than
        sent separately."""
        import numpy as np

        pages = [np.zeros((3, 10, 20), dtype=np.float32), np.zeros((3, 30, 40), dtype=np.float32)]
        info = [self._info(10, 20), self._info(30, 40)]

        inputs = _stub(input_names=["image", "scale_factor"]).create_inputs(pages, info)

        assert inputs["image"].shape == (2, 3, 30, 40)

    def test_the_padding_goes_after_the_page(self) -> None:
        """So the boxes keep the coordinates they were detected at."""
        import numpy as np

        small = np.ones((3, 10, 20), dtype=np.float32)
        pages = [small, np.zeros((3, 30, 40), dtype=np.float32)]
        info = [self._info(10, 20), self._info(30, 40)]

        inputs = _stub(input_names=["image", "scale_factor"]).create_inputs(pages, info)

        assert inputs["image"][0, :, :10, :20].min() == pytest.approx(1.0)
        assert inputs["image"][0, :, 10:, :].max() == pytest.approx(0.0)


class TestRunningABatch:
    """``__call__``: pages in, one list of regions per page out."""

    @staticmethod
    def _running(answer, *, input_shape=(64, 64)):
        recogniser = _stub(input_names=["images"], input_shape=input_shape)
        recogniser.batches = []  # type: ignore[attr-defined]

        def run(_names, feed, _options=None):
            recogniser.batches.append(feed)  # type: ignore[attr-defined]
            return [answer]

        recogniser.ort_sess = type("S", (), {"run": staticmethod(run)})()  # type: ignore[attr-defined]
        return recogniser

    @staticmethod
    def _answer():
        import numpy as np

        return np.array([[[0.0, 0.0, 10.0, 20.0, 0.9, 0.0]]], dtype=np.float32)

    def test_each_page_answers_its_own_regions(self) -> None:
        import numpy as np

        recogniser = self._running(self._answer())

        out = recogniser([np.zeros((50, 50, 3), dtype=np.uint8) for _ in range(2)])

        assert len(out) == 2
        assert out[0][0]["type"] == "table"

    def test_a_long_document_is_run_in_batches(self) -> None:
        import numpy as np

        recogniser = self._running(self._answer())

        recogniser([np.zeros((50, 50, 3), dtype=np.uint8) for _ in range(5)], batch_size=2)

        assert len(recogniser.batches) == 5, "one run per page, three batches of pages"

    def test_a_page_given_as_an_image_object_is_converted(self) -> None:
        """The caller hands pages straight from the renderer, which are not
        always arrays."""
        from PIL import Image

        recogniser = self._running(self._answer())

        out = recogniser([Image.new("RGB", (50, 50))])

        assert len(out) == 1

    def test_no_pages_answer_nothing(self) -> None:
        assert self._running(self._answer())([]) == []
