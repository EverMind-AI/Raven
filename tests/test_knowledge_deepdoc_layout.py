"""The deepdoc layout model: what it says a region is, and what that buys.

Skipped whole when the weights are not installed, which is a checkout where
`make fetch-resources` has not run. These are the only tests in the suite that
load a 76 MB graph and run inference, so they are also the slow ones.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.knowledge.parser.deepdoc import _onnx, _pipeline
from raven.knowledge.parser.pdf_parser import PdfParser

pytestmark = pytest.mark.skipif(not _onnx.available("layout"), reason="the deepdoc layout model is not installed")


def _page(build) -> bytes:
    import pymupdf

    document = pymupdf.open()
    build(document.new_page(), pymupdf)
    raw = document.tobytes()
    document.close()
    return raw


def _report(page, pymupdf) -> None:
    """A page with the furniture a real document carries: a running header, a
    heading, body text, and a page number."""
    page.insert_text(pymupdf.Point(60, 40), "ACME CONFIDENTIAL", fontsize=8)
    page.insert_text(pymupdf.Point(60, 100), "3. Results", fontsize=20)
    for y in range(140, 260, 26):
        page.insert_text(
            pymupdf.Point(60, y),
            "We measured the effect across every cohort and found a consistent improvement.",
            fontsize=10,
        )
    page.insert_text(pymupdf.Point(270, 810), "12", fontsize=8)


def _parse(raw: bytes, **kwargs):
    return asyncio.run(PdfParser(**kwargs).parse(raw, "report.pdf"))


# -- what the model is asked ---------------------------------------


def test_the_model_labels_the_regions_of_a_page() -> None:
    """Title, prose and page furniture, told apart by how they look rather than
    by how big they are set."""
    import pymupdf

    document = pymupdf.open(stream=_page(_report), filetype="pdf")

    kinds = [region["type"] for region in _pipeline.regions(document[0], 1)]

    assert "title" in kinds
    assert "text" in kinds
    assert "abandon" in kinds, "the page number is furniture, and the model says so"


def test_the_boxes_come_back_in_the_pages_own_points() -> None:
    """The model works on a 3x render. A box left in render pixels would put a
    highlight three times too far down the page."""
    import pymupdf

    document = pymupdf.open(stream=_page(_report), filetype="pdf")
    height = document[0].rect.y1

    for region in _pipeline.regions(document[0], 1):
        assert region["bottom"] <= height + 1, region


# -- what it buys --------------------------------------------------


def test_running_furniture_is_not_indexed() -> None:
    """The clearest thing the model buys. A page number indexed as prose is a
    chunk that matches every query about a number, and a running header is the
    same sentence repeated once per page of the document."""
    sections = _parse(_page(_report))

    text = " ".join(section.content.text for section in sections)
    assert "12" not in text.split()
    assert "CONFIDENTIAL" not in text


def test_the_heading_is_found_without_measuring_type_size() -> None:
    sections = _parse(_page(_report))

    assert [s.metadata.get("heading_path") for s in sections] == [["3. Results"]]


def test_the_geometric_path_keeps_what_the_model_drops() -> None:
    """Not a criticism of it -- it has no way to know. Stated here because the
    difference is the whole argument for a hundred megabytes of weights."""
    sections = _parse(_page(_report), layout=False)

    text = " ".join(section.content.text for section in sections)
    assert "CONFIDENTIAL" in text and "12" in text.split()


# -- when it is not there ------------------------------------------


def test_an_absent_model_reads_the_file_geometrically(monkeypatch) -> None:
    """An install that skipped the weights still reads PDFs. What it loses is
    the labelling, not the document."""
    monkeypatch.setattr(_pipeline, "usable", lambda: False)

    sections = _parse(_page(_report))

    assert sections, "the file was still read"
    assert "CONFIDENTIAL" in " ".join(s.content.text for s in sections), "by the geometric path"


def test_asking_for_it_by_name_says_when_it_is_missing(monkeypatch) -> None:
    """Silence here is a reader wondering why a document came out the way it
    did."""
    from raven.knowledge._notes import collecting

    monkeypatch.setattr(_pipeline, "usable", lambda: False)

    with collecting() as notes:
        _parse(_page(_report), layout=True)

    assert any("not installed" in n for n in notes)


def test_a_page_the_model_chokes_on_falls_back_rather_than_failing(monkeypatch) -> None:
    """One page's layout is not worth the document."""

    def explode(*args, **kwargs):
        raise RuntimeError("the graph refused this page")

    monkeypatch.setattr(_pipeline, "regions", explode)

    sections = _parse(_page(_report))

    assert sections, "read geometrically instead"


# -- the labels themselves -----------------------------------------


def test_the_label_list_matches_the_weights_that_are_published() -> None:
    """The list RAGFlow carries beside this model is for an older one: it
    starts with a background class and runs Text, Title, where these weights
    run Title, Text, Abandon. Read against that list, every title comes back as
    background and every page number as a title.

    Pinned as a list rather than as a comment because it is not checkable by
    reading either project -- it was established by running the model.
    """
    from raven.knowledge.parser.deepdoc._layout_recognizer import LayoutRecognizer

    assert LayoutRecognizer.labels[:4] == ["Title", "Text", "Abandon", "Figure"]
    assert len(LayoutRecognizer.labels) == 10
