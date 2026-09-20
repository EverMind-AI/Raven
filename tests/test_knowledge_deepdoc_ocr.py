"""Reading a page that carries no text: the detector and the recognizer.

Skipped whole when the weights are not installed, which is a checkout where
`make fetch-resources` has not run.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.knowledge.parser.deepdoc import _onnx, _pipeline
from raven.knowledge.parser.pdf_parser import PdfParser

pytestmark = pytest.mark.skipif(
    not _onnx.available("det", "rec", "layout"), reason="the deepdoc recognition models are not installed"
)

_LINES = [
    "Quarterly report",
    "Revenue grew across every region we measured this quarter.",
    "The northern market remains the largest by some distance.",
]


def _typed() -> bytes:
    """A page with a text layer, the ordinary case."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(pymupdf.Point(60, 80), _LINES[0], fontsize=20)
    for index, line in enumerate(_LINES[1:]):
        page.insert_text(pymupdf.Point(60, 140 + index * 30), line, fontsize=12)
    raw = document.tobytes()
    document.close()
    return raw


def _scanned() -> bytes:
    """The same page as a picture of itself, which is what a scan is.

    Rendered and pasted back, so the words are pixels and nothing in the file
    states what they are. PyMuPDF reads no characters at all off this.
    """
    import pymupdf

    source = pymupdf.open(stream=_typed(), filetype="pdf")
    image = source[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")
    box = source[0].rect
    source.close()

    document = pymupdf.open()
    document.new_page(width=box.width, height=box.height).insert_image(box, stream=image)
    raw = document.tobytes()
    document.close()
    return raw


def _parse(raw: bytes, **kwargs) -> str:
    sections = asyncio.run(PdfParser(**kwargs).parse(raw, "report.pdf"))
    return " ".join(section.content.text for section in sections)


def _squeezed(text: str) -> str:
    """Text with its spaces taken out, which is how recognised text is checked.

    The recognizer reads a word boundary from the gap between glyphs, and on a
    render of a synthetic page some of those gaps are a pixel too narrow -- one
    line here comes back as `revenuegrewacross`. Real scans are rendered from
    paper and do not do this, so the spacing is a property of the fixture
    rather than of the model, and asserting on it would pin the wrong thing.
    What these tests are about is whether the words were read at all.
    """
    return "".join(text.lower().split())


# -- what the models are asked -------------------------------------


def test_a_scanned_page_carries_nothing_to_read() -> None:
    """The premise of everything below. Stated as a test because a fixture that
    quietly kept its text layer would make every assertion here vacuous."""
    import pymupdf

    with pymupdf.open(stream=_scanned(), filetype="pdf") as document:
        assert document[0].get_text().strip() == ""


def test_the_models_read_the_words_off_the_picture() -> None:
    import pymupdf

    with pymupdf.open(stream=_scanned(), filetype="pdf") as document:
        page = document[0]
        boxes, recognised = _pipeline.text_boxes(page, _pipeline.render(page), 1)

    assert recognised, "there was nothing to read, so they were recognised"
    read = _squeezed(" ".join(box["text"] for box in boxes))
    assert "revenuegrewacrosseveryregion" in read
    assert "northernmarketremainsthelargest" in read


# -- what they buy -------------------------------------------------


def test_a_scan_is_indexed_without_a_vision_model() -> None:
    """The point of carrying the weights. Before this, a scanned PDF needed a
    configured vision model and a call per page; now it needs neither."""
    text = _squeezed(_parse(_scanned()))

    assert "revenuegrewacrosseveryregion" in text
    assert "northernmarketremainsthelargest" in text


def test_a_scans_heading_is_still_a_heading() -> None:
    """Recognised text goes through the same layout model as read text, so a
    scan gets the structure a digital page gets rather than one flat block."""
    sections = asyncio.run(PdfParser().parse(_scanned(), "report.pdf"))

    paths = [path for s in sections if (path := s.metadata.get("heading_path"))]
    assert paths, "the layout model found a heading on the picture"
    assert "quarterly" in " ".join(paths[0]).lower()


# -- when they are not wanted --------------------------------------


def test_a_page_that_states_its_text_is_never_recognised(monkeypatch) -> None:
    """Recognising text the file spells out exactly costs an inference pass to
    lose accuracy. The text layer wins wherever there is one."""

    def explode(*args, **kwargs):
        raise AssertionError("the recognizer was asked to read a page that could be read")

    monkeypatch.setattr(_pipeline, "_from_ocr", explode)

    assert "northern market remains the largest" in _parse(_typed()).lower()


def test_turning_it_off_leaves_the_page_for_the_vision_model() -> None:
    """The vision model is the slower and dearer of the two, and the only one
    that can say what a photograph on the page shows. Unconfigured -- which the
    suite guarantees -- the page is reported unread rather than indexed empty."""
    from raven.knowledge._notes import collecting

    with collecting() as notes:
        text = _parse(_scanned(), ocr=False)

    assert "revenuegrew" not in _squeezed(text)
    assert any("no vision model is configured" in note for note in notes)


def test_asking_for_it_by_name_says_when_it_is_missing(monkeypatch) -> None:
    """Silence here is a reader wondering why a scan came out empty."""
    from raven.knowledge._notes import collecting

    monkeypatch.setattr(_pipeline, "readable", lambda: False)

    with collecting() as notes:
        _parse(_scanned(), ocr=True)

    assert any("text recognizer is not installed" in note for note in notes)
