"""One line per example page: what it is, and whether code can redraw it.

The template reference used to arrive as source and renders, and both are the
wrong grain for the decision actually being made. A live run asked for pages 1-6,
received 30,000 characters of python-pptx with the template's placeholder copy
still in it, and then wrote its own layout system from scratch: eight pages, every
one on the emptiest layout, `clone_page` never imported. Measured on that same
template afterwards: 9 of its 13 pages hold something python-pptx cannot write, so
redrawing them in code could only ever have produced a worse page.

A menu is the grain of that decision. Thirteen lines, each naming what the page
is for and what it costs to use, is what lets an author say "page 5 is my metric
row" before reading any code at all -- and it puts the 9-of-13 verdict in front of
them at the moment they choose, rather than in a footnote about the pages they
happened to ask for.

What a page "is" is read off the page rather than guessed: the layout it uses, its
first line of text, and how many text blocks, pictures, tables and drawn shapes it
carries. Those five facts identify a cover, an agenda, a section divider, a card
row and a table page unambiguously enough to choose between them, and none of them
is a judgement this module has to defend.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pptx.enum.shapes import MSO_SHAPE_TYPE

from raven_ppt.services.measure.geometry import (
    EMU_PER_INCH,
    ICON_MAX_IN,
    PICTURE,
    is_filled,
    is_icon_sized,
    iter_shapes,
)
from raven_ppt.services.measure.variety import page_signature
from raven_ppt.services.template.compose import _all_shapes, _blip_fill, _is_cut_out, _is_drawing, _outermost

# What a page is *for*, when the page says so itself. A deck built in someone's
# template has to open, index and close in their template's own pages -- those three
# are the pages a reader recognises the house by, and a deck that draws its own cover
# announces itself as not theirs before a word is read.
COVER, AGENDA, SECTION, CLOSING = "cover", "agenda", "section", "closing"

_AGENDA_WORDS = ("agenda", "contents", "outline", "目录", "议程", "大纲")
_CLOSING_WORDS = ("closing", "thank", "谢谢", "感谢", "end", "结束")
_COVER_LAYOUTS = ("title slide", "cover", "封面")
_SECTION_WORDS = ("section", "divider", "transition", "章节", "过渡", "part ")
# A divider says one thing. Two text blocks is the number a real one carries -- a
# title and its number, or a title and one line under it -- and the third block is
# where content pages start.
_SECTION_BLOCKS = 2
_SECTION_CHARS = 24
# What a closing page may carry and still be one. A farewell page in the bundled
# templates holds three blocks -- the words, a presenter line and a vendor URL -- and a
# page that says thank you over six cards of copy is a content page whose heading
# happens to thank someone: gold_panel page 18 carries 25 text blocks in 6 repeating
# units, and calling it the closing page took it out of the offered content list
# entirely. Seven of the measured answers named it anyway, off the render, which is the
# offer this listing was refusing to make.
_CLOSING_BLOCKS = 6
# How long a line can be and still be something the page calls itself rather than
# something it says. The same length `_heading` treats as a title, and the reason the
# role words are matched against short lines only: `beige_geometric` page 2 has a body
# line reading "Review of the work content", and one more letter on it would have made
# the page an agenda for the wrong reason.
_TITLE_CHARS = 24


@dataclass(frozen=True)
class PageEntry:
    """One example page, as the menu lists it."""

    number: int
    layout: str
    heading: str
    text_blocks: int
    pictures: int
    tables: int
    shapes: int
    clone_only: bool
    unwritable: tuple[str, ...] = ()
    role: str = ""
    """`cover`, `agenda`, `closing`, or empty for an ordinary content page."""
    slots: int = 0
    """How many times the page's longest repeating unit repeats: the number of items
    `adapt(items=...)` can fill before it has to be a different page.

    The count an author actually chooses a prototype by -- five findings want a page
    with five slots -- and the one the listing did not carry: a live program filled a
    four-slot row with five items, was refused, and spent a round learning the count
    the render already showed."""
    arrangement: str = ""
    """What the page is made of and the grid it falls into, as `page_signature` reads it.

    The one thing an author choosing a prototype needs and the menu did not carry. A
    content page came back as its heading -- somebody else's quarterly report -- the
    layout name every content page in the file shares, and two counts, so the only
    signal telling one from another was the thumbnail. Measured on a live run against
    an eighteen-page template: the program cloned all five pages the reply named by
    role and drew every one of the twelve content pages from primitives, losing the
    template's own timeline, its ring of badges and its figure cards. The run before
    it, whose request happened to list the arrangements in prose, cloned eleven."""
    picture_slots: tuple[str, ...] = ()
    """Where a picture of the author's can go, each as `[n] WxHin photo|cut-out|icon|drawing`
    in the numbering `adapt` takes (`# [n]` over the reference, groups opened). A cut-out is
    a transparent illustration on the page's own ground; its box runs wherever the drawing
    does, so a photograph wants a box of its own there, or a cut-out of its own. An icon is
    a picture no longer than `ICON_MAX_IN` a side: a mark beside one unit, not a picture of
    anything, and a live deck kept a template's three seals as the marks on three phases of
    its own plan -- see ICON_SLOT_NOTE for what that slot asks of an author.

    The template's picture placeholders are seldom `p:pic`: the amber template draws
    every photograph as a rounded rectangle *filled* with one, and a section page's
    cartoon is a group of freeforms. Counting `p:pic` alone told an author those pages
    held no picture, and `pictures={2: ...}` against the photograph it could see was
    refused with "no picture frame at all" on three pages of one live run. What fills a
    slot is the author's decision; this says which shapes are slots."""
    hidden: bool = False
    """Marked not-for-show in the file. Numbering still counts it, because the
    numbers a page is asked for by are its place in the file; offering it is what
    has to stop. The bundled templates each shipped two hidden pages of the
    vendor's own advertising, and an author could name one as a prototype."""

    def line(self) -> str:
        parts = [f"[{self.number}]"]
        if self.hidden:
            parts.append("hidden in the file, not for building on")
        if self.role:
            parts.append(f"the template's {self.role}")
        if self.heading:
            parts.append(f"“{self.heading}”")
        parts.append(f"layout {self.layout!r}")
        if self.arrangement:
            parts.append(self.arrangement + (f" ({self.slots} slots)" if self.slots else ""))
        holds = [
            f"{self.text_blocks} text" if self.text_blocks else "",
            f"{self.pictures} picture" if self.pictures else "",
            f"{self.tables} table" if self.tables else "",
            f"{self.shapes} drawn" if self.shapes else "",
        ]
        parts.append("holds " + ", ".join(part for part in holds if part) if any(holds) else "empty")
        if self.picture_slots:
            parts.append("picture slots " + ", ".join(self.picture_slots))
        parts.append("clone it" if self.clone_only else "code can redraw it")
        return " — ".join(parts)


_MENUS: dict[tuple, tuple[PageEntry, ...]] = {}


def menu(path: Path, unwritable: dict[int, tuple[str, ...]] | None = None) -> tuple[PageEntry, ...]:
    """Every example page of this template, one entry each.

    `unwritable` is what `decompile` found it could not reproduce, keyed by page
    number; a page named there is one to clone rather than redraw. Passed in rather
    than recomputed because decompiling thirteen pages to build a menu would cost
    more than the menu saves.
    """
    # Read once per template file: a measurement pass asks for this menu nine times
    # for one build, 0.36s each on a 13MB template, and the file does not change
    # between them.
    try:
        stat = Path(path).stat()
        key = (str(Path(path).resolve()), stat.st_mtime_ns, stat.st_size, _frozen(unwritable))
    except OSError:
        key = None
    if key is not None and key in _MENUS:
        return _MENUS[key]
    entries = _menu(path, unwritable)
    if key is not None:
        if len(_MENUS) >= 8:
            _MENUS.pop(next(iter(_MENUS)))
        _MENUS[key] = entries
    return entries


def _frozen(unwritable: dict[int, tuple[str, ...]] | None) -> tuple:
    return tuple(sorted((number, tuple(what)) for number, what in (unwritable or {}).items()))


def _menu(path: Path, unwritable: dict[int, tuple[str, ...]] | None = None) -> tuple[PageEntry, ...]:
    try:
        from pptx import Presentation
    except ImportError:  # pragma: no cover -- python-pptx ships with the extra
        return ()
    try:
        presentation = Presentation(str(path))
    except Exception:  # noqa: BLE001 -- a malformed file is simply not a template
        return ()
    named = unwritable or {}
    canvas_h = (presentation.slide_height or 0) / EMU_PER_INCH
    entries: list[PageEntry] = []
    for number, slide in enumerate(presentation.slides, start=1):
        shapes = list(iter_shapes(slide.shapes))
        texts = [s for s in shapes if getattr(s, "has_text_frame", False) and s.text_frame.text.strip()]
        entries.append(
            PageEntry(
                number=number,
                layout=slide.slide_layout.name or "unnamed",
                heading=_heading(texts),
                text_blocks=len(texts),
                pictures=sum(1 for s in shapes if _is_photo(s)),
                picture_slots=_picture_slots(slide, presentation.slide_width or 0),
                tables=sum(1 for s in shapes if getattr(s, "has_table", False)),
                shapes=sum(1 for s in shapes if is_filled(s) and s not in texts),
                arrangement=page_signature(slide, canvas_h) or "",
                slots=_slots(slide),
                clone_only=number in named,
                hidden=slide.element.get("show") == "0",
                unwritable=tuple(named.get(number, ())),
                role=_role(
                    number,
                    slide.slide_layout.name or "",
                    _heading(texts),
                    blocks=len(texts),
                    longest=max((len(s.text_frame.text.strip()) for s in texts), default=0),
                    says=_says(texts),
                ),
            )
        )
    return tuple(entries)


def _role(number: int, layout: str, heading: str, blocks: int = 0, longest: int = 0, says: tuple = ()) -> str:
    """What this page is for, from what it says about itself.

    Its layout's name first, then its own words, and position only for the cover --
    position is the weakest signal and the one a template full of variants breaks. A
    page that matches nothing is a content page, which most of them are.

    The layout leads because it is the template's own declaration of what the page is
    for, and a heading is only what this page happens to say. Read the other way round,
    `gold_panel` page 14 -- headed "annual reflections and thanks" on a layout called
    `Section Header` -- became that template's closing page, and page 25, on a layout
    called `Closing`, was never offered as one; three of that template's 25 pages left
    the content menu between them.

    `says` is the page's own short lines, which is where an agenda names itself when
    its title is not the first shape in document order: `beige_geometric` page 2 leads
    with "01" and carries "Agenda" in the last shape on the page, `gold_panel` page 2
    breaks the same word across a line, so it arrives as "AG" and "ENDA", and both
    agenda page at all. Passed in rather than read here so the caller walks the shapes
    once.
    """
    named = layout.lower()
    if any(word in named for word in _COVER_LAYOUTS):
        return COVER
    if any(word in named for word in _CLOSING_WORDS):
        return CLOSING
    if any(word in named for word in _AGENDA_WORDS):
        return AGENDA
    # A section divider, which a deck of any length needs and which is the one
    # structural page a template does not name in its own words: this template calls
    # its page 3 "Section Header" on the layout and "单击此处添加章节标题" on the page,
    # and both say it.
    if any(word in named for word in _SECTION_WORDS):
        return SECTION
    lines = tuple(line.lower() for line in says)
    if any(word in line for line in lines for word in _AGENDA_WORDS):
        return AGENDA
    said = heading.lower()
    # Gated on the shape, because a thank-you in a heading is not a farewell page. The
    # words alone made a six-card content page the closing one.
    if blocks <= _CLOSING_BLOCKS and any(word in said for word in _CLOSING_WORDS):
        return CLOSING
    # Position, and only for the cover: it is the weakest signal and the one a template
    # full of variants breaks, but it still outranks the shape of the page below -- a
    # first page whose layout is not named for a cover and whose title is one short line
    # is a cover, not a divider.
    if number == 1:
        return COVER
    if any(word in said for word in _SECTION_WORDS):
        return SECTION
    # The shape of the page is the last fallback -- one or two blocks of short text and
    # nothing else is not a content page.
    if blocks and blocks <= _SECTION_BLOCKS and longest and longest <= _SECTION_CHARS:
        return SECTION
    return ""


def _says(texts: list) -> tuple[str, ...]:
    """The page's own short lines: what it calls itself, not what it says.

    Each line of each text block, plus each block with its whitespace taken out -- the
    second because a title set to wrap mid-word arrives as two lines and is one word
    to every reader but a substring test. Long lines are left out: they are body copy,
    and matching a role word inside one names the page after a sentence in it.
    """
    found: list[str] = []
    for shape in texts:
        whole = shape.text_frame.text
        for part in whole.splitlines():
            line = part.strip()
            if line and len(line) <= _TITLE_CHARS:
                found.append(line)
        packed = "".join(whole.split())
        if packed and len(packed) <= _TITLE_CHARS:
            found.append(packed)
    return tuple(dict.fromkeys(found))


def _slots(slide: Any) -> int:
    """How many units the page's longest run repeats, or 0 when nothing repeats."""
    from raven_ppt.services.template.compose import units

    try:
        runs = units(slide)
    except Exception:  # noqa: BLE001 -- a page python-pptx cannot walk has no slots to name
        return 0
    return max((len(run) for run in runs), default=0)


def roles(entries: tuple[PageEntry, ...]) -> dict[str, int]:
    """role -> the page number that plays it, first one wins.

    First rather than best because a template ships several dividers and several
    content-page variants: the deck needs one of each, and the earliest is the one the
    template itself leads with.
    """
    found: dict[str, int] = {}
    for entry in entries:
        if entry.role and entry.role not in found:
            found[entry.role] = entry.number
    return found


def _heading(texts: list) -> str:
    """The page's own first line, which is usually what the page is called.

    Longest-first would find the body copy; the first line in document order is the
    heading on every real template page checked, and a placeholder ("单击添加标题")
    is as good an answer as any -- it says the page has a title slot.
    """
    for shape in texts:
        line = next((part.strip() for part in shape.text_frame.text.splitlines() if part.strip()), "")
        if line:
            return line if len(line) <= 24 else line[:23] + "…"
    return ""


# A drawing smaller than this on either side is an icon, not an illustration: the
# bundled templates' card icons are 0.73in, their cartoons 2.4in and up.
ILLUSTRATION_MIN_INCHES = 1.0

# What an icon slot asks of an author, said once and quoted wherever the slot is named:
# the slot table's legend, the caption over the render, and the finding on a built page
# whose marks still stand for nothing. Measured on a live deck built in the red template:
# page 4 carried the template's three seals over four things of the author's (one seal
# twice), page 18 the same three seals over three phases, and nothing on either page
# told a reader which mark meant what.
ICON_SLOT_NOTE = (
    f"an icon slot (a picture at most {ICON_MAX_IN:g}in on a side) is a mark standing for the unit beside it, "
    "one per unit: on a cloned page give each unit its own -- `swap_icon(slide, shape_at(slide, n), '<icon "
    "name>', colour=T['accent'])` draws a Tabler icon in the slot's box (section 7 of the skill; "
    "`find_icons('<what the unit is about>')` names one) -- or drop them all with `drop=[n, ...]`. The "
    "template's marks kept, or one mark beside several things, mark nothing"
)
# A drawing at least this share of the page wide is a band or a backdrop, not a slot.
PAGE_WIDE = 0.9
FREEFORM = MSO_SHAPE_TYPE.FREEFORM


def _is_photo(shape) -> bool:
    return getattr(shape, "shape_type", None) == PICTURE or _blip_fill(shape) is not None


def _picture_slots(slide, page_width: int = 0) -> tuple[str, ...]:
    """The page's picture placeholders in `adapt`'s numbering: photographs, then drawings.

    A drawing is named by its first member's number with the whole drawing's size, since
    `adapt` opens groups and a member stands for the wordless group around it.
    """
    found: list[str] = []
    named: list[Any] = []
    for index, shape in enumerate(_all_shapes(slide.shapes), start=1):
        if _is_photo(shape):
            # A cut-out is named as one: an opaque photograph put in its box lands on
            # whatever the transparent drawing floated over (a live page's title row).
            found.append(f"[{index}] {_inches(shape)} {_picture_kind(shape)}")
            continue
        if not _is_drawing(shape):
            continue
        whole = _outermost(shape)
        if any(whole._element is done for done in named):
            continue
        if whole is shape and getattr(shape, "shape_type", None) != FREEFORM:
            # A lone preset shape without words is a panel or a band, not an illustration.
            continue
        if min(whole.width or 0, whole.height or 0) / EMU_PER_INCH < ILLUSTRATION_MIN_INCHES:
            continue
        if page_width and (whole.width or 0) >= PAGE_WIDE * page_width:
            # A wave along the foot of the page, a band behind the title: the page's own
            # design, drawn as a group of freeforms, and not a place for a picture.
            continue
        named.append(whole._element)
        found.append(f"[{index}] {_inches(whole)} drawing")
    return tuple(found)


def _inches(shape) -> str:
    return f"{(shape.width or 0) / EMU_PER_INCH:.1f}x{(shape.height or 0) / EMU_PER_INCH:.1f}in"


def _picture_kind(shape) -> str:
    """`icon`, `cut-out` or `photo`: what an author is being offered a slot for.

    Size before transparency: a small transparent glyph is a mark whichever way it is
    drawn, and the thing to say about a mark is that each unit wants its own.
    """
    if is_icon_sized(shape):
        return "icon"
    return "cut-out" if _is_cut_out(shape) else "photo"
