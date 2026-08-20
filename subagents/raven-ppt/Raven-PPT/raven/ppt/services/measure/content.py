"""How much a page carries, and whether it shows anything.

Three measurements of content rather than arrangement, which is why all three
are addressed to whoever writes the deck. The design pass may rearrange a page
and is told explicitly never to change what it says, so handing it "this page
carries three times what a slide holds" leaves it with a problem it is forbidden
to solve -- and that is not a hypothetical: the same overloaded page came back
arranged into a card grid however often it was redesigned.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.services.measure.geometry import (
    is_panel,
    iter_shapes,
    iter_text_frames,
    open_deck,
    shows_picture,
)

# The most copy one slide of a talk can hold, measured off the deck this work is
# held against: a hand-built reference deck, reviewed and accepted as good if a
# little dense, runs a median of 158 words a page and tops out at 244. A separate
# paper-to-deck system puts a slide at 45-110 words, which is calibrated for a
# different renderer and, applied here, produced a deck of half-empty pages.
#
# A ceiling only. There is no floor, deliberately: a page that says little may be
# a page whose figure does the talking, and no measurement here can tell that
# from a page with nothing on it. What the ceiling catches is measured twice
# over -- past it, a page gets compartmented into a card grid, because boxing is
# what arranging 300 words looks like; and with the boxes forbidden but the copy
# left alone, the same page came back at 11pt instead.
# Characters rather than words, because `text.split()` cannot count Chinese: a whole
# CJK paragraph is one "word" to it, and the old 250-word ceiling never fired on any
# deck measured -- the densest page in four real decks reached 75. In characters, and
# with whitespace stripped so a script's spacing habits do not decide the number.
#
# Where the ceiling comes from: the densest page a reviewer accepted carries 467
# characters -- a five-row comparison table, two takeaways and a caveat -- and it is a
# good page, so a ceiling anywhere near it would be a rule against saying anything.
# 700 is half again as much, which is a page nobody can read in a room.
MAX_CHARS_PER_PAGE = 700

# The share of *content* pages that should carry something other than prose: a source
# figure, a table, a chart, an equation or a drawn diagram.
#
# 0.8 rather than the 0.7 this started at, because the denominator changed under it.
# With a deck's cover and closing counted, the two published decks measured landed at
# 6 of 8 -- and on the pages this check is actually about they were 6 of 6. Measured
# again on two live decks with the structural pages excluded: one shows something on
# 11 of 11 content pages, the other on 7 of 9, and the second is the one with three
# pages of prose a reader would ask about.
EVIDENCE_SHARE = 0.8
# A diagram is several shapes, so a page with one card behind its text does not
# count as having shown anything.
DIAGRAM_SHAPES = 4

# A wide table stops being read and starts being looked at.
MAX_TABLE_COLUMNS = 8


def copy_density(pptx_path: Path) -> list[Finding]:
    """Pages carrying more copy than a slide of a talk can hold."""
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        chars = sum(len("".join(frame.text.split())) for frame in iter_text_frames(slide))
        if chars <= MAX_CHARS_PER_PAGE:
            continue
        findings.append(
            Finding(
                kind="density",
                severity=Severity.WARNING,
                page=number,
                audience=Audience.AUTHOR,
                message=(
                    f"the page carries {chars} characters, past the {MAX_CHARS_PER_PAGE} a spoken slide holds. "
                    "Cut it, split it, or move part of it to a page of its own -- a page this full can only "
                    "be arranged into compartments, never composed"
                ),
                detail={"chars": chars, "ceiling": MAX_CHARS_PER_PAGE},
            )
        )
    return findings


# Sequences a slide never shows: an escape that was meant to be a line break, a tab
# that was meant to be a column, an HTML entity that came through a template. Measured
# on a delivered deck, where two cards read "YTVIS: 46.3 -> 48.3\\nOVIS: 29.8 -> 31.1"
# with the backslash-n printed -- the author's string went through a JSON round trip on
# its way into the tool and came out with its escape escaped.
_LITERAL_ESCAPES = ("\\n", "\\t", "\\r", "&nbsp;", "&amp;", "&lt;", "&gt;", "<br")
# Unless the line is showing code, where "a\\nb" is the point. A quote or a bracket in
# the same paragraph is what tells the two apart.
_CODE_MARKS = ('"', "'", "`", "(", "[", "{")


def literal_escapes(pptx_path: Path) -> list[Finding]:
    """Copy that prints an escape sequence instead of what it stands for."""
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        for frame in iter_text_frames(slide):
            for para in frame.paragraphs:
                text = "".join(run.text for run in para.runs)
                found = [mark for mark in _LITERAL_ESCAPES if mark in text]
                if not found or any(mark in text for mark in _CODE_MARKS):
                    continue
                findings.append(
                    Finding(
                        kind="literal_escape",
                        severity=Severity.BLOCKING,
                        page=number,
                        audience=Audience.AUTHOR,
                        message=(
                            f"this page prints {', '.join(repr(mark) for mark in found)} as characters: "
                            f"'{_head(text)}'. The escape was escaped somewhere on its way in -- pass a real "
                            "line break (a newline in the string, which becomes a new paragraph) or split the "
                            "copy into two values"
                        ),
                        detail={"page": number, "escapes": found, "text": _head(text, 80)},
                    )
                )
    return findings


def _head(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _carries_text(shape) -> bool:
    """Whether this shape holds copy of its own, empty text frame or none at all."""
    frame = getattr(shape, "text_frame", None)
    return bool(frame is not None and frame.text.strip())


def evidence_coverage(pptx_path: Path, structural: Sequence[int] = ()) -> list[Finding]:
    """How much of the deck shows something rather than telling it.

    A deck of prose pages is a document read aloud. Counted per page -- a
    picture, a table, a chart, or a diagram drawn from several shapes all count
    once -- and reported as one deck-wide finding, because the fix is what the
    deck is made of and not any single page.

    The diagram clause used to ask for a filled shape with no text *frame*, and
    in python-pptx every shape that can be filled has one (an autoshape's is
    merely empty), so it never fired. A live deck that drew its own flow, its own
    metric cards, its own table and its own line chart was told "0 of 8 pages show
    anything" -- a message that would push an author to paste rasters instead of
    drawing. Measured on two published decks: the panels these programs draw carry
    no text of their own (the copy goes in a textbox over them), so asking for an
    empty shape counts the diagrams and still leaves out a page of four cards each
    holding a paragraph, which is a compartment and not a diagram. Both decks land
    at 6 of 8 pages, which is what a reader of them would say.
    """
    presentation = open_deck(pptx_path)
    slides = list(presentation.slides)
    if not slides:
        return []
    # A cover and a closing page are not pages that failed to show something, and
    # counting them drags every deck's share down by two: one deck at 11 of 13 was at
    # 11 of 11 on the pages this check is about. `structural` is what the outline named
    # -- the pages built on the template's own -- so it needs no guess here.
    judged = [(number, slide) for number, slide in enumerate(slides, start=1) if number not in set(structural)]
    if not judged:
        return []
    carrying = 0
    for _number, slide in judged:
        shapes = list(iter_shapes(slide.shapes))
        shown = any(
            shows_picture(shape) or getattr(shape, "has_table", False) or getattr(shape, "has_chart", False)
            for shape in shapes
        )
        drawn = sum(1 for shape in shapes if is_panel(shape) and not _carries_text(shape))
        if shown or drawn >= DIAGRAM_SHAPES:
            carrying += 1
    if carrying / len(judged) >= EVIDENCE_SHARE:
        return []
    return [
        Finding(
            kind="evidence",
            severity=Severity.WARNING,
            audience=Audience.AUTHOR,
            message=(
                f"only {carrying} of {len(judged)} content pages show anything -- a figure, a table, a chart "
                "or a diagram. A talk wants that on most of them, not prose to read out"
            ),
            detail={
                "pages_with_evidence": carrying,
                "pages": len(judged),
                "share": EVIDENCE_SHARE,
                "structural": list(structural),
            },
        )
    ]


# What makes a line an expression rather than a sentence: a relation between two
# things. "训练成本 = 32 张 A100" has one too, which is why a grouping mark is
# needed as well -- a function call, a tuple, an inner product.
_RELATIONS = ("=", "←", "→", "⟵", "⟶", "∈", "≤", "≥", "≈", "≠", "∝", "⊂", "∑", "∏", "⟨")
_GROUPINGS = ("(", "（", "⟨", "[")
# Past this it is prose that happens to contain a formula, and the author is the one
# who can tell which part. Measured on real pages: the longest expression on a
# delivered deck runs 78 characters.
_EXPRESSION_CHARS = 130


def flat_formulas(pptx_path: Path) -> list[Finding]:
    """Expressions set as prose: no subscript is a subscript, and the box may break
    the line inside a symbol.

    What this catches, verbatim from a delivered deck: "Qin = concat(Qsem, Qinst,
    Qobj, Qbg)" set with `write` in a 4.7in column, which the render broke after the
    third comma with ")" alone on the next line -- and every subscript in it flat, so
    Qsem reads as a word rather than as Q with a subscript. The page beside it used
    `formula` for the same notation and came out right, which is what makes this
    measurable rather than a matter of taste.

    The test for "already set as a formula" is a raised or lowered run, because that
    is the thing `formula` produces that nothing else does.
    """
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        for frame in iter_text_frames(slide):
            for para in frame.paragraphs:
                text = "".join(run.text for run in para.runs)
                flat = " ".join(text.split())
                if not _is_expression(flat) or _has_raised_run(para):
                    continue
                findings.append(
                    Finding(
                        kind="flat_formula",
                        severity=Severity.WARNING,
                        page=number,
                        audience=Audience.AUTHOR,
                        message=(
                            f"'{_head(flat, 60)}' is an expression set as prose: nothing in it is a real "
                            "subscript, and the box is free to break it inside a symbol. Set it with "
                            "formula() from ppt_layout -- `_x` and `_{xyz}` subscript, `^x` superscripts, "
                            "and the line does not wrap"
                        ),
                        detail={"page": number, "text": _head(flat, 90)},
                    )
                )
    return findings


def _is_expression(text: str) -> bool:
    if not text or len(text) > _EXPRESSION_CHARS:
        return False
    if not any(mark in text for mark in _RELATIONS):
        return False
    return any(mark in text for mark in _GROUPINGS)


def _has_raised_run(para) -> bool:
    """Whether any run in this paragraph is raised or lowered -- what `formula` writes."""
    for run in para.runs:
        properties = getattr(run.font, "_rPr", None)  # noqa: SLF001 -- no API for the baseline
        if properties is not None and properties.get("baseline"):
            return True
    return False


# How wide a box has to be before the marks are worth putting in. A narrow box is a
# card's body or one column of a split page -- something already grouped, where two
# sentences of explanation read as explanation and bullets read as clutter. A box
# across half the page or more is the page talking, and there a stack of unmarked
# claims reads as one paragraph. Measured on a live deck: at half the canvas the 15
# boxes this reported became the 12 that are page-level, and the three dropped were
# all card bodies -- one of them with a formula as its first line.
_POINTS_SHARE = 0.5

# A point long enough that it is a claim rather than a label. Below this a stack of
# short lines is a list of names, a legend or an axis, and marks on those are noise.
_POINT_CHARS = 25
# Marks an author may have typed at the front instead of using a real bullet. Not
# ideal (the wrap does not hang), but the reader can see the list, which is what this
# measures.
_TYPED_MARKS = ("·", "•", "-", "–", "—", "*", "▪", "◦", "①", "②", "③", "1.", "2.", "3.", "一、", "二、", "三、")


def unmarked_points(pptx_path: Path) -> list[Finding]:
    """Parallel claims stacked in one box with nothing in front of any of them.

    Two 47- and 58-character sentences under a rule, no mark on either: a reader has
    to work out that they are two things rather than one paragraph with a line break
    in it. Measured on a delivered page, and the fix is one call -- `points()` writes
    a real bullet with a hanging indent, so the second line of a point lines up with
    its first.

    Table cells are left out: a cell holding two sentences is a cell, and marks in a
    table read as clutter. So is any box narrower than half the page -- see
    `_POINTS_SHARE`.
    """
    findings: list[Finding] = []
    presentation = open_deck(pptx_path)
    room = (presentation.slide_width or 0) * _POINTS_SHARE
    for number, slide in enumerate(presentation.slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if getattr(shape, "has_table", False) or not getattr(shape, "has_text_frame", False):
                continue
            if (shape.width or 0) < room:
                continue
            claims = [para for para in shape.text_frame.paragraphs if _is_claim(para)]
            if len(claims) < 2 or any(_is_marked(para) for para in shape.text_frame.paragraphs):
                continue
            findings.append(
                Finding(
                    kind="unmarked_points",
                    severity=Severity.WARNING,
                    page=number,
                    audience=Audience.AUTHOR,
                    message=(
                        f"{len(claims)} parallel points share a box with nothing in front of any of them, so "
                        f"they read as one paragraph: '{_head(claims[0].text, 40)}'. Set them with points() "
                        "from ppt_layout -- it writes a real bullet with a hanging indent, and takes "
                        "numbered=True when the order matters"
                    ),
                    detail={"page": number, "points": len(claims), "first": _head(claims[0].text, 60)},
                )
            )
    return findings


def _is_claim(para) -> bool:
    return len("".join(para.text.split())) >= _POINT_CHARS


def _is_marked(para) -> bool:
    """Whether this paragraph carries a mark: a real bullet, or one typed in."""
    properties = para._pPr  # noqa: SLF001 -- no API for a paragraph's bullet
    if properties is not None:
        for tag in ("buChar", "buAutoNum"):
            if properties.find(f"{{http://schemas.openxmlformats.org/drawingml/2006/main}}{tag}") is not None:
                return True
    return para.text.strip().startswith(_TYPED_MARKS)


def wide_tables(pptx_path: Path) -> list[Finding]:
    """Tables too wide to be read from a seat."""
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_table", False):
                continue
            columns = len(shape.table.columns)
            if columns <= MAX_TABLE_COLUMNS:
                continue
            findings.append(
                Finding(
                    kind="wide_table",
                    severity=Severity.WARNING,
                    page=number,
                    audience=Audience.AUTHOR,
                    message=(
                        f"a table {columns} columns wide; past {MAX_TABLE_COLUMNS} nobody reads it. "
                        "Subset it to the columns the page's claim actually rests on"
                    ),
                    detail={"columns": columns, "ceiling": MAX_TABLE_COLUMNS},
                )
            )
    return findings
