"""Everything read back out of the rendered PDF: pixels, page sizes, word boxes.

Two backends, in a fixed order. PDFium through `pypdfium2` is preferred because it
is in-process -- no subprocess, no argv, nothing to time out -- and because it
ships in the `ppt` extra, so it is present wherever the capability is installed.
Poppler's `pdftoppm` / `pdftotext` are the fallback for a machine that has
LibreOffice from the distribution but no wheels, which is the common shape of a
minimal container.

Both are converted into one convention, defined once in
`raven.ppt.contracts.rendered`: points, origin top-left, y downward. Poppler is
already there; PDFium reports text with the origin at the bottom-left and y going
up, and gets flipped here. Getting that flip wrong is not a crash, it is a
measurement that reads a page upside down and reports collisions between things
that are nowhere near each other, so the test suite pins the convention with a
deck whose text is known to sit near the top.
"""

from __future__ import annotations

import html
import math
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from raven.ppt.contracts.rendered import PageSize, WordBox
from raven.ppt.services.render import process
from raven.ppt.services.render.capabilities import pdfium
from raven.ppt.services.render.errors import RenderError, RenderUnavailableError

# 144dpi renders a 16:9 slide at exactly 1920x1080, which is what a vision pass
# wants: enough to read 14pt body copy, small enough to send several at once.
DEFAULT_DPI = 144
# Above this, a page carries no more information a reader or a model can use, and
# the PNG starts costing real bytes on every review round.
MAX_DPI = 600
# A slide is at most ~13in wide, so this is only reachable by a PDF that is not a
# deck -- a poster-sized page at 144dpi would otherwise ask for a bitmap large
# enough to end the process.
MAX_SIDE_PX = 10_000
# Poppler on a big deck: seconds, not minutes.
DEFAULT_POPPLER_TIMEOUT_S = 60.0

# Three digits because a deck is never 1000 pages and `page-7.png` sorting after
# `page-10.png` has bitten every tool that used the plain number.
PAGE_PNG = "page-{page:03d}.png"

_PDFTOPPM_PAGE = re.compile(r"-(\d+)\.png$")


def page_count(pdf: Path, *, pdftotext: str | None = None, timeout_s: float = DEFAULT_POPPLER_TIMEOUT_S) -> int:
    """How many pages the render produced, which is not always how many were built."""
    module = pdfium()
    if module is None:
        return len(page_sizes(pdf, pdftotext=pdftotext, timeout_s=timeout_s))
    document = _open(module, Path(pdf))
    try:
        return len(document)
    finally:
        document.close()


def page_sizes(
    pdf: Path,
    *,
    pdftotext: str | None = None,
    timeout_s: float = DEFAULT_POPPLER_TIMEOUT_S,
) -> dict[int, PageSize]:
    """Each page's paper size in points, keyed by 1-based page number."""
    module = pdfium()
    if module is not None:
        document = _open(module, Path(pdf))
        try:
            sizes = {}
            for index in range(len(document)):
                page = document[index]
                try:
                    width, height = page.get_size()
                finally:
                    page.close()
                sizes[index + 1] = PageSize(width_pt=float(width), height_pt=float(height))
            return sizes
        finally:
            document.close()
    sizes, _ = _parse_bbox_xml(_bbox_xml(Path(pdf), pdftotext, timeout_s))
    return sizes


def to_pngs(
    pdf: Path,
    out_dir: Path,
    dpi: int = DEFAULT_DPI,
    pages: list[int] | None = None,
    *,
    pdftoppm: str | None = None,
    timeout_s: float = DEFAULT_POPPLER_TIMEOUT_S,
) -> list[Path]:
    """One PNG per page, named `page-001.png`, returned in page order.

    `pages` is 1-based and selects a subset; None means every page. The file name
    always carries the true page number, so a subset stays traceable -- a finding
    that cites a page has to cite the page the reader will turn to.
    """
    source = Path(pdf)
    if not source.is_file():
        raise RenderError(f"there is no PDF to rasterise at {source}")
    if not 1 <= int(dpi) <= MAX_DPI:
        raise RenderError(f"dpi must be between 1 and {MAX_DPI}, got {dpi}")
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    module = pdfium()
    if module is not None:
        return _pdfium_pngs(module, source, destination, int(dpi), pages)
    binary = pdftoppm or shutil.which("pdftoppm")
    if binary is None:
        raise RenderUnavailableError(
            "no PDF rasteriser is installed: install pypdfium2 (pip install 'raven[ppt]') or poppler-utils; "
            "without one the deck can be built and exported but not looked at"
        )
    return _poppler_pngs(binary, source, destination, int(dpi), pages, timeout_s)


def page_pixels(size: PageSize, dpi: int) -> tuple[int, int]:
    """The pixel size a page of `size` comes out as at `dpi`.

    Both backends round the same way -- ceil on each axis, independently -- and
    that was measured rather than derived: PDFium and `pdftoppm` agree exactly on
    a 959.976x540pt deck page and on a fractional 300.5x200.25pt one at 72, 100,
    144, 150 and 201dpi.
    """
    scale = int(dpi) / 72
    return math.ceil(size.width_pt * scale), math.ceil(size.height_pt * scale)


def is_page_render(size: PageSize, pixels: tuple[int, int]) -> bool:
    """Whether `pixels` is what a page of `size` comes out as at some resolution.

    The resolution a PNG was written at is not recorded in the file -- PDFium
    writes no `pHYs` chunk -- so what there is to check against is the page size
    the PDF records, and the question is whether any dpi `to_pngs` accepts maps
    that page onto exactly these pixels. The same page at 72dpi and at 144 both
    answer yes, because both are true renders of it; an image of another shape, a
    thumbnail, or a 1x1 placeholder answers no at every resolution in range.
    """
    return any(page_pixels(size, dpi) == pixels for dpi in range(1, MAX_DPI + 1))


def page_number(png: Path) -> int | None:
    """The 1-based page a rasterised file's name carries, or None when it has none."""
    match = _PDFTOPPM_PAGE.search(Path(png).name)
    return int(match.group(1)) if match else None


def unusable_pages(sizes: Mapping[int, PageSize], pngs: Iterable[Path]) -> dict[int, str]:
    """Which of these page images did not come out, and what is wrong with each.

    A sanity pass to run before anything measures a page image, because a blank or
    mis-sized PNG has none of what the measurements look for: every one of them
    passes, and a page that never rendered is reported as a page measured clean.

    Keyed by page number, in page order, empty when every image is usable. Pages
    `sizes` does not name are skipped -- a review directory outlives the deck in
    it, and an image left behind by a longer build is not this deck's page.

    Nearly-flat pages are deliberately not in here: telling "one colour and a
    stray pixel" from "a page with very little on it" needs a share of the
    histogram to call flat, and that number has not been derived from anything.
    A page of exactly one colour needs no such number.
    """
    verdicts: dict[int, str] = {}
    for png in pngs:
        number = page_number(png)
        if number is None or number not in sizes:
            continue
        reason = _unusable(Path(png), sizes[number])
        if reason is not None:
            verdicts[number] = reason
    return dict(sorted(verdicts.items()))


def _unusable(png: Path, size: PageSize) -> str | None:
    if not png.is_file():
        return "no file was written for it"
    if png.stat().st_size == 0:
        return "the file written for it is empty"
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with the ppt extra
        return None
    try:
        with Image.open(png) as opened:
            opened.load()
            pixels = (opened.width, opened.height)
            colours = opened.convert("RGB").getcolors(maxcolors=2)
    except Exception:  # noqa: BLE001 - Pillow raises its own type per cause
        return "the file written for it cannot be read as an image"
    if not is_page_render(size, pixels):
        return (
            f"it came out {pixels[0]}x{pixels[1]}px, which is not a "
            f"{size.width_pt:g}x{size.height_pt:g}pt page at any resolution"
        )
    if colours is not None and len(colours) == 1:
        return "it came out a single flat colour"
    return None


def word_boxes(
    pdf: Path,
    *,
    pdftotext: str | None = None,
    timeout_s: float = DEFAULT_POPPLER_TIMEOUT_S,
) -> dict[int, list[WordBox]]:
    """Every rendered word on every page, in points from the top-left.

    Keyed by 1-based page number; a page with no text maps to an empty list rather
    than being absent, so a caller can tell "nothing on it" from "not rendered".
    """
    source = Path(pdf)
    if not source.is_file():
        raise RenderError(f"there is no PDF to measure at {source}")
    module = pdfium()
    if module is not None:
        try:
            return _pdfium_words(module, source)
        except _RotatedPageError as exc:
            # PDFium reports text in unrotated page space, so the flip below would
            # be wrong for a rotated page. Poppler applies the rotation itself, so
            # hand the job over rather than returning numbers that look fine.
            if (poppler := pdftotext or shutil.which("pdftotext")) is None:
                raise RenderError(
                    f"page {exc.page} is rotated, which this reader cannot measure; "
                    "install poppler-utils (pdftotext) to measure rotated pages"
                ) from exc
            _, words = _parse_bbox_xml(_bbox_xml(source, poppler, timeout_s))
            return words
    _, words = _parse_bbox_xml(_bbox_xml(source, pdftotext, timeout_s))
    return words


# --- PDFium ----------------------------------------------------------------


class _RotatedPageError(Exception):
    def __init__(self, page: int) -> None:
        super().__init__(f"page {page} is rotated")
        self.page = page


def _open(module: ModuleType, pdf: Path) -> Any:
    if not pdf.is_file():
        raise RenderError(f"there is no PDF at {pdf}")
    try:
        return module.PdfDocument(str(pdf))
    except Exception as exc:  # pypdfium2 raises its own error type for every cause
        raise RenderError(f"{pdf.name} cannot be opened as a PDF: {exc}") from exc


def _pdfium_pngs(module: ModuleType, pdf: Path, out_dir: Path, dpi: int, pages: list[int] | None) -> list[Path]:
    document = _open(module, pdf)
    try:
        wanted = _selected(pages, len(document))
        scale = dpi / 72
        written: list[Path] = []
        for number in wanted:
            page = document[number - 1]
            try:
                width, height = page.get_size()
                if max(width, height) * scale > MAX_SIDE_PX:
                    raise RenderError(
                        f"page {number} would rasterise to {width * scale:.0f}x{height * scale:.0f}px at {dpi}dpi; "
                        "lower the dpi"
                    )
                bitmap = page.render(scale=scale)
                target = out_dir / PAGE_PNG.format(page=number)
                bitmap.to_pil().save(target, "PNG", optimize=True)
            finally:
                page.close()
            written.append(target)
        return written
    finally:
        document.close()


def _pdfium_words(module: ModuleType, pdf: Path) -> dict[int, list[WordBox]]:
    document = _open(module, pdf)
    try:
        found: dict[int, list[WordBox]] = {}
        for index in range(len(document)):
            number = index + 1
            page = document[index]
            try:
                if page.get_rotation():
                    raise _RotatedPageError(number)
                _, height = page.get_size()
                textpage = page.get_textpage()
                try:
                    found[number] = _words_on_page(textpage, number, float(height))
                finally:
                    textpage.close()
            finally:
                page.close()
        return found
    finally:
        document.close()


# How far apart two characters can sit and still belong to one word, as a share of
# the line's height. Characters inside a word touch (their loose boxes share an
# edge), so anything here only has to stay under the width of a space -- roughly
# 0.3 of the line height for Latin type -- because a PDF that positions its spaces
# instead of writing them has no whitespace character to break on, and joining
# across such a gap would fuse two neighbouring shapes into one box that overlaps
# everything between them.
_MAX_GAP_RATIO = 0.25
# Text runs left to right within a shape; a jump back this far means the next
# shape started.
_MAX_BACKWARD_RATIO = 0.35
# Two characters are on the same line when their boxes share most of their height.
# Loose boxes on one line are identical vertically, so this only has to tolerate
# superscripts and mixed sizes.
_MIN_LINE_OVERLAP = 0.6

# Characters that occupy no space and end a word without starting one: the
# zero-width family and a soft hyphen. They arrive in copy pasted out of a browser
# and would otherwise be measured as text. Written as escapes on purpose -- as
# literals they are invisible in the source too.
_INVISIBLE = "\u200b\u200c\u200d\ufeff\u00ad"


def _words_on_page(textpage: Any, number: int, page_height: float) -> list[WordBox]:
    count = textpage.count_chars()
    if not count:
        return []
    text = textpage.get_text_range(0, count)
    if len(text) != count:
        # PDFium can materialise characters (a generated line break) that the char
        # index does not carry, which shifts every following character. Paying for
        # one call per character is worth never labelling a box with a neighbour's
        # text.
        text = "".join(textpage.get_text_range(index, 1) for index in range(count))
    words: list[WordBox] = []
    run: list[float] | None = None
    letters: list[str] = []
    for index, char in enumerate(text):
        if char.isspace() or char in _INVISIBLE:
            run, letters = _flush(words, number, run, letters)
            continue
        left, bottom, right, top = textpage.get_charbox(index, loose=True)
        # The flip: PDFium measures y up from the bottom of the page, the contract
        # measures it down from the top.
        box = [float(left), page_height - float(top), float(right), page_height - float(bottom)]
        if run is not None and _continues(run, box):
            run[0] = min(run[0], box[0])
            run[1] = min(run[1], box[1])
            run[2] = max(run[2], box[2])
            run[3] = max(run[3], box[3])
            letters.append(char)
            continue
        _flush(words, number, run, letters)
        run, letters = box, [char]
    _flush(words, number, run, letters)
    return words


def _flush(
    words: list[WordBox],
    number: int,
    run: list[float] | None,
    letters: list[str],
) -> tuple[None, list[str]]:
    if run is not None and letters:
        words.append(WordBox(page=number, text="".join(letters), x0=run[0], y0=run[1], x1=run[2], y1=run[3]))
    return None, []


def _continues(run: list[float], box: list[float]) -> bool:
    """Whether `box` belongs to the run being accumulated."""
    tall, thin = max(run[3] - run[1], box[3] - box[1]), min(run[3] - run[1], box[3] - box[1])
    if thin <= 0:
        return False
    shared = min(run[3], box[3]) - max(run[1], box[1])
    if shared / thin < _MIN_LINE_OVERLAP:
        return False
    gap = box[0] - run[2]
    return -_MAX_BACKWARD_RATIO * tall <= gap <= _MAX_GAP_RATIO * tall


# --- poppler ---------------------------------------------------------------


def _poppler_pngs(
    binary: str,
    pdf: Path,
    out_dir: Path,
    dpi: int,
    pages: list[int] | None,
    timeout_s: float,
) -> list[Path]:
    """Rasterise through `pdftoppm`, which names its own output.

    It writes `<prefix>-<page>.png` with the digit count taken from the document's
    page count, so the file name cannot be predicted from a single page number.
    Everything therefore lands in a private directory and is read back by page
    number -- which also answers "how many pages" for the whole-document case
    without needing a second tool to count them.

    It also means a selection cannot be validated up front the way the PDFium path
    validates it, because nothing here knows the page count until poppler has run.
    A page past the end is therefore reported after the attempt rather than before
    it, and worded so that the caller reads the same reason from either backend --
    poppler's own account of it ("the first page (3) can not be after the last page
    (2)") rides along in the detail.
    """
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="raven-ppt-pdftoppm-") as scratch:
        prefix = Path(scratch) / "page"
        runs = [None] if pages is None else _selected(pages, None)
        for number in runs:
            command = [binary, "-png", "-r", str(dpi)]
            if number is not None:
                command += ["-f", str(number), "-l", str(number)]
            command += [str(pdf), str(prefix)]
            completed = process.run(command, timeout_s=timeout_s, what="pdftoppm")
            produced = sorted(Path(scratch).glob("page-*.png"))
            if not produced:
                asked = f"page {number} of {pdf.name}" if number is not None else pdf.name
                raise RenderError(
                    f"pdftoppm rasterised nothing for {asked}: it is either outside this document "
                    "or the PDF cannot be read",
                    detail=completed.log_tails,
                )
            for image in produced:
                match = _PDFTOPPM_PAGE.search(image.name)
                page_number = number if number is not None else int(match.group(1)) if match else None
                if page_number is None:
                    raise RenderError(f"pdftoppm wrote {image.name}, which carries no page number")
                target = out_dir / PAGE_PNG.format(page=page_number)
                shutil.move(str(image), str(target))
                written.append(target)
    return sorted(set(written))


def _bbox_xml(pdf: Path, pdftotext: str | None, timeout_s: float) -> str:
    binary = pdftotext or shutil.which("pdftotext")
    if binary is None:
        raise RenderUnavailableError(
            "no PDF text reader is installed: install pypdfium2 (pip install 'raven[ppt]') or poppler-utils; "
            "without one the rendered page cannot be measured and only declared geometry is checked"
        )
    completed = process.run([binary, "-bbox", str(pdf), "-"], timeout_s=timeout_s, what="pdftotext")
    if completed.returncode != 0:
        raise RenderError(f"pdftotext could not read {pdf.name}", detail=completed.log_tails)
    return completed.stdout


_NUMBER = r"-?[0-9]+(?:\.[0-9]+)?"
# Poppler emits both elements with their attributes in a fixed order; the
# whitespace is loose because the indentation has changed between releases.
_BBOX_ELEMENT = re.compile(
    rf'<page\s+width="(?P<width>{_NUMBER})"\s+height="(?P<height>{_NUMBER})"'
    rf'|<word\s+xMin="(?P<x0>{_NUMBER})"\s+yMin="(?P<y0>{_NUMBER})"'
    rf'\s+xMax="(?P<x1>{_NUMBER})"\s+yMax="(?P<y1>{_NUMBER})"\s*>(?P<text>[^<]*)</word>'
)


def _parse_bbox_xml(xml: str) -> tuple[dict[int, PageSize], dict[int, list[WordBox]]]:
    """Pull page sizes and word boxes out of `pdftotext -bbox` output.

    Regex rather than an XML parser because the output is XHTML with a DOCTYPE and
    a default namespace, which costs a namespace dance to walk, and because the
    two elements that matter are flat. The word text is unescaped: poppler writes
    `R&amp;D`, and a gate comparing that against source material would find no
    match for a word that is in it.
    """
    sizes: dict[int, PageSize] = {}
    words: dict[int, list[WordBox]] = {}
    number = 0
    for match in _BBOX_ELEMENT.finditer(xml):
        if match.group("width") is not None:
            number += 1
            sizes[number] = PageSize(width_pt=float(match.group("width")), height_pt=float(match.group("height")))
            words[number] = []
            continue
        if number == 0:  # pragma: no cover - poppler always opens a page first
            continue
        text = html.unescape(match.group("text"))
        if not text.strip():
            continue
        words[number].append(
            WordBox(
                page=number,
                text=text,
                x0=float(match.group("x0")),
                y0=float(match.group("y0")),
                x1=float(match.group("x1")),
                y1=float(match.group("y1")),
            )
        )
    return sizes, words


def _selected(pages: list[int] | None, count: int | None) -> list[int]:
    """Validate a 1-based page selection; None means every page of `count`."""
    if pages is None:
        if count is None:  # pragma: no cover - guarded by the callers
            raise RenderError("a page count is needed to select every page")
        if count < 1:
            raise RenderError("the PDF has no pages")
        return list(range(1, count + 1))
    wanted = sorted({int(page) for page in pages})
    if not wanted:
        raise RenderError("no pages were selected")
    for page in wanted:
        if page < 1 or (count is not None and page > count):
            raise RenderError(
                f"page {page} is outside this document"
                + (f", which has {count} page{'s' if count != 1 else ''}" if count is not None else "")
            )
    return wanted
