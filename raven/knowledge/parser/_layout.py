"""Grouping a document's elements into the sections that get embedded.

Three formats arrive here as the same shape: a Word file's paragraphs, a slide
deck's shapes, a PDF page's blocks. Each is a run of text with a kind, a place
on a page and, sometimes, a heading level -- and each wants the same thing done
with it, which is to be cut into sections at the headings and laid out so every
element can still be found inside the section that swallowed it.

Here rather than inside one parser because the third copy is where a shared
rule stops being shared: the section metadata contract (``elements`` spans,
``heading_path``, :data:`SECTION_ORDINAL`) is read by the chunker and by the
page, and two parsers drifting on it is a difference nothing downstream can
see until a citation points at the wrong place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raven.knowledge._sections import SECTION_ORDINAL
from raven.knowledge._types import Section, TextBlock
from raven.knowledge.parser import BBox, ElementSpan, LayoutType, section_metadata


@dataclass
class Element:
    """One paragraph, table, figure or block, with where it sits."""

    layout: LayoutType
    text: str
    page_number: int
    page_end: int
    bbox: BBox
    #: Set on a heading, and what the grouping cuts on. ``None`` on everything
    #: else, including a line that merely looks like a heading to a reader.
    heading_level: int | None = None
    #: How the format that produced this element names the thing behind it: a
    #: Word relationship id, a PDF object number. Opaque here, and carried so a
    #: parser can resolve it after the walk -- a picture's bytes are read from
    #: the package, not from the element tree.
    ref: str = ""


@dataclass
class Block:
    """A heading and the elements running up to the next heading."""

    level: int | None = None
    title: str = ""
    elements: list[Element] = field(default_factory=list)


def sections_from(elements: list[Element], filename: str) -> list[Section]:
    """Group elements into heading-bounded sections, in document order."""
    blocks: list[Block] = [Block()]
    for element in elements:
        if element.heading_level is not None:
            blocks.append(Block(level=element.heading_level, title=element.text))
        blocks[-1].elements.append(element)

    stack: list[tuple[int, str]] = []
    sections: list[Section] = []
    for block in blocks:
        if block.level is not None:
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.title))

        body = block.elements[1:] if block.level is not None else block.elements
        # A heading whose only content is itself -- a parent holding nothing
        # but subheadings -- would index as a chunk of its own title. The name
        # is not lost: its children carry it in their heading path.
        if not block.elements or (block.level is not None and not body):
            continue

        text, spans = lay_out(block.elements)
        if not text.strip():
            continue

        path = [title for _, title in stack]
        box = BBox()
        for element in block.elements:
            box = box.union(element.bbox)
        lead = block.elements[0]
        sections.append(
            Section(
                content=TextBlock(text=text),
                source=filename,
                metadata=section_metadata(
                    reading_order=len(sections),
                    layout_type=lead.layout,
                    page_number=lead.page_number,
                    page_end=block.elements[-1].page_end,
                    bbox=box,
                    elements=spans,
                    heading=path[-1] if path else None,
                    heading_path=path or None,
                    heading_level=block.level,
                    **{SECTION_ORDINAL: len(sections)},
                ),
            )
        )
    return sections


def lay_out(elements: list[Element]) -> tuple[str, list[ElementSpan]]:
    """Join a section's elements and record where each one landed.

    Consecutive list items are kept one line apart so a list reads as a list;
    everything else is separated by a blank line. ``reading_order`` on a span
    is the element's place in the section, not in the document -- the section's
    own reading order says where the section is, and the two together locate
    the element.

    An element with no text of its own -- an unlabelled image -- still gets a
    span, an empty one at the offset it sat at. Its page and box are what the
    span was for; giving it a placeholder to occupy would put words into the
    document that nobody wrote.
    """
    parts: list[str] = []
    spans: list[ElementSpan] = []
    cursor = 0
    previous: Element | None = None
    for index, element in enumerate(elements):
        if cursor and element.text:
            separator = (
                "\n"
                if previous is not None
                and previous.layout is LayoutType.LIST_ITEM
                and element.layout is LayoutType.LIST_ITEM
                else "\n\n"
            )
            parts.append(separator)
            cursor += len(separator)
        parts.append(element.text)
        spans.append(
            ElementSpan(
                reading_order=index,
                layout_type=element.layout,
                char_start=cursor,
                char_end=cursor + len(element.text),
                page_number=element.page_number,
                page_end=element.page_end,
                bbox=element.bbox if not element.bbox.is_empty else None,
            )
        )
        cursor += len(element.text)
        previous = element
    return "".join(parts), spans


__all__ = ["Block", "Element", "lay_out", "sections_from"]
