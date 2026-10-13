"""Reading a PDF: the text it states, the tables it draws, and a picture of a page.

Everything above this module asks a PDF the same four questions -- how big is
this page, what text is on it, what tables are on it, and what does it look
like -- so they are answered once, here, rather than four times against a
library's own vocabulary.

The library is pdfplumber, rasterising through pypdfium2, which is what the
RAGFlow deepdoc pipeline these models were ported from reads its PDFs with.
The obvious alternative does all four on its own, but PyMuPDF is AGPL-3.0 and
a knowledge base is part of the core wheel; an AGPL dependency there is an
AGPL obligation on every install, including the ones that never open a PDF.
The plugins that genuinely need PyMuPDF's writing side pin it themselves, and
the obligation stays on those wheels.

The shapes here are this package's own, not a translation of one library's.
That is the point of the module: a reader above it names a line, a block and a
box, and swapping what reads them is an edit to this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL.Image import Image

#: How far apart two words' tops may sit and still be the same line. A fraction
#: of the text's own size rather than a fixed number of points: a line of 9pt
#: footnotes and a line of 24pt display type are both one line, and a constant
#: that holds the first together splits the second.
_LINE_TOLERANCE = 0.4

#: How wide a gap between two words has to be before they stop being one line.
#: Multiples of the type size, so it scales the same way. A word space runs to
#: about a third of the size and a wide one to half; two points of column
#: gutter is many times that. Set well above the first and well below the
#: second, because the cost of being wrong is asymmetric -- a line wrongly cut
#: is two shorter lines in the same place, a line wrongly joined reads straight
#: across a gutter and puts the right-hand column's words inside the left-hand
#: column's region.
_COLUMN_GAP = 2.5

#: How far apart two lines may sit and still be one block, as a multiple of the
#: taller one's height. Leading runs to about 1.2 of the type size, so a gap of
#: roughly two thirds of a line height is the space a paragraph break leaves.
_BLOCK_GAP = 0.65


class PasswordProtectedError(Exception):
    """Raised opening a PDF that will not open without a password."""


@dataclass(frozen=True, slots=True)
class Line:
    """One printed line, and the size most of it is set in."""

    text: str
    size: float
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def box(self) -> "tuple[float, float, float, float]":
        return self.x0, self.top, self.x1, self.bottom


@dataclass(frozen=True, slots=True)
class Block:
    """Lines that are set as one run: a paragraph, a heading, a cell.

    A picture is a block too, with no lines -- it has a place on the page and
    nothing to read, which is exactly what the readers above want to know.
    """

    lines: "tuple[Line, ...]"
    x0: float
    top: float
    x1: float
    bottom: float
    picture: bool = False

    @property
    def box(self) -> "tuple[float, float, float, float]":
        return self.x0, self.top, self.x1, self.bottom


class Page:
    """One page: its size, what is written on it, and what it looks like."""

    def __init__(self, page: Any, number: int) -> None:
        self._page = page
        self.number = number
        self._blocks: "list[Block] | None" = None

    @property
    def width(self) -> float:
        return float(self._page.width)

    @property
    def height(self) -> float:
        return float(self._page.height)

    @property
    def box(self) -> "tuple[float, float, float, float]":
        """The page itself as a box, in the points everything else measures in."""
        return 0.0, 0.0, self.width, self.height

    def blocks(self) -> "list[Block]":
        """Every run of text on the page, and every picture, in no order.

        Held after the first call: the geometric reader asks for the body size,
        then for the elements, and assembling a page's words into lines twice
        costs the same as assembling it once.
        """
        if self._blocks is None:
            self._blocks = _blocks_of(self._page)
        return self._blocks

    def tables(self) -> "list[Any]":
        """The tables found on the page, each with ``bbox`` and ``extract()``.

        Table finding is heuristic and occasionally throws on a page whose
        drawing commands it cannot make sense of. One page's tables are not
        worth the document, so a failure here is no tables rather than an
        error -- the prose on the page still reads.
        """
        from loguru import logger

        try:
            return list(self._page.find_tables())
        except Exception as exc:  # noqa: BLE001 - the prose on the page is still readable
            logger.debug("pdf: table detection failed on a page ({})", exc)
            return []

    def render(self, scale: float) -> "Image":
        """The page as an RGB image, ``scale`` times the size the file states.

        A PDF states its geometry at 72 points to the inch, so a scale is a
        resolution divided by that -- which is the form the one library here
        takes it in.
        """
        drawn = self._page.to_image(resolution=72 * scale, antialias=True).original
        return drawn if drawn.mode == "RGB" else drawn.convert("RGB")

    def release(self) -> None:
        """Drop what reading this page cached.

        pdfplumber holds every page's objects once they are read, so walking a
        long document otherwise ends it holding all of them. The reader here
        walks forward and does not look back, so each page can be let go as it
        is passed -- and a page asked for again simply reads itself again.
        """
        self._blocks = None
        self._page.close()


class Document:
    """An open PDF, as a sequence of pages numbered from one."""

    def __init__(self, pdf: Any) -> None:
        self._pdf = pdf

    @property
    def page_count(self) -> int:
        return len(self._pdf.pages)

    def __len__(self) -> int:
        return self.page_count

    def __getitem__(self, index: int) -> Page:
        return Page(self._pdf.pages[index], index + 1)

    def __iter__(self) -> "Any":
        for index, page in enumerate(self._pdf.pages):
            yield Page(page, index + 1)

    def __enter__(self) -> "Document":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._pdf.close()


def open_document(file: "bytes | str | Path") -> Document:
    """Open a PDF from a path or from its bytes.

    Raises:
        `FileNotFoundError`: if a path was given and there is nothing there.
        `PasswordProtectedError`: if the file will not open without a password.
        `Exception`: whatever the reader raises for a file that is not a PDF.
    """
    import pdfplumber

    source: Any
    if isinstance(file, (str, Path)):
        if not Path(file).is_file():
            raise FileNotFoundError(f"no such file: {str(file)!r}")
        source = str(file)
    else:
        source = BytesIO(file)
    try:
        return Document(pdfplumber.open(source))
    except Exception as error:
        if _is_password(error):
            raise PasswordProtectedError(str(error)) from error
        raise


def _is_password(error: BaseException) -> bool:
    """Whether an open failed for want of a password rather than for a bad file.

    Asked of the chain rather than the exception, because the reader wraps
    every cause it can hit in one type: a truncated download and a locked file
    arrive here as the same class, and only what is underneath tells them
    apart. A reader can do something about a password and nothing about a
    corrupt file, so the two must not read the same on the page.
    """
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    seen: "set[int]" = set()
    pending: "list[Any]" = [error]
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PDFPasswordIncorrect):
            return True
        pending.extend([*current.args, current.__cause__, current.__context__])
    return False


def _blocks_of(page: Any) -> "list[Block]":
    """A page's words gathered into lines, and its lines into blocks."""
    visible = _visible(page)
    words = visible.extract_words(extra_attrs=["size"])
    blocks = [_block(group) for group in _grouped(_lines_of(words))]
    blocks.extend(
        Block(
            lines=(),
            x0=float(image["x0"]),
            top=float(image["top"]),
            x1=float(image["x1"]),
            bottom=float(image["bottom"]),
            picture=True,
        )
        for image in visible.images
    )
    return blocks


def _visible(page: Any) -> Any:
    """The page with what falls outside it dropped.

    A PDF's content stream is not bounded by its page: a line set too long
    simply carries on past the edge, and the reader never sees the rest. The
    library here hands back every glyph the stream draws, including those --
    so a line that runs off the page is reported as a box wider than the page
    it is on, which is a box no region of that page can contain. Everything
    downstream places text by asking which region it fell in, and a box that
    fits none of them is text that was read and then dropped.

    Cropping to the page is what a viewer does, and it is the same cut: the
    characters that survive are the characters that are printed.
    """
    from loguru import logger

    try:
        return page.crop(page.bbox, strict=True)
    except Exception as exc:  # noqa: BLE001 - an odd page box is not the document
        logger.debug("pdf: page {} could not be cropped to its own box ({})", page.page_number, exc)
        return page


def _lines_of(words: "list[dict]") -> "list[Line]":
    """Words gathered into the lines they were printed as.

    Two passes rather than one. Across the page first, by where the words sit
    vertically, which is what makes them a line; then along each line, cutting
    it wherever the gap between two words is too wide to be a space. The second
    pass is what keeps a two-column page two columns: both columns' words share
    a baseline, so the first pass alone reads straight across the gutter.
    """
    rows: "list[list[dict]]" = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        top = float(word["top"])
        size = float(word.get("size") or 0) or (float(word["bottom"]) - top)
        if rows and abs(float(rows[-1][-1]["top"]) - top) <= size * _LINE_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])

    lines: "list[Line]" = []
    for row in rows:
        run: "list[dict]" = []
        for word in sorted(row, key=lambda w: float(w["x0"])):
            if run:
                size = float(run[-1].get("size") or 0) or 1.0
                if float(word["x0"]) - float(run[-1]["x1"]) > size * _COLUMN_GAP:
                    lines.append(_line(run))
                    run = []
            run.append(word)
        if run:
            lines.append(_line(run))
    return lines


def _line(words: "list[dict]") -> Line:
    """One run of words as a line, set in the size most of its text is set in."""
    from collections import Counter

    sizes: "Counter[float]" = Counter()
    for word in words:
        text = (word.get("text") or "").strip()
        if text:
            sizes[round(float(word.get("size") or 0), 1)] += len(text)
    return Line(
        text=" ".join((word.get("text") or "") for word in words).strip(),
        size=sizes.most_common(1)[0][0] if sizes else 0.0,
        x0=min(float(word["x0"]) for word in words),
        top=min(float(word["top"]) for word in words),
        x1=max(float(word["x1"]) for word in words),
        bottom=max(float(word["bottom"]) for word in words),
    )


def _grouped(lines: "list[Line]") -> "list[list[Line]]":
    """Lines gathered into the runs they were set as.

    Two tests, and a line has to pass both to join a run: it has to follow
    closely enough to be the next line of the same paragraph, and it has to sit
    under that run rather than beside it. The second is what keeps two columns
    apart once the leading happens to line up across the gutter.

    Against every open run rather than only the newest one, because on a
    two-column page the newest one is the other column: the lines arrive down
    the page, so they alternate left, right, left, and a line compared only
    with the one before it never finds the run it belongs to. The scan stops
    at the first run far enough above that nothing below could reach it, which
    on a page set in one column is the run just closed.
    """
    groups: "list[list[Line]]" = []
    for line in sorted(lines, key=lambda piece: (piece.top, piece.x0)):
        for group in reversed(groups):
            last = group[-1]
            height = max(last.bottom - last.top, line.bottom - line.top, 1.0)
            if line.top - last.bottom > height * _BLOCK_GAP:
                continue
            if min(last.x1, line.x1) > max(last.x0, line.x0):
                group.append(line)
                break
        else:
            groups.append([line])
    return groups


def _block(lines: "list[Line]") -> Block:
    return Block(
        lines=tuple(lines),
        x0=min(line.x0 for line in lines),
        top=min(line.top for line in lines),
        x1=max(line.x1 for line in lines),
        bottom=max(line.bottom for line in lines),
    )


__all__ = ["Block", "Document", "Line", "Page", "PasswordProtectedError", "open_document"]
