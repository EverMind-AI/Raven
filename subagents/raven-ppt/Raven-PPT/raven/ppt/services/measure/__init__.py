"""Measurement of a built deck. Everything here returns `Finding`.

Two ground truths, never merged:

* the .pptx's declared geometry, for shapes the renderer will not move -- a
  picture, a filled panel, a hairline, a table;
* the rendered PDF's word boxes, for text, which the renderer reflows, so its
  declared position is where its frame was put and not where it landed.

Nothing here decides whether a deck may be published; it decides what is true
about one. Every finding it produces is a WARNING, because every one of them is
satisfiable by shrinking the copy, and a gate that refused publication until
they cleared could be answered by making the page worse (design doc D2). The
gates that do refuse a deck are provenance and design gates, and they live in
`services.gates`.
"""

from raven.ppt.services.measure.content import (
    COLUMN_SQUEEZE,
    DIAGRAM_SHAPES,
    EVIDENCE_SHARE,
    SAFE_MARGIN_IN,
    evidence_coverage,
    native_tables,
    wide_tables,
)
from raven.ppt.services.measure.geometry import (
    EMU_PER_INCH,
    EMU_PER_POINT,
    Rect,
    deck_text,
    is_panel,
    iter_shapes,
    iter_text_frames,
    open_deck,
    page_paragraphs,
    pages,
    slide_count,
    text_boxes_emu,
)
from raven.ppt.services.measure.layout import (
    EDGE_SLACK_EMU,
    LABEL_SLACK,
    off_page_shapes,
    wrapped_labels,
)
from raven.ppt.services.measure.rendered import (
    CARD_SLOP_PT,
    COLLISION_SHARE,
    WORD_IN_CARD_SHARE,
    card_overflows,
    copy_boxes,
    cards,
    excessive_whitespace,
    hairline_rules,
    rule_strikes,
    word_collisions,
)
from raven.ppt.services.measure.type_size import (
    BODY_FLOOR_PT,
    MIN_FLOOR_PT,
    TypeCensus,
    census,
    scale_findings,
    type_findings,
    type_floors,
)
from raven.ppt.services.measure.width import DEFAULT_MEASURER, EstimatedWidth, WidthMeasurer
from raven.ppt.services.measure.words import WordBox, by_page, parse_bbox_xml, words_from_pdf

__all__ = [
    "slide_count",
    "BODY_FLOOR_PT",
    "CARD_SLOP_PT",
    "COLLISION_SHARE",
    "DEFAULT_MEASURER",
    "DIAGRAM_SHAPES",
    "EDGE_SLACK_EMU",
    "EMU_PER_INCH",
    "EMU_PER_POINT",
    "EVIDENCE_SHARE",
    "EstimatedWidth",
    "LABEL_SLACK",
    "COLUMN_SQUEEZE",
    "MIN_FLOOR_PT",
    "Rect",
    "SAFE_MARGIN_IN",
    "TypeCensus",
    "WORD_IN_CARD_SHARE",
    "WidthMeasurer",
    "WordBox",
    "by_page",
    "card_overflows",
    "copy_boxes",
    "cards",
    "census",
    "deck_text",
    "evidence_coverage",
    "excessive_whitespace",
    "hairline_rules",
    "is_panel",
    "iter_shapes",
    "iter_text_frames",
    "off_page_shapes",
    "native_tables",
    "open_deck",
    "page_paragraphs",
    "pages",
    "parse_bbox_xml",
    "rule_strikes",
    "scale_findings",
    "text_boxes_emu",
    "type_findings",
    "type_floors",
    "wide_tables",
    "word_collisions",
    "words_from_pdf",
    "wrapped_labels",
]
