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

from raven.knowledge._types import Section
from raven.knowledge.parser import ParserBase
from raven.knowledge.parser._office import TIMEOUT_S as _TIMEOUT_S
from raven.knowledge.parser._office import converted
from raven.knowledge.parser.docx_parser import DocxParser


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
        docx = await converted(file, filename, target="docx", suffix=".doc", timeout_s=self.timeout_s)
        return await DocxParser().parse(docx, filename)
