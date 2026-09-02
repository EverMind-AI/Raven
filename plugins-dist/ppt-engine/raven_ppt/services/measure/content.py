"""How much a page carries, and whether it shows anything.

Three measurements of content rather than arrangement, and the difference used to
decide who heard about them. A stage that could rearrange a page and never change
what it says was handed "this page carries three times what a slide holds", which
it could not answer: the same overloaded page came back arranged into a card grid
however often it was redesigned. That stage is gone and every finding goes to the
author, who can cut the copy -- but the distinction is still what these three
measure, and it is still the reason none of them is answerable by moving a box.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    is_panel,
    iter_shapes,
    iter_text_frames,
    open_deck,
    page_box,
    shows_picture,
)

# The most copy one slide of a talk can hold, measured off the deck this work is
# held against: a hand-built reference deck, reviewed and accepted as good if a
# little dense, runs a median of 158 words a page and tops out at 244. A separate
# paper-to-deck system puts a slide at 45-110 words, which is calibrated for a
# different renderer and, applied here, produced a deck of half-empty pages.
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

# What makes a table unreadable is a column narrower than the string in it, not a
# column count. This used to fire past eight columns, which is the wrong question
# twice over: eight short columns of figures read fine across a 13.3in canvas, and
# five columns of phrases do not. The measurement below asks the readable question
# instead -- does each column have the width its own widest cell needs -- and it is
# the same question `ppt_layout.table` already answers when it sizes columns from
# their content, so a table drawn with that helper does not trip this.
#
# The one number left is what counts as "narrower": a cell whose text needs more
# than this share more than its column has is squeezed rather than merely tight.
# Wrapping is not the defect on its own -- a two-line cell is ordinary -- so the
# line sits where a cell has lost most of a second line's worth of room.
COLUMN_SQUEEZE = 1.6


# Sequences a slide never shows: an escape that was meant to be a line break, a tab
# that was meant to be a column, an HTML entity that came through a template. Measured
# on a delivered deck, where two cards read "YTVIS: 46.3 -> 48.3\\nOVIS: 29.8 -> 31.1"
# with the backslash-n printed -- the author's string went through a JSON round trip on
# its way into the tool and came out with its escape escaped.
_LITERAL_ESCAPES = ("\\n", "\\t", "\\r", "&nbsp;", "&amp;", "&lt;", "&gt;", "<br")
# And the form python-pptx writes when a control character reaches `.text`: XML cannot
# carry one, so it comes out spelled. `\x0b` is the one that happens, because it is what
# PowerPoint's own format uses for a soft break inside a paragraph -- read a template's
# placeholder with `.text` and every `<a:br/>` in it arrives as `\x0b`, so a title
# written back with the same character prints `_x000B_`. Measured on a delivered cover:
# "EverMind AI_x000B_给 AI 智能体_x000B_一个持久记忆", at title size, on page 1.
_ESCAPED_CONTROL = re.compile(r"_x[0-9A-Fa-f]{4}_")
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
                found += sorted(set(_ESCAPED_CONTROL.findall(text)))
                if not found or any(mark in text for mark in _CODE_MARKS):
                    continue
                findings.append(
                    Finding(
                        kind="literal_escape",
                        severity=Severity.BLOCKING,
                        page=number,
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


# How much of the page a box has to hold before what is in it is the page talking
# rather than the inside of something. Two clauses, because a region can be tall and
# narrow or short and wide and the same stack of claims is the page's own either way.
#
# An eighth of the canvas is one of the page's own regions. `page()`'s body is 62% of
# the canvas and a page divides it into a handful of bands or columns, so an eighth is
# still a region and anything under it is inside a block -- a card's body, a panel's
# copy, one cell of a grid. Measured against the module's own geometry: a card's body
# box holds 3.0% of the canvas in a three-up row, 6.4% in a two-up and 8.6% inside a
# half-page panel, while a reading lane beside a chart holds 19%, the column beside a
# figure 23%, and the column of the delivered page this was rewritten for 34%.
#
# Width alone was the whole test and it missed that page by a fifth of an inch: at
# half the canvas the bar is 6.67in and the column was 6.48in -- wider than every card
# body in the deck, and under the bar. It stays as the second clause because a box
# across three quarters of the page is the page talking whatever its height, and a
# full-width band two lines deep does not hold an eighth of the canvas.
_A_REGION_OF_THE_PAGE = 0.125
_ACROSS_THE_PAGE = 0.75

# A line long enough to be a claim rather than a label. Below this a stack of short
# lines is a list of names, a legend or an axis, and none of those wants a block each.
_POINT_CHARS = 25


def listed_claims(pptx_path: Path) -> list[Finding]:
    """Parallel claims stacked in one box, where the page wanted a block for each.

    Two claims in one frame read as one thing whatever sits in front of them. As bare
    paragraphs they read as one paragraph that happens to have a line break in it --
    two 47- and 58-character sentences under a rule, measured on a delivered page. As
    bullets they read as a list to be read out: a later page put four
    `Label: sentence` claims in a 6.5in column as four dots with a paragraph after
    each, no card, no ground and nothing the eye can use as a boundary, and the answer
    it got was to delete that layout. Both readings are this finding, which is why a
    mark no longer clears it -- the marks were on that page, written by the call this
    check used to name.

    One claim, one block, so one claim per frame. `card()` draws a block in a call,
    `card_size()` levels a row of them without padding any out to the region, and
    `plane()` is the ground under the set.

    Table cells are left out: a cell holding two sentences is a cell. So is any box too
    small to be one of the page's own regions -- see `_A_REGION_OF_THE_PAGE`.
    """
    findings: list[Finding] = []
    presentation = open_deck(pptx_path)
    canvas_w = (presentation.slide_width or 0) / EMU_PER_INCH
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    canvas = canvas_w * canvas_h
    if not canvas:
        return findings
    for number, slide in enumerate(presentation.slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if getattr(shape, "has_table", False) or not getattr(shape, "has_text_frame", False):
                continue
            box = page_box(shape)
            if box is None:
                continue
            if box.area / canvas < _A_REGION_OF_THE_PAGE and box.width / canvas_w < _ACROSS_THE_PAGE:
                continue
            claims = [para for para in shape.text_frame.paragraphs if _is_claim(para)]
            if len(claims) < 2:
                continue
            findings.append(
                Finding(
                    kind="listed_claims",
                    severity=Severity.WARNING,
                    page=number,
                    message=(
                        f"{len(claims)} parallel claims share one box, so they read as a list to be read out "
                        f"rather than as {len(claims)} things: '{_head(claims[0].text, 40)}'. Separate them -- a "
                        "frame each, fewer claims on the page, or the page split -- or leave it: prose that is "
                        "one argument in two paragraphs is a reading and not a defect"
                    ),
                    detail={"page": number, "claims": len(claims), "first": _head(claims[0].text, 60)},
                )
            )
    return findings


def _is_claim(para) -> bool:
    return len("".join(para.text.split())) >= _POINT_CHARS


def wide_tables(pptx_path: Path) -> list[Finding]:
    """Tables whose columns are narrower than what they hold."""
    from raven_ppt.services.measure.width import DEFAULT_MEASURER

    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_table", False):
                continue
            squeezed = _squeezed_columns(shape.table, DEFAULT_MEASURER)
            if not squeezed:
                continue
            worst = max(squeezed, key=lambda column: column[2] / column[1])
            findings.append(
                Finding(
                    kind="wide_table",
                    severity=Severity.WARNING,
                    page=number,
                    message=(
                        f"{len(squeezed)} of this table's {len(shape.table.columns)} columns are narrower "
                        f"than what they hold -- column {worst[0]} has {worst[1]:.2f}in and its widest cell "
                        f"needs {worst[2]:.2f}in. Give those columns the room, drop the ones the page's "
                        "claim does not rest on, or let ppt_layout.table size them from their content"
                    ),
                    detail={
                        "columns": len(shape.table.columns),
                        "squeezed": [
                            {"column": index, "has_in": round(has, 2), "needs_in": round(needs, 2)}
                            for index, has, needs in squeezed
                        ],
                    },
                )
            )
    return findings


def banded_tables(pptx_path: Path) -> list[Finding]:
    """Tables tinting alternate rows, which this deck's style does not need.

    `table()` has banding off by default and says in as many words that a tint on
    every other row is what makes a table look cheap. A run that read the first
    hundred lines of `tables.md` and none of the dials past them turned it on anyway,
    on all three of its tables, over a template whose own palette was already doing
    that work.

    Read off the built file because that is the only place it is a fact: banding
    arrives three ways -- the `banding` keyword, a gallery style's `bandRow`, and a
    fill written cell by cell -- and the last of those is invisible to anything
    reading the call.

    A warning. A twenty-row lookup table nobody reads straight through is the case
    banding is for, and this cannot tell that page from the others.
    """
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_table", False):
                continue
            banded = _banded_rows(shape.table)
            if banded is None:
                continue
            findings.append(
                Finding(
                    kind="banded_table",
                    severity=Severity.WARNING,
                    page=number,
                    message=(
                        f"this table tints {len(banded)} of its {len(shape.table.rows) - 1} body rows in one "
                        "alternating pattern. Row spacing and alignment already tell the rows apart, and the "
                        "tint fights whatever the template's palette is doing -- pass `banding=False` (its "
                        "default) and let the header rule and the row hairline carry it. Keep it only for a "
                        "long lookup table somebody scans down for one line, and say so"
                    ),
                    detail={"rows": len(shape.table.rows), "banded": banded},
                )
            )
    return findings


def _banded_rows(table) -> list[int] | None:
    """The body rows carrying the alternating tint, or None when there is no pattern.

    Read as whole rows alternating between two fill patterns rather than as "some rows
    are tinted", because the two things banding is confused with are both single rows:
    `emphasize_rows` paints the row carrying the page's point, and a `total_rows` row
    sits under a rule. Neither alternates, and refusing either would refuse the dial.

    A row is its tuple of cell fills, so a column filled down the whole table is
    constant in both patterns and cancels out -- which is how the first version of this
    missed every table it was written for: the deck's tinted `ours` column made each
    banded row two tones, and a rule looking for one tone across the row found none.
    """
    body = range(1, len(table.rows))
    if len(body) < 4:
        return None
    patterns = [tuple(_cell_fill(cell) for cell in table.rows[index].cells) for index in body]
    if len({pattern for pattern in patterns}) != 2:
        return None
    first = patterns[0]
    if any((pattern == first) != (position % 2 == 0) for position, pattern in enumerate(patterns)):
        return None
    # The tinted half is the one with fills in it, whichever phase it landed on.
    even = [index for index, pattern in zip(body, patterns, strict=False) if pattern == first]
    odd = [index for index in body if index not in even]
    return even if sum(one is not None for one in first) > sum(one is not None for one in patterns[1]) else odd


def _cell_fill(cell) -> str | None:
    """This cell's solid fill as a hex string, or None when it has none."""
    try:
        fill = cell.fill
        if fill.type != 1:
            return None
        return str(fill.fore_color.rgb)
    except (AttributeError, TypeError, ValueError):
        return None


def _squeezed_columns(table, measurer) -> list[tuple[int, float, float]]:
    """(column, inches it has, inches its widest cell needs) for the squeezed ones.

    A cell's own font size, because a table that stepped its body down to fit is a
    table whose columns are already measured against the smaller type. Merged cells
    are skipped: their text belongs to a span rather than to one column, and counting
    it against the first column reported every group row in a grouped table.
    """
    widths = [(column.width or 0) / EMU_PER_INCH for column in table.columns]
    needed = [0.0] * len(widths)
    for row in table.rows:
        for index, cell in enumerate(row.cells):
            if index >= len(widths) or getattr(cell, "is_spanned", False) or getattr(cell, "span_width", 1) > 1:
                continue
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    if not run.text.strip():
                        continue
                    points = run.font.size.pt if run.font.size is not None else 14.0
                    inches = measurer.width(run.text, int(round(points))) / 72.0
                    padding = (cell.margin_left or 0) + (cell.margin_right or 0)
                    needed[index] = max(needed[index], inches + padding / EMU_PER_INCH)
    return [
        (index, widths[index], needed[index])
        for index in range(len(widths))
        if widths[index] > 0 and needed[index] > widths[index] * COLUMN_SQUEEZE
    ]


# The safe area a page keeps on every side, which is `ppt_layout`'s own MARGIN, and
# the only number of the grid restated here -- the canvas comes off the built file,
# which states its own, so a deck on a 4:3 template is held to the page it really has
# rather than to the 16:9 a plan-time reading has to assume. A test holds this against
# `layout_module_source()` so the two cannot drift apart.
SAFE_MARGIN_IN = 0.72
# python-pptx's own cell margins, 0.1in a side -- the same padding `_squeezed_columns`
# adds to a drawn cell's text before it compares it with the column it sits in.
_PLANNED_CELL_PADDING_IN = 0.2


def _overflow_evidence(drawn: list, carried: float, measurer) -> str:
    """What the built table shows, or "" when it shows nothing of the kind.

    Two ways one page carries the plan's forecast into the file. The columns can be
    narrower than what they hold, which is `wide_table`'s reading of the same shape;
    or the table can simply reach past the margin, which is what a program that hands
    `ppt_layout.table` a box wider than the page produces. The extent is the wider of
    the frame and its own columns, because a renderer lays a table out on the column
    widths and ignores a frame narrower than their sum, and it is read through
    `page_box` because a table inside a group is positioned in the group's space.
    """
    for shape in drawn:
        squeezed = _squeezed_columns(shape.table, measurer)
        if squeezed:
            worst = max(squeezed, key=lambda column: column[2] / column[1])
            return (
                f"{len(squeezed)} of its {len(shape.table.columns)} columns are narrower than what they hold, "
                f"and column {worst[0]} has {worst[1]:.2f}in where its widest cell needs {worst[2]:.2f}in"
            )
    for shape in drawn:
        box = page_box(shape)
        if box is None:
            continue
        spread = max(box.width, sum((column.width or 0) for column in shape.table.columns) / EMU_PER_INCH)
        past = box.x0 + spread - (carried + SAFE_MARGIN_IN)
        if past > 0:
            return f"it starts at {box.x0:.2f}in and runs {past:.2f}in past the page's right margin"
    return ""


# The look python-pptx stamps on a table nobody styled: "Medium Style 2 - Accent 1"
# out of the Office gallery, which draws a white hairline around every cell and sets
# the header in a blue no deck here owns. Recognised by value rather than by presence,
# because a table cloned out of a user's template carries the style its designer chose
# and that one is the house style -- the same rule `ppt_layout._drop_gallery_style`
# already follows when it takes this one off.
_OFFICE_GALLERY_STYLE = "{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"
_DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006/main"


def native_tables(pptx_path: Path) -> list[Finding]:
    """Tables still wearing the look Office gives one nobody styled.

    The predecessor reported every shape whose `has_table` was true, which measured
    the component and not the defect. `ppt_layout.table()` is built on `add_table`
    too -- there is no other way to put a table in a .pptx -- so the deck's own table
    helper reported on every page it drew, while the thing it exists to prevent and
    the thing it produces were counted the same. A gate that fires on the best answer
    available teaches the author to avoid the best answer available.

    What is measurable off the file, and what a reader actually sees, is the pair the
    helper takes off and a bare `add_table` keeps. One page of each, saved and read
    back:

        ppt_layout.table()   <a:tblPr firstRow="1"/>              no tableStyleId
        slide.shapes.add_table()
                             <a:tblPr firstRow="1" bandRow="1">   {5C22544A-...}

    So the two clauses, and the threshold is exact rather than tuned: banding is on
    or it is not, and the style id is that GUID or it is somebody's choice. Both
    survive into the render whatever palette the deck is in, and neither was chosen
    by anyone -- python-pptx stamps both on a table it creates.

    What this deliberately does not judge is how much of its region a table fills.
    Columns sized from their content make a table of short values come out narrow,
    and nothing in the file says whether the page wanted it that way; that reading
    needs the render and belongs to the pass that holds one (see the skill, S6).
    """
    findings: list[Finding] = []
    for number, slide in enumerate(open_deck(pptx_path).slides, start=1):
        count, worn = 0, []
        for shape in iter_shapes(slide.shapes):
            if not getattr(shape, "has_table", False):
                continue
            here = _office_look(shape.table)
            if not here:
                continue
            count += 1
            worn.extend(reason for reason in here if reason not in worn)
        if not count:
            continue
        findings.append(
            Finding(
                kind="native_table",
                severity=Severity.WARNING,
                page=number,
                message=(
                    f"{count} table(s) on this page still wear the look Office gives a table nobody styled "
                    f"({', '.join(worn)}): a white hairline around every cell, banded rows, and a header in "
                    "a blue this deck does not use. Draw it with `ppt_layout.table()`, which takes both off, "
                    "keeps every row and cell, and sizes each column from what that column holds"
                ),
                detail={"tables": count, "wearing": worn},
            )
        )
    return findings


def _office_look(table: object) -> list[str]:
    """Which parts of the Office default a table is still wearing, if any."""
    worn: list[str] = []
    if getattr(table, "horz_banding", False) or getattr(table, "vert_banding", False):
        worn.append("banded rows")
    element = getattr(table, "_tbl", None)
    if element is not None:
        for named in element.iter(f"{{{_DRAWINGML}}}tableStyleId"):
            if (named.text or "").strip().upper() == _OFFICE_GALLERY_STYLE:
                worn.append("the Office gallery style")
                break
    return worn
