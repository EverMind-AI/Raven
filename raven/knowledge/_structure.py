# -*- coding: utf-8 -*-
"""Hierarchy-aware sectioning for knowledge documents.

AgentScope indexes a file as Parser -> Section -> Chunk, and the Section
boundary is where document structure is meant to land: ``ChunkerBase``
guarantees no chunk ever spans two sections, so whatever the parser calls a
section is the structure that survives into retrieval.

``TextParser`` serves ``text/markdown`` and ``text/html`` alongside plain text
and returns the whole file as one unstructured section, so ``ApproxTokenChunker``
cuts fixed ~512-token windows blind to headings: a chunk carries nothing saying
which part of the document it came from, and HTML arrives with its tags as text.

This module supplies the structure for the two structured formats:

- ``StructuredTextParser`` splits Markdown on ATX headings and HTML on
  ``<h1>``-``<h6>``, emits one Section per heading, and records the full
  ancestor path in ``Section.metadata``. ``KnowledgeManager`` registers it ahead
  of ``TextParser``, so it takes those two media types and leaves every other
  text type with the plain parser.
- ``HeadingAwareChunker`` prefixes that path onto each chunk after the first, so
  a section long enough to be split still carries its heading context into the
  embedding instead of only in its opening slice.

A document with no headings degrades to exactly the previous behaviour: one
section, empty metadata, identical chunks.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser

from raven.knowledge._chunker import ApproxTokenChunker
from raven.knowledge._parser import ParserBase
from raven.knowledge._sections import MAX_SECTION_CHARS, SECTION_ORDINAL, SECTION_TEXT
from raven.knowledge._types import Chunk, Section, TextBlock

_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_CODE_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

_HTML_EXTENSIONS = (".htm", ".html", ".xhtml")
_HTML_SNIFF = re.compile(r"<\s*(!doctype\s+html|html|head|body|h[1-6])\b", re.IGNORECASE)

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Carry no readable prose, and their contents would otherwise be indexed as if
# they were body text.
_DROPPED_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_CELL_TAGS = {"td", "th"}
_BREAK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figure",
    "footer",
    "form",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "tfoot",
    "thead",
    "tr",
    "ul",
}


class _Block:
    """One heading and the body running up to the next heading.

    ``lead`` is how the heading should read back inside the section text:
    Markdown keeps its ``##`` markers so a chunk stays valid Markdown, HTML has
    none to keep.
    """

    __slots__ = ("level", "title", "lead", "parts")

    def __init__(self, level: int = 0, title: str = "", lead: str = "") -> None:
        self.level = level
        self.title = title
        self.lead = lead
        self.parts: list[str] = []


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _tidy_html_body(text: str) -> str:
    """Squeeze the whitespace an HTML source leaves behind.

    Markup indentation carries no meaning once the tags are gone, and
    ``get_text``-style extraction leaves long runs of blank lines that would
    otherwise eat into the chunk budget.
    """
    lines = (_collapse(line) for line in text.replace("\xa0", " ").split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _tidy_markdown_body(text: str) -> str:
    """Markdown keeps its own whitespace -- indentation is syntax here."""
    return text.strip("\n").rstrip()


def _markdown_blocks(text: str) -> list[_Block]:
    """Split Markdown at ATX headings, ignoring anything inside a code fence.

    Only ATX (``## Title``) is recognised. Setext underlines are left alone
    because a lone ``---`` is equally a horizontal rule and a front-matter
    delimiter, and guessing wrong invents sections that are not there.
    """
    blocks = [_Block()]
    fence = ""
    for line in text.splitlines():
        if fence:
            if line.lstrip().startswith(fence):
                fence = ""
            blocks[-1].parts.append(line + "\n")
            continue

        opening = _CODE_FENCE.match(line)
        if opening:
            fence = opening.group(1)[:3]
            blocks[-1].parts.append(line + "\n")
            continue

        heading = _ATX_HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            blocks.append(_Block(level, title, f"{'#' * level} {title}"))
            continue

        blocks[-1].parts.append(line + "\n")
    return blocks


class _HtmlBlocks(HTMLParser):
    """Collect text into one block per ``<h1>``-``<h6>``.

    Written against the standard library rather than BeautifulSoup: the only
    structure needed is "cut at the heading tags", the raven environment carries
    no lxml, and a lenient streaming parser handles the malformed markup real
    exports produce without a new dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks = [_Block()]
        self._dropped = 0
        self._heading_level = 0
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        # `</head>` is optional in HTML and `html.parser` never closes it; `head`
        # can only precede `body`, so `body` starting means any unclosed one ended.
        if tag == "body":
            self._dropped = 0
        if tag in _DROPPED_TAGS:
            self._dropped += 1
            return
        if self._dropped:
            return
        if tag in _HEADING_TAGS:
            self._heading_level = int(tag[1])
            self._title = []
            return
        if tag in _BREAK_TAGS:
            self.blocks[-1].parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._dropped = max(0, self._dropped - 1)
            return
        if self._dropped:
            return
        if tag in _HEADING_TAGS:
            title = _collapse("".join(self._title))
            self._heading_level, self._title = 0, []
            if title:
                self.blocks.append(_Block(int(tag[1]), title, title))
            return
        if tag in _CELL_TAGS:
            self.blocks[-1].parts.append(" | ")
        elif tag in _BREAK_TAGS:
            self.blocks[-1].parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._dropped:
            return
        if self._heading_level:
            self._title.append(data)
        else:
            self.blocks[-1].parts.append(data)


def _sections_from_blocks(blocks: list[_Block], filename: str, tidy) -> list[Section]:
    """Turn a flat block list into Sections carrying their ancestor path."""
    stack: list[tuple[int, str]] = []
    sections: list[Section] = []
    for block in blocks:
        if block.level:
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.title))

        body = tidy("".join(block.parts))
        # A heading whose body is empty -- a parent holding only subsections --
        # would index as a chunk of nothing but its own title. Its name is not
        # lost: the children still carry it in their path.
        if not body:
            continue

        path = [title for _, title in stack]
        # The ordinal is what makes a section identifiable. The heading path is
        # not: one parent with two same-named children -- two `### Example` under
        # one `## Usage` -- gives both the same full path, and a reader keying on
        # the path alone hands a hit in the second the text of the first, cited as
        # that heading. Nothing on the reading side can recover the boundary,
        # because same-path siblings are adjacent by construction and their chunk
        # indices are indistinguishable from one longer section's. So it is
        # recorded here, where the boundary is known.
        #
        # Only when there is a path: a document with no headings is one section,
        # nothing can be ambiguous, and its metadata stays empty so the
        # no-headings case degrades to the previous behaviour exactly.
        metadata = (
            {
                "heading": path[-1],
                "heading_path": path,
                "heading_level": block.level,
                SECTION_ORDINAL: len(sections),
            }
            if path
            else {}
        )
        sections.append(
            Section(
                content=TextBlock(text=f"{block.lead}\n\n{body}" if block.lead else body),
                source=filename,
                metadata=metadata,
            ),
        )
    return sections


class StructuredTextParser(ParserBase):
    """Split Markdown and HTML into one Section per heading.

    ``KnowledgeManager`` registers it ahead of ``TextParser`` (see ``_manager.py``),
    so it wins those two media types.
    """

    supported_media_types: list[str] = ["text/markdown", "text/html"]

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """The base reverse-lookup answers ``text/markdown`` with nothing on
        some platforms and drags a developer-tool tail behind ``text/html``."""
        return [".htm", ".html", ".markdown", ".md"]

    def __init__(self, encoding: str = "utf-8") -> None:
        self.encoding = encoding

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """Parse into one Section per heading, in document order.

        Falls back to a single whole-file Section when the document has no
        headings, which is what ``TextParser`` would have produced anyway.
        """
        text = self._decode(file, filename)

        if self._is_html(filename, text):
            reader = _HtmlBlocks()
            reader.feed(text)
            reader.close()
            sections = _sections_from_blocks(reader.blocks, filename, _tidy_html_body)
        else:
            sections = _sections_from_blocks(
                _markdown_blocks(text),
                filename,
                _tidy_markdown_body,
            )

        if not sections:
            return [
                Section(
                    content=TextBlock(text=text),
                    source=filename,
                    metadata={},
                ),
            ]
        return sections

    @staticmethod
    def _is_html(filename: str, text: str) -> bool:
        """Both media types route here, and ``parse`` is told only the filename.

        The extension decides; the sniff is for uploads that arrive labelled
        ``text/html`` under some other name.
        """
        if filename.lower().endswith(_HTML_EXTENSIONS):
            return True
        if filename.lower().endswith((".md", ".markdown")):
            return False
        return bool(_HTML_SNIFF.search(text[:4096]))

    def _decode(self, file: bytes | str, filename: str) -> str:
        """Accept the three shapes ``ParserBase`` documents: bytes, a path, or
        already-decoded text."""
        if isinstance(file, str):
            if not os.path.isfile(file):
                return file
            with open(file, "rb") as handle:
                raw = handle.read()
        else:
            raw = file

        try:
            return raw.decode(self.encoding)
        except UnicodeDecodeError as error:
            raise ValueError(
                f"Failed to decode {filename!r} as {self.encoding!r}: {error}",
            ) from error


class HeadingAwareChunker(ApproxTokenChunker):
    """Keep the heading path readable in every chunk of a split section.

    Sections short enough to survive as one chunk already open with their own
    heading. Longer ones lose it on the second slice onward, which is where a
    retrieved chunk stops saying what it is about -- so those get the ancestor
    path prefixed, and the chunk budget is narrowed by what the prefix costs so
    the result still fits.

    Sections with no heading metadata take the unmodified parent path.
    """

    def __init__(self, chunk_size: int = 512, overlap: int = 50) -> None:
        super().__init__(chunk_size=chunk_size, overlap=overlap)

    async def chunk(self, sections: list[Section]) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in sections:
            path = section.metadata.get("heading_path")
            if not path or not isinstance(section.content, TextBlock):
                chunks.extend(await super().chunk([section]))
                continue

            prefix = " > ".join(str(part) for part in path) + "\n\n"
            narrowed = ApproxTokenChunker(
                chunk_size=max(
                    self.overlap + 1,
                    self.chunk_size - self._approx_count_tokens(prefix),
                ),
                overlap=self.overlap,
            )
            pieces = await narrowed.chunk([section])
            whole = section.content.text
            if len(pieces) > 1 and len(whole) <= MAX_SECTION_CHARS:
                pieces[0].metadata[SECTION_TEXT] = whole
            for position, piece in enumerate(pieces):
                if position and isinstance(piece.content, TextBlock):
                    piece.content = TextBlock(text=prefix + piece.content.text)
            chunks.extend(pieces)

        for index, chunk in enumerate(chunks):
            chunk.chunk_index = index
            chunk.total_chunks = len(chunks)
        return chunks
