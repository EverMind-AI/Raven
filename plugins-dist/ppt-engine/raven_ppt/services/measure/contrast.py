"""Whether the words can be read off the ground they were rendered onto.

Every other check here asks about geometry -- is the box big enough, do two boxes
overlap, is the type above the floor. A live deck cleared all of them and still had
pages a reader could not use: its page 9 set the title in #1A1A1A on #000000, and four
of its labels in #1A1A1A on #1A1A1A, the same colour exactly. Page 12's title was
#1A1A1A on black too. Nothing measured it.

The cause is one confusion, and naming it is most of the fix. A deck's theme carries
`background`, `surface` and `foreground`; on a dark template those are black, near-black
and white. An author reaching for "the dark one" for its type picks `surface` -- which is
what the ground is painted with -- and the page comes out with black text on black.

Measured against the render, because that is the only place the ground has an answer:
what sits behind a text box is a layout's artwork, a photograph, a panel three shapes
down, or the master's gradient, and resolving that stack by hand reproduces the renderer.
The declared ink is read off the file, where it is stated exactly; the ground is the
modal pixel under the box, where the renderer has already resolved everything.

An earlier version segmented the crop into ink and ground by quantile and reported the
ratio between them. It flagged every page, and what it flagged were the commas: a `·` is
a few pixels wide, its crop is nearly all ground, and both quantiles land on the same
colour -- 1.0:1 meaning "no glyph here", not "unreadable".

Each finding also says who drew the shape it measured. "It came from the template's own
page, so it is the template's choice" is how a live author waved away a finding on six
chevrons its own program drew, and a refusal is where that excuse costs the most: which of
the two files the fix belongs in is a question only the files can answer. So they answer
it: cloning copies a prototype's shape positions exactly, so a shape sitting where a
prototype puts one, on a page that is that prototype's clone, is the template's, and a
shape sitting nowhere any prototype puts one is the program's. With no template to compare
against, the finding says nothing about it rather than guessing.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from raven_ppt.contracts.findings import Finding, Severity

# Who drew a shape is `adherence`'s question and `adherence`'s answer: a cloned page
# keeps its prototype's shape positions exactly, and the tolerance that reads as "the
# same box" took three live decks to calibrate. Private and imported anyway, the way
# `type_size` already imports `_matches` -- a second way of deciding whether a shape is
# the template's is how the two come to disagree.
from raven_ppt.services.measure.adherence import MIN_SHAPES, _matches, _nearest, _pages
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    iter_shapes,
    open_deck,
    page_box,
    shape_rect_pt,
)
from raven_ppt.services.measure.type_size import _inches, _template_boxes

# WCAG AA asks 4.5:1 for body copy and 3:1 for large text, and 3:1 applied to everything
# is where this started. It refused a deck for its own template's design: the agenda page
# every deck is required to clone sets white numerals on the template's orange circles,
# which measures 2.5:1 and is perfectly readable -- a live run rebuilt that page five
# times and could not publish, because the only fix was to break the template.
#
# That was answered by splitting the check in two -- 2:1 refusing, and a 3:1 warning
# above it for type that is legible but thin -- and the warning is gone as well now.
# Measured, the 2-to-3 band is where template design lives rather than where mistakes do.
# The bound template's own theme puts these ratios between its own roles:
#
#   #FCFCFC on #50BBB6  ->  2.24:1   background on accent
#   #FCFCFC on #EDF6F6  ->  1.07:1   background on surface, which is no text at all
#
# and its own eleven example pages, rendered and measured, came back with four warnings
# around 2.3:1, every one of them a shape its designer drew. On one run an authored shape
# measured 2.24:1 and a template-drawn one 2.30:1, so no threshold inside the band
# separates the two and only provenance does. The cost was never the noise: a live author
# met a real 2.2:1 finding with "the template's own accent1 color relationship, acceptable
# per spec note" -- about six chevrons its own program drew. A category that is usually
# wrong is what teaches that answer, so do not re-add it at a lower number.
#
# What is left is the case no design argues with:
#
#   #1A1A1A on #000000  ->  1.2:1   invisible; the case this check was built for
#
# Under 2:1 nothing is legible and the deck is refused. Above it this file says nothing.
UNREADABLE_RATIO = 2.0
# Below this the crop cannot say what its ground is.
_MIN_PIXELS = 24
# A run that is one mark and nothing else. Measured over eight delivered decks, about
# 160 pages: this check found three things, two of them right and the third a single
# bullet at 1.8:1 -- which refused the whole deck. A dim bullet is not worth refusing a
# deck over, and it is the same shape as the failure this file was already rewritten once
# for, when quantile segmentation reported every comma.
_A_MARK = frozenset("\u2022\u00b7\u25cf\u25aa\u2013\u2014-\u2192\u2713\u2715\u00d7|/\\.,;:!?")


def _is_copy(text: str) -> bool:
    """Whether the run that measured badly is copy rather than one mark."""
    return bool(set(str(text).strip()) - _A_MARK)


_TEMPLATE_DREW = "template"
_AUTHOR_DREW = "authored"
# The two halves of the fact, spelled so neither can be read as the other. Each states
# what settles it, because "the template's" is the claim an author reaches for and a
# claim with its evidence attached is one they can check.
_DREW = {
    _TEMPLATE_DREW: (
        "this shape is the template's own -- the page is a clone of the template's page {prototype}, "
        "and the shape sits where that page puts one"
    ),
    _AUTHOR_DREW: "this shape is your own program's -- it sits nowhere any of the template's own pages puts one",
}


# The deck's shapes by page, the template's by page, and every box the template puts
# a shape at -- read once, because each of the three costs opening a deck.
_Origins = tuple[dict[int, set], dict[int, set], set]


def _origins(pptx_path: Path, prototypes: Path | None) -> _Origins | None:
    """What is needed to say who drew a shape, or None when nothing can be said.

    `prototypes` is the user's template as handed over, example pages included -- not
    the prepared copy the build opens, which has those pages removed and so holds no
    page a shape could have been cloned from. An adherence check wired on the prepared
    copy found nothing to compare and reported that every deck was fine; here the same
    mistake would report that every shape is the author's.
    """
    if prototypes is None or not Path(prototypes).is_file():
        return None
    everywhere = _template_boxes(prototypes)
    by_page = _pages(Path(prototypes))
    if not everywhere or not by_page:
        return None
    return _pages(pptx_path), by_page, everywhere


def _drawn_by(shape: Any, number: int, origins: _Origins | None) -> tuple[str | None, int | None]:
    """Who drew this shape: `_TEMPLATE_DREW`, `_AUTHOR_DREW`, or None when it cannot say.

    Two gates rather than one, because a single one answers wrongly in both directions.
    A shape matching a prototype's box is not enough on its own -- a full-bleed panel
    sits where every template puts one -- so the page has to be that prototype's clone
    as well. And a shape on a cloned page that matches nothing the template ships is one
    the program added on top, which is the `template_underlay` shape exactly.
    """
    if origins is None:
        return None, None
    built, by_page, everywhere = origins
    if not _matches(_inches(shape_rect_pt(shape)), everywhere):
        return _AUTHOR_DREW, None
    shapes = built.get(number) or set()
    # Under this many shapes a page is a divider or a quote and a match is coincidence,
    # which is the reason `adherence` refuses to read a page this sparse at all.
    if len(shapes) < MIN_SHAPES:
        return None, None
    home = _nearest(shapes, by_page)
    if home is None:
        return None, None
    return _TEMPLATE_DREW, home[0]


_HEAD = 30


def contrast_findings(
    pptx_path: Path,
    pdf_path: Path | None,
    dpi: int = 72,
    pages: list[Path] | None = None,
    prototypes: Path | None = None,
) -> list[Finding]:
    """Text whose declared colour is too close to the ground it landed on.

    `pages` are rendered pages when the caller already has them, which is also how a
    test hands over a page without a renderer on the machine.

    `prototypes` is the user's template as handed over, which is what lets the finding
    name who drew the shape it measured. Without it the finding says nothing about that
    -- an absent input is not a defect, and a guess here is what the excuse this answers
    was claimed on in the first place.
    """
    if pages is None and (pdf_path is None or not Path(pdf_path).is_file()):
        return []
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover -- Pillow ships with the route
        return []
    from raven_ppt.services.render import pdf as pdf_render  # noqa: F401 -- used when pages is None

    if pages is None:
        try:
            pages = pdf_render.to_pngs(Path(pdf_path), Path(pdf_path).parent, dpi=dpi)
        except Exception:  # noqa: BLE001 -- no render, no measurement
            return []
    presentation = open_deck(pptx_path)
    canvas_w = (presentation.slide_width or 1) / EMU_PER_INCH
    canvas_h = (presentation.slide_height or 1) / EMU_PER_INCH
    origins = _origins(pptx_path, prototypes)

    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        png = next((p for p in pages if f"{number:03d}" in p.name), None)
        if png is None:
            continue
        with Image.open(png) as opened:
            image = opened.convert("RGB")
            worst: tuple[float, str, str, tuple[int, int, int], Any] | None = None
            count = 0
            for shape in iter_shapes(slide.shapes):
                measured = _measure(shape, image, canvas_w, canvas_h)
                if measured is None:
                    continue
                ratio, text, ink, ground = measured
                if ratio >= UNREADABLE_RATIO:
                    continue
                # Asked here rather than of the page's worst block. A mark can measure
                # worse than any copy on the page -- a bullet at 1.0:1 beside a title at
                # 1.5:1 -- and filtering afterwards let that mark be chosen as `worst`
                # and then take the whole page's refusal down with it. What is refused
                # is unreadable copy, so a mark is not a candidate for it at all, and
                # `count` is the copy this page carries and not every dim thing on it.
                if not _is_copy(text):
                    continue
                count += 1
                if worst is None or ratio < worst[0]:
                    worst = (ratio, text, ink, ground, shape)
        if worst is None:
            continue
        ratio, text, ink, ground, shape = worst
        drew, prototype = _drawn_by(shape, number, origins)
        clause = "" if drew is None else _DREW[drew].format(prototype=prototype)
        others = f" and {count - 1} more block(s) on the page" if count > 1 else ""
        detail: dict[str, Any] = {"ratio": round(ratio, 2), "blocks": count, "ink": ink, "text": text[:60]}
        if drew is not None:
            detail["drawn_by"] = drew
        if prototype is not None:
            detail["prototype"] = prototype
        findings.append(
            Finding(
                kind="unreadable",
                severity=Severity.BLOCKING,
                page=number,
                message=(
                    f"'{text[:_HEAD]}' is set in #{ink} on a ground that renders "
                    f"#{'%02X%02X%02X' % ground}{others} -- {ratio:.1f}:1, "
                    f"under the {UNREADABLE_RATIO:g}:1 at which the characters stop being there at all. On a "
                    f"dark deck the type colour is the theme's `foreground`; `surface` and `background` are "
                    f"what the ground is painted with, and reaching for one of those gives you black on black"
                    + (f". And {clause}" if clause else "")
                ),
                detail=detail,
            )
        )
    return findings


def _measure(shape: Any, image: Any, canvas_w: float, canvas_h: float):
    """(ratio, text, ink hex, ground rgb) for one text block, or None when it cannot say."""
    if not getattr(shape, "has_text_frame", False):
        return None
    text = " ".join(shape.text_frame.text.split())
    if not text:
        return None
    ink = _declared(shape.text_frame)
    if ink is None:
        return None  # inherited from the theme or the layout: not stated here, not judged here
    # Through the groups above it, or the crop lands somewhere else on the page and
    # the ground this reads is not the ground the words sit on.
    where = page_box(shape)
    if where is None:
        return None
    left = int(where.x0 / canvas_w * image.width)
    top = int(where.y0 / canvas_h * image.height)
    right = int(where.x1 / canvas_w * image.width)
    bottom = int(where.y1 / canvas_h * image.height)
    crop = image.crop((max(left, 0), max(top, 0), min(right, image.width), min(bottom, image.height)))
    if crop.width < 2 or crop.height < 2:
        return None
    pixels = list(crop.getdata())
    if len(pixels) < _MIN_PIXELS:
        return None
    ground = Counter(pixels).most_common(1)[0][0]
    return _ratio(_rgb(ink), ground), text, ink, ground


def _declared(frame: Any) -> str | None:
    """The colour the file states for this text, or None when it inherits one.

    The largest run wins where a block mixes them: a heading with one accented word is
    judged on the heading.
    """
    weighed: Counter = Counter()
    for para in frame.paragraphs:
        for run in para.runs:
            if not run.text.strip():
                continue
            colour = run.font.color
            try:
                if colour is None or colour.type is None or colour.rgb is None:
                    continue
                weighed[str(colour.rgb).upper()] += len(run.text)
            except (AttributeError, TypeError, ValueError):
                continue
    if not weighed:
        return None
    return weighed.most_common(1)[0][0]


def _rgb(value: str) -> tuple[int, int, int]:
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _ratio(ink: tuple[int, int, int], ground: tuple[int, int, int]) -> float:
    first, second = _luminance(ink), _luminance(ground)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(colour: tuple[int, int, int]) -> float:
    """WCAG relative luminance."""
    channels = []
    for value in colour:
        share = value / 255
        channels.append(share / 12.92 if share <= 0.04045 else ((share + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
