"""Naive chunking: cut on delimiters, merge to a size, keep units whole.

Ported from RAGFlow's ``naive_merge_docx`` (Apache-2.0; see NOTICES.md), which
is three passes over a flat list of units: split text on the delimiters, let a
table or a figure stand as its own unit, then merge neighbouring text units
until one is full.

What makes it naive is that it ignores the structure a parser found. A heading
starts nothing; a section boundary stops nothing. That is the trade a reader
makes when they turn smart chunking off -- even-sized pieces, cut where the
punctuation says, instead of pieces that follow the document. It is the mode to
pick for material with no headings worth following, and the wrong one for a
handbook.

Two properties are load-bearing and easy to lose:

- **A unit is never split.** The size is checked before a unit is added, not
  after, so a chunk can overshoot by the size of its last unit. Cutting to make
  it fit would put half a paragraph in one chunk and half in the next, which is
  the thing the whole arrangement exists to avoid.
- **Delimiter text never reaches a chunk.** A delimiter is a boundary, not
  content: leaving it in means every chunk starts or ends with a stray mark
  that the embedding then has to account for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from raven.knowledge._chunker import ChunkerBase
from raven.knowledge._sections import SECTION_ORDINAL
from raven.knowledge._types import Chunk, DataBlock, Section, TextBlock
from raven.knowledge.parser import (
    ATOMIC,
    BBOX,
    ELEMENTS,
    LAYOUT_TYPE,
    PAGE_END,
    PAGE_NUMBER,
    READING_ORDER,
    LayoutType,
)

#: Where a section records the headings above it. Not a parser constant: the
#: structured parsers write it and the page reads it, and a merged chunk has to
#: keep each part's own path or a citation names the wrong heading.
HEADING_PATH = "heading_path"

#: RAGFlow's own default: a newline, and the sentence marks of both scripts it
#: serves -- full stop, semicolon, exclamation and question mark in their
#: full-width forms. Kept verbatim: a Chinese full stop ends a sentence as much
#: as a latin one does, and a chunker that knew only the latin marks would cut
#: a Chinese document nowhere. Written as escapes so this file stays English
#: source (AGENTS.md section 1.3), the way the docx parser writes its own.
DEFAULT_DELIMITER = "\n!?;\u3002\uff1b\uff01\uff1f"

#: A backtick-wrapped run in the delimiter field is one delimiter, however many
#: characters it holds -- which is how a reader asks to cut on ``\n\n`` rather
#: than on two separate newlines. Everything outside the backticks is read one
#: character at a time.
_WRAPPED = re.compile(r"`([^`]+)`")

#: Where a sentence ends, for the purpose of lifting context around a table or
#: a figure. Coarser than the delimiter field on purpose: this is about finding
#: a readable boundary near a fixed number of tokens, not about where to cut.
_SENTENCE = re.compile("([\u3002!?\uff1f\uff1b\uff01\n]|\\. )")


def count_tokens(text: str) -> int:
    """How many tokens a string is, by the tokenizer rather than by a rule.

    RAGFlow's ``num_tokens_from_string``: cl100k_base, the encoding every
    OpenAI-compatible endpoint is closest to. The chunker's own estimate --
    utf-8 bytes over four -- reads a CJK character as three quarters of a token
    when it costs about one, which makes every size in a Chinese document wrong
    by a third.

    A failure falls back to that estimate rather than to zero. RAGFlow answers
    zero here, and zero is the one answer that cannot be recovered from: every
    unit looks empty, so the merge never fills a chunk and the whole document
    comes back as one.
    """
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=()))
    except Exception:
        return len(text.encode("utf-8")) // 4


def parse_delimiters(field_value: str) -> list[str]:
    """The delimiter field as a list of delimiters, longest first.

    Bare characters are one delimiter each; a backtick-wrapped run is one
    delimiter of its own length. Longest-first matters because the pattern is
    an alternation: with ``\\n`` before ``\\n\\n``, a blank line would match the
    single newline twice and cut twice where the reader asked for one cut.
    """
    if not field_value:
        return []
    normalized = field_value.replace("\r\n", "\n").replace("\r", "\n")

    found: list[str] = []
    seen: set[str] = set()

    def take(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            found.append(value)

    cursor = 0
    for match in _WRAPPED.finditer(normalized):
        for char in normalized[cursor : match.start()]:
            take(char)
        take(match.group(1))
        cursor = match.end()
    for char in normalized[cursor:]:
        take(char)

    return sorted(found, key=len, reverse=True)


def delimiter_pattern(delimiters: list[str]) -> str:
    """The alternation that splits on any of them, each matched literally."""
    return "|".join(re.escape(d) for d in delimiters if d)


def has_wrapped_delimiter(field_value: str) -> bool:
    """Whether the field names a delimiter in backticks.

    RAGFlow reads that as "every segment is its own chunk, whatever the size" --
    a reader who spells a delimiter out that carefully is describing the pieces
    they want, not a hint about where to cut.
    """
    return bool(_WRAPPED.search(field_value or ""))


@dataclass
class _Unit:
    """One thing that will not be cut: a run of text, a table, or a figure."""

    text: str
    kind: str
    metadata: dict = field(default_factory=dict)
    source: str = ""
    tokens: int = 0
    above: str = ""
    below: str = ""
    #: Non-text content a parser could not reduce to a string. Carried whole:
    #: an image is not merged with anything and is not cut.
    block: DataBlock | None = None
    #: What this unit is made of, in order: the metadata of each piece that was
    #: folded into it, and where that piece landed in ``text``. One entry until
    #: something merges into it -- and the reason it is kept is that a merged
    #: chunk whose metadata is only the first piece's states a page number, a
    #: box and a heading that are true of a fraction of what a reader is
    #: looking at.
    parts: "list[tuple[int, int, dict, dict]]" = field(default_factory=list)
    #: Which section of the document this unit was cut from, as the identity
    #: keys of that section. Held apart from ``metadata`` because the row
    #: metadata a section's element spans produce overwrites the section's own
    #: ``reading_order`` with the element's -- so by the time a unit exists,
    #: its metadata no longer says which section it came from.
    origin: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.parts:
            self.parts = [(0, len(self.text), self.metadata, self.origin)]

    def absorb(self, other: "_Unit", joiner: str = "\n") -> None:
        """Fold ``other`` into this unit, keeping track of where it landed."""
        at = len(self.text) + len(joiner)
        self.text = f"{self.text}{joiner}{other.text}"
        self.tokens += other.tokens
        self.parts.extend((start + at, end + at, metadata, origin) for start, end, metadata, origin in other.parts)

    def whole(self) -> str:
        parts = [part for part in (self.above, self.text, self.below) if part]
        return "\n".join(parts)

    def merged_metadata(self, shift: int = 0) -> dict:
        """The metadata of a chunk made of these parts.

        ``shift`` is what got prepended to the text before this chunk was
        written out -- lifted context, or an overlap tail. The spans below
        address the chunk's *final* text, so they move with it; unshifted they
        would point a reader at the wrong sentence, which is worse than not
        pointing anywhere.

        One part: its own metadata, unchanged -- which is every chunk in a
        document whose sections are bigger than the chunk size, and the case
        this must not disturb.

        Several: the first part's, because a chunk is filed where it starts,
        and then corrected everywhere "where it starts" would be a lie about
        the rest. The page becomes a range, because that is what the shape
        already had room for (``page_number`` and ``page_end``). The box is
        dropped when the parts do not share a page, because a rectangle
        spanning two sheets of paper is not a location. And every part is
        written into ``elements`` with the character range it occupies here,
        which is the list this was always going to need: each piece keeps its
        own page, box, layout, heading path -- and the section it was cut from.

        That last one is the load-bearing part. ``section_ordinal`` is defined
        as *the* section identity, precisely because a heading path is not one
        (two same-named children of a parent share it), and a merged chunk that
        carried only the first part's would attribute every later part to a
        section it never came from -- a hit widened back to "its" section would
        be handed the wrong text, and nothing about the chunk would say so. So
        each span carries its own, and the chunk-level key is dropped when the
        parts disagree: a chunk built from several sections does not have one.

        The heading path stays at chunk level even then, because it is a label
        rather than an identity -- it is what a list row shows -- and each
        part's own travels in its span beside the section it belongs to.
        """
        if len(self.parts) == 1:
            return dict(self.metadata)

        merged = dict(self.parts[0][2])
        pages = [
            page
            for _, _, metadata, _ in self.parts
            for page in (metadata.get(PAGE_NUMBER), metadata.get(PAGE_END))
            if isinstance(page, int)
        ]
        if pages:
            merged[PAGE_NUMBER] = min(pages)
            merged[PAGE_END] = max(pages)
        if len(set(pages)) > 1:
            merged.pop(BBOX, None)

        sections = {origin.get(SECTION_ORDINAL) for _, _, _, origin in self.parts}
        if len(sections) > 1:
            merged.pop(SECTION_ORDINAL, None)

        merged[ELEMENTS] = [
            {
                "reading_order": order,
                "layout_type": str(metadata.get(LAYOUT_TYPE) or LayoutType.TEXT),
                "char_start": start + shift,
                "char_end": end + shift,
                **{
                    key: metadata[key]
                    for key in (PAGE_NUMBER, PAGE_END, BBOX, HEADING_PATH)
                    if metadata.get(key) is not None
                },
                **origin,
            }
            for order, (start, end, metadata, origin) in enumerate(self.parts)
        ]
        return merged


class NaiveChunker(ChunkerBase):
    """Cut on delimiters, merge to a size, never split a unit.

    Deliberately flat: unlike every other chunker here, a chunk may span two
    sections, because in this mode sections are not a boundary a reader asked
    to keep. Refusing to cross one turns a document of short sections into a
    chunk per section, each too small to answer anything.

    A chunk that crossed carries metadata merged to match: the page becomes a
    range, a box that would span pages is dropped, and every piece that went in
    is recorded in ``elements`` with the character range it occupies -- so a
    hit in the middle still resolves to the page and box it came from. See
    :meth:`_Unit.merged_metadata`. Metadata that described only the first
    section would be worse than none, because it reads as a fact about the
    whole chunk.
    """

    def __init__(
        self,
        chunk_size: int = 512,
        separator: str = DEFAULT_DELIMITER,
        overlap_size: int = 0,
        table_context_size: int = 64,
        image_context_size: int = 64,
    ) -> None:
        """
        Args:
            chunk_size (`int`): Tokens a chunk is merged up to. A chunk may
                pass it by the size of its last unit, which is what keeps units
                whole.
            separator (`str`): The delimiter field. Bare characters are one
                delimiter each; backticks wrap one longer delimiter.
            overlap_size (`int`, defaults to 0): Tokens of the neighbouring
                chunks to repeat at each end. Off by default: repeated text is
                retrieved twice and read as two findings.
            table_context_size (`int`), image_context_size (`int`): Tokens of
                the surrounding prose to carry into a chunk that is otherwise
                only a table or a figure.
        """
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}.")
        if overlap_size < 0:
            raise ValueError(f"overlap_size must not be negative, got {overlap_size}.")
        self.chunk_size = chunk_size
        self.separator = separator
        self.overlap_size = overlap_size
        self.table_context_size = table_context_size
        self.image_context_size = image_context_size

    async def chunk(self, sections: list[Section]) -> list[Chunk]:
        units = self._units(sections)
        if not units:
            return []
        self._lift_context(units)
        merged = self._merge(units)
        written = _overlapped(
            [unit.whole() for unit in merged],
            self.overlap_size,
            # An atomic chunk takes no overlap and lends none of its own to the
            # chunk beside it: it stands for one thing -- a slide -- and text
            # from the slide before it, carried in, would make the chunk say
            # what that page does not.
            standalone=[unit.kind == "atomic" for unit in merged],
        )

        return [
            Chunk(
                content=unit.block if unit.block is not None else TextBlock(text=text),
                source=unit.source,
                chunk_index=index,
                total_chunks=len(written),
                # Both shifts: the prose lifted in front of a table or a figure
                # moves its text down as surely as an overlap tail does.
                metadata=unit.merged_metadata(shift + (len(unit.above) + 1 if unit.above else 0)),
            )
            for index, (unit, (text, shift)) in enumerate(zip(merged, written, strict=True))
        ]

    # ── units ─────────────────────────────────────────────────────

    def _units(self, sections: list[Section]) -> list[_Unit]:
        """Everything that will not be cut, in document order.

        A table or a figure is one unit whatever its size; text is split on the
        delimiters, and a run of text between two delimiters is one unit.
        """
        delimiters = parse_delimiters(self.separator)
        pattern = delimiter_pattern(delimiters)
        units: list[_Unit] = []

        for section in sections:
            # Read from the section, before `_rows` overlays an element's own
            # reading order on top of the section's: after that the row cannot
            # say which section it belongs to any more.
            origin = _origin_of(section)
            if not isinstance(section.content, TextBlock):
                units.append(
                    _Unit(
                        text="",
                        kind="data",
                        metadata=dict(section.metadata),
                        source=section.source,
                        origin=origin,
                    )
                )
                units[-1].block = section.content  # type: ignore[attr-defined]
                continue
            if section.metadata.get(ATOMIC):
                # Before `_rows`, which is the whole point: an atomic section's
                # element spans say where each part of it sits, not where it
                # may be cut, and decomposing by them is exactly what this flag
                # exists to stop. `kind` is not "text", so the merge below
                # leaves it standing alone as well.
                units.append(
                    _Unit(
                        text=section.content.text,
                        kind="atomic",
                        metadata=dict(section.metadata),
                        source=section.source,
                        tokens=count_tokens(section.content.text),
                        origin=origin,
                    )
                )
                continue
            for text, kind, metadata in _rows(section):
                if kind in ("table", "figure"):
                    units.append(
                        _Unit(
                            text=text,
                            kind=kind,
                            metadata=metadata,
                            source=section.source,
                            tokens=count_tokens(text),
                            origin=origin,
                        )
                    )
                    continue
                for piece in _split(text, pattern):
                    units.append(
                        _Unit(
                            text=piece,
                            kind="text",
                            metadata=metadata,
                            source=section.source,
                            tokens=count_tokens(piece),
                            origin=origin,
                        )
                    )
        return units

    # ── context around a table or a figure ────────────────────────

    def _lift_context(self, units: list[_Unit]) -> None:
        """Carry neighbouring prose into a table or figure chunk.

        A table on its own embeds as a grid of values with nothing saying what
        they are about, and a figure with only its caption is worse. The
        sentences either side are what a reader would have had in front of
        them, so a fixed number of tokens of them comes along.
        """
        for index, unit in enumerate(units):
            size = self.table_context_size if unit.kind == "table" else self.image_context_size
            if size <= 0 or unit.kind not in ("table", "figure"):
                continue
            unit.above = _tail_sentences(_text_before(units, index), size)
            unit.below = _head_sentences(_text_after(units, index), size)

    # ── merge ─────────────────────────────────────────────────────

    def _merge(self, units: list[_Unit]) -> list[_Unit]:
        """Fold text units into the one before them until it is full.

        The size is read off the chunk as it stands *before* the next unit is
        added, which is what lets a chunk overshoot rather than split. A table
        or a figure never merges with anything: it is its own chunk, so that a
        query matching a table gets the table and not a paragraph that happened
        to sit beside it.
        """
        one_each = has_wrapped_delimiter(self.separator)
        merged: list[_Unit] = []
        open_text = -1

        for unit in units:
            if unit.kind != "text":
                merged.append(unit)
                open_text = -1
                continue
            if open_text < 0 or merged[open_text].tokens >= self.chunk_size or one_each:
                merged.append(unit)
                open_text = len(merged) - 1
                continue
            merged[open_text].absorb(unit)
        return merged


def _origin_of(section: Section) -> dict:
    """Which section of its document this is, as the one key that says so.

    ``section_ordinal`` when a parser wrote one -- it is defined as the section
    identity for exactly this purpose. Otherwise the section's own
    ``reading_order``, which is the same fact under the only name a parser
    without sections of its own records it by: one section per row for a
    spreadsheet, one per file for plain text.

    Empty when neither is a number, which is a section that cannot say where it
    came from. Nothing is invented for it: a span with no origin is honest
    about not knowing, and one carrying a guessed ordinal is not.
    """
    for key in (SECTION_ORDINAL, READING_ORDER):
        value = section.metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return {SECTION_ORDINAL: value}
    return {}


def _rows(section: Section) -> list[tuple[str, str, dict]]:
    """A section as the rows RAGFlow would have been handed.

    The parser's element spans are exactly that list -- one row per paragraph,
    table and figure, with the layout type saying which. A section with no
    spans (plain text, markdown) is one text row.
    """
    text = section.content.text if isinstance(section.content, TextBlock) else ""
    spans = section.metadata.get(ELEMENTS)
    if not isinstance(spans, list) or not spans:
        return [(text, "text", dict(section.metadata))]

    rows: list[tuple[str, str, dict]] = []
    for span in spans:
        start, end = span.get("char_start"), span.get("char_end")
        if not isinstance(start, int) or not isinstance(end, int) or start >= end:
            continue
        layout = str(span.get(LAYOUT_TYPE) or "")
        kind = "table" if layout == LayoutType.TABLE else "figure" if layout == LayoutType.FIGURE else "text"
        metadata = {**section.metadata, **{key: value for key, value in span.items() if key != "char_start"}}
        metadata.pop(ELEMENTS, None)
        rows.append((text[start:end], kind, metadata))
    return rows


def _split(text: str, pattern: str) -> list[str]:
    """Text cut at the delimiters, with the delimiters left out.

    Whitespace-only segments end a run the way a delimiter does: a blank line
    between two paragraphs is a boundary whether or not the reader named one.
    """
    if not pattern:
        stripped = text.strip()
        return [stripped] if stripped else []

    pieces: list[str] = []
    held = ""
    for segment in re.split(f"({pattern})", text, flags=re.DOTALL):
        if not segment:
            continue
        if re.fullmatch(pattern, segment, flags=re.DOTALL) or not segment.strip():
            if held.strip():
                pieces.append(held.strip())
            held = ""
            continue
        held += segment
    if held.strip():
        pieces.append(held.strip())
    return pieces


def _text_before(units: list[_Unit], index: int) -> str:
    for unit in reversed(units[:index]):
        if unit.kind == "text":
            return unit.text
    return ""


def _text_after(units: list[_Unit], index: int) -> str:
    for unit in units[index + 1 :]:
        if unit.kind == "text":
            return unit.text
    return ""


def _sentences(text: str) -> list[str]:
    """Split keeping the mark that ended each sentence attached to it."""
    parts = _SENTENCE.split(text)
    out: list[str] = []
    for at in range(0, len(parts), 2):
        piece = parts[at] + (parts[at + 1] if at + 1 < len(parts) else "")
        if piece:
            out.append(piece)
    return out


def _tail_sentences(text: str, budget: int) -> str:
    """The last whole sentences that reach ``budget`` tokens."""
    held = ""
    for sentence in reversed(_sentences(text)):
        held = sentence + held
        if count_tokens(held) >= budget:
            break
    return held.strip()


def _head_sentences(text: str, budget: int) -> str:
    held = ""
    for sentence in _sentences(text):
        held += sentence
        if count_tokens(held) >= budget:
            break
    return held.strip()


def _overlapped(texts: list[str], size: int, standalone: list[bool] | None = None) -> list[tuple[str, int]]:
    """Repeat each chunk's neighbours at its ends.

    What a reader means by overlap: a chunk carries the tail of the one before
    it and the head of the one after, so a passage that straddles a boundary is
    whole in both. Taken from the chunks as they finally are, not from the
    units they were merged from -- the boundary only exists once the merge has
    decided where it falls.

    Off by default. Repeated text is retrieved twice and reads as two findings,
    which is a real cost to weigh against the passage that would otherwise be
    split.
    """
    if size <= 0 or len(texts) < 2:
        return [(text, 0) for text in texts]
    alone = standalone or [False] * len(texts)
    overlapped: list[tuple[str, int]] = []
    for at, text in enumerate(texts):
        if alone[at]:
            overlapped.append((text, 0))
            continue
        head = _tail_tokens(texts[at - 1], size) if at > 0 and not alone[at - 1] else ""
        tail = _head_tokens(texts[at + 1], size) if at + 1 < len(texts) and not alone[at + 1] else ""
        parts = [part for part in (head, text, tail) if part]
        # How far the chunk's own text moved, so the element spans can follow.
        overlapped.append(("\n".join(parts), len(head) + 1 if head else 0))
    return overlapped


def _tail_tokens(text: str, size: int) -> str:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return encoding.decode(encoding.encode(text, disallowed_special=())[-size:])
    except Exception:
        return text[-size * 4 :]


def _head_tokens(text: str, size: int) -> str:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return encoding.decode(encoding.encode(text, disallowed_special=())[:size])
    except Exception:
        return text[: size * 4]
