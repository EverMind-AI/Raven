"""The plain-text family: one file in, one section out.

Adopted from AgentScope's own implementation (Apache-2.0; see NOTICES.md).

The file is not split. A text file has no boundary a parser can see -- a blank
line is not reliably a section break, and cutting on one anyway would be worse
than not cutting: a chunk never spans two sections, so every paragraph would
become its own chunk and a query would match fragments too small to answer it.
So the whole file is one section at reading order 0, and all splitting stays
with the chunker, which is sized for it.

Formats that do carry their own structure leave this parser: Markdown and HTML
go to ``StructuredTextParser``, which is registered ahead of it.
"""

from __future__ import annotations

import os

from raven.knowledge._types import Section, TextBlock
from raven.knowledge.parser import LayoutType, ParserBase, section_metadata


class TextParser(ParserBase):
    """Parser for plain-text file formats.

    Reads the entire file as UTF-8 text and returns a single :class:`Section`.
    No internal boundaries are inferred -- the file is treated as one
    unstructured blob, leaving all splitting to a downstream chunker.

    Supports a fixed set of standard text-based IANA media types
    (``text/plain``, ``text/markdown``, ``text/csv``, ...). Use
    ``TextParser.supported_media_types`` to enumerate them.
    """

    supported_media_types: list[str] = [
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/x-rst",
        "application/json",
        "application/xml",
        "application/x-yaml",
    ]
    """Standard IANA media types this parser handles."""

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """Return the human-friendly text extensions.

        Override the base reverse-lookup because
        :func:`mimetypes.guess_all_extensions` returns a long tail of
        developer-tool extensions for ``text/plain`` (``.bat`` / ``.c`` /
        ``.pl`` / ``.ksh`` / ...) that have no place in a KB file picker, and
        returns the empty list for ``application/x-yaml``.
        """
        return [
            ".csv",
            ".htm",
            ".html",
            ".json",
            ".markdown",
            ".md",
            ".rst",
            ".txt",
            ".xml",
            ".yaml",
            ".yml",
        ]

    def __init__(self, encoding: str = "utf-8") -> None:
        """Initialize the text parser.

        Args:
            encoding (`str`, defaults to ``"utf-8"``):
                The text encoding used to decode the file bytes.
        """
        self.encoding = encoding

    async def parse(
        self,
        file: bytes | str,
        filename: str,
    ) -> list[Section]:
        """Read the file as text and return a single :class:`Section`.

        Args:
            file (`bytes | str`):
                The file content. ``bytes`` is decoded with the configured
                encoding. ``str`` is disambiguated at runtime: if it names an
                existing file on disk the file is read and decoded; otherwise
                it is used verbatim as pre-decoded text -- letting local-mode
                callers skip the encode -> decode round trip.
            filename (`str`):
                The source filename, copied verbatim into
                :attr:`Section.source`.

        Returns:
            `list[Section]`:
                Always a one-element list containing the entire file contents,
                at reading order 0.

        Raises:
            `ValueError`: If the bytes cannot be decoded with the configured
                encoding.
        """
        if isinstance(file, str):
            if os.path.isfile(file):
                with open(file, "rb") as fp:
                    raw = fp.read()
                text = self._decode(raw, filename)
            else:
                text = file
        else:
            text = self._decode(file, filename)

        return [
            Section(
                content=TextBlock(text=text),
                source=filename,
                metadata=section_metadata(reading_order=0, layout_type=LayoutType.TEXT),
            ),
        ]

    def _decode(self, raw: bytes, filename: str) -> str:
        try:
            return raw.decode(self.encoding)
        except UnicodeDecodeError as error:
            raise ValueError(
                f"Failed to decode {filename!r} as {self.encoding!r}: {error}",
            ) from error
