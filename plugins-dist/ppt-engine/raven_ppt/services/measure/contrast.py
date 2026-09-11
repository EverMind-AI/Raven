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

import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.contracts.rendered import WordBox

# Who drew a shape is `adherence`'s question and `adherence`'s answer: a cloned page
# keeps its prototype's shape positions exactly, and the tolerance that reads as "the
# same box" took three live decks to calibrate. Private and imported anyway, the way
# `type_size` already imports `_matches` -- a second way of deciding whether a shape is
# the template's is how the two come to disagree.
from raven_ppt.services.measure.adherence import MIN_SHAPES, _matches, _nearest, _pages
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    ink_box,
    iter_shapes,
    open_deck,
    shape_rect_pt,
)
from raven_ppt.services.measure.type_size import _inches, _template_boxes
from raven_ppt.services.measure.words import by_page
from raven_ppt.services.template.decompile import inherited_ink, page_design, run_ink

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
# A shape the template drew, holding the template's own colours, is the designer's
# decision: the bundled beige and mint templates set white numerals on yellow chevrons
# at 1.4 to 1.9 against 1, and every deck cloning those pages was refused for it. Above
# this floor a template-drawn block is reported and not refused; under it -- the same
# colour on the same colour -- nothing was designed and the refusal stands.
TEMPLATE_OWN_FLOOR = 1.3
# Below this the crop cannot say what its ground is.
_MIN_PIXELS = 24
# A run that is one mark and nothing else. Measured over eight delivered decks, about
# 160 pages: this check found three things, two of them right and the third a single
# bullet at 1.8:1 -- which refused the whole deck. A dim bullet is not worth refusing a
# deck over, and it is the same shape as the failure this file was already rewritten once
# for, when quantile segmentation reported every comma.
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_A_MARK = frozenset("\u2022\u00b7\u25cf\u25aa\u2013\u2014-\u2192\u2713\u2715\u00d7|/\\.,;:!?")


def _is_copy(text: str) -> bool:
    """Whether the run that measured badly is copy rather than one mark.

    One character is a mark whatever it is: the numerals a template sets beside a list
    in its accent colour read at 1.8:1 by design, and measuring them as copy called
    four of one template's own pages unreadable.
    """
    stripped = str(text).strip()
    return len(stripped) > 1 and bool(set(stripped) - _A_MARK)


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
    words: Sequence[WordBox] | None = None,
) -> list[Finding]:
    """Text whose declared colour is too close to the ground it landed on.

    `pages` are rendered pages when the caller already has them, which is also how a
    test hands over a page without a renderer on the machine.

    `words` are the render's own word boxes, which give the reading a region the type
    fills rather than one the file declares -- see `_word_slices`. Without them the
    declared box is all there is, which is what this measured before.

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
    rendered = by_page(words) if words else {}

    findings: list[Finding] = []
    for number, slide in enumerate(presentation.slides, start=1):
        png = next((p for p in pages if f"{number:03d}" in p.name), None)
        if png is None:
            continue
        with Image.open(png) as opened:
            image = opened.convert("RGB")
            worst: tuple[float, str, str, tuple[int, int, int], Any, str | None] | None = None
            count = 0
            design = page_design(presentation, slide)
            for shape in iter_shapes(slide.shapes):
                measured = _measure(shape, image, canvas_w, canvas_h, design, rendered.get(number))
                if measured is None:
                    continue
                ratio, text, ink, ground, under = measured
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
                    worst = (ratio, text, ink, ground, shape, under)
        if worst is None:
            continue
        ratio, text, ink, ground, shape, under = worst
        drew, prototype = _drawn_by(shape, number, origins)
        clause = "" if drew is None else _DREW[drew].format(prototype=prototype)
        others = f" and {count - 1} more block(s) on the page" if count > 1 else ""
        detail: dict[str, Any] = {"ratio": round(ratio, 2), "blocks": count, "ink": ink, "text": text[:60]}
        if under is not None:
            detail["word"] = under
        if drew is not None:
            detail["drawn_by"] = drew
        if prototype is not None:
            detail["prototype"] = prototype
        designed = drew == _TEMPLATE_DREW and ratio >= TEMPLATE_OWN_FLOOR
        findings.append(
            Finding(
                kind="unreadable",
                severity=Severity.WARNING if designed else Severity.BLOCKING,
                page=number,
                message=(
                    f"'{text[:_HEAD]}' is set in #{ink} on a ground that renders "
                    f"#{'%02X%02X%02X' % ground}"
                    + (f", under the word '{under}'" if under else "")
                    + f"{others} -- {ratio:.1f}:1, "
                    f"under the {UNREADABLE_RATIO:g}:1 at which the characters stop being there at all. On a "
                    f"dark deck the type colour is the theme's `foreground`; `surface` and `background` are "
                    f"what the ground is painted with, and reaching for one of those gives you black on black"
                    + (f". And {clause}" if clause else "")
                    + (
                        ". The template's designer set it this way -- a numeral on its own chevron, a label in "
                        "its own tint -- so this is a report and not a refusal; recolour it or leave it"
                        if designed
                        else ""
                    )
                ),
                detail=detail,
            )
        )
    return findings


def _measure(
    shape: Any,
    image: Any,
    canvas_w: float,
    canvas_h: float,
    design: Any = None,
    words: Sequence[WordBox] | None = None,
):
    """(ratio, text, ink hex, ground rgb, the word read under) for one block, or None.

    The last element is the word whose own box gave the reading, or None when the
    declared box did -- which is every block the render offered no word for.
    """
    if not getattr(shape, "has_text_frame", False):
        return None
    text = " ".join(shape.text_frame.text.split())
    if not text:
        return None
    stated_rgb = _declared(shape.text_frame)
    ink = stated_rgb or _declared(shape.text_frame, design)
    if ink is None and _states_a_fill(shape.text_frame):
        # A gradient or picture fill on the type: stated on the page, so the master's
        # colour is not what the reader sees, and there is no one colour to judge.
        return None
    if ink is None and design is not None:
        # Stated nowhere on the page: the layout's placeholder or the master says what
        # the type is, and that is what the reader sees. Resolved the way the reference
        # resolves it -- a cloned page states almost nothing itself, and skipping it
        # left a 1.09:1 body line unjudged on a measured deck.
        ink = inherited_ink(shape, design)
    if ink is None:
        return None
    # Through the groups above it and through its own rotation, or the crop lands
    # somewhere else on the page and the ground this reads is not the ground the words
    # sit on. A 90-degree heading declares a tall narrow box and sets a wide short line,
    # so the declared box crops straight across the words: on one bundled template that
    # put the gold pill under the type at 17% of the strip and the white page under it at
    # 18%, and called seven of the template's own pages unreadable at 1.0:1.
    where = ink_box(shape)
    if where is None:
        return None
    left = int(where.x0 / canvas_w * image.width)
    top = int(where.y0 / canvas_h * image.height)
    right = int(where.x1 / canvas_w * image.width)
    bottom = int(where.y1 / canvas_h * image.height)
    crop = image.crop((max(left, 0), max(top, 0), min(right, image.width), min(bottom, image.height)))
    if crop.width < 2 or crop.height < 2:
        return None
    if stated_rgb is None and not _painted_in(crop, _rgb(ink)):
        # The ink was resolved rather than read, and the render holds no glyph in
        # it: a theme colour with a lightness modifier, or a chain this reader walked
        # wrong. Judging the page on a colour it does not show is the one thing a
        # resolved ink must not do.
        return None
    ground = _worst_ground(crop, _rgb(ink), _word_slices(shape, text, where, words, image, canvas_w, canvas_h))
    if ground is None:
        return None
    band, under = ground
    return _ratio(_rgb(ink), band), text, ink, band, under


_PT_PER_INCH = 72.0
# How far outside the declared box a word box may sit and still be this shape's, in
# inches. Both boxes are points from the page's top left, so this covers rounding and
# the hairline a glyph's side bearing puts past the frame, not a real gap.
_WORD_SLACK = 0.02


def _word_slices(
    shape: Any,
    text: str,
    where: Any,
    words: Sequence[WordBox] | None,
    image: Any,
    canvas_w: float,
    canvas_h: float,
) -> tuple[tuple[str, Any], ...]:
    """This shape's copy as the render drew it, word by word, as pixels.

    The region the column slices cannot make. A slice is a full-height column of the
    declared box, so a block of two lines is judged on a column holding both of them
    and the dark leading between; a word box holds one word's glyphs and the ground
    behind them and nothing else. On a delivered page 12, two white bullets crossed the
    bright crest of a green wave: at 226x151px per column the crest is a minority of
    every column, every column's commonest band came back #161B1E, and the page passed
    at 17.4:1 with none of the guards below the cause. Under the word 'flexibility' the
    ground is #01E697 and the reading is 1.6:1. The same page's title, white on the dark
    half, reads 17.4:1 through either region, which is why the page needed the sharper
    one to tell its two cases of one colour pair apart.

    Two tests decide whether a word is this shape's, because either alone answers
    wrongly. Inside the declared box, or the word belongs to whatever else the page puts
    there; and the word's own characters present in this shape's copy, or two
    overlapping boxes each claim the other's words and a block is judged against a
    colour it is not set in -- a footer overlapping a body box is that case, on this
    same delivered page.
    """
    if not words or where is None:
        return ()
    found: list[tuple[str, Any]] = []
    for word in words:
        if not word.text.strip() or word.text not in text:
            continue
        x0, y0, x1, y1 = (value / _PT_PER_INCH for value in (word.x0, word.y0, word.x1, word.y1))
        if x0 < where.x0 - _WORD_SLACK or x1 > where.x1 + _WORD_SLACK:
            continue
        if y0 < where.y0 - _WORD_SLACK or y1 > where.y1 + _WORD_SLACK:
            continue
        box = (
            max(int(x0 / canvas_w * image.width), 0),
            max(int(y0 / canvas_h * image.height), 0),
            min(math.ceil(x1 / canvas_w * image.width), image.width),
            min(math.ceil(y1 / canvas_h * image.height), image.height),
        )
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            continue
        found.append((word.text, np.asarray(image.crop(box), dtype=np.uint8)))
    return tuple(found)


# How wide a slice of a block gets its own ground reading, in the crop's own pixels
# scaled off its height: a block straddling two grounds is judged on the majority one,
# so the part of it on the other ground is judged against a colour it does not sit on.
# Measured on a live page: a chart's label declared #F8F8F8 reads 3.43:1 against the
# panel it mostly covers and 1.08:1 against the cream wedge its last five characters
# actually sit on, and 1.08 is the reading a reader gets. Its neighbour on the same page,
# same size, same declared ink, reads 3.43:1 in every slice -- so this separates the two
# rather than reporting both.
_SLICE_SHARE = 1.5
# What a slice's commonest band has to hold to be that slice's ground rather than the
# glyphs standing in it. Type never covers half of its own line box, and a slice whose
# winner holds less than this has no ground to read: reporting one there is how a
# quantile split came to call every comma unreadable.
_SLICE_GROUND_SHARE = 0.35
# The most of a slice the ink's own bucket may hold and still be type standing on a
# ground rather than something painted in that colour.
_SLICE_INK_MAX = 0.5
# The least of a slice the ink's bucket has to hold for the slice to carry any type.
_SLICE_INK_MIN = 0.02
# The least of one column a resolved ink's bucket has to hold somewhere in the crop
# for the render to be showing type in that colour at all.
_COLUMN_INK_MIN = 0.05


def _keys(arr: Any) -> Any:
    """Each pixel's ground bucket as one integer, shape (H, W)."""
    banded = (arr[..., :3] // _GROUND_STEP).astype(np.int32)
    return (banded[..., 0] * _GROUND_LEVELS + banded[..., 1]) * _GROUND_LEVELS + banded[..., 2]


# How far a rendered pixel may sit from the declared ink, per channel, and still be
# the ink. The ground buckets are 16 levels wide with edges at multiples of 16, so a
# bucket test put #2F2F2F and the #303030 the renderer painted it as in different
# buckets -- and then took the glyphs' own pixels for the ground, 1.0:1, on three
# readable pages of measured decks -- while it put #110D0A photograph pixels in the
# same bucket as #2F2F2F type, and read a title as standing on the photograph beside
# it. A distance from the colour itself has neither edge.
_INK_TOLERANCE = 20
# How close a pixel has to be to the ink to prove type is painted *here*. Glyph interiors
# come off the renderer within a level or two of the declared colour; a night photograph
# beside the title holds plenty of pixels within twenty levels of #2F2F2F and almost none
# within six, so the loose distance says what to keep out of the ground and the tight one
# says which columns carry words. With one distance the title's span ran into the
# photograph and the title was judged against it (v13 gold, page 7).
_INK_EXACT = 6


def _ink_mask(arr: Any, ink: tuple[int, int, int], within: int = _INK_TOLERANCE) -> Any:
    """Which pixels are the ink: within `within` of it on every channel."""
    diff = np.abs(arr[..., :3].astype(np.int32) - np.asarray(ink, dtype=np.int32))
    return (diff <= within).all(axis=-1)


def _ink_span(arr: Any, ink: tuple[int, int, int]) -> tuple[int, int] | None:
    """The columns the ink lands in, (first, last + 1).

    None where none of them do, which is a block whose glyphs the render did not paint
    in the colour the file states -- an inherited size shrunk to nothing, a run the
    theme overrode. There is no span to read then and the whole crop stands.
    """
    columns = np.flatnonzero(_ink_mask(arr, ink, _INK_EXACT).any(axis=0))
    if columns.size == 0:
        return None
    return int(columns[0]), int(columns[-1]) + 1


def _rgb_of(ink: Any) -> tuple[int, int, int]:
    """`ink` as a colour triple, whichever way the caller holds it."""
    return ink if isinstance(ink, tuple) else _rgb(ink)


def _second_ground(here: tuple[int, int, int], dominant: tuple[int, int, int]) -> bool:
    """Whether a slice sits on a second ground rather than further along one fill.

    A gradient pill with white type across it has a light end, and a slice there reads
    low against white without anything being wrong: the fill is one ground and the type
    spans it. Measured over the bundled templates, that is a large class -- four pages
    on two templates, all four readable, all four reported once slices were read at all.
    The two readings separate cleanly on how far apart the grounds are: a gradient's
    ends measure 1.26 to 1.77 against each other and a genuine second ground measured
    3.18, so the line is the one this module already draws for whether a reader can tell
    two colours apart. The margin is not wide: the closest real pair in the calibration
    set reads 1.96 against `UNREADABLE_RATIO`'s 2.0, so a wider gradient than any yet
    measured would be taken for a second ground.
    """
    return _ratio(here, dominant) >= UNREADABLE_RATIO


def _painted_in(crop: Any, ink: tuple[int, int, int]) -> bool:
    """Whether some column of the crop holds enough of the ink's bucket to be glyphs in it."""
    arr = np.asarray(crop, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[0] * arr.shape[1] < _MIN_PIXELS:
        return False
    if bool((_ink_mask(arr, ink, _INK_EXACT).mean(axis=0) >= _COLUMN_INK_MIN).any()):
        return True
    # Nothing exactly the ink anywhere, but nearly the whole crop is near it: black type
    # on a black panel paints no glyph the renderer can tell from its ground, and that
    # is the page this check exists for, not a page with no type on it.
    return bool(_ink_mask(arr, ink).mean() >= _INK_IS_GROUND_SHARE)


def _worst_ground(
    crop: Any,
    ink: tuple[int, int, int],
    named: tuple[tuple[str, Any], ...] = (),
) -> tuple[tuple[int, int, int], str | None] | None:
    """The ground this block sits worst against and the word it was read under, or None.

    `named` are the render's own word boxes as pixels (see `_word_slices`), judged after
    the column slices and through the same four guards below. Extra regions rather than
    a replacement: the column reading is what every measured deck calibrated those
    guards against, so a word box can only make the answer worse, never quieter, and
    the word comes back so the finding can say where on the line it went wrong. None
    for the word when a column slice or the whole crop gave the answer.

    Sliced across rather than taken whole -- see `_SLICE_SHARE`. A block whose slices
    all agree gets the same answer as one ground for the whole crop, which is every
    block that sits on one colour.

    Sliced across the ink and not across the box. The crop is the block's declared
    rectangle, which is as wide as the author drew it and not as wide as the words came
    out: a 3.0in label whose copy fills 1.8in leaves 1.2in of whatever the page is
    painted with, and read as a slice that is a second ground the type never sits on.
    That is how the reading this replaces refused a page over a box whose empty tail
    hung off the edge of its panel -- and refusing is what `unreadable` does, so the
    cost is a deck rejected over a box with nothing wrong with it. The same argument
    the module docstring gives against a quantile split applies here: a ratio taken
    where no glyph landed means "no glyph here", not "unreadable".
    """
    arr = np.asarray(crop, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[0] * arr.shape[1] < _MIN_PIXELS:
        return None
    whole = _ground(arr, ink)
    span = _ink_span(arr, _rgb_of(ink))
    has_ink = span is not None
    if span is not None and span[1] - span[0] >= 1:
        narrowed = arr[:, span[0] : span[1]]
        if narrowed.shape[0] * narrowed.shape[1] >= _MIN_PIXELS:
            arr = narrowed
    height, columns = arr.shape[:2]
    width = max(1, int(round(height * _SLICE_SHARE)))
    regions: list[tuple[str | None, Any]] = []
    if columns > width:
        regions = [(None, arr[:, start : min(start + width, columns)]) for start in range(0, columns, width)]
    regions.extend(named)
    worst, held, under = whole, _ratio(ink, whole), None
    for word, part in regions:
        if part.shape[0] * part.shape[1] < _MIN_PIXELS:
            continue
        band = _ground(part, ink)
        step = _GROUND_STEP
        key = ((band[0] // step) * _GROUND_LEVELS + band[1] // step) * _GROUND_LEVELS + band[2] // step
        pixels = part.shape[0] * part.shape[1]
        # Asked of a column and not of a word. The question is whether the winning
        # band is this region's ground or the glyphs standing in it, and a column of
        # the declared box can hold neither -- an empty tail, the page beside a panel
        # -- so a column with no dominant colour has no ground to read. A word box
        # holds that word's glyphs and the ground behind them and nothing else, so
        # there is nothing else the winner could be, and the two guards below already
        # test the ink directly. Dominance is what a textured ground does not have:
        # the crest of a rendered wave is a continuum of greens, no 16-level band of
        # it holds 15% of the word 'flexibility' sits on, and that word is the one a
        # reader cannot make out.
        if word is None and int((_keys(part) == key).sum()) / pixels < _SLICE_GROUND_SHARE:
            continue
        # Type never covers half of its own line box. A region mostly in the ink's own
        # bucket is a photograph or a filled shape the box reaches over, not words on
        # a ground: a dark title's declared box ran into the dark photograph beside
        # it and the photograph was read as its ground at 1.4:1.
        painted = float(_ink_mask(part, ink, _INK_EXACT).mean())
        if painted > _SLICE_INK_MAX:
            continue
        # And a region with no glyph in it has no text to read: a title's declared
        # box ran across the photograph beside it, and the photograph -- distinct
        # from the ink, distinct from the page -- was read as the title's ground.
        if has_ink and painted < _SLICE_INK_MIN:
            continue
        if not _second_ground(band, whole):
            continue
        ratio = _ratio(ink, band)
        if ratio < held:
            worst, held, under = band, ratio, word
    return worst, under


# How coarsely the ground's pixels are bucketed before the commonest one is taken. A
# gradient's every pixel is a slightly different colour, so the raw mode is whatever
# small flat area the crop happens to include: a bundled template's gold pill came out
# 4% per shade against 10% for the white the rounded corners left in the crop, so the
# mode was white and the reading called the page unreadable at 1.0:1 with white type on
# gold. Sixteen levels a channel holds a gradient together and still separates the
# grounds a page actually paints -- `surface` and `background` in every bundled theme
# are further apart than one level.
_GROUND_LEVELS = 16
_GROUND_STEP = 256 // _GROUND_LEVELS


# The share of a crop the ink may cover before it is read as the ground rather than as
# type standing on one. A bold title fills a third to a half of its line with glyphs, so
# on a photograph -- no bucket of which is common -- the commonest bucket was the ink
# itself, and a cover's title measured 1.0:1 against its own strokes on a page anyone
# could read (v21, page 1, refused and never published). Black type on a black panel is
# different in exactly one way: the ink is then nearly the whole crop, glyphs and ground
# alike.
_INK_IS_GROUND_SHARE = 0.75


def _ground(arr: Any, ink: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    """The ground these pixels are, as the mean of the commonest band of them.

    Bucketed rather than counted outright, and then averaged inside the winning bucket
    so the answer is a colour the crop really holds rather than the corner of a bucket.
    With `ink` given, the pixels that are the ink are not candidates unless they are
    most of the crop: the glyphs are what sits *on* the ground, not the ground.
    """
    flat = arr.reshape(-1, arr.shape[-1])[:, :3]
    if ink is not None:
        painted = _ink_mask(flat, _rgb_of(ink))
        if 0 < int(painted.sum()) < _INK_IS_GROUND_SHARE * painted.size:
            flat = flat[~painted]
    codes = _keys(flat.reshape(1, -1, 3)).reshape(-1)
    counts = np.bincount(codes, minlength=_GROUND_LEVELS**3)
    top = int(counts.max())
    candidates = np.flatnonzero(counts == top)
    # The commonest bucket; on a tie, the one seen first, as a Counter's most_common
    # answers -- the reading this replaces was calibrated on that order.
    winner = (
        int(candidates[0])
        if candidates.size == 1
        else int(min(candidates, key=lambda code: int(np.argmax(codes == code))))
    )
    held = flat[codes == winner].astype(np.float64)
    return tuple(int(round(float(value))) for value in held.sum(axis=0) / held.shape[0])


def _states_a_fill(frame: Any) -> bool:
    """Whether any run states a fill this check cannot read as one colour."""
    for para in frame.paragraphs:
        for run in para.runs:
            properties = run._r.find(f"{_A_NS}rPr")
            if properties is None:
                continue
            if any(properties.find(f"{_A_NS}{tag}") is not None for tag in ("gradFill", "blipFill", "pattFill")):
                return True
    return False


def _declared(frame: Any, design: Any = None) -> str | None:
    """The colour the file states for this text, or None when it inherits one.

    The largest run wins where a block mixes them: a heading with one accented word is
    judged on the heading. Largest of the block, though, and not largest of whichever
    runs happen to state one: a bundled template has a body block of seventeen
    characters where sixteen inherit their colour and one states #F8F8F8, and judging the
    block on that character read white type on the white page under dark copy and called
    the page unreadable. A block most of which inherits is a block whose colour is not
    stated here, which is the same answer as one that states none at all.
    """
    weighed: Counter = Counter()
    held = 0
    for para in frame.paragraphs:
        for run in para.runs:
            if not run.text.strip():
                continue
            held += len(run.text)
            stated = None
            colour = run.font.color
            try:
                if colour is not None and colour.type is not None and colour.rgb is not None:
                    stated = str(colour.rgb).upper()
            except (AttributeError, TypeError, ValueError):
                stated = None
            if stated is None and design is not None:
                # A theme colour: `rgb` has nothing to say about it, the palette does.
                try:
                    stated = run_ink(run, design)
                except (AttributeError, TypeError, ValueError):
                    stated = None
            if stated is None:
                continue
            weighed[stated] += len(run.text)
    if not weighed or sum(weighed.values()) * 2 < held:
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
