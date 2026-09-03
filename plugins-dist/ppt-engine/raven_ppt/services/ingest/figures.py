"""Getting the pictures out of a PDF page, as the page printed them.

A published figure is rarely one embedded image. It is laid in as one image
per cell (this paper's Figure 4 is twelve, in four rows of three), or as a
column of same-width bands a "print to PDF" export sliced a screenshot into, or
as drawing commands with no image at all. Handing over the pieces gives a deck
one frame out of twelve, or a 6:1 strip no layout can carry.

So each of the four readers here answers "which region of the page is one
figure", and the region is rendered rather than reassembled: the page draws
panel letters, arrows and axis labels *between* the pieces, and those belong to
the figure too.

Every reader returns a region alongside its file, because the region is what
lets a caption be matched to it and what lets a fragment of an already
extracted figure be recognised as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven_ppt.services.ingest.geometry import rect, union
from raven_ppt.services.ingest.images import autocrop_border, crop_figure, segment_page_blocks, stitch_vertical

# Embedded images below either bound are decorations and logos, not figures.
MIN_FIGURE_PIXELS = 160
_MIN_FIGURE_BYTES = 8_000

# Vector figures (academic plots, architecture diagrams) are PDF drawing
# commands, invisible to get_images; drawing clusters above these bounds are
# rendered to PNG instead. Tolerance 18pt merges sub-panels of one figure; the
# page-fraction cap rejects full-page frames and backgrounds.
_VEC_CLUSTER_TOL_PT = 18.0
_VEC_MIN_W_PT = 120.0
_VEC_MIN_H_PT = 90.0
_VEC_MIN_AREA_PT2 = 24_000.0
_VEC_MAX_PAGE_FRACTION = 0.92
_VEC_PAGE_CAP = 3
_VEC_PAD_PT = 6.0
_VEC_RENDER_DPI = 150
# A cluster mostly inside an embedded bitmap is that bitmap's own content
# re-read as drawings.
_VEC_BITMAP_OVERLAP = 0.5

# "Print to PDF" exports of a long web page ship each page as a stack of
# same-width bands with touching edges. Extracted one by one they are 6:1
# strips that every slot squashes and no crop rescues; stacked back in
# placement order they are the screenshot the page actually shows.
_STRIP_EDGE_TOL_PT = 1.0
_STRIP_SEAM_TOL_PT = 2.0

# How far apart two images can sit and still belong to one printed figure. A
# figure's own frames are separated by a hairline gutter; two different figures
# on a page are separated by a caption and a paragraph.
_CLUSTER_GAP_PT = 8.0

# Wide enough that a panel label survives the render, capped so a full-page
# region does not turn into a 40MB PNG.
_REGION_TARGET_PX = 1800


@dataclass(frozen=True)
class ExtractedFigure:
    """One figure file, and the page region it was taken from."""

    path: Path
    size: tuple[int, int]
    coverage: float
    bbox: Any | None  # fitz.Rect


def figure_clusters(placements: list[dict]) -> list[list[dict]]:
    """Group placements that make up one printed figure.

    Bucketing by shared edges answered a tidier question than the page was
    asking: this paper's Figure 4 has its second row printed wider than the
    rest and overlapping its neighbours, and a reader got one frame out of
    twelve. Proximity is the question that actually separates one figure from
    the next.
    """
    remaining = list(placements)
    clusters: list[list[dict]] = []
    while remaining:
        cluster = [remaining.pop()]
        grew = True
        while grew:
            grew = False
            for candidate in list(remaining):
                if any(_near(candidate["bbox"], member["bbox"]) for member in cluster):
                    cluster.append(candidate)
                    remaining.remove(candidate)
                    grew = True
        clusters.append(cluster)
    return clusters


def _near(one: tuple[float, float, float, float], other: tuple[float, float, float, float]) -> bool:
    gap_x = max(one[0] - other[2], other[0] - one[2])
    gap_y = max(one[1] - other[3], other[1] - one[3])
    return gap_x <= _CLUSTER_GAP_PT and gap_y <= _CLUSTER_GAP_PT


def strip_stacks(placements: list[dict]) -> list[list[dict]]:
    """Group ``placements`` that are vertical slices of one source image.

    Each placement needs ``bbox`` (page points, top-left origin) and ``px_w``
    (decoded pixel width). Slices of one screenshot share both horizontal edges
    and the pixel width, and each one's bottom edge meets the next one's top.
    Runs of one are not stacks and are left out.
    """
    buckets: dict[tuple[int, int, int], list[dict]] = {}
    for placement in placements:
        x0, _, x1, _ = placement["bbox"]
        key = (round(x0 / _STRIP_EDGE_TOL_PT), round(x1 / _STRIP_EDGE_TOL_PT), int(placement["px_w"]))
        buckets.setdefault(key, []).append(placement)
    stacks: list[list[dict]] = []
    for members in buckets.values():
        members.sort(key=lambda p: p["bbox"][1])
        run = [members[0]]
        for previous, current in zip(members, members[1:]):
            if abs(current["bbox"][1] - previous["bbox"][3]) <= _STRIP_SEAM_TOL_PT:
                run.append(current)
            else:
                stacks.append(run)
                run = [current]
        stacks.append(run)
    return [stack for stack in stacks if len(stack) > 1]


def placements_bbox(members: list[dict]):
    """The page region a group of placements covers, as one rect."""
    return union([member["bbox"] for member in members if member.get("bbox")])


def render_cluster(page, cluster: list[dict], out: Path) -> ExtractedFigure | None:
    """Render the region a multi-image figure occupies, as the page printed it."""
    bbox = placements_bbox(cluster)
    if bbox is None:
        return None
    dims = _render_page_region(page, bbox, out)
    if min(dims) < MIN_FIGURE_PIXELS:
        out.unlink(missing_ok=True)
        return None
    # The slices tile their span, so their page coverage simply adds up.
    coverage = min(1.0, sum(member.get("coverage", 0.0) for member in cluster))
    return ExtractedFigure(out, dims, coverage, bbox)


def _render_page_region(page, bbox, out: Path) -> tuple[int, int]:
    """Rasterise one region of the page.

    Stitching the member images back together guesses at the arrangement and
    loses whatever the page drew between them -- panel letters, arrows, axis
    labels set as text. Rendering the region keeps all of it.
    """
    import fitz

    region = rect(bbox)
    if region.is_empty or region.width <= 0:
        return (0, 0)
    zoom = max(1.0, min(8.0, _REGION_TARGET_PX / region.width))
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=region, alpha=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(out))
    # The region is the union of the member images, so a figure whose panels sit
    # unevenly in it arrives framed in the page's own white -- one paper figure
    # came through with 203 of its 1801 pixels blank down the sides. The deck
    # then scales that frame up with the figure and pays for it in layout.
    dims = autocrop_border(out)
    if min(dims) <= 0:
        dims = (pixmap.width, pixmap.height)
    return (int(dims[0]), int(dims[1]))


def stitch_stack(stack: list[dict], out: Path) -> ExtractedFigure | None:
    """Write one stack of slices as a single figure.

    Returns ``None`` -- leaving the slices to be emitted individually -- when
    the stitch fails or the reassembled image is still below the figure bounds.
    """
    parts: list[Path] = []
    for index, member in enumerate(stack):
        part = out.parent / f".stack_{out.stem}_{member['xref']}_{index}.{member['ext']}"
        part.write_bytes(member["data"])
        parts.append(part)
    try:
        dims = stitch_vertical(parts, out)
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
    if min(dims) < MIN_FIGURE_PIXELS:
        out.unlink(missing_ok=True)
        return None
    trimmed = autocrop_border(out)
    if min(trimmed) > 0:
        dims = trimmed
    coverage = min(1.0, sum(member["coverage"] for member in stack))
    return ExtractedFigure(out, (int(dims[0]), int(dims[1])), coverage, placements_bbox(stack))


def write_placement(placement: dict, out: Path) -> ExtractedFigure | None:
    """Write one embedded bitmap out, unless it is decoration.

    Both bounds matter: a logo is small in pixels, and a solid-colour spacer is
    large in pixels and tiny on disk.
    """
    width, height = placement["px_w"], placement["px_h"]
    if min(width, height) < MIN_FIGURE_PIXELS or len(placement["data"]) < _MIN_FIGURE_BYTES:
        return None
    out.write_bytes(placement["data"])
    # Web-print PDFs ship bitmaps as huge letterbox bars around a thin content
    # strip; trim so contain-fit sees the content.
    cropped = autocrop_border(out)
    if min(cropped) > 0:
        width, height = cropped
    return ExtractedFigure(out, (int(width), int(height)), placement["coverage"], placements_bbox([placement]))


def extract_vector_figures(page, out_prefix: Path, occupied: list) -> list[ExtractedFigure]:
    """Render large vector-drawing clusters (plots, diagrams) to PNGs.

    Best-effort by design: a pathological drawing stream skips the page rather
    than failing the ingest.
    """
    import fitz

    try:
        clusters = page.cluster_drawings(x_tolerance=_VEC_CLUSTER_TOL_PT, y_tolerance=_VEC_CLUSTER_TOL_PT)
    except Exception:  # noqa: BLE001 -- vendor drawing parser, page-level fallback
        return []
    page_rect = page.rect
    page_area = page_rect.get_area()
    rendered: list[ExtractedFigure] = []
    for region in clusters:
        if len(rendered) >= _VEC_PAGE_CAP:
            break
        if region.width < _VEC_MIN_W_PT or region.height < _VEC_MIN_H_PT:
            continue
        if region.width * region.height < _VEC_MIN_AREA_PT2:
            continue
        if region.width > _VEC_MAX_PAGE_FRACTION * page_rect.width and (
            region.height > _VEC_MAX_PAGE_FRACTION * page_rect.height
        ):
            continue
        area = region.get_area()
        if area and any((region & box).get_area() / area > _VEC_BITMAP_OVERLAP for box in occupied):
            continue
        clip = (
            fitz.Rect(
                region.x0 - _VEC_PAD_PT, region.y0 - _VEC_PAD_PT, region.x1 + _VEC_PAD_PT, region.y1 + _VEC_PAD_PT
            )
            & page_rect
        )
        try:
            pix = page.get_pixmap(clip=clip, dpi=_VEC_RENDER_DPI)
        except Exception:  # noqa: BLE001
            continue
        if min(pix.width, pix.height) < MIN_FIGURE_PIXELS:
            continue
        out = out_prefix.with_name(f"{out_prefix.name}_vec{len(rendered) + 1}.png")
        pix.save(str(out))
        # The 6pt render padding rarely leaves a big margin, but source pages
        # with letterboxed panels still benefit from the trim.
        dims = autocrop_border(out)
        if min(dims) <= 0:
            dims = (pix.width, pix.height)
        rendered.append(
            ExtractedFigure(out, (int(dims[0]), int(dims[1])), area / page_area if page_area else 0.0, region)
        )
    return rendered


def cut_page_blocks(source: Path, figures_dir: Path) -> list[ExtractedFigure]:
    """Register the content blocks of a page screenshot as figures of their own.

    Placed whole, a page screenshot is unreadable; asking a model to eyeball a
    crop box out of one is where product decks lose their product shots. The
    page's own layout already says where its blocks are, so the ingest hands
    them over ready to place and keeps the whole page too.

    A block carries no page region: it is a crop of an image, not an area of
    the PDF page, so it can be neither matched to a caption nor mistaken for a
    fragment of the page it came from.
    """
    emitted: list[ExtractedFigure] = []
    for index, box in enumerate(segment_page_blocks(source), start=1):
        out = figures_dir / f"{source.stem}_b{index}.png"
        try:
            dims = crop_figure(source, box, out, autocrop=False, snap=False)
        except ValueError:
            # The block was a strip or a flat band after all; the whole page
            # stays available and the planner can still crop it by hand.
            continue
        if min(dims) >= MIN_FIGURE_PIXELS:
            emitted.append(ExtractedFigure(out, dims, 0.0, None))
        else:
            out.unlink(missing_ok=True)
    return emitted
