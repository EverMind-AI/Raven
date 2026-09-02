"""What an asset says about itself, and which assets are not assets at all.

The predecessor's three-way `usability` grade is gone; these pin what replaced
it -- sentences derived from measurements, and a fragment check that removes
the pieces the grade used to hand over labelled "avoid".

The sentences are about the file, never about how large to place it. The
placement verdicts that used to live here fired on every figure of two real
runs (17 of 17, 14 of 14), so several tests below exist to keep silence
silent: a paper figure at its real dimensions, a wide diagram and a dense
table each assert that nothing is said.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven_ppt.contracts.sources import AssetKind, SourceAsset
from raven_ppt.services.ingest.assets import (
    Measurements,
    concerns,
    drop_fragments,
    is_page_screenshot,
    load_catalogue,
    noting,
    write_catalogue,
)

pytest.importorskip("fitz", reason="render extra (pymupdf) not installed")


def _measured(
    width: int,
    height: int,
    *,
    coverage: float = 0.2,
    panels: int = 1,
    interior: float = 1.0,
) -> Measurements:
    return Measurements(
        width_px=width,
        height_px=height,
        page_coverage=coverage,
        panel_count=panels,
        interior_ink_ratio=interior,
    )


def _asset(
    asset_id: str,
    tmp_path: Path,
    bbox: tuple[float, float, float, float] | None,
    *,
    label: str | None = None,
    page: int | None = 1,
    kind: AssetKind = AssetKind.FIGURE,
) -> SourceAsset:
    path = tmp_path / f"{asset_id}.png"
    path.write_bytes(b"pixels")
    return SourceAsset(
        asset_id=asset_id,
        path=path,
        kind=kind,
        width_px=400,
        height_px=300,
        source_file="paper.pdf",
        source_page=page,
        source_label=label,
        source_bbox=bbox,
    )


def test_a_placeable_figure_has_nothing_to_say() -> None:
    assert concerns(_measured(900, 600), AssetKind.FIGURE) == ()


def test_every_applicable_concern_is_stated_not_just_the_first() -> None:
    """A 6:1 band covering its whole page has two problems, and reporting one
    read as though the other had been checked and passed."""
    found = concerns(_measured(1900, 300, coverage=0.9, panels=8), AssetKind.FIGURE)
    assert any("strip" in note or "banner" in note for note in found)
    assert any("whole-page screenshot" in note for note in found)
    assert any("panels" in note for note in found)


def test_a_figure_too_small_to_read_says_so_with_its_size() -> None:
    (note,) = concerns(_measured(90, 400, coverage=0.1), AssetKind.FIGURE)[:1]
    assert "90x400px" in note


def test_an_icon_sized_share_of_a_page_is_named_as_one() -> None:
    found = concerns(_measured(500, 500, coverage=0.002), AssetKind.FIGURE)
    assert any("0.2% of its source page" in note for note in found)


def test_a_blank_frame_reports_its_interior_not_its_size() -> None:
    found = concerns(_measured(800, 600, interior=0.0), AssetKind.FIGURE)
    assert any("interior is blank" in note for note in found)


def test_a_wide_diagram_is_left_alone() -> None:
    """Real slide material at 4.6:1. Placed across a page it is ordinary, and
    the width it is given is the page's decision, not the extraction's."""
    assert concerns(_measured(2047, 443, coverage=0.1), AssetKind.FIGURE) == ()


def test_a_strip_states_its_shape_without_ruling_on_a_layout() -> None:
    """A banner placed full-width is ordinary; the shape is the fact, and
    "no slide layout carries that shape" was not one."""
    (note,) = concerns(_measured(1600, 200, coverage=0.1), AssetKind.FIGURE)
    assert "aspect 8.0:1" in note
    assert "no slide layout carries" not in note
    # 5.3:1 is a wide figure, and a wide figure is placeable.
    assert concerns(_measured(1600, 300, coverage=0.1), AssetKind.FIGURE) == ()


def test_a_paper_figure_is_not_flagged_for_the_size_it_arrives_at() -> None:
    """The four figures a person read off two real runs and judged usable at
    full width; the retired scale verdict flagged all four."""
    for width, height in ((2578, 1074), (2191, 650), (1378, 578), (2348, 1640)):
        assert concerns(_measured(width, height, coverage=0.2), AssetKind.FIGURE) == ()


def test_a_table_is_never_told_to_crop_or_counted_in_panels() -> None:
    """Half a table is a misquote, and a table's rows are not panels -- both
    were wrong in the grade this replaced. A dense one is still a table: the
    only reader who can see how dense is the one looking at the render."""
    assert concerns(_measured(1131, 508, coverage=0.21, panels=9), AssetKind.TABLE) == ()
    assert concerns(_measured(1270, 524, coverage=0.242), AssetKind.TABLE) == ()


def test_a_page_screenshot_is_recognised_by_its_coverage(tmp_path: Path) -> None:
    page = _asset("page", tmp_path, (0.0, 0.0, 612.0, 792.0))
    from dataclasses import replace

    assert is_page_screenshot(replace(page, page_coverage=0.85))
    assert not is_page_screenshot(replace(page, page_coverage=0.25))
    # A block cut out of a page carries no coverage at all.
    assert not is_page_screenshot(replace(page, page_coverage=0.0))


def test_a_piece_of_an_extracted_figure_is_not_a_second_figure(tmp_path: Path) -> None:
    """Four of five assets the grade called "avoid" on one paper were this:
    a region already extracted whole, handed over again in pieces."""
    whole = _asset("whole", tmp_path, (60.0, 100.0, 540.0, 400.0), label="Figure 4")
    piece = _asset("piece", tmp_path, (100.0, 150.0, 200.0, 220.0))

    kept, dropped = drop_fragments([whole, piece])

    assert [asset.asset_id for asset in kept] == ["whole"]
    assert [asset.asset_id for asset in dropped] == ["piece"]
    # Left on disk it would be an orphan a figure listing still finds.
    assert not piece.path.exists()


def test_a_labelled_figure_inside_another_region_survives(tmp_path: Path) -> None:
    """The caption is the veto: a page that names it can cite it."""
    whole = _asset("whole", tmp_path, (60.0, 100.0, 540.0, 400.0), label="Figure 4")
    inner = _asset("inner", tmp_path, (100.0, 150.0, 300.0, 300.0), label="Figure 5")

    kept, dropped = drop_fragments([whole, inner])

    assert {asset.asset_id for asset in kept} == {"whole", "inner"}
    assert dropped == []


def test_containment_is_only_checked_within_one_page(tmp_path: Path) -> None:
    whole = _asset("whole", tmp_path, (60.0, 100.0, 540.0, 400.0))
    same_box_elsewhere = _asset("elsewhere", tmp_path, (100.0, 150.0, 200.0, 220.0), page=2)

    kept, dropped = drop_fragments([whole, same_box_elsewhere])

    assert {asset.asset_id for asset in kept} == {"whole", "elsewhere"}
    assert dropped == []


def test_an_asset_with_no_page_region_is_never_a_fragment(tmp_path: Path) -> None:
    """Supplied images and blocks cut out of a screenshot have no page bbox."""
    whole = _asset("whole", tmp_path, (0.0, 0.0, 612.0, 792.0))
    supplied = _asset("supplied", tmp_path, None, kind=AssetKind.IMAGE, page=None)
    block = _asset("page_b1", tmp_path, None)

    kept, dropped = drop_fragments([whole, supplied, block])

    assert [asset.asset_id for asset in kept] == ["whole", "supplied", "page_b1"]
    assert dropped == []


def test_a_near_miss_of_containment_still_counts(tmp_path: Path) -> None:
    """Both boxes are measured, not declared: a drawing cluster sits a fraction
    of a point outside the region rendered for the figure it belongs to."""
    whole = _asset("whole", tmp_path, (60.0, 100.0, 540.0, 400.0))
    nearly_inside = _asset("nearly", tmp_path, (59.5, 99.5, 300.0, 300.0))

    kept, dropped = drop_fragments([whole, nearly_inside])

    assert [asset.asset_id for asset in kept] == ["whole"]
    assert [asset.asset_id for asset in dropped] == ["nearly"]


def test_a_neighbouring_figure_is_not_a_fragment(tmp_path: Path) -> None:
    left = _asset("left", tmp_path, (60.0, 100.0, 290.0, 400.0))
    right = _asset("right", tmp_path, (310.0, 100.0, 540.0, 400.0))

    kept, dropped = drop_fragments([left, right])

    assert {asset.asset_id for asset in kept} == {"left", "right"}
    assert dropped == []


def test_the_catalogue_round_trips(tmp_path: Path) -> None:
    figures = tmp_path / "figures"
    figures.mkdir()
    asset = SourceAsset(
        asset_id="paper_p001_table1",
        path=figures / "paper_p001_table1.png",
        kind=AssetKind.TABLE,
        width_px=1131,
        height_px=508,
        page_coverage=0.211,
        panel_count=1,
        concerns=("covers only 0.4% of its source page — the footprint of an icon or a page decoration",),
        source_file="paper.pdf",
        source_page=1,
        source_label="Table 1",
        caption="Table 1. Benchmark results.",
        source_bbox=(60.0, 84.0, 540.0, 300.0),
    )
    path = tmp_path / "figures.json"
    write_catalogue([asset], path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == "raven_ppt.assets.v1"
    assert raw["assets"]["paper_p001_table1"]["source_label"] == "Table 1"
    assert raw["assets"]["paper_p001_table1"]["file"] == "paper_p001_table1.png"
    assert load_catalogue(path, figures_dir=figures) == {"paper_p001_table1": asset}


def test_a_note_can_be_added_without_losing_what_was_measured() -> None:
    asset = SourceAsset(
        asset_id="page",
        path=Path("page.png"),
        kind=AssetKind.FIGURE,
        width_px=1400,
        height_px=1800,
        concerns=("covers 96% of its source page",),
    )
    assert noting(asset, "its blocks are cut as page_b1").concerns == (
        "covers 96% of its source page",
        "its blocks are cut as page_b1",
    )
