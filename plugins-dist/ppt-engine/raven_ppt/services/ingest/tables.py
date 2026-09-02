"""Taking a table out of a PDF as the source printed it.

A table redrawn by a model is a table whose numbers moved. So a table is
captured twice: as an image of the printed region, which is what goes on the
slide, and as markdown in the material text, which is what an author reads a
number off without retyping it from the picture.

The hard part is the region. A table finder reports the cell grid it can see,
which is neither what a reader calls "Table 2" -- that includes the caption
above it and the footnote under it -- nor reliably one table: two tables
printed side by side come back merged, two stacked ones have their rules
clustered together. The caption is what separates them, so nothing is emitted
without one, and every candidate region is cut back to the caption that names
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven_ppt.contracts.sources import AssetKind
from raven_ppt.services.ingest.captions import Caption, caption_blocks, caption_match, nearest_captions
from raven_ppt.services.ingest.figures import MIN_FIGURE_PIXELS
from raven_ppt.services.ingest.geometry import horizontal_overlap, rect, union, vertical_gap
from raven_ppt.services.ingest.images import autocrop_border

_MIN_ROWS = 3
_MIN_COLS = 2
_MIN_W_PT = 120.0
_MIN_H_PT = 54.0
_PAGE_CAP = 3
_PAD_PT = 12.0
# How far above or below the grid a rule may sit and still bound the table.
_RULE_MARGIN_PT = 24.0
# Kept clear between one table's region and the next one's caption.
_NEIGHBOUR_GAP_PT = 4.0
_RENDER_DPI = 180
_DEDUP_OVERLAP = 0.6
_MIN_CELL_OCCUPANCY = 0.35
# A caption this much further away than the nearest one is a different table's.
_CAPTION_TIE_PT = 16.0
# Past this the "table" is the page: a full-page grid is the paper's own
# layout, not a table in it.
_MAX_PAGE_FRACTION = 0.55
# A line under the table that explains its notation belongs to it: "Results in
# bold are best", "Higher is better", a dagger, "Notes:".
_FOOTNOTE_RE = re.compile(
    r"(?ix)^\s*(?:"
    r"(?:notes?|sources?|footnotes?|remarks?|legend|abbreviations?|definitions?)\s*[:.]"
    r"|[*†‡§¶]+"
    r"|(?:bold|underline|best|second(?:-|\s)best|higher\s+is\s+better|lower\s+is\s+better)\b"
    r"|(?:values?|numbers?)\s+(?:are|denote|represent)\b"
    r"|results?\s+(?:in\s+)?(?:bold|underline)\b"
    r"|mean\s*(?:±|\+/-)"
    r")"
)
_FOOTNOTE_MAX_GAP_PT = 36.0


@dataclass(frozen=True)
class ExtractedTable:
    """One table image, its caption, and the markdown that anchors its numbers."""

    path: Path
    size: tuple[int, int]
    coverage: float
    bbox: Any  # fitz.Rect -- the region actually rendered
    caption: Caption
    markdown: str

    @property
    def source_label(self) -> str:
        return self.caption.label


def extract_tables(page, out_prefix: Path, *, page_text: str, text_blocks=None) -> list[ExtractedTable]:
    """Every captioned table printed on ``page``, rendered as it appears.

    Two cheap tests come first because table finding is the most expensive read
    on a page: no `Table N` anywhere in the text, or no caption block, means
    there is nothing here worth the search.
    """
    if caption_match(page_text, AssetKind.TABLE) is None:
        return []
    if text_blocks is None:
        try:
            text_blocks = page.get_text("blocks", sort=True)
        except Exception:  # noqa: BLE001
            return []
    captions = caption_blocks(text_blocks, AssetKind.TABLE)
    if not captions:
        return []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        drawings = []
    candidates = _candidates(page, captions, drawings)
    return _render_all(page, _group_by_label(page, candidates, text_blocks), out_prefix)


# The strategies a table finder is asked to try, in order of trust. The first
# is the library's default; the others loosen it for tables ruled only
# horizontally, or not at all. A later strategy only wins where an earlier one
# found nothing usable, which is what `priority` in the score expresses.
_STRATEGIES = (
    {},
    {"vertical_strategy": "text", "horizontal_strategy": "lines", "min_words_vertical": 2},
    {"strategy": "text", "min_words_vertical": 2, "min_words_horizontal": 1},
)


def _candidates(page, captions: list[Caption], drawings) -> list[dict]:
    """Every plausible table region on the page, deduplicated by overlap."""
    import fitz

    page_rect = page.rect
    page_area = page_rect.get_area()
    candidates: list[dict] = []
    for priority, strategy in enumerate(_STRATEGIES):
        try:
            finder = page.find_tables(**strategy)
        except Exception:  # noqa: BLE001
            continue
        if finder is None:
            continue
        # The loosened strategies find text that lines up in columns anywhere on
        # a page, so they are trusted only where the page drew rules.
        require_rules = bool(strategy)
        for table in finder.tables:
            if table.row_count < _MIN_ROWS or table.col_count < _MIN_COLS:
                continue
            try:
                cells = table.extract()
                nonempty = sum(bool(str(cell).strip()) for row in cells for cell in row if cell is not None)
                populated_rows = sum(
                    sum(bool(str(cell).strip()) for cell in row if cell is not None) >= 2 for row in cells
                )
                bbox = fitz.Rect(table.bbox) & page_rect
                markdown = table.to_markdown(clean=True).strip()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            cell_count = table.row_count * table.col_count
            if nonempty < _MIN_ROWS * _MIN_COLS or nonempty / cell_count < _MIN_CELL_OCCUPANCY:
                continue
            if populated_rows < _MIN_ROWS or bbox.width < _MIN_W_PT:
                continue
            if bbox.height < _MIN_H_PT or not markdown:
                continue
            if page_area and bbox.get_area() / page_area > _MAX_PAGE_FRACTION:
                continue
            rules = _horizontal_rules(bbox, drawings)
            if require_rules and len(rules) < 2:
                continue
            content_bbox = bbox
            if len(rules) >= 2:
                bounded = _rule_bounded(page, bbox, drawings)
                if bounded.width >= _MIN_W_PT and bounded.height >= _MIN_H_PT:
                    content_bbox = bounded
            matched = nearest_captions(content_bbox, captions)
            if not matched:
                continue
            for table_bbox, caption in _segments(page, content_bbox, matched, drawings):
                _keep_best(
                    candidates,
                    {
                        "bbox": table_bbox,
                        "markdown": _segment_text(page, table_bbox, markdown),
                        "caption": caption,
                        "score": (
                            int(len(rules) >= 2),
                            -priority,
                            populated_rows,
                            nonempty,
                            -table_bbox.get_area(),
                            cell_count,
                        ),
                    },
                )
    return candidates


def _keep_best(candidates: list[dict], candidate: dict) -> None:
    """Add ``candidate``, or replace the region it duplicates if it scores higher."""
    for index, existing in enumerate(candidates):
        intersection = (candidate["bbox"] & existing["bbox"]).get_area()
        smaller = min(candidate["bbox"].get_area(), existing["bbox"].get_area())
        if smaller and intersection / smaller >= _DEDUP_OVERLAP:
            if candidate["score"] > existing["score"]:
                candidates[index] = candidate
            return
    candidates.append(candidate)


def _segments(page, content_bbox, matched: list[Caption], drawings) -> list[tuple[Any, Caption]]:
    """Split one detected region into the tables its captions say are in it."""
    import fitz

    segments: list[tuple[Any, Caption]] = []
    if len(matched) == 1:
        table_bbox = _excluding_caption(fitz.Rect(content_bbox), matched[0])
        if table_bbox.height >= _MIN_H_PT:
            segments.append((table_bbox, matched[0]))
    else:
        for table_bbox, caption in _side_by_side_slices(content_bbox, matched):
            table_bbox = _rule_bounded(page, _excluding_caption(table_bbox, caption), drawings)
            if table_bbox.width < _MIN_W_PT or table_bbox.height < _MIN_H_PT:
                continue
            segments.append((table_bbox, caption))
    if segments or matched[0].bbox.y1 > content_bbox.y0 + _CAPTION_TIE_PT:
        return segments
    # Nothing usable, and the first caption sits at the top of the region: the
    # captions are stacked over rows the finder merged, each one heading what
    # follows it. This is also a single caption's second chance -- the rules the
    # page drew can bound a table the caption cut too short.
    for index, caption in enumerate(matched):
        top = max(content_bbox.y0, caption.bbox.y1)
        bottom = matched[index + 1].bbox.y0 if index + 1 < len(matched) else content_bbox.y1
        table_bbox = _rule_bounded(page, fitz.Rect(content_bbox.x0, top, content_bbox.x1, bottom), drawings)
        if table_bbox.height < _MIN_H_PT:
            continue
        segments.append((table_bbox, caption))
    return segments


def _excluding_caption(table_bbox, caption: Caption):
    """Cut the caption out of the region, from whichever side it sits on."""
    if not (table_bbox.contains(caption.bbox.tl) or table_bbox.contains(caption.bbox.br)):
        return table_bbox
    if caption.bbox.y0 >= (table_bbox.y0 + table_bbox.y1) / 2:
        table_bbox.y1 = min(table_bbox.y1, caption.bbox.y0)
    else:
        table_bbox.y0 = max(table_bbox.y0, caption.bbox.y1)
    return table_bbox


def _side_by_side_slices(content_bbox, captions: list[Caption]) -> list[tuple[Any, Caption]]:
    """Vertical slices of one region, one per caption, when they sit in a row.

    Two tables printed side by side are one region to the finder. Their
    captions are not: they sit on the same line, one over each table, and the
    midpoints between them are where the region divides.
    """
    import fitz

    if len(captions) < 2:
        return []
    ordered = sorted(captions, key=lambda caption: caption.bbox.x0)
    centers_y = [(caption.bbox.y0 + caption.bbox.y1) / 2 for caption in ordered]
    row_tolerance = max(_CAPTION_TIE_PT, max(caption.bbox.height for caption in ordered))
    if max(centers_y) - min(centers_y) > row_tolerance:
        return []
    centers_x = [(caption.bbox.x0 + caption.bbox.x1) / 2 for caption in ordered]
    boundaries = [content_bbox.x0]
    boundaries.extend((left + right) / 2 for left, right in zip(centers_x, centers_x[1:]))
    boundaries.append(content_bbox.x1)
    if any(right - left < _MIN_W_PT for left, right in zip(boundaries, boundaries[1:])):
        return []
    return [
        (fitz.Rect(left, content_bbox.y0, right, content_bbox.y1), caption)
        for left, right, caption in zip(boundaries, boundaries[1:], ordered)
    ]


def _group_by_label(page, candidates: list[dict], text_blocks) -> list[dict]:
    """One region per label, in reading order, caption and footnote included.

    A table split across two detections is still one table, and what a reader
    calls "Table 2" is the grid plus the caption naming it plus the note under
    it explaining its notation. All three go in the image.
    """
    page_rect = page.rect
    groups: dict[str, list[dict]] = {}
    for candidate in candidates:
        groups.setdefault(candidate["caption"].label, []).append(candidate)
    grouped: list[dict] = []
    for members in groups.values():
        table_bbox = union([member["bbox"] for member in members])
        caption = min(
            (member["caption"] for member in members),
            key=lambda item: vertical_gap(table_bbox, item.bbox),
        )
        parts = [table_bbox, caption.bbox]
        footnote = _footnote_bbox(text_blocks, table_bbox, caption.bbox)
        if footnote is not None:
            parts.append(footnote)
        grouped.append(
            {
                "bbox": union(parts) & page_rect,
                "table_bbox": table_bbox,
                "caption": caption,
                "markdown": max(members, key=lambda item: item["score"])["markdown"],
            }
        )
    grouped.sort(key=lambda item: (item["bbox"].y0, item["bbox"].x0))
    return grouped


def _render_all(page, grouped: list[dict], out_prefix: Path) -> list[ExtractedTable]:
    import fitz

    page_rect = page.rect
    page_area = page_rect.get_area()
    extracted: list[ExtractedTable] = []
    for candidate, (top, floor) in list(zip(grouped, _clip_limits(grouped, page_rect), strict=True))[:_PAGE_CAP]:
        content_bbox = candidate["bbox"]
        clip = (
            fitz.Rect(
                content_bbox.x0 - _PAD_PT,
                max(content_bbox.y0 - _PAD_PT, top),
                content_bbox.x1 + _PAD_PT,
                min(content_bbox.y1 + _PAD_PT, floor),
            )
            & page_rect
        )
        try:
            pix = page.get_pixmap(clip=clip, dpi=_RENDER_DPI, alpha=False)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        if min(pix.width, pix.height) < MIN_FIGURE_PIXELS:
            continue
        out = out_prefix.with_name(f"{out_prefix.name}_table{len(extracted) + 1}.png")
        pix.save(str(out))
        dims = autocrop_border(out)
        if min(dims) <= 0:
            dims = (pix.width, pix.height)
        extracted.append(
            ExtractedTable(
                path=out,
                size=(int(dims[0]), int(dims[1])),
                # The clipped rect, not the union it came from: this is what the
                # pixels are, and a crop expressed as a fraction of the figure
                # is computed against it.
                coverage=clip.get_area() / page_area if page_area else 0.0,
                bbox=clip,
                caption=candidate["caption"],
                markdown=candidate["markdown"],
            )
        )
    return extracted


def _clip_limits(grouped: list[dict], page_rect) -> list[tuple[float, float]]:
    """Where each table's image may start and stop, so two do not overlap.

    Two tables stacked on one page have their rules merged by the clustering
    tolerance, and the caption union plus padding then pushes each region into
    the other: on one paper page Table 1 ran to y=264 while Table 2's caption
    began at y=249, so Table 1's image carried Table 2's caption and header
    rows, and Table 2's began inside Table 1's last row. A table ends where the
    next one's caption starts, and begins where the previous one ended.
    """
    limits: list[tuple[float, float]] = []
    ceiling = page_rect.y0
    for index, candidate in enumerate(grouped):
        box = candidate["bbox"]
        floor = page_rect.y1
        following = grouped[index + 1] if index + 1 < len(grouped) else None
        below = False
        if following is not None:
            neighbour_top = min(following["caption"].bbox.y0, following["table_bbox"].y0)
            below = neighbour_top > box.y0
            if below:
                floor = min(floor, neighbour_top - _NEIGHBOUR_GAP_PT)
        top = max(box.y0, ceiling)
        limits.append((top, max(top + _MIN_H_PT, floor)))
        # Only a table that really is below this one starts where this one
        # ended. The predecessor advanced the ceiling unconditionally, so two
        # tables printed side by side pushed the second one's region down the
        # page: its image began below the first table's floor and held rows
        # neither table had.
        ceiling = floor + _NEIGHBOUR_GAP_PT if below else ceiling
    return limits


def _horizontal_rules(bbox, drawings) -> list[tuple[float, float, float]]:
    """The horizontal rules that run across ``bbox`` -- a table's own lines."""
    rules: list[tuple[float, float, float]] = []
    for drawing in drawings:
        for item in drawing.get("items", ()):
            if item[0] == "l":
                start, end = item[1], item[2]
                x0, x1 = sorted((start.x, end.x))
                overlap = min(x1, bbox.x1) - max(x0, bbox.x0)
                if abs(start.y - end.y) <= 1.0 and overlap >= bbox.width * 0.5:
                    if bbox.y0 - _RULE_MARGIN_PT <= start.y <= bbox.y1 + _RULE_MARGIN_PT:
                        rules.append((x0, start.y, x1))
            elif item[0] == "re":
                box = item[1]
                overlap = min(box.x1, bbox.x1) - max(box.x0, bbox.x0)
                if overlap >= bbox.width * 0.5 and box.y1 >= bbox.y0 and box.y0 <= bbox.y1:
                    rules.extend(((box.x0, box.y0, box.x1), (box.x0, box.y1, box.x1)))
    return rules


def _rule_bounded(page, bbox, drawings):
    """``bbox`` cut back to the rules the page drew, when it drew any."""
    rules = _horizontal_rules(bbox, drawings)
    if len(rules) < 2:
        return rect(bbox)
    return (
        rect(
            (
                min(rule[0] for rule in rules),
                min(rule[1] for rule in rules),
                max(rule[2] for rule in rules),
                max(rule[1] for rule in rules),
            )
        )
        & page.rect
    )


def _segment_text(page, bbox, fallback: str) -> str:
    """The text inside one slice of a merged region, or the whole markdown.

    A side-by-side split has to re-read its own half: the finder's markdown
    covers both tables, and handing each half the same rows would make one of
    them a misquote.
    """
    try:
        text = page.get_text("text", clip=bbox, sort=True).strip()
    except Exception:  # noqa: BLE001
        return fallback
    return text or fallback


def _footnote_bbox(text_blocks, table_bbox, caption_bbox):
    anchor_bottom = max(table_bbox.y1, caption_bbox.y1)
    matches = []
    for block in text_blocks:
        if len(block) < 5:
            continue
        text = str(block[4]).strip()
        bbox = rect(block[:4])
        if not _FOOTNOTE_RE.search(text):
            continue
        if bbox.y0 < table_bbox.y0 or bbox.y0 - anchor_bottom > _FOOTNOTE_MAX_GAP_PT:
            continue
        if horizontal_overlap(table_bbox, bbox) >= 0.25:
            matches.append(bbox)
    return union(matches)
