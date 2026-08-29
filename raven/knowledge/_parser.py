"""File parsers: raw upload bytes to a list of sections.

A parser handles one format family and preserves boundaries; it never chunks.
Adopted from AgentScope's own implementation (Apache-2.0; see NOTICES.md), narrowed to the text family -- the only parsers
the deployment being replaced ever registered. PDF, Word, Excel and PowerPoint
each need a third-party library, so they arrive with the optional extra that
carries those, not with the package.
"""

from __future__ import annotations

import mimetypes
import os
from abc import ABC, abstractmethod

from raven.knowledge._types import Section, TextBlock


class ParserBase(ABC):
    """Abstract base class for file-format parsers.

    Each subclass handles a single file format (or a related family,
    e.g. all plain-text MIME types).  Subclasses are typically
    instantiated once and reused across many ``parse()`` calls.

    Subclasses should be stateless or thread-safe — a single
    instance may be invoked concurrently from multiple agent runs.

    Subclasses must declare :attr:`supported_media_types` so that the
    KnowledgeBaseManager can route uploaded files to the right parser
    based on standard IANA media types (RFC 6838).
    """

    supported_media_types: list[str]
    """Standard IANA media types (RFC 6838) this parser handles,
    e.g. ``["application/pdf"]`` or
    ``["text/plain", "text/markdown"]``.  Used by the
    KnowledgeBaseManager to select a parser for an uploaded file."""

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """Filename extensions (including the leading ``.``) this parser
        can produce uploads for.

        The base implementation derives extensions from
        :attr:`supported_media_types` via
        :func:`mimetypes.guess_all_extensions` — good enough for clean
        IANA types like ``application/pdf``.  Subclasses **should
        override** this when the default reverse-lookup is noisy
        (``text/plain`` resolves to ``.bat`` / ``.c`` / ``.pl`` and a
        dozen other developer extensions no KB user wants in the file
        picker) or when a media type has no registered extension at all
        (``application/x-yaml`` returns the empty list).

        The result is consumed by the front-end's ``<input accept>`` and
        by the client-side filename guard; it is **not** consulted for
        media-type routing — that always goes through
        :attr:`supported_media_types`.

        Returns:
            `list[str]`:
                Deduplicated, sorted extensions (each starting with
                ``.``).  May be empty when no media type resolves.
        """
        out: set[str] = set()
        for media_type in cls.supported_media_types:
            out.update(mimetypes.guess_all_extensions(media_type))
        return sorted(out)

    @abstractmethod
    async def parse(
        self,
        file: bytes | str,
        filename: str,
    ) -> list[Section]:
        """Parse a file into a list of :class:`Section` objects.

        The ``file`` argument is a union covering the three call sites
        a parser sees in practice:

        - ``bytes`` — the raw payload, as handed in by HTTP uploads
          and blob-store reads.
        - ``str`` for binary parsers (PDF, PPT, image, …) — a
          **filesystem path** to the file to read.  The parser opens
          the path itself; callers do not need to read the bytes first.
        - ``str`` for :class:`TextParser` — disambiguated at runtime:
          if the string names an existing file on disk it is treated
          as a path and the file is decoded with the configured
          encoding; otherwise it is treated as pre-decoded text.

        Args:
            file (`bytes | str`):
                The file content or a path to it (see above).
            filename (`str`):
                The original filename (e.g. ``"report.pdf"``).  Used
                for error messages and copied into each Section's
                :attr:`Section.source` field for downstream display
                / citation.

        Returns:
            `list[Section]`:
                One Section per natural boundary in the source file.
                For unstructured formats (plain text, image, video),
                a single Section may cover the whole file.  Sections
                are returned in document order.

        Raises:
            `TypeError`: If the subclass does not accept the supplied
                ``file`` form.
            `FileNotFoundError`: If a binary parser is handed a
                ``str`` that does not name an existing file.
            `ValueError`: If the file cannot be parsed.
        """


class TextParser(ParserBase):
    """Parser for plain-text file formats.

    Reads the entire file as UTF-8 text and returns a single
    :class:`Section`.  No internal boundaries are inferred — the file
    is treated as one unstructured blob, leaving all splitting to a
    downstream chunker.

    Supports a fixed set of standard text-based IANA media types
    (``text/plain``, ``text/markdown``, ``text/csv``, …).  Use
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
        developer-tool extensions for ``text/plain`` (``.bat`` /
        ``.c`` / ``.pl`` / ``.ksh`` / …) that have no place in a KB
        file picker, and returns the empty list for
        ``application/x-yaml``.
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
                The file content.  ``bytes`` is decoded with the
                configured encoding.  ``str`` is disambiguated at
                runtime: if it names an existing file on disk the
                file is read and decoded; otherwise it is used
                verbatim as pre-decoded text — letting local-mode
                callers skip the encode → decode round trip.
            filename (`str`):
                The source filename, copied verbatim into
                :attr:`Section.source`.

        Returns:
            `list[Section]`:
                Always a one-element list containing the entire file
                contents.

        Raises:
            `ValueError`: If the bytes cannot be decoded with the
                configured encoding.
        """
        if isinstance(file, str):
            if os.path.isfile(file):
                with open(file, "rb") as fp:
                    raw = fp.read()
                try:
                    text = raw.decode(self.encoding)
                except UnicodeDecodeError as e:
                    raise ValueError(
                        f"Failed to decode {filename!r} as {self.encoding!r}: {e}",
                    ) from e
            else:
                text = file
        else:
            try:
                text = file.decode(self.encoding)
            except UnicodeDecodeError as e:
                raise ValueError(
                    f"Failed to decode {filename!r} as {self.encoding!r}: {e}",
                ) from e

        return [
            Section(
                content=TextBlock(text=text),
                source=filename,
                metadata={},
            ),
        ]
