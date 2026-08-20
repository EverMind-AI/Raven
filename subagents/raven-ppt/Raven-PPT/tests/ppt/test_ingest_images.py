"""Pixel work on an extracted figure: trimming, measuring, cutting a panel.

Fixtures are synthesised here on purpose. Real benchmark materials must never
reach the code or the tests, and synthetic shapes pin the geometry these
heuristics actually key on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.services.ingest.images import (
    DETAIL_INK_MAX,
    autocrop_border,
    contain_scale,
    crop_figure,
    detect_panels,
    edge_cut_share,
    estimate_panels,
    ink_ratio,
    interior_ink_ratio,
    segment_page_blocks,
    stitch_vertical,
)

pytest.importorskip("PIL", reason="render extra (pillow) not installed")


def _grid_figure(path: Path, cols: int, rows: int) -> Path:
    """Composite figure: `cols`x`rows` panels separated by wide gutters."""
    from PIL import Image, ImageDraw

    cell, gutter = 120, 40
    img = Image.new("RGB", (cols * cell + (cols + 1) * gutter, rows * cell + (rows + 1) * gutter), "white")
    draw = ImageDraw.Draw(img)
    for row in range(rows):
        for col in range(cols):
            x = gutter + col * (cell + gutter)
            y = gutter + row * (cell + gutter)
            box = (x + 10, y + 10, x + cell - 10, y + cell - 10)
            if (row + col) % 2:
                draw.ellipse(box, fill="black")
            else:
                draw.rectangle(box, fill="black")
    img.save(path)
    return path


def _single_plot(path: Path) -> Path:
    """One sparse plot: axes box plus a data line, nothing else."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (420, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((40, 20, 400, 260), outline="black", width=2)
    draw.line((40, 250, 400, 40), fill="black", width=2)
    draw.text((160, 270), "Sample series", fill="black")
    img.save(path)
    return path


def _quadrants(path: Path, *, border: int = 0) -> Path:
    """Four quadrants, each carrying a mark so a crop of one holds content,
    optionally inside a uniform black border."""
    from PIL import Image, ImageDraw

    inner = Image.new("RGB", (200, 200), "white")
    draw = ImageDraw.Draw(inner)
    for box, color in (
        ((0, 0, 100, 100), (200, 0, 0)),
        ((100, 0, 200, 100), (0, 160, 0)),
        ((0, 100, 100, 200), (0, 0, 200)),
        ((100, 100, 200, 200), (240, 200, 0)),
    ):
        inner.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), color), box[:2])
        draw.rectangle((box[0] + 18, box[1] + 18, box[2] - 18, box[3] - 18), outline=(255, 255, 255), width=8)
    if border:
        canvas = Image.new("RGB", (200 + 2 * border, 200 + 2 * border), (0, 0, 0))
        canvas.paste(inner, (border, border))
        inner = canvas
    inner.save(path)
    return path


def _table_over_a_caption(path: Path) -> None:
    """A ruled block with a line of text under it, the shape a paper prints."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    for y in range(60, 300, 40):
        draw.line((40, y, 760, y), fill="black", width=2)
    for x in range(40, 760, 20):  # word-shaped runs, so the row is part ink part gap
        draw.rectangle((x, 384, x + 9, 400), fill="black")
    image.save(path)


# --- trimming ---------------------------------------------------------------


def test_autocrop_trims_a_letterboxed_banner(tmp_path: Path, noise_image) -> None:
    """Web-print PDF shape: black bars around a thin content strip."""
    from PIL import Image

    banner = Image.new("RGB", (2000, 400), (0, 0, 0))
    banner.paste(noise_image(600, 240), (700, 80))
    path = tmp_path / "banner.png"
    banner.save(path)

    width, height = autocrop_border(path)

    assert 600 <= width <= 690 and 240 <= height <= 270, (width, height)
    with Image.open(path) as reloaded:
        assert reloaded.size == (width, height)


def test_autocrop_trims_white_margins(tmp_path: Path, noise_image) -> None:
    """Paper-figure shape: white margins around a centred panel."""
    from PIL import Image

    square = Image.new("RGB", (800, 800), (255, 255, 255))
    square.paste(noise_image(500, 500), (150, 150))
    path = tmp_path / "figure.png"
    square.save(path)

    width, height = autocrop_border(path)

    assert 500 <= width <= 540 and 500 <= height <= 540, (width, height)


def test_autocrop_trims_white_down_one_side_only(tmp_path: Path, noise_image) -> None:
    """A figure whose panels sit off-centre in the region it was cut from has
    disagreeing corners; giving up there left one paper figure carrying 264
    blank pixels of its 1801, which the deck then scaled up with it."""
    from PIL import Image

    canvas = Image.new("RGB", (1800, 900), (255, 255, 255))
    canvas.paste(noise_image(1300, 860), (0, 20))
    path = tmp_path / "offcentre.png"
    canvas.save(path)

    width, _height = autocrop_border(path)

    assert width <= 1340, width


def test_autocrop_declines_a_thin_fragment_that_is_not_a_letterbox(tmp_path: Path) -> None:
    """A 30x130 mark in a large image is a detection artefact, not content:
    cropping to it would destroy the image in place. A thin result is trusted
    only when it spans most of the image -- the letterbox shape."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1800, 1300), "white")
    ImageDraw.Draw(image).rectangle((100, 100, 130, 230), fill=(20, 80, 180))
    path = tmp_path / "fragment.png"
    image.save(path)

    assert autocrop_border(path) == (1800, 1300)
    with Image.open(path) as reloaded:
        assert reloaded.size == (1800, 1300)


def test_autocrop_keeps_a_thin_result_that_spans_the_image(tmp_path: Path, noise_image) -> None:
    """The other side of the same bound: a letterboxed strip is a real shape."""
    from PIL import Image

    image = Image.new("RGB", (1800, 1300), "white")
    image.paste(noise_image(1700, 90), (50, 600))
    path = tmp_path / "strip.png"
    image.save(path)

    width, height = autocrop_border(path)

    assert width >= 1700 and height <= 130, (width, height)


def test_autocrop_keeps_a_solid_image_untouched(tmp_path: Path) -> None:
    """A solid-colour image has an empty content box: no crop, no rewrite."""
    from PIL import Image

    path = tmp_path / "solid.png"
    Image.new("RGB", (400, 300), (90, 120, 150)).save(path)
    before = path.read_bytes()

    assert autocrop_border(path) == (400, 300)
    assert path.read_bytes() == before


def test_autocrop_reports_zero_for_a_file_it_cannot_open(tmp_path: Path) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert autocrop_border(broken) == (0, 0)


# --- measurements -----------------------------------------------------------


def test_estimate_panels_separates_composites_from_single_plots(tmp_path: Path) -> None:
    assert 4 <= estimate_panels(_grid_figure(tmp_path / "grid.png", cols=2, rows=3)) <= 9
    assert estimate_panels(_single_plot(tmp_path / "plot.png")) <= 2


def test_estimate_panels_is_fail_soft(tmp_path: Path) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert estimate_panels(broken) == 1


def test_detect_panels_finds_the_grid_and_declines_a_single_plot(tmp_path: Path) -> None:
    """Panel boxes exist so a crop can be taken by index: measured on real paper
    figures the gutters are only 2-3% of the figure wide, which is what makes an
    eyeballed box slice a panel in half."""
    boxes = detect_panels(_grid_figure(tmp_path / "grid.png", cols=2, rows=3))
    assert 4 <= len(boxes) <= 9
    for x0, y0, x1, y1 in boxes:
        assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0
    assert detect_panels(_single_plot(tmp_path / "plot.png")) == []


def test_ink_ratio_separates_line_art_from_photographs(tmp_path: Path, noise_image) -> None:
    assert ink_ratio(_single_plot(tmp_path / "plot.png")) < DETAIL_INK_MAX
    noise = tmp_path / "photo.png"
    noise_image(400, 300).save(noise)
    assert ink_ratio(noise) > DETAIL_INK_MAX
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"nope")
    assert ink_ratio(broken) == 1.0


@pytest.mark.parametrize("border", [24, 60, 100, 160])
def test_interior_ink_rejects_a_blank_frame(tmp_path: Path, border: int) -> None:
    from PIL import Image, ImageDraw

    blank = tmp_path / f"blank_frame_{border}.png"
    image = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(image).rectangle((0, 0, 799, 599), outline="black", width=border)
    image.save(blank)

    assert interior_ink_ratio(blank) < 0.001


@pytest.mark.parametrize("inset", [5, 20, 60, 72])
def test_interior_ink_rejects_an_inset_blank_frame(tmp_path: Path, inset: int) -> None:
    from PIL import Image, ImageDraw

    path = tmp_path / f"inset_frame_{inset}.png"
    image = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(image).rectangle((inset, inset, 799 - inset, 599 - inset), outline="black", width=30)
    image.save(path)

    assert interior_ink_ratio(path) < 0.001


def test_interior_ink_rejects_rounded_and_broken_blank_frames(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    rounded = tmp_path / "rounded_frame.png"
    image = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(image).rounded_rectangle((20, 20, 779, 579), radius=70, outline="black", width=30)
    image.save(rounded)
    assert interior_ink_ratio(rounded) < 0.004
    with pytest.raises(ValueError, match="margin or a flat"):
        crop_figure(rounded, (0.0, 0.0, 1.0, 1.0), tmp_path / "rounded_crop.png", snap=False)

    broken = tmp_path / "broken_frame.png"
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    for line in (
        (20, 20, 330, 20),
        (430, 20, 779, 20),
        (20, 579, 330, 579),
        (430, 579, 779, 579),
        (20, 20, 20, 240),
        (20, 340, 20, 579),
        (779, 20, 779, 240),
        (779, 340, 779, 579),
    ):
        draw.line(line, fill="black", width=30)
    image.save(broken)
    assert interior_ink_ratio(broken) < 0.004


def test_interior_ink_keeps_plot_marks_connected_to_the_frame(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    line_plot = tmp_path / "boxed_line.png"
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 740, 540), outline="black", width=8)
    draw.line((60, 390, 240, 330, 420, 410, 600, 350, 740, 390), fill=(20, 80, 180), width=8)
    image.save(line_plot)
    assert interior_ink_ratio(line_plot) > 0.004

    bar_plot = tmp_path / "boxed_bars.png"
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 60, 740, 540), outline="black", width=8)
    for left, top in ((110, 360), (230, 300), (510, 340), (630, 250)):
        draw.rectangle((left, top, left + 55, 540), fill=(20, 80, 180))
    image.save(bar_plot)
    assert interior_ink_ratio(bar_plot) > 0.004


@pytest.mark.parametrize(
    "box",
    [(0, 180, 120, 420), (680, 180, 800, 420), (260, 0, 540, 100), (260, 500, 540, 600)],
)
def test_interior_ink_keeps_content_that_touches_one_edge(tmp_path: Path, box) -> None:
    from PIL import Image, ImageDraw

    path = tmp_path / "edge_content.png"
    image = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(image).rectangle(box, fill="black")
    image.save(path)

    assert interior_ink_ratio(path) > 0.004


def test_alpha_is_flattened_to_white_for_content_metrics(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    transparent = tmp_path / "transparent.png"
    image = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((140, 100, 360, 300), fill=(20, 80, 180, 255))
    image.save(transparent)
    assert 0.1 < ink_ratio(transparent) < 0.6

    white_art = tmp_path / "transparent_white.png"
    image = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((140, 100, 360, 300), fill=(255, 255, 255, 255))
    image.save(white_art)
    assert 0.1 < ink_ratio(white_art) < 0.6
    assert interior_ink_ratio(white_art) > 0.1


def test_contain_scale_is_the_shrink_a_slot_imposes() -> None:
    assert contain_scale((1000, 500), (500.0, 500.0)) == 0.5
    assert contain_scale((0, 0), (500.0, 500.0)) == 0.0


def test_edge_cut_share_separates_a_sliced_crop_from_a_full_frame(tmp_path: Path, noise_image) -> None:
    """Edge ink alone cannot tell the three cases apart: a photograph fills every
    edge and a text line sliced at an edge barely registers. Measured per edge
    *line* they separate cleanly."""
    from PIL import Image, ImageDraw

    def measure(image, box, name: str) -> float:
        source = tmp_path / f"{name}_src.png"
        image.save(source)
        destination = tmp_path / f"{name}.png"
        crop_figure(source, box, destination, autocrop=False, snap=False)
        return edge_cut_share(destination)

    page = Image.new("RGB", (1200, 800), (12, 12, 14))
    draw = ImageDraw.Draw(page)
    for row in range(3):
        draw.rectangle((90, 80 + row * 60, 900, 110 + row * 60), fill=(240, 240, 240))
    draw.rectangle((300, 320, 900, 700), fill=(70, 40, 30))
    assert 0.01 < measure(page, (0.12, 0.06, 0.60, 0.30), "sliced") < 0.90
    # The same page cut on its own margins leaves every edge quiet.
    assert measure(page, (0.20, 0.36, 0.78, 0.90), "margins") <= 0.01
    # A photograph has no margins to cut at, so nothing to advise.
    assert measure(noise_image(600, 400), (0.15, 0.15, 0.85, 0.85), "photo") >= 0.90


# --- cropping ---------------------------------------------------------------


def test_crop_figure_cuts_the_requested_region(tmp_path: Path) -> None:
    from PIL import Image

    source = _quadrants(tmp_path / "quad.png")
    destination = tmp_path / "crop.png"
    assert crop_figure(source, (0.5, 0.5, 1.0, 1.0), destination, autocrop=False) == (100, 100)
    with Image.open(destination) as img:
        assert img.convert("RGB").getpixel((50, 50)) == (240, 200, 0)


def test_crop_figure_trims_a_uniform_border(tmp_path: Path) -> None:
    source = _quadrants(tmp_path / "framed.png", border=60)
    width, height = crop_figure(source, (0.0, 0.0, 1.0, 1.0), tmp_path / "framed_crop.png")
    assert max(width, height) < 320  # the black frame is gone, the quadrants stay


@pytest.mark.parametrize(
    "box",
    [
        (0.6, 0.0, 0.4, 1.0),  # x1 <= x0
        (0.0, 0.0, 1.4, 1.0),  # outside 0..1
        (0.0, 0.0, 0.05, 0.05),  # degenerate result
    ],
)
def test_crop_figure_rejects_bad_boxes(tmp_path: Path, box) -> None:
    source = _quadrants(tmp_path / "quad.png")
    with pytest.raises(ValueError):
        crop_figure(source, box, tmp_path / "out.png")


def test_crop_figure_rejects_a_result_nobody_could_read(tmp_path: Path) -> None:
    """A box aimed off the panel comes back a strip or a flat band. Placed, it
    reads as a defect and nothing downstream can tell it from a deliberate
    figure, so it fails where the caller can still re-aim it."""
    from PIL import Image

    source = _grid_figure(tmp_path / "grid.png", cols=3, rows=3)
    strip = tmp_path / "strip.png"
    with pytest.raises(ValueError, match="strip"):
        crop_figure(source, (0.0, 0.45, 1.0, 0.52), strip, autocrop=False, snap=False)
    assert not strip.exists()

    flat = tmp_path / "flat.png"
    Image.new("RGB", (600, 400), (18, 18, 18)).save(flat)
    with pytest.raises(ValueError, match="margin or a flat"):
        crop_figure(flat, (0.1, 0.1, 0.9, 0.9), tmp_path / "band.png")


def test_crop_figure_rechecks_usability_after_autocrop(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    source = tmp_path / "letterboxed_strip.png"
    image = Image.new("RGB", (2000, 1000), "white")
    ImageDraw.Draw(image).rectangle((60, 450, 1940, 550), fill=(20, 80, 180))
    image.save(source)
    destination = tmp_path / "crop.png"

    with pytest.raises(ValueError, match="strip"):
        crop_figure(source, (0.0, 0.0, 1.0, 1.0), destination, snap=False)
    assert not destination.exists()


def test_a_failed_crop_preserves_an_existing_destination(tmp_path: Path) -> None:
    from PIL import Image

    source = tmp_path / "blank.png"
    Image.new("RGB", (600, 400), (18, 18, 18)).save(source)
    destination = tmp_path / "existing.png"
    Image.new("RGB", (300, 200), (20, 80, 180)).save(destination)
    before = destination.read_bytes()

    with pytest.raises(ValueError, match="margin or a flat"):
        crop_figure(source, (0.0, 0.0, 1.0, 1.0), destination, snap=False)
    assert destination.read_bytes() == before


def test_a_cut_through_content_warns_and_still_writes_the_crop(tmp_path: Path) -> None:
    """Only a clean gutter or a solid fill clears the band this measures, and a
    figure printed above its caption has neither -- so refusing turned "look at
    the crop and nudge the edge" into guessing at a number the caller never
    sees. It has eyes; hand it the crop and say what looked wrong."""
    source = tmp_path / "source.png"
    _table_over_a_caption(source)
    destination = tmp_path / "crop.png"
    notes: list[str] = []

    size = crop_figure(source, (0.0, 0.0, 1.0, 0.653), destination, snap=False, autocrop=False, cut_edge_notes=notes)

    assert destination.is_file()
    assert size == (800, 392)
    assert notes and "ink across" in notes[0]


def test_rejection_stays_available_for_callers_that_want_it(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    _table_over_a_caption(source)
    with pytest.raises(ValueError, match="ink across"):
        crop_figure(
            source, (0.0, 0.0, 1.0, 0.653), tmp_path / "out.png", snap=False, autocrop=False, reject_cut_edges=True
        )


# --- page segmentation and stitching ----------------------------------------


def test_segment_page_blocks_finds_the_images_on_a_page(tmp_path: Path, product_page) -> None:
    blocks = segment_page_blocks(product_page(tmp_path / "page.png"))

    assert len(blocks) == 2, blocks
    for x0, y0, x1, y1 in blocks:
        assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0
        assert 0.02 < (x1 - x0) * (y1 - y0) < 0.6, (x0, y0, x1, y1)
    tops = sorted(box[1] for box in blocks)
    assert tops[0] < 0.3 and tops[1] > 0.5  # one block per image, not one merged


def test_segment_page_blocks_pads_a_strip_into_a_placeable_shape(tmp_path: Path, noise_image) -> None:
    """A 12:1 crop of a laptop edge is a real product shot; shrink-wrapped it is
    a banner strip nothing can place."""
    from PIL import Image

    page = Image.new("RGB", (1400, 1400), (10, 10, 10))
    page.paste(noise_image(900, 70), (250, 660))
    page.save(tmp_path / "thin.png")

    blocks = segment_page_blocks(tmp_path / "thin.png")

    assert blocks, "the strip is the page's only content"
    x0, y0, x1, y1 = blocks[0]
    assert (x1 - x0) / (y1 - y0) < 3.0, "padded back out of banner territory"


def test_segment_page_blocks_is_fail_soft(tmp_path: Path) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert segment_page_blocks(broken) == []


def test_segment_page_blocks_keys_on_a_light_ground_too(tmp_path: Path) -> None:
    """The dominant-colour encoding overflowed in uint8, so any page whose ground
    was not black keyed on magenta and came back as one block -- the whole page.
    Two real product brochures produced nothing at all."""
    import numpy as np
    from PIL import Image

    from raven.ppt.services.ingest.images import _dominant_color

    page = Image.new("RGB", (1400, 1600), (250, 250, 250))
    for top, tint in ((250, (200, 60, 40)), (1000, (30, 90, 170))):
        block = Image.new("RGB", (760, 460))
        pixels = block.load()
        for y in range(460):
            for x in range(760):
                pixels[x, y] = ((tint[0] + x // 6) % 256, (tint[1] + y // 5) % 256, (tint[2] + (x + y) // 7) % 256)
        page.paste(block, (320, top))
    page.save(tmp_path / "light.png")

    assert _dominant_color(np.asarray(page)) == [248, 248, 248]
    blocks = segment_page_blocks(tmp_path / "light.png")
    assert len(blocks) == 2, blocks
    assert all((x1 - x0) * (y1 - y0) < 0.6 for x0, y0, x1, y1 in blocks)


def test_segment_page_blocks_drops_a_second_crop_of_the_same_image(tmp_path: Path, noise_image) -> None:
    """Padding a block out to a placeable shape can push it over its neighbour,
    and a badge sitting in a photograph's own clear area is a separate region
    with the same bounding box. Two crops of one image is worse than one."""
    from PIL import Image, ImageDraw

    page = Image.new("RGB", (1400, 1400), (250, 250, 250))
    page.paste(noise_image(800, 700), (300, 350))
    draw = ImageDraw.Draw(page)
    draw.rectangle((520, 600, 880, 800), fill=(250, 250, 250))  # a clear area in the photo
    draw.rectangle((580, 650, 820, 750), fill=(20, 30, 40))  # a badge sitting in it
    page.save(tmp_path / "badged.png")

    blocks = segment_page_blocks(tmp_path / "badged.png")

    assert len(blocks) == 1, blocks
    x0, y0, x1, y1 = blocks[0]
    assert (x1 - x0) * (y1 - y0) > 0.2, "the photograph, not the badge inside it"


def test_stitch_vertical_stacks_bands_and_reports_zero_on_a_bad_part(tmp_path: Path, noise_image) -> None:
    parts = []
    for index in range(3):
        part = tmp_path / f"band{index}.png"
        noise_image(600, 120).save(part)
        parts.append(part)

    assert stitch_vertical(parts, tmp_path / "stacked.png") == (600, 360)

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    assert stitch_vertical([parts[0], broken], tmp_path / "failed.png") == (0, 0)
