"""Grouping the pieces a page laid a figure in, before any piece is written out."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven_ppt.services.ingest.figures import cut_page_blocks, figure_clusters, strip_stacks
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401

pytest.importorskip("PIL", reason="render extra (pillow) not installed")


def test_strip_stacks_reassembles_a_sliced_screenshot() -> None:
    """Print-to-PDF exports slice one screenshot into same-width bands with
    touching edges; alone each is a 6:1 strip no slot can use."""
    bands = [
        {"xref": 4, "bbox": (0.0, 71.5, 841.8, 208.4), "px_w": 3072},
        {"xref": 5, "bbox": (0.0, 208.4, 841.8, 345.4), "px_w": 3072},
        {"xref": 6, "bbox": (0.0, 345.4, 841.8, 482.3), "px_w": 3072},
        {"xref": 9, "bbox": (60.0, 520.0, 400.0, 700.0), "px_w": 900},  # a real standalone figure
    ]

    stacks = strip_stacks(bands)

    assert len(stacks) == 1
    assert [member["xref"] for member in stacks[0]] == [4, 5, 6]


def test_bands_with_a_gap_between_them_are_two_figures() -> None:
    apart = [
        {"xref": 1, "bbox": (0.0, 0.0, 500.0, 100.0), "px_w": 1000},
        {"xref": 2, "bbox": (0.0, 300.0, 500.0, 400.0), "px_w": 1000},
    ]
    assert strip_stacks(apart) == []


def test_bands_of_different_decoded_widths_are_not_one_image() -> None:
    """Same page position, different source images."""
    mismatched = [
        {"xref": 1, "bbox": (0.0, 0.0, 500.0, 100.0), "px_w": 1000},
        {"xref": 2, "bbox": (0.0, 100.0, 500.0, 200.0), "px_w": 640},
    ]
    assert strip_stacks(mismatched) == []


def test_a_figure_laid_in_as_a_grid_of_cells_is_one_cluster() -> None:
    """This paper's Figure 4 is twelve images in four rows of three, with the
    second row printed wider and overlapping its neighbours. Bucketing by shared
    edges answered a tidier question than the page was asking, and a reader got
    one frame out of twelve."""
    cells = []
    xref = 1
    for row in range(4):
        width = 150 if row == 1 else 100
        for column in range(3):
            left = 60 + column * (width + 4)
            top = 100 + row * 84
            cells.append({"xref": xref, "bbox": (left, top, left + width, top + 80)})
            xref += 1

    clusters = figure_clusters(cells)

    assert len(clusters) == 1
    assert len(clusters[0]) == 12


def test_two_figures_separated_by_a_caption_stay_two_clusters() -> None:
    """A figure's own frames are a hairline apart; two figures are a caption and
    a paragraph apart."""
    placements = [
        {"xref": 1, "bbox": (60.0, 100.0, 300.0, 260.0)},
        {"xref": 2, "bbox": (60.0, 400.0, 300.0, 560.0)},
    ]

    clusters = figure_clusters(placements)

    assert sorted(len(cluster) for cluster in clusters) == [1, 1]


def test_a_page_screenshots_blocks_are_cut_and_carry_no_page_region(tmp_path: Path, product_page) -> None:
    """Placed whole a page screenshot is unreadable, and asking a model to
    eyeball a crop box out of one is where product decks lose their product
    shots. A block is a crop of an image, not an area of a PDF page, so it
    carries no bbox -- which is what keeps it from being read as a fragment of
    the page it came from."""
    source = product_page(tmp_path / "page.png")
    figures = tmp_path / "figures"
    figures.mkdir()

    blocks = cut_page_blocks(source, figures)

    assert len(blocks) == 2
    for block in blocks:
        assert block.path.is_file()
        assert block.bbox is None
        assert block.coverage == 0.0
        assert min(block.size) >= 160


def test_a_page_with_no_separable_block_yields_nothing(tmp_path: Path) -> None:
    from PIL import Image

    flat = tmp_path / "flat.png"
    Image.new("RGB", (1200, 1600), (250, 250, 250)).save(flat)
    figures = tmp_path / "figures"
    figures.mkdir()

    assert cut_page_blocks(flat, figures) == []
    assert list(figures.iterdir()) == []
