"""Reading "Figure 3: ..." off a page and deciding which picture it names.

Extracting these was the missing half of figure ingestion. A deck whose
figures arrive unlabelled has nothing tying one to the claim it is evidence
for, so the model numbers them by guessing: real runs cited "Fig. 4" under a
page showing Fig. 5, and used a paper's own teaser figure to argue the
opposite of what it shows. Neither is catchable afterwards -- the label has to
come off the page, at extraction time, for every path that emits a figure.

Geometry only, no pixels: a caption belongs to the thing it sits over or under
and shares columns with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from raven.ppt.contracts.sources import AssetKind
from raven.ppt.services.ingest.geometry import horizontal_overlap, rect, vertical_gap

_LABEL_RE = {
    AssetKind.TABLE: re.compile(r"(?im)^\s*((?:table\s+|表\s*)(?:[A-Z]?\d+(?:[.-]\d+)?|[IVXLC]+))"),
    AssetKind.FIGURE: re.compile(r"(?im)^\s*((?:figures?\s+|fig\.?\s*|图\s*)(?:[A-Z]?\d+(?:[.-]\d+)?|[IVXLC]+))"),
}

# A figure's caption sits under it, a table's above it -- the usual convention
# in a paper, and only a preference here: either side is accepted when the
# preferred one holds nothing, because a two-column paper breaks it often.
_BELOW = {AssetKind.FIGURE: True, AssetKind.TABLE: False}

# "Table 3 shows that ..." is a sentence in the body, not the caption of the
# table beside it, and matching it puts a paragraph in the caption field.
_PROSE_AFTER_LABEL_RE = re.compile(
    r"(?i)^(?:shows?|reports?|summari[sz]es?|lists?|compares?|presents?|provides?|contains?|"
    r"demonstrates?|indicates?|is|are)\b|^(?:显示|给出|报告|总结|列出|比较|展示|说明|包含|为)"
)

# How far a caption may sit from the thing it names, and how much closer a
# rival has to be to win outright.
_MAX_GAP_PT = 90.0
_TIE_PT = 16.0
# Below this a caption sits beside the figure rather than over it, which in a
# two-column paper means it belongs to the other column.
_MIN_COLUMN_OVERLAP = 0.25


@dataclass(frozen=True)
class Caption:
    """One caption line on a page: its canonical label, its text, where it is."""

    label: str
    text: str
    bbox: Any  # fitz.Rect

    @property
    def prose(self) -> str:
        return re.sub(r"\s+", " ", self.text).strip()


def canonical_label(raw: str, kind: AssetKind) -> str:
    """`fig. 4`, `Figure  4` and `图 4` are one label; say it one way."""
    collapsed = re.sub(r"\s+", " ", raw).strip()
    number = re.sub(r"(?i)^(?:tables?|figures?|fig\.?|表|图)\s*", "", collapsed).strip(" .:：")
    return f"{'Table' if kind is AssetKind.TABLE else 'Figure'} {number}"


def caption_match(text: str, kind: AssetKind) -> re.Match[str] | None:
    """The label match in ``text``, unless the line reads as prose about it."""
    for match in _LABEL_RE[kind].finditer(text):
        line_tail = text[match.end() :].splitlines()[0].strip()
        if not line_tail or line_tail[0] in ".:：":
            return match
        if _PROSE_AFTER_LABEL_RE.search(line_tail) is None:
            return match
    return None


def caption_blocks(text_blocks, kind: AssetKind) -> list[Caption]:
    """Every text block on the page that opens with a `Figure N` / `Table N` label."""
    captions: list[Caption] = []
    for block in text_blocks:
        if len(block) < 5:
            continue
        text = str(block[4]).strip()
        match = caption_match(text, kind)
        if match is None:
            continue
        captions.append(Caption(canonical_label(match.group(1), kind), text, rect(block[:4])))
    return captions


def page_captions(page, kind: AssetKind = AssetKind.FIGURE) -> list[Caption]:
    """Best effort: a page whose text will not parse simply has no captions."""
    try:
        blocks = page.get_text("blocks", sort=True)
    except Exception:  # noqa: BLE001 -- one unreadable page must not fail the ingest
        return []
    return caption_blocks(blocks, kind)


def caption_for(bbox, captions: list[Caption], kind: AssetKind) -> Caption | None:
    """The caption belonging to the thing at ``bbox``, or None.

    Nearest by vertical gap among the captions that sit over the same columns,
    preferring the side the convention puts it on and accepting the other side
    when that one is empty.
    """
    candidates = _in_reach(bbox, captions)
    if not candidates:
        return None
    below = _BELOW.get(kind, True)
    preferred = [
        caption for caption in candidates if (caption.bbox.y0 >= bbox.y1 - 1) is below and _outside(bbox, caption)
    ]
    pool = preferred or [caption for caption in candidates if _outside(bbox, caption)] or candidates
    return min(pool, key=lambda caption: vertical_gap(bbox, caption.bbox))


def nearest_captions(bbox, captions: list[Caption]) -> list[Caption]:
    """Every caption tied for nearest to ``bbox``, in reading order.

    A table region that two captions are equally close to is two tables the
    detector merged, and the pair is what tells them apart -- so this keeps
    them all rather than picking one.
    """
    reachable = [(vertical_gap(bbox, caption.bbox), caption) for caption in _in_reach(bbox, captions)]
    if not reachable:
        return []
    closest = min(gap for gap, _caption in reachable)
    nearest = [caption for gap, caption in reachable if gap <= closest + _TIE_PT]
    by_label = {caption.label: caption for caption in nearest}
    return sorted(by_label.values(), key=lambda caption: (caption.bbox.y0, caption.bbox.x0))


def _in_reach(bbox, captions: list[Caption]) -> list[Caption]:
    return [
        caption
        for caption in captions
        if horizontal_overlap(bbox, caption.bbox) >= _MIN_COLUMN_OVERLAP
        and vertical_gap(bbox, caption.bbox) <= _MAX_GAP_PT
    ]


def _outside(bbox, caption: Caption) -> bool:
    """A caption inside the region is part of the picture, not its caption."""
    return caption.bbox.y0 >= bbox.y1 - 1 or caption.bbox.y1 <= bbox.y0 + 1
