"""The render chain, end to end and at its edges.

The chain is the deck's only mirror: what the author looks at, what the design
pass judges, and the surface every rendered-page measurement is taken off. It is
also the part of the capability made almost entirely of other people's software --
LibreOffice, PDFium, poppler, Pillow -- so what is worth testing is not the code's
branches but its agreements with them: that the coordinates come back in the units
and the orientation the contract promises, that the fallback backend reads the same
page as the preferred one, and that two conversions at once both produce a file.

Prerequisites, and what is skipped without them:

    libreoffice   pptx -> pdf. Without it, everything downstream of a real deck
                  skips; the parsers and the segmenter still run.
    pypdfium2     preferred reader. Absent, poppler covers it.
    poppler-utils pdftoppm / pdftotext, the fallback. Absent, its parity test skips.
"""

from __future__ import annotations

import inspect
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from raven_ppt.contracts.rendered import PageSize, WordBox
from raven_ppt.services.render import capabilities, office, process, sheet
from raven_ppt.services.render import pdf as render_pdf
from raven_ppt.services.render.errors import RenderError, RenderTimeoutError, RenderUnavailableError
from raven_ppt.services.render.service import DeckRenderer, LocalDeckRenderer
from tests._ppt_engine_fixtures import deck, image, noise_image, noise_png, product_page, template_file  # noqa: F401

CAPS = capabilities.available()

needs_soffice = pytest.mark.skipif(
    not CAPS.can_convert,
    reason="LibreOffice is not installed (apt install libreoffice-impress)",
)
needs_reader = pytest.mark.skipif(
    not (CAPS.can_rasterise and CAPS.can_measure_words),
    reason="no PDF reader installed (pip install 'raven[ppt]', or apt install poppler-utils)",
)
needs_poppler = pytest.mark.skipif(
    not (CAPS.pdftoppm and CAPS.pdftotext),
    reason="poppler-utils is not installed, so the fallback backend cannot be compared",
)

# Where the fixture deck puts its copy, in inches on a 13.333x7.5in canvas. The
# convention test reads these back out of the render, so they are named rather
# than repeated.
TOP_IN = 0.5
BOTTOM_IN = 6.4
LEFT_IN = 0.5
TOP_LINE = "Alpha revenue grew"
BOTTOM_LINE = "Footnote sits at the bottom"
SECOND_PAGE_LINE = "Beta margin held"


def build_deck(path: Path, lines: tuple[tuple[str, float], ...] | None = None) -> Path:
    """A two-page deck with copy at a known place on the page.

    `word_wrap` on purpose: python-pptx writes text boxes with `wrap="none"` and
    autofit, and LibreOffice grows such a box around its centre, which moves the
    copy away from the box's declared left edge. Wrapping keeps the text where it
    was put, which is what makes the coordinate assertions meaningful.
    """
    pptx = pytest.importorskip("pptx", reason="python-pptx is needed to build a deck to render")
    from pptx.util import Inches, Pt

    presentation = pptx.Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)

    def add(slide, text: str, top: float, size: int) -> None:
        box = slide.shapes.add_textbox(Inches(LEFT_IN), Inches(top), Inches(9), Inches(0.9))
        frame = box.text_frame
        frame.word_wrap = True
        frame.text = text
        frame.paragraphs[0].runs[0].font.size = Pt(size)

    first = presentation.slides.add_slide(presentation.slide_layouts[6])
    for text, top in lines or ((TOP_LINE, TOP_IN), (BOTTOM_LINE, BOTTOM_IN)):
        add(first, text, top, 30 if top < 1 else 14)
    second = presentation.slides.add_slide(presentation.slide_layouts[6])
    add(second, SECOND_PAGE_LINE, TOP_IN, 30)
    path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(path))
    return path


@pytest.fixture(scope="module")
def deck(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_deck(tmp_path_factory.mktemp("deck") / "deck.pptx")


@pytest.fixture(scope="module")
def rendered(deck: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The deck as a PDF. One conversion, shared by everything that reads it."""
    if not CAPS.can_convert:
        pytest.skip("LibreOffice is not installed (apt install libreoffice-impress)")
    return LocalDeckRenderer().to_pdf(deck, tmp_path_factory.mktemp("pdf"))


# --- capability probing ------------------------------------------------------


def test_the_probe_reports_each_link_of_the_chain_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing converter and a missing reader are different problems.

    Without LibreOffice there is nothing to look at; without PDFium the fallback
    binary does the same job. A caller that cannot tell them apart either warns
    about nothing or refuses when it did not have to.
    """
    monkeypatch.setattr(capabilities, "find_soffice", lambda: None)
    monkeypatch.setattr(capabilities, "pdfium", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    bare = capabilities.available()
    assert not bare.can_convert
    assert bare.can_rasterise and bare.can_measure_words  # poppler still there
    assert "libreoffice" in bare.explain()

    monkeypatch.setattr("shutil.which", lambda name: None)
    empty = capabilities.available()
    assert not empty.can_rasterise and not empty.can_measure_words
    assert "pypdfium2" in empty.explain()
    # Each gap named once, however many capabilities it takes out.
    assert len(empty.missing()) == len(set(empty.missing()))


def test_a_configured_soffice_path_wins_over_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capabilities, "find_soffice", lambda: "/usr/bin/soffice")
    assert capabilities.available(soffice="/opt/lo/soffice").soffice == "/opt/lo/soffice"


def test_the_local_renderer_offers_exactly_what_the_protocol_promises() -> None:
    """Parameter names included, because a caller may pass them by keyword.

    The protocol is what the rest of the capability is written against, so a
    method that quietly renamed an argument would only fail at the one call site
    that used the keyword -- and this implementation had `pdf` on one side and
    `pdf_path` on the other until this test was written.
    """
    for name in ("available", "to_pdf", "to_pngs", "word_boxes", "page_sizes", "page_count", "contact_sheet"):
        promised = inspect.signature(getattr(DeckRenderer, name))
        offered = inspect.signature(getattr(LocalDeckRenderer, name))
        assert list(promised.parameters) == list(offered.parameters), f"{name} does not match the protocol"


def test_a_complete_chain_says_so() -> None:
    if not CAPS.missing():
        assert CAPS.explain() == "the render chain is complete"
    assert CAPS.can_contact_sheet, "Pillow is a hard dependency of raven"


# --- what a missing dependency does -----------------------------------------


def test_without_libreoffice_the_error_names_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(office, "find_soffice", lambda: None)
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not really a deck, and never opened")
    with pytest.raises(RenderUnavailableError, match="LibreOffice is not installed") as caught:
        office.to_pdf(deck, tmp_path / "out")
    assert caught.value.as_detail()["code"] == "renderer_unavailable"
    assert caught.value.as_detail()["retryable"] is False


def test_without_a_reader_the_error_says_what_to_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(render_pdf, "pdfium", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    document = tmp_path / "deck.pdf"
    document.write_bytes(b"%PDF-1.7\n")
    with pytest.raises(RenderUnavailableError, match="pypdfium2"):
        render_pdf.to_pngs(document, tmp_path / "pages")
    with pytest.raises(RenderUnavailableError, match="poppler"):
        render_pdf.word_boxes(document)


def test_a_deck_that_is_not_there_is_reported_as_such(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="no deck to convert"):
        office.to_pdf(tmp_path / "absent.pptx", tmp_path)
    with pytest.raises(RenderError, match="no PDF to rasterise"):
        render_pdf.to_pngs(tmp_path / "absent.pdf", tmp_path)
    with pytest.raises(RenderError, match="no PDF to measure"):
        render_pdf.word_boxes(tmp_path / "absent.pdf")


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="the surviving-process check reads /proc")
def test_a_timeout_takes_the_whole_process_tree_with_it(tmp_path: Path) -> None:
    """The reason every subprocess here goes through `process.run`.

    `/usr/bin/soffice` is a shell wrapper that execs `oosplash`, which forks
    `soffice.bin`. Killing what was started leaves the worker running, holding the
    throwaway profile open while the cleanup tries to delete it. That leak was
    found on a live machine -- an `oosplash` reparented to init, days old, still
    pointing at a temporary profile path that no longer existed. So the child gets
    its own session and a timeout kills the group.

    Stood in for by a Python process that spawns a sleeping child and waits on it:
    the same shape as the wrapper, and it needs neither LibreOffice nor anything on
    PATH, which a shell one-liner calling `sleep` would.
    """
    marker = "97.13579"
    grandchild = f"import time; time.sleep({marker})"
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {grandchild!r}]).wait()"
    with pytest.raises(RenderTimeoutError, match="budget") as caught:
        process.run([sys.executable, "-c", parent], timeout_s=1.0, what="a stand-in for LibreOffice")
    assert caught.value.retryable is True
    assert caught.value.as_detail()["code"] == "render_timeout"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _running_with(marker):
        time.sleep(0.1)
    assert not _running_with(marker), "the grandchild outlived the process that was killed"


def _running_with(marker: str) -> list[int]:
    """PIDs whose command line mentions `marker`, ignoring processes we cannot read."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if marker in (entry / "cmdline").read_bytes().decode("utf-8", "replace"):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


# --- the chain --------------------------------------------------------------


@needs_soffice
@needs_reader
def test_the_chain_runs_from_a_deck_to_pages_and_words(rendered: Path, tmp_path: Path) -> None:
    renderer = LocalDeckRenderer()
    assert rendered.is_file() and rendered.stat().st_size > 0
    assert rendered.name == "deck.pdf", "the PDF is named after the deck, not after LibreOffice's mood"
    assert [entry.name for entry in rendered.parent.iterdir()] == ["deck.pdf"], (
        "the conversion left a profile or a staging directory behind"
    )
    assert renderer.page_count(rendered) == 2

    pages = renderer.to_pngs(rendered, tmp_path / "pages")
    assert [page.name for page in pages] == ["page-001.png", "page-002.png"]
    assert all(page.stat().st_size > 0 for page in pages)

    image = pytest.importorskip("PIL.Image", reason="Pillow reads back the page size")
    with image.open(pages[0]) as first:
        # 144dpi on a 13.333x7.5in slide, which is the whole reason for that default.
        assert first.size == (1920, 1080)

    words = renderer.word_boxes(rendered)
    assert sorted(words) == [1, 2]
    assert _line(words[1]).startswith(TOP_LINE)
    assert BOTTOM_LINE.split()[0] in _line(words[1])
    assert _line(words[2]) == SECOND_PAGE_LINE


@needs_soffice
@needs_reader
def test_word_boxes_are_points_measured_from_the_top_left(rendered: Path) -> None:
    """The convention the measuring stages are built on, pinned to a known deck.

    Both halves matter. Points, not pixels: the same box at 144dpi would read
    ~2x larger and every geometry comparison against the .pptx would be off by
    that factor. From the top, not the bottom: with the axis flipped, copy at the
    top of the page reads as copy at the bottom, and a measurement pass would
    report collisions between things a reader sees nowhere near each other.
    """
    renderer = LocalDeckRenderer()
    sizes = renderer.page_sizes(rendered)
    assert sizes[1] == PageSize(width_pt=pytest.approx(960, abs=1), height_pt=pytest.approx(540, abs=1))
    page = sizes[1]
    words = {word.text: word for word in renderer.word_boxes(rendered)[1]}

    top = words["Alpha"]
    bottom = words["Footnote"]
    # 0.5in from the left plus the 0.1in text inset LibreOffice applies.
    assert top.x0 == pytest.approx(43.2, abs=3)
    # 0.5in from the top, so a small number -- not 540 - a small number.
    assert 36 <= top.y0 <= 60, f"copy 0.5in from the top came back at y0={top.y0:.1f}"
    assert top.y1 > top.y0, "y grows downward, so the bottom edge is the larger number"
    # 6.4in down a 7.5in page: past three quarters of it, and nowhere near 930,
    # which is where 144dpi pixels would have put it.
    assert 0.75 * page.height_pt < bottom.y0 < page.height_pt
    for word in words.values():
        assert 0 <= word.x0 < word.x1 <= page.width_pt + 1
        assert 0 <= word.y0 < word.y1 <= page.height_pt + 1


@needs_soffice
@needs_reader
def test_a_subset_of_pages_keeps_the_page_numbers_it_was_asked_for(rendered: Path, tmp_path: Path) -> None:
    """Page 2 rendered alone is still page 2.

    A finding cites a page number, and a reviewer sent "the second page you asked
    for" numbered as page 1 puts every finding it produced on the wrong page.
    """
    pages = LocalDeckRenderer().to_pngs(rendered, tmp_path / "one", 72, [2])
    assert [page.name for page in pages] == ["page-002.png"]


@needs_soffice
@needs_reader
def test_an_impossible_request_is_refused_with_the_reason(rendered: Path, tmp_path: Path) -> None:
    """And with the same reason whichever backend is installed.

    PDFium knows the page count before it starts and says "page 3 is outside this
    document, which has 2 pages"; poppler only finds out by trying, and says so
    afterwards. Both have to reach the caller as the same problem, or a tool's
    error text depends on which wheels the machine has -- which is how the phrase
    asserted here came to be shared rather than each backend keeping its own.
    """
    renderer = LocalDeckRenderer()
    with pytest.raises(RenderError, match="outside this document"):
        renderer.to_pngs(rendered, tmp_path / "out", 72, [3])
    with pytest.raises(RenderError, match="dpi must be between"):
        renderer.to_pngs(rendered, tmp_path / "out", render_pdf.MAX_DPI + 1)
    with pytest.raises(RenderError, match="no pages were selected"):
        renderer.to_pngs(rendered, tmp_path / "out", 72, [])


@needs_soffice
@needs_poppler
def test_poppler_reads_the_same_page_as_pdfium(
    rendered: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is only useful if it agrees with the preferred backend.

    It is also the backend nobody runs, so nobody notices when it drifts. The two
    disagree slightly on vertical extent by design -- poppler pads a word's box to
    the font's full ascent and descent, PDFium reports the line box -- so the
    horizontal edges are held tight and the vertical ones loosely.
    """
    if not CAPS.pdfium:
        pytest.skip("PDFium is not installed, so there is nothing to compare poppler against")
    preferred = render_pdf.word_boxes(rendered)
    preferred_pngs = render_pdf.to_pngs(rendered, tmp_path / "pdfium", 72)

    monkeypatch.setattr(render_pdf, "pdfium", lambda: None)
    fallback = render_pdf.word_boxes(rendered)
    fallback_pngs = render_pdf.to_pngs(rendered, tmp_path / "poppler", 72)

    assert [p.name for p in fallback_pngs] == [p.name for p in preferred_pngs]
    assert render_pdf.page_count(rendered) == len(preferred)
    for number, words in preferred.items():
        # Order differs: poppler sorts into reading order across the page, PDFium
        # keeps content-stream order. Compare by word, not by position.
        by_text = {word.text: word for word in fallback[number]}
        if set(by_text) != {word.text for word in words}:
            # Poppler versions drift on where a word ends ("bottom" vs
            # "bot" + "tom"). The same characters prove the fallback read the
            # same page, and the box comparison below needs matching words, so
            # pure tokenization drift is this machine's skip, not a fail.
            preferred_chars = sorted("".join(word.text for word in words))
            fallback_chars = sorted("".join(by_text))
            assert fallback_chars == preferred_chars, "the backends read different page content"
            pytest.skip("poppler tokenizes word boundaries differently on this machine")
        for word in words:
            other = by_text[word.text]
            assert other.x0 == pytest.approx(word.x0, abs=1.0)
            assert other.x1 == pytest.approx(word.x1, abs=1.0)
            assert other.y0 == pytest.approx(word.y0, abs=6.0)


@needs_soffice
def test_two_conversions_at_once_do_not_lock_each_other_out(tmp_path: Path) -> None:
    """The trap this module was written around.

    LibreOffice allows one instance per user profile: a second invocation sharing
    a profile hands its request to the first and exits 0 having written nothing.
    Measured, before the fix: two concurrent conversions, one PDF, an empty output
    directory for the loser, and not a word on stderr. Two at once is ordinary
    here -- a review renders while the author builds -- so it gets a test rather
    than a comment.
    """
    decks = [build_deck(tmp_path / f"deck{index}" / f"deck{index}.pptx") for index in (1, 2)]
    renderer = LocalDeckRenderer()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda deck: renderer.to_pdf(deck, tmp_path / "out" / deck.stem), decks))
    assert [pdf.name for pdf in results] == ["deck1.pdf", "deck2.pdf"]
    for pdf in results:
        assert pdf.is_file() and pdf.stat().st_size > 0


# --- a render that came back, checked before it is measured ------------------

# The deck canvas in points, and the fractional page that catches a rounding
# convention: 13.333x7.5in is not a whole number of points on the long edge.
DECK_PAGE = PageSize(width_pt=959.976, height_pt=540.0)
ODD_PAGE = PageSize(width_pt=300.5, height_pt=200.25)


def _rendered_png(path: Path, size: tuple[int, int], *, flat: bool = False) -> Path:
    image = pytest.importorskip("PIL.Image", reason="Pillow reads the page images back")
    draw = pytest.importorskip("PIL.ImageDraw", reason="Pillow draws something onto the page")
    page = image.new("RGB", size, (255, 255, 255))
    if not flat:
        draw.Draw(page).rectangle([1, 1, max(2, size[0] // 2), max(2, size[1] // 2)], fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    page.save(path, "PNG")
    return path


def test_a_page_comes_out_the_size_its_recorded_page_size_predicts() -> None:
    """Both backends ceil each axis independently, which was measured, not assumed.

    PDFium and `pdftoppm` were compared on these two pages at 72, 100, 144, 150 and
    201dpi and agreed on every one, so one formula answers for both.
    """
    assert render_pdf.page_pixels(DECK_PAGE, 144) == (1920, 1080)
    assert render_pdf.page_pixels(DECK_PAGE, 72) == (960, 540)
    assert render_pdf.page_pixels(ODD_PAGE, 72) == (301, 201)
    assert render_pdf.page_pixels(ODD_PAGE, 144) == (601, 401)


def test_the_same_page_at_two_resolutions_is_that_page_at_both() -> None:
    """The dpi a PNG was written at is not in the file, so the record to check
    against is the page's size in points and the question is whether any
    resolution maps it onto exactly these pixels."""
    assert render_pdf.is_page_render(DECK_PAGE, (960, 540))
    assert render_pdf.is_page_render(DECK_PAGE, (1920, 1080))
    assert render_pdf.is_page_render(DECK_PAGE, (480, 270)), "36dpi is a resolution like any other"
    assert not render_pdf.is_page_render(DECK_PAGE, (800, 600)), "another shape entirely"
    assert not render_pdf.is_page_render(DECK_PAGE, (1, 1)), "a placeholder, not a page"
    assert not render_pdf.is_page_render(DECK_PAGE, (1920, 1081)), "off by one pixel is off"


def test_a_page_image_that_did_not_come_out_is_reported_rather_than_measured(tmp_path: Path) -> None:
    """The state this pass exists for: a blank or mis-sized page image has none of
    what the pixel measurements look for, so every one of them passes."""
    sizes = {number: DECK_PAGE for number in (1, 2, 3, 4, 5)}
    _rendered_png(tmp_path / "page-001.png", (960, 540))
    _rendered_png(tmp_path / "page-002.png", (960, 540), flat=True)
    _rendered_png(tmp_path / "page-003.png", (800, 600))
    (tmp_path / "page-004.png").write_bytes(b"")
    (tmp_path / "page-005.png").write_bytes(b"not an image")

    verdicts = render_pdf.unusable_pages(sizes, sorted(tmp_path.glob("page-*.png")))

    assert list(verdicts) == [2, 3, 4, 5], "page 1 came out and the rest did not"
    assert verdicts[2] == "it came out a single flat colour"
    assert "800x600px" in verdicts[3]
    assert verdicts[4] == "the file written for it is empty"
    assert "cannot be read as an image" in verdicts[5]


def test_a_page_image_the_renderer_never_wrote_is_reported(tmp_path: Path) -> None:
    assert render_pdf.unusable_pages({1: DECK_PAGE}, [tmp_path / "page-001.png"]) == {1: "no file was written for it"}


def test_an_image_left_behind_by_a_longer_build_is_not_this_decks_page(tmp_path: Path) -> None:
    """A review directory outlives the deck in it, and page 3 of the last build is
    not a page of a deck that now has two."""
    _rendered_png(tmp_path / "page-003.png", (800, 600))
    _rendered_png(tmp_path / "cover.png", (800, 600))

    assert render_pdf.unusable_pages({1: DECK_PAGE, 2: DECK_PAGE}, sorted(tmp_path.glob("*.png"))) == {}


@needs_soffice
@needs_reader
def test_a_real_render_passes_the_sanity_pass(rendered: Path, tmp_path: Path) -> None:
    """The other direction: what LibreOffice and the rasteriser actually produce
    has to read as usable, or the pass is a source of false reports."""
    pngs = render_pdf.to_pngs(rendered, tmp_path / "pages")

    assert len(pngs) == 2
    assert render_pdf.unusable_pages(render_pdf.page_sizes(rendered), pngs) == {}


# --- the parsers, without any of the tools ----------------------------------

_BBOX_XML = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "x.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
  <page width="959.981102" height="540.000000">
    <word xMin="43.100000" yMin="-2.500000" xMax="128.200000" yMax="80.000000">R&amp;D</word>
    <word xMin="137.700000" yMin="41.000000" xMax="260.300000" yMax="80.000000">spend</word>
    <word xMin="1.000000" yMin="1.000000" xMax="2.000000" yMax="2.000000"> </word>
  </page>
  <page width="720.000000" height="540.000000">
    <word xMin="43.100000" yMin="41.000000" xMax="112.200000" yMax="80.000000">&lt;next&gt;</word>
  </page>
</doc></body></html>
"""


def test_the_bbox_parser_keeps_pages_apart_and_unescapes_the_text() -> None:
    """Two details that only look cosmetic.

    `R&amp;D` left escaped is a word no search can find in source material that
    plainly contains it. A negative coordinate -- poppler emits them for copy
    whose box starts above the page edge -- fails a digits-only pattern, and the
    predecessor's regex was digits-only, so those words silently vanished from the
    measurement.
    """
    sizes, words = render_pdf._parse_bbox_xml(_BBOX_XML)
    assert sizes == {
        1: PageSize(width_pt=959.981102, height_pt=540.0),
        2: PageSize(width_pt=720.0, height_pt=540.0),
    }
    assert [word.text for word in words[1]] == ["R&D", "spend"], "whitespace-only words are not copy"
    assert words[1][0].y0 == -2.5
    assert [word.text for word in words[2]] == ["<next>"]
    assert {word.page for word in words[1]} == {1}


def test_an_empty_page_is_present_and_empty_rather_than_absent() -> None:
    _, words = render_pdf._parse_bbox_xml('<page width="720.000000" height="540.000000">\n</page>')
    assert words == {1: []}


_PAGE_HEIGHT = 540.0


class _StubTextPage:
    """Enough of PDFium's text page to drive the segmenter.

    Boxes are given the way a reader thinks about them -- left, right, top,
    bottom, measured down from the top of the page -- and handed back the way
    PDFium hands them over, which is the flip the segmenter has to undo.
    """

    def __init__(self, chars: list[tuple[str, float, float, float, float]]) -> None:
        self._chars = chars

    def count_chars(self) -> int:
        return len(self._chars)

    def get_text_range(self, start: int, count: int) -> str:
        return "".join(char for char, *_ in self._chars[start : start + count])

    def get_charbox(self, index: int, loose: bool = False) -> tuple[float, float, float, float]:
        _, x0, x1, top, bottom = self._chars[index]
        return (x0, _PAGE_HEIGHT - bottom, x1, _PAGE_HEIGHT - top)


def _row(text: str, x: float, top: float, *, width: float = 10.0, height: float = 20.0, step: float = 10.0):
    return [(char, x + index * step, x + index * step + width, top, top + height) for index, char in enumerate(text)]


def test_words_break_where_the_line_or_the_shape_breaks() -> None:
    """What counts as one word when the PDF only carries characters.

    Whitespace is the obvious break. The other two are not: a jump across the page
    on the same baseline is a second shape, and joining across it would produce one
    box spanning the gap that overlaps everything between the two -- inventing
    collisions. A change of line is a break for the same reason. What must *not*
    break a word is a character starting a hair before the previous one ends,
    which is ordinary kerning.
    """
    chars = [
        *_row("ab cd", 10, 100),
        *_row("gh", 300, 100),
        ("x", 319, 329, 100, 120),  # kerned onto the previous character
        *_row("ef", 10, 140),
    ]
    words = render_pdf._words_on_page(_StubTextPage(chars), 4, _PAGE_HEIGHT)
    assert [word.text for word in words] == ["ab", "cd", "ghx", "ef"]
    assert {word.page for word in words} == {4}

    first = words[0]
    assert (first.x0, first.x1) == (10, 30)
    assert (first.y0, first.y1) == (100, 120), "the flip put the first line back at the top"


def test_a_page_with_no_text_measures_as_no_words() -> None:
    assert render_pdf._words_on_page(_StubTextPage([]), 1, _PAGE_HEIGHT) == []


def test_invisible_characters_are_not_copy() -> None:
    words = render_pdf._words_on_page(_StubTextPage(_row("a\u200bb", 10, 100)), 1, _PAGE_HEIGHT)
    assert [word.text for word in words] == ["a", "b"]


# --- the contact sheet ------------------------------------------------------


def test_the_sheet_defaults_are_the_measured_ones() -> None:
    """The three numbers a switch to sheets is only worth making at.

    Three columns under a 1568px long edge -- what this module and the encoder used to
    agree on -- can never produce a cell wider than 522px, and produced 232px on the
    largest template measured. The ceiling has to clear the sheets that get built: 36
    reference pages at six columns come out 4664px wide.
    """
    assert (sheet.DEFAULT_COLUMNS, sheet.DEFAULT_CELL_WIDTH) == (4, 768)
    assert sheet.DEFAULT_MAX_EDGE >= 4664


def _png(path: Path, size: tuple[int, int] = (400, 225)) -> Path:
    image = pytest.importorskip("PIL.Image", reason="Pillow draws the pages to tile")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.new("RGB", size, (200, 40, 40)).save(path, "PNG")
    return path


def test_a_contact_sheet_lays_the_pages_out_in_rows(tmp_path: Path) -> None:
    image = pytest.importorskip("PIL.Image")
    pages = [_png(tmp_path / f"page-{number:03d}.png") for number in range(1, 6)]
    out = sheet.contact_sheet(pages, tmp_path / "sheet" / "sheet.png", 2)
    with image.open(out) as tiled:
        width, height = tiled.size
    # Five pages in two columns is three rows, the last one half empty.
    assert width < height * 1.2, f"a 2x3 grid of 16:9 pages should be taller than it is wide, got {width}x{height}"
    assert width / height == pytest.approx((2 * 400) / (3 * 225), rel=0.15)


def test_a_contact_sheet_never_grows_past_its_budget(tmp_path: Path) -> None:
    image = pytest.importorskip("PIL.Image")
    pages = [_png(tmp_path / f"page-{number:03d}.png", (1920, 1080)) for number in range(1, 5)]
    out = sheet.contact_sheet(pages, tmp_path / "sheet.png", 2, max_edge=1024)
    with image.open(out) as tiled:
        assert max(tiled.size) <= 1024


def test_more_columns_than_pages_still_makes_one_row(tmp_path: Path) -> None:
    image = pytest.importorskip("PIL.Image")
    pages = [_png(tmp_path / f"page-{number:03d}.png") for number in (1, 2)]
    out = sheet.contact_sheet(pages, tmp_path / "sheet.png", 6)
    with image.open(out) as tiled:
        width, height = tiled.size
    assert width > height, "two pages asked for in six columns are one row of two, not a strip of six"


def test_a_contact_sheet_labels_the_page_the_reader_will_turn_to() -> None:
    """The label comes off the file name, so a subset stays truthful."""
    assert sheet._page_label(Path("page-007.png"), 0) == "7"
    assert sheet._page_label(Path("page-012.png"), 3) == "12"
    assert sheet._page_label(Path("cover.png"), 2) == "3", "no number in the name falls back to the grid position"


def test_a_caller_whose_cells_are_not_one_decks_pages_names_them_itself(tmp_path: Path) -> None:
    """A page number is not an identity once the cells come from several files.

    `_page_label` parses the number out of the file name, which is exactly right inside
    one deck and ambiguous across seven templates -- amber page 4 and gold page 4 would
    both be labelled `4`. So the caller says what each cell is, and a list that does not
    line up with the pages is refused rather than silently mislabelling one.
    """
    pages = [_png(tmp_path / f"page-{number:03d}.png") for number in (4, 4, 7)]

    out = sheet.contact_sheet(pages, tmp_path / "sheet.png", 3, labels=["amber 4", "gold 4", "warm 7"])

    assert out.is_file()
    with pytest.raises(RenderError, match="needs 3 label"):
        sheet.contact_sheet(pages, tmp_path / "short.png", 3, labels=["amber 4"])


def _cell_of(out: Path) -> tuple[int, int]:
    """The first cell's size, measured off the finished sheet.

    Measured rather than solved from the sheet's dimensions, so this asserts on the
    picture and not on a restatement of the tiler's own arithmetic. Each page fills its
    cell exactly here -- one page size, one scale -- so the run of non-background pixels
    from the first cell's corner is the cell.
    """
    image = pytest.importorskip("PIL.Image")
    with image.open(out) as tiled:
        page = tiled.convert("RGB")
        pixels, (width, height) = page.load(), page.size
        ground = pixels[0, 0]
        # The first cell's corner, inside the sheet's own margin, which is one gap wide.
        left, top = next((x, y) for y in range(80) for x in range(80) if pixels[x, y] != ground)
        across = next((x for x in range(left, width) if pixels[x, top + 1] == ground), width) - left
        down = next((y for y in range(top, height) if pixels[left + 1, y] == ground), height) - top
    return across, down


def test_a_cell_stays_the_size_the_model_can_read_it_at_however_many_pages_there_are(tmp_path: Path) -> None:
    """The floor the whole switch to sheets rests on, and the flaw it had to fix.

    Measured on the model this engine runs: 767px-wide cells name the right page 90% of
    the time, matching separate full-size renders, and 232-523px cells manage 82% -- the
    loss being the model naming the page next door in the grid, 5 of 150 answers there
    and 0 of 150 at 400-510px. A sheet sized by clipping its long edge loses cell width
    with every row, so an 11-page template got 523px cells and a 34-page one 232px; the
    cell is sized first here, and 34 pages get the same cell 8 pages do.
    """

    def sheet_of(count: int) -> tuple[int, int]:
        pages = [_png(tmp_path / f"{count}" / f"page-{number:03d}.png", (1280, 720)) for number in range(1, count + 1)]
        out = sheet.contact_sheet(pages, tmp_path / f"sheet-{count}.png")
        return _cell_of(out)

    small, large = sheet_of(8), sheet_of(34)

    assert small == large, "the cell is what is sized, so the page count does not shrink it"
    assert small[0] == sheet.DEFAULT_CELL_WIDTH
    assert min(small) >= 400, f"a cell of {small} is under the width the neighbour-in-the-grid error appears at"


def test_a_page_is_never_pasted_bigger_than_it_was_rendered(tmp_path: Path) -> None:
    """A 320px page in a 768px cell would be grey padding around a soft page."""
    pages = [_png(tmp_path / f"page-{number:03d}.png", (320, 180)) for number in range(1, 5)]

    out = sheet.contact_sheet(pages, tmp_path / "sheet.png", 2)

    assert _cell_of(out) == (320, 180)


def test_a_contact_sheet_of_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="at least one page"):
        sheet.contact_sheet([], tmp_path / "sheet.png", 2)
    with pytest.raises(RenderError, match="not on disk"):
        sheet.contact_sheet([tmp_path / "absent.png"], tmp_path / "sheet.png", 2)
    with pytest.raises(RenderError, match="at least one column"):
        sheet.contact_sheet([_png(tmp_path / "page-001.png")], tmp_path / "sheet.png", 0)


def _line(words: list[WordBox]) -> str:
    """The page's copy in reading order, for asserting on what was rendered."""
    return " ".join(word.text for word in sorted(words, key=lambda word: (round(word.y0), word.x0)))


def test_the_conversion_width_is_read_off_the_box_not_fixed_at_two() -> None:
    """Half the cores, floored at two and capped at eight.

    Each conversion is its own process with its own profile (the test above is
    why), so they contend for the machine and nothing else. Measured here on 32
    cores with seven image-heavy 34-page templates: 87.0s one at a time, 67.8s
    two-wide, 53.5s four-wide, 50.3s seven-wide, seven PDFs every time, and the
    slowest single conversion 50.0 / 52.8 / 49.7 / 50.3s -- flat, so the width
    buys wall clock without costing a conversion anything. The floor keeps the
    narrowest box overlapping at all; the cap is where the measurement stops and
    where eight profiles already cost a couple of GB.
    """
    assert office.default_concurrency(1) == office.MIN_CONCURRENCY
    assert office.default_concurrency(4) == 2
    assert office.default_concurrency(8) == 4
    assert office.default_concurrency(16) == 8
    assert office.default_concurrency(128) == office.MAX_CONCURRENCY
    assert office.MIN_CONCURRENCY <= office.default_concurrency() <= office.MAX_CONCURRENCY
