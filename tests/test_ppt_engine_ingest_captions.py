"""A figure arrives with the label its caption gave it, or nothing can check it."""

from __future__ import annotations

import pytest

from raven_ppt.contracts.sources import AssetKind
from raven_ppt.services.ingest.captions import (
    Caption,
    canonical_label,
    caption_blocks,
    caption_for,
    caption_match,
    nearest_captions,
)

pytest.importorskip("fitz", reason="render extra (pymupdf) not installed")


def _block(x0: float, y0: float, x1: float, y1: float, text: str):
    return (x0, y0, x1, y1, text, 0, 0)


def _caption(label: str, text: str, box: tuple[float, float, float, float]) -> Caption:
    import fitz

    return Caption(label, text, fitz.Rect(*box))


def test_figure_and_table_labels_are_both_read() -> None:
    """Only tables were read before, so every figure arrived unlabelled."""
    blocks = [
        _block(0, 300, 200, 320, "Figure 4. Qualitative results from a single model."),
        _block(0, 100, 200, 120, "Table 1. Results for video instance segmentation."),
        _block(0, 500, 200, 520, "Fig. 5 One model performing two tasks in one pass."),
    ]

    assert [c.label for c in caption_blocks(blocks, AssetKind.FIGURE)] == ["Figure 4", "Figure 5"]
    assert [c.label for c in caption_blocks(blocks, AssetKind.TABLE)] == ["Table 1"]


def test_a_label_is_said_one_way() -> None:
    assert canonical_label("fig. 4", AssetKind.FIGURE) == "Figure 4"
    assert canonical_label("Figure  4", AssetKind.FIGURE) == "Figure 4"
    assert canonical_label("图 3", AssetKind.FIGURE) == "Figure 3"
    assert canonical_label("table  II", AssetKind.TABLE) == "Table II"
    assert canonical_label("TABLE I", AssetKind.TABLE) == "Table I"
    assert canonical_label("Figure A1", AssetKind.FIGURE) == "Figure A1"
    assert canonical_label("Fig. 2.1:", AssetKind.FIGURE) == "Figure 2.1"


def test_prose_about_a_figure_is_not_its_caption() -> None:
    """'Figure 3 shows that ...' is a sentence in the body."""
    assert caption_match("Figure 3 shows that the neck alternates attention.", AssetKind.FIGURE) is None
    assert caption_match("Table 4 shows the main result", AssetKind.TABLE) is None
    assert caption_match("Figure 3. Temporal neck layer.", AssetKind.FIGURE) is not None
    assert caption_match("表 1 实验结果", AssetKind.TABLE) is not None


def test_a_figures_caption_is_the_one_under_it() -> None:
    """Papers caption a figure below and a table above; both sides are accepted,
    and the convention decides which wins when a page offers both."""
    import fitz

    figure = fitz.Rect(0, 200, 300, 400)
    above = _caption("Figure 1", "Figure 1. The one above.", (0, 170, 300, 190))
    below = _caption("Figure 2", "Figure 2. The one below.", (0, 410, 300, 430))

    assert caption_for(figure, [above, below], AssetKind.FIGURE).label == "Figure 2"
    assert caption_for(figure, [above, below], AssetKind.TABLE).label == "Figure 1"
    # The other side, when it is all there is.
    assert caption_for(figure, [above], AssetKind.FIGURE).label == "Figure 1"


def test_a_caption_needs_to_share_the_figures_columns() -> None:
    """In a two-column paper the caption beside a figure belongs to the other one."""
    import fitz

    figure = fitz.Rect(0, 200, 200, 400)
    other_column = _caption("Figure 9", "Figure 9. Elsewhere.", (400, 410, 600, 430))

    assert caption_for(figure, [other_column], AssetKind.FIGURE) is None


def test_the_nearest_caption_wins_over_one_further_down_the_page() -> None:
    import fitz

    figure = fitz.Rect(0, 200, 300, 400)
    near = _caption("Figure 2", "Figure 2. Right below it.", (0, 404, 300, 424))
    far = _caption("Figure 3", "Figure 3. Much further down.", (0, 470, 300, 488))

    assert caption_for(figure, [near, far], AssetKind.FIGURE).label == "Figure 2"


def test_a_page_with_no_caption_leaves_the_figure_unlabelled() -> None:
    import fitz

    assert caption_for(fitz.Rect(0, 200, 300, 400), [], AssetKind.FIGURE) is None


def test_a_caption_printed_inside_the_region_is_part_of_the_picture() -> None:
    """A legend drawn inside a plot is not the plot's caption."""
    import fitz

    figure = fitz.Rect(0, 200, 300, 400)
    inside = _caption("Figure 7", "Figure 7. Inside the frame.", (20, 260, 280, 280))
    outside = _caption("Figure 8", "Figure 8. Under the frame.", (0, 410, 300, 430))

    assert caption_for(figure, [inside, outside], AssetKind.FIGURE).label == "Figure 8"


def test_two_captions_tied_over_one_region_are_both_reported() -> None:
    """Two tables printed side by side are one region; their captions are two."""
    import fitz

    region = fitz.Rect(50, 120, 562, 280)
    left = _caption("Table 1", "Table 1 Left results", (50, 90, 250, 110))
    right = _caption("Table 2", "Table 2 Right results", (325, 90, 562, 110))

    assert [c.label for c in nearest_captions(region, [left, right])] == ["Table 1", "Table 2"]


def test_a_caption_too_far_from_the_region_is_not_reported() -> None:
    import fitz

    region = fitz.Rect(50, 400, 562, 560)
    distant = _caption("Table 9", "Table 9 On the other half of the page", (50, 90, 250, 110))

    assert nearest_captions(region, [distant]) == []
    assert caption_for(region, [distant], AssetKind.TABLE) is None
