"""Legacy Word documents (``.doc``): converted first, then read as ``.docx``.

The binary ``.doc`` format is a compound file with Word's own record layout,
and nothing in this package reads it. What the install already has is
LibreOffice -- the viewer renders one to show it -- so the file is converted to
the format the parser beside this one was written for, and that parser does the
work. A conversion is not free and not always faithful, but the alternative is
a document a reader can see in the panel and cannot search, which is what this
engine did until now.

Not PDF, though that is what the viewer converts to. A PDF of a Word document
has lost the headings, the table cells and the paragraph boundaries that the
docx parser reports positions from, and this package has no PDF parser to read
one with anyway. Going to ``.docx`` keeps the structure the format still has.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from loguru import logger

from raven.knowledge._types import Section
from raven.knowledge.parser import ParserBase
from raven.knowledge.parser.docx_parser import DocxParser

#: Long enough for a large document on a cold LibreOffice, short enough that a
#: conversion which will never finish does not hold an indexing run open. The
#: viewer's own renderer uses the same order of magnitude.
_TIMEOUT_S = 120.0


class LegacyDocParser(ParserBase):
    """Read a ``.doc`` by converting it and handing the result to DocxParser."""

    supported_media_types: list[str] = ["application/msword"]

    @classmethod
    def supported_extensions(cls) -> list[str]:
        """``.doc`` only. ``.dot`` is a template of the same vintage and would
        convert as happily, but nothing has asked for one and a claim this
        parser cannot demonstrate is a claim that goes stale."""
        return [".doc"]

    def __init__(self, timeout_s: float = _TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    async def parse(self, file: bytes | str, filename: str) -> list[Section]:
        """Convert, then parse, and say plainly when the converter is missing.

        Args:
            file (`bytes | str`): The ``.doc`` payload, or a path to it.
            filename (`str`): The source filename, carried into each Section.

        Returns:
            `list[Section]`: What ``DocxParser`` makes of the converted file.

        Raises:
            `FileNotFoundError`: If ``file`` is a path that does not exist.
            `ValueError`: If LibreOffice is not installed, or the conversion
                produced nothing. Both are reported as the document's own
                error, where the reader who can act on them is looking.
        """
        from raven.utils.office import convert, find_soffice, install_hint

        executable = find_soffice()
        if executable is None:
            raise ValueError(
                f"{filename!r} is a legacy Word document, which needs LibreOffice to read: {install_hint()}"
            )

        with tempfile.TemporaryDirectory(prefix="raven-doc-") as scratch:
            room = Path(scratch)
            source = room / "source.doc"
            if isinstance(file, str):
                if not os.path.isfile(file):
                    raise FileNotFoundError(f"{filename!r}: no such file: {file!r}")
                source.write_bytes(Path(file).read_bytes())
            else:
                source.write_bytes(file)

            staged = room / "out"
            staged.mkdir()
            # Off the event loop: LibreOffice is a subprocess that takes
            # seconds, and indexing runs in the gateway process, which is also
            # answering the page that is watching the document's status.
            try:
                done = await asyncio.to_thread(
                    convert,
                    source,
                    staged,
                    executable=executable,
                    timeout_s=self.timeout_s,
                    target="docx",
                )
            except (OSError, TimeoutError) as exc:
                raise ValueError(f"Failed to convert {filename!r} with LibreOffice: {exc}") from exc

            if not done.produced:
                logger.debug("knowledge: soffice said {!r} / {!r}", done.stdout[-400:], done.stderr[-400:])
                raise ValueError(
                    f"LibreOffice could not convert {filename!r} (exit {done.returncode}); "
                    "the file may be corrupt or password-protected"
                )
            return await DocxParser().parse(done.produced[0].read_bytes(), filename)
