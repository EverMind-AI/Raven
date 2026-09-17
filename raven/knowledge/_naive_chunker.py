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
from raven.knowledge._types import Chunk, DataBlock, Section, TextBlock
from raven.knowledge.parser import ELEMENTS, LAYOUT_TYPE, LayoutType

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

    def whole(self) -> str:
        parts = [part for part in (self.above, self.text, self.below) if part]
        return "\n".join(parts)


class NaiveChunker(ChunkerBase):
    """Cut on delimiters, merge to a size, never split a unit.

    Deliberately flat: unlike every other chunker here, a chunk may span two
    sections, because in this mode sections are not a boundary a reader asked
    to keep. The chunk carries the metadata of the section it starts in.
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
        texts = _overlapped([unit.whole() for unit in merged], self.overlap_size)

        return [
            Chunk(
                content=unit.block if unit.block is not None else TextBlock(text=text),
                source=unit.source,
                chunk_index=index,
                total_chunks=len(texts),
                metadata=dict(unit.metadata),
            )
            for index, (unit, text) in enumerate(zip(merged, texts, strict=True))
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
            if not isinstance(section.content, TextBlock):
                units.append(_Unit(text="", kind="data", metadata=dict(section.metadata), source=section.source))
                units[-1].block = section.content  # type: ignore[attr-defined]
                continue
            for text, kind, metadata in _rows(section):
                if kind in ("table", "figure"):
                    units.append(
                        _Unit(text=text, kind=kind, metadata=metadata, source=section.source, tokens=count_tokens(text))
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
            held = merged[open_text]
            held.text = f"{held.text}\n{unit.text}"
            held.tokens += unit.tokens
        return merged


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


def _overlapped(texts: list[str], size: int) -> list[str]:
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
        return texts
    return [
        "\n".join(
            part
            for part in (
                _tail_tokens(texts[at - 1], size) if at > 0 else "",
                text,
                _head_tokens(texts[at + 1], size) if at + 1 < len(texts) else "",
            )
            if part
        )
        for at, text in enumerate(texts)
    ]


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
