"""One page through all three models, the way RAGFlow's PDF parser runs them.

The geometric parser beside this one infers what a region is from how it is
set: bigger than the body text means a heading, cells that line up mean a
table. That works on documents written to a template and degrades on
everything else -- a caption set in the body size is prose, a running header is
a paragraph, and a scanned page is nothing at all.

So this asks the models instead, in deepdoc's own order, each step depending on
the one before it:

1. the page is rendered at :data:`ZOOM`, because that is the scale the models
   were trained at and the scale their boxes are divided back down by;
2. text boxes are found -- from the file's own text layer where there is one,
   and from the detector and recogniser where there is not;
3. the layout model tags every box with the region it fell in, which is how a
   running header is told from a paragraph and a caption from prose;
4. every region the layout model called a table is cropped and handed to the
   table-structure model, and its rows are rebuilt from where the text sits
   inside them.

Step 2 is where this parts company with upstream, deliberately. RAGFlow runs
the detector on every page and matches the extracted characters into the boxes
it answers with, because it wants one code path whether or not the page has a
text layer. Here the text layer is used directly when there is one: it already
states where every glyph is, to the glyph, so detecting boxes around text we
can already read costs an inference pass per page to learn less than we started
with -- and recognising it costs accuracy, since the recogniser would be
reading pixels of text the file spells out exactly. The detector and the
recogniser run on the pages that have nothing to read, which is what they are
for.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
from loguru import logger

from raven.knowledge.parser import BBox, LayoutType
from raven.knowledge.parser._layout import Element
from raven.knowledge.parser.deepdoc._layout_recognizer import LayoutRecognizer
from raven.knowledge.parser.deepdoc._onnx import ModelsMissingError, available
from raven.knowledge.parser.deepdoc._tsr import TableStructureRecognizer

#: What the page is rendered at. Deepdoc's own factor: the models were trained
#: near this density, and every box they answer with is divided back down by it
#: so the rest of the package measures in the page's own points.
ZOOM = 3

#: Below this the layout model is guessing. Deepdoc's default.
THRESHOLD = 0.2

#: Points of slack around a table before it is cropped for the structure model,
#: so a ruling line or an outer row is not cut off by a box that ends on it.
MARGIN = 10

#: Characters on a page below which its text layer is not worth pouring into
#: the regions. The detector runs instead -- which is the scanned-page case,
#: and the case a few stray characters of watermark would otherwise hide.
_TEXT_LAYER_MIN = 24

#: How the layout model's ten classes map onto the layout vocabulary this
#: package records. Two collapse: a caption is a caption whichever thing it
#: captions, and an equation is set apart from prose but is prose.
_AS_LAYOUT = {
    "title": LayoutType.HEADING,
    "text": LayoutType.TEXT,
    "figure": LayoutType.FIGURE,
    "figure caption": LayoutType.CAPTION,
    "table": LayoutType.TABLE,
    "table caption": LayoutType.CAPTION,
    "table footnote": LayoutType.FOOTNOTE,
    "equation": LayoutType.TEXT,
    "equation caption": LayoutType.CAPTION,
}

_layout: LayoutRecognizer | None = None
_tables: TableStructureRecognizer | None = None
_ocr: Any = None


def usable() -> bool:
    """Whether the layout model is installed, which is what this needs at all.

    The other two are improvements on top of it: without the structure model a
    table is read as prose, and without the recogniser a scanned page falls
    through to whatever the caller does with a page it cannot read.
    """
    return available("layout")


def readable() -> bool:
    """Whether a page with no text layer can be read here."""
    return available("det", "rec")


def _layout_model() -> LayoutRecognizer:
    global _layout
    if _layout is None:
        _layout = LayoutRecognizer()
    return _layout


def _table_model() -> TableStructureRecognizer:
    global _tables
    if _tables is None:
        _tables = TableStructureRecognizer()
    return _tables


def _ocr_model() -> Any:
    global _ocr
    if _ocr is None:
        from raven.knowledge.parser.deepdoc._ocr import OCR

        _ocr = OCR()
    return _ocr


def render(page: Any) -> np.ndarray:
    """The page as the models want it: RGB, at :data:`ZOOM`.

    From the pixmap's own buffer rather than through an encode and decode.
    PyMuPDF hands out RGB, which is what these graphs were trained on, so
    there is no channel swap here -- see the note in ``_recognizer.preprocess``
    about what one costs.
    """
    import pymupdf

    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False)
    return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)


def text_boxes(page: Any, image: np.ndarray, number: int) -> "tuple[list[dict], bool]":
    """Every run of text on the page, and whether it was recognised rather than read.

    In the page's own points, as the models' boxes are once they come back, so
    a region and a line of text can be compared without a scale factor between
    them.
    """
    found = _from_text_layer(page, number)
    if sum(len(box["text"]) for box in found) >= _TEXT_LAYER_MIN:
        return found, False
    return _from_ocr(image, number), True


def regions(
    page: Any, number: int, *, image: np.ndarray | None = None, boxes: "list[dict] | None" = None
) -> "list[dict]":
    """What the layout model says the regions of one page are, in reading order.

    Coordinates come back in the page's own points rather than the render's
    pixels, so everything downstream measures in one unit. The text boxes are
    tagged in place with the region each fell into, which is why this takes
    them: asked without any, the model still finds the regions but its own
    cleanup pass has nothing to check them against.
    """
    if image is None:
        image = render(page)
    tagged, layouts = _layout_model()([image], [boxes if boxes is not None else []], scale_factor=ZOOM, thr=THRESHOLD)
    found = layouts[0] if layouts else []
    for region in found:
        region["page_number"] = number
    if boxes is not None:
        boxes[:] = tagged
    return found


def elements(page: Any, number: int, *, ocr: bool = True) -> "list[Element] | None":
    """One page as elements, or ``None`` when nothing here could read it.

    ``None`` means "do what you would have done without any of this": the page
    is blank, the models found nothing, or one of them failed on it. A page
    failing is not a document failing.

    ``ocr`` false keeps the detector and the recogniser out of it, so a page
    with no text layer comes back as ``None`` and the caller's own fallback --
    the vision model -- reads it instead.
    """
    try:
        return _read(page, number, ocr=ocr)
    except ModelsMissingError:
        raise
    except Exception as exc:  # noqa: BLE001 - one page, not the document
        logger.warning("deepdoc: page {} could not be read by the models ({}); reading it geometrically", number, exc)
        return None


def _read(page: Any, number: int, *, ocr: bool) -> "list[Element] | None":
    image = render(page)
    boxes, recognised = text_boxes(page, image, number)
    if recognised and not ocr:
        return None
    if not boxes:
        return None

    found = regions(page, number, image=image, boxes=boxes)
    if not found:
        return None

    tables = _table_text(image, found, boxes)
    built: list[Element] = []
    for index, region in enumerate(found):
        kind = str(region.get("type") or "").lower()
        if kind == "abandon":
            # A running header, a footer, a page number. Dropped rather than
            # indexed: it repeats on every page, and a search that matches it
            # answers with the furniture of the document rather than with the
            # document. This is the clearest thing the layout model buys.
            continue
        layout = _AS_LAYOUT.get(kind, LayoutType.TEXT)
        # A table falls back to its own text when the structure model is not
        # installed or could not read it. Prose is worse than a grid and much
        # better than dropping the region, which is a table missing from the
        # document with nothing to say it was ever there.
        text = tables.get(index) or _text_of(region, boxes)
        if not text and layout is not LayoutType.FIGURE:
            continue
        built.append(
            Element(
                layout=layout,
                text=text or "",
                page_number=number,
                page_end=number,
                bbox=BBox(x0=region["x0"], x1=region["x1"], top=region["top"], bottom=region["bottom"]),
                heading_level=1 if layout is LayoutType.HEADING else None,
            )
        )
    return built or None


def _text_of(region: dict, boxes: "list[dict]") -> str:
    """The text of every box whose middle falls inside a region.

    By the middle rather than by containment: a region's box comes from a model
    looking at pixels and a line's from the glyphs themselves, so the two agree
    on where a paragraph is and not on its exact edge. Asking for containment
    loses the line that sticks out by a point.
    """
    inside = [box["text"] for box in boxes if _within(box, region)]
    return " ".join(inside).strip()


def _table_text(image: np.ndarray, found: "list[dict]", boxes: "list[dict]") -> "dict[int, str]":
    """Every table region's rows, composed so each carries its column names.

    The structure model reads the crop and the text comes from the boxes that
    fall inside it, which is the point of running it at all: a table ruled with
    whitespace rather than lines is one PyMuPDF's own finder cannot see, and in
    a paper those are most of the tables.
    """
    wanted = [(index, region) for index, region in enumerate(found) if str(region.get("type") or "") == "table"]
    if not wanted or not available("tsr"):
        return {}

    crops, spans = [], []
    height, width = image.shape[:2]
    for index, region in wanted:
        left = max(0, int((region["x0"] - MARGIN) * ZOOM))
        top = max(0, int((region["top"] - MARGIN) * ZOOM))
        right = min(width, int((region["x1"] + MARGIN) * ZOOM))
        bottom = min(height, int((region["bottom"] + MARGIN) * ZOOM))
        if right <= left or bottom <= top:
            continue
        crops.append(image[top:bottom, left:right])
        spans.append((index, region))
    if not crops:
        return {}

    try:
        structures = _table_model()(crops)
    except Exception as exc:  # noqa: BLE001 - the table is still readable as prose
        logger.warning("deepdoc: table structure recognition failed ({}); reading the tables as prose", exc)
        return {}

    out: dict[int, str] = {}
    for (index, region), structure in zip(spans, structures, strict=False):
        cells = [dict(box) for box in boxes if _within(box, region)]
        if not cells:
            continue
        text = _as_html(structure, cells, region)
        if text:
            out[index] = text
    return out


def _within(box: dict, region: dict) -> bool:
    x = (box["x0"] + box["x1"]) / 2
    y = (box["top"] + box["bottom"]) / 2
    return region["x0"] <= x <= region["x1"] and region["top"] <= y <= region["bottom"]


def _as_html(structure: "list[dict]", cells: "list[dict]", region: dict) -> str:
    """One table region as an HTML table, spans and header rows and all.

    A grid is not a list of lines, and the two lossy shapes a parser can reach
    for both throw away the half that makes a table answerable. Flattened to
    prose, a cell loses the column it was under. Flattened to one row per line,
    a cell that spans three columns is either repeated three times or dropped,
    and a header that sits over a group of columns has nowhere to go at all.
    `<table>` is the shape that holds all of it -- and it is what a language
    model reads a table as, which is what these chunks are for.

    The composition itself is RAGFlow's ``construct_table``: which rows are
    headers, which cells span, and the HTML around them. What happens here is
    the tagging it runs on -- the port of ``_table_transformer_job`` -- which
    is where the structure model's rows, columns, headers and spanning cells
    are matched against the text boxes that fall inside them.
    """
    from raven.knowledge.parser.deepdoc._recognizer import Recognizer

    parts = _in_points(structure, region)
    rows = _gathered(cells, parts, r".* (row|header)$")
    headers = _gathered(cells, parts, r".*header$")
    spanning = _gathered(cells, parts, r".*spanning")
    columns = Recognizer.layouts_cleanup(
        cells, sorted([part for part in parts if part["label"] == "table column"], key=lambda c: c["x0"]), 5, 0.5
    )
    if not rows or not columns:
        return ""

    for cell in cells:
        found = Recognizer.find_overlapped_with_threshold(cell, rows, thr=0.3)
        if found is not None:
            cell["R"], cell["R_top"], cell["R_bott"] = found, rows[found]["top"], rows[found]["bottom"]
        found = Recognizer.find_overlapped_with_threshold(cell, headers, thr=0.3)
        if found is not None:
            cell["H"] = found
            cell["H_top"], cell["H_bott"] = headers[found]["top"], headers[found]["bottom"]
            cell["H_left"], cell["H_right"] = headers[found]["x0"], headers[found]["x1"]
        found = Recognizer.find_horizontally_tightest_fit(cell, columns)
        if found is not None:
            cell["C"], cell["C_left"], cell["C_right"] = found, columns[found]["x0"], columns[found]["x1"]
        found = Recognizer.find_overlapped_with_threshold(cell, spanning, thr=0.3)
        if found is not None:
            cell["H_top"], cell["H_bott"] = spanning[found]["top"], spanning[found]["bottom"]
            cell["H_left"], cell["H_right"] = spanning[found]["x0"], spanning[found]["x1"]
            cell["SP"] = found

    built = TableStructureRecognizer.construct_table(cells, is_english=_is_english(cells), html=True)
    return built if isinstance(built, str) else ""


def _in_points(structure: "list[dict]", region: dict) -> "list[dict]":
    """The structure model's boxes in the page's points rather than the crop's.

    It was handed a crop taken at :data:`ZOOM` from :data:`MARGIN` points
    outside the region, and it answers in that crop's own pixels. Everything it
    is about to be compared against -- the text boxes -- is in points, so the
    conversion happens once, here.
    """
    left, top = region["x0"] - MARGIN, region["top"] - MARGIN
    return [
        {
            "label": part["label"],
            "score": part["score"],
            "x0": left + part["x0"] / ZOOM,
            "x1": left + part["x1"] / ZOOM,
            "top": top + part["top"] / ZOOM,
            "bottom": top + part["bottom"] / ZOOM,
        }
        for part in structure
    ]


def _gathered(cells: "list[dict]", parts: "list[dict]", pattern: str) -> "list[dict]":
    """One kind of structure component, sorted down the page and cleaned up.

    RAGFlow's ``gather``. The cleanup pass is what the text boxes are for: a
    component the model drew where no text fell is one it invented, and a row
    that overlaps its neighbour is two readings of one row.
    """
    from raven.knowledge.parser.deepdoc._recognizer import Recognizer

    found = Recognizer.sort_Y_firstly([part for part in parts if re.match(pattern, part["label"])], 10)
    return Recognizer.sort_Y_firstly(Recognizer.layouts_cleanup(cells, found, 5, 0.6), 0)


def _is_english(cells: "list[dict]") -> bool:
    """Whether this table reads as English, for how its caption is joined.

    RAGFlow decides this for a whole document by counting how many of its lines
    look like English sentences. One table is a much smaller sample, so the
    test is looser: most of what is in the cells being latin letters and
    digits. It reaches only the caption's spacing in the HTML path -- a Chinese
    caption takes no space between its runs, an English one does.
    """
    text = "".join(cell.get("text") or "" for cell in cells)
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return True
    return sum(character.isascii() for character in letters) / len(letters) > 0.5


def _from_text_layer(page: Any, number: int) -> "list[dict]":
    """The lines the file itself states, with their boxes."""
    found = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") == 1:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text") or "" for span in line.get("spans", [])).strip()
            if not text:
                continue
            x0, top, x1, bottom = line["bbox"]
            found.append(
                {
                    "x0": float(x0),
                    "x1": float(x1),
                    "top": float(top),
                    "bottom": float(bottom),
                    "text": text,
                    "page_number": number,
                }
            )
    return found


def _from_ocr(image: np.ndarray, number: int) -> "list[dict]":
    """What the detector and the recogniser find on a page with nothing to read."""
    found = []
    for quad, (text, score) in _ocr_model()(image):
        if not text.strip():
            continue
        xs = [float(point[0]) / ZOOM for point in quad]
        ys = [float(point[1]) / ZOOM for point in quad]
        found.append(
            {
                "x0": min(xs),
                "x1": max(xs),
                "top": min(ys),
                "bottom": max(ys),
                "text": text.strip(),
                "score": float(score),
                "page_number": number,
            }
        )
    return found


__all__ = ["MARGIN", "THRESHOLD", "ZOOM", "elements", "readable", "regions", "render", "text_boxes", "usable"]
