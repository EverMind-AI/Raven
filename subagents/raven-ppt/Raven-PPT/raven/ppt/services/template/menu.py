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

from raven.ppt.services.measure.geometry import PICTURE, is_panel, iter_shapes

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

    def line(self) -> str:
        parts = [f"[{self.number}]"]
        if self.role:
            parts.append(f"the template's {self.role}")
        if self.heading:
            parts.append(f"“{self.heading}”")
        parts.append(f"layout {self.layout!r}")
        holds = [
            f"{self.text_blocks} text" if self.text_blocks else "",
            f"{self.pictures} picture" if self.pictures else "",
            f"{self.tables} table" if self.tables else "",
            f"{self.shapes} drawn" if self.shapes else "",
        ]
        parts.append("holds " + ", ".join(part for part in holds if part) if any(holds) else "empty")
        parts.append("clone it" if self.clone_only else "code can redraw it")
        return " — ".join(parts)


def menu(path: Path, unwritable: dict[int, tuple[str, ...]] | None = None) -> tuple[PageEntry, ...]:
    """Every example page of this template, one entry each.

    `unwritable` is what `decompile` found it could not reproduce, keyed by page
    number; a page named there is one to clone rather than redraw. Passed in rather
    than recomputed because decompiling thirteen pages to build a menu would cost
    more than the menu saves.
    """
    try:
        from pptx import Presentation
    except ImportError:  # pragma: no cover -- python-pptx ships with the extra
        return ()
    try:
        presentation = Presentation(str(path))
    except Exception:  # noqa: BLE001 -- a malformed file is simply not a template
        return ()
    named = unwritable or {}
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
                pictures=sum(1 for s in shapes if getattr(s, "shape_type", None) == PICTURE),
                tables=sum(1 for s in shapes if getattr(s, "has_table", False)),
                shapes=sum(1 for s in shapes if is_panel(s) and s not in texts),
                clone_only=number in named,
                unwritable=tuple(named.get(number, ())),
                role=_role(
                    number,
                    slide.slide_layout.name or "",
                    _heading(texts),
                    blocks=len(texts),
                    longest=max((len(s.text_frame.text.strip()) for s in texts), default=0),
                ),
            )
        )
    return tuple(entries)


def _role(number: int, layout: str, heading: str, blocks: int = 0, longest: int = 0) -> str:
    """What this page is for, from what it says about itself.

    The page's own words first, then its layout's name, and position only for the
    cover -- position is the weakest signal and the one a template full of variants
    breaks. A page that matches nothing is a content page, which most of them are.
    """
    said = f"{heading} {layout}".lower()
    if any(word in said for word in _AGENDA_WORDS):
        return AGENDA
    if any(word in said for word in _CLOSING_WORDS):
        return CLOSING
    if any(word in layout.lower() for word in _COVER_LAYOUTS) or number == 1:
        return COVER
    # A section divider, which a deck of any length needs and which is the one
    # structural page a template does not name in its own words: this template calls
    # its page 3 "Section Header" on the layout and "单击此处添加章节标题" on the page,
    # and both say it. The shape of the page is the fallback -- one or two blocks of
    # short text and nothing else is not a content page.
    if any(word in said for word in _SECTION_WORDS):
        return SECTION
    if blocks and blocks <= _SECTION_BLOCKS and longest and longest <= _SECTION_CHARS:
        return SECTION
    return ""


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
