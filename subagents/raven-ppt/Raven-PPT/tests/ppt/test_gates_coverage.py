"""What the coverage rows claim when a render is missing, and when one is unusable.

Two questions live behind "no PDF came back", and they take opposite remedies: a
machine with no LibreOffice wants one installed, and a deck that did not come out
of a LibreOffice that is right there does not. Sending the second author at the
first remedy is the failure these tests pin.

And the case after that: a PDF came back, a page image came back with it, and the
image is blank or the wrong size. Every measurement that reads pixels then finds
nothing to complain about and the page is reported clean, which is the one thing it
demonstrably is not.

The page's recorded size is read off a PDF written by hand here rather than off a
converted deck, so the whole set runs on a machine with no LibreOffice; only a PDF
reader is needed, and both of the two read this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.contracts import Severity
from raven.ppt.services.gates import DeckUnderReview
from raven.ppt.services.gates.coverage import unrendered
from raven.ppt.services.render import capabilities

CAPS = capabilities.available()

needs_reader = pytest.mark.skipif(
    not CAPS.can_rasterise,
    reason="no PDF reader installed (pip install 'raven[ppt]', or apt install poppler-utils)",
)

# The deck canvas: 13.333x7.5in in points, which is what LibreOffice writes into
# the MediaBox of a 16:9 deck's PDF.
PAGE_PT = (959.976, 540.0)


def mini_pdf(path: Path, pages: tuple[tuple[float, float], ...] = (PAGE_PT,)) -> Path:
    """A PDF that records a page size and nothing else.

    Written by hand because the only thing under test here is what the reader says
    a page measures, and producing that through LibreOffice would make every one
    of these tests skip on a machine without it.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{3 + i} 0 R' for i in range(len(pages)))}] "
        f"/Count {len(pages)} >>".encode(),
        *(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:g} {h:g}] >>".encode() for w, h in pages),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def page_image(path: Path, size: tuple[int, int] = (960, 540), *, flat: bool = False) -> Path:
    """A page image of `size`, with something on it unless `flat`."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, (255, 255, 255))
    if not flat:
        ImageDraw.Draw(image).rectangle([1, 1, max(2, size[0] // 2), max(2, size[1] // 2)], fill=(0, 0, 0))
    image.save(path)
    return path


def deck(tmp_path: Path, *, pdf: Path | None, pages: list[Path] | None = None) -> DeckUnderReview:
    return DeckUnderReview(pptx_path=tmp_path / "deck.pptx", pdf_path=pdf, rendered_pages=pages)


# --- no PDF at all: the machine, or the deck ---------------------------------


def test_a_machine_with_no_renderer_is_told_to_install_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capabilities, "find_soffice", lambda: None)

    findings = unrendered(deck(tmp_path, pdf=None))

    assert len(findings) == 1
    assert findings[0].kind == "unrendered"
    assert findings[0].severity is Severity.WARNING
    assert "could not be rendered on this machine" in findings[0].message
    assert "install LibreOffice" in findings[0].message


def test_a_deck_that_did_not_render_where_the_renderer_is_installed_is_not_sent_to_install_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy that is exactly wrong for this author, and the one they were given.

    Both cases arrive as `pdf_path is None`, because the stage that renders turns
    `RenderUnavailableError` and a conversion that failed into the same None.
    """
    monkeypatch.setattr(capabilities, "find_soffice", lambda: "/usr/bin/soffice")

    findings = unrendered(deck(tmp_path, pdf=None))

    assert len(findings) == 1
    assert findings[0].kind == "unrendered"
    assert findings[0].severity is Severity.WARNING
    assert "install LibreOffice" not in findings[0].message
    assert "LibreOffice is installed on this machine" in findings[0].message
    assert "convert it again" in findings[0].message


def test_a_pdf_path_pointing_at_nothing_is_the_same_as_no_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capabilities, "find_soffice", lambda: None)

    findings = unrendered(deck(tmp_path, pdf=tmp_path / "absent.pdf"))

    assert [f.kind for f in findings] == ["unrendered"]
    assert "install LibreOffice" in findings[0].message


@needs_reader
def test_a_pdf_no_reader_can_open_is_reported_rather_than_measured(tmp_path: Path) -> None:
    broken = tmp_path / "deck.pdf"
    broken.write_bytes(b"not a PDF at all")

    findings = unrendered(deck(tmp_path, pdf=broken))

    assert [f.kind for f in findings] == ["unrendered"]
    assert "cannot be read back" in findings[0].message


# --- a render that came back, and a page image that did not ------------------


@needs_reader
def test_a_page_image_of_a_single_flat_colour_did_not_render(tmp_path: Path) -> None:
    """The case the sanity pass exists for: every pixel measurement passes on it."""
    pdf = mini_pdf(tmp_path / "deck.pdf")
    page_image(tmp_path / "page-001.png", flat=True)

    findings = unrendered(deck(tmp_path, pdf=pdf))

    assert [(f.kind, f.page) for f in findings] == [("unrendered", 1)]
    assert "a single flat colour" in findings[0].message
    assert "page 1 did not come out of the renderer" in findings[0].message


@needs_reader
def test_a_page_image_of_the_wrong_size_did_not_render(tmp_path: Path) -> None:
    pdf = mini_pdf(tmp_path / "deck.pdf")
    page_image(tmp_path / "page-001.png", (800, 600))

    findings = unrendered(deck(tmp_path, pdf=pdf))

    assert [(f.kind, f.page) for f in findings] == [("unrendered", 1)]
    assert "800x600px" in findings[0].message


@needs_reader
def test_an_empty_page_image_did_not_render(tmp_path: Path) -> None:
    pdf = mini_pdf(tmp_path / "deck.pdf")
    (tmp_path / "page-001.png").write_bytes(b"")

    findings = unrendered(deck(tmp_path, pdf=pdf))

    assert [(f.kind, f.page) for f in findings] == [("unrendered", 1)]
    assert "is empty" in findings[0].message


@needs_reader
def test_a_page_image_the_renderer_never_wrote_did_not_render(tmp_path: Path) -> None:
    """A path handed over on the record, with no file behind it."""
    pdf = mini_pdf(tmp_path / "deck.pdf")

    findings = unrendered(deck(tmp_path, pdf=pdf, pages=[tmp_path / "page-001.png"]))

    assert [(f.kind, f.page) for f in findings] == [("unrendered", 1)]
    assert "no file was written for it" in findings[0].message


@needs_reader
def test_a_usable_page_image_is_reported_by_nothing(tmp_path: Path) -> None:
    pdf = mini_pdf(tmp_path / "deck.pdf")
    page_image(tmp_path / "page-001.png")

    assert unrendered(deck(tmp_path, pdf=pdf)) == []


@needs_reader
def test_every_unusable_page_is_named_rather_than_only_the_first(tmp_path: Path) -> None:
    pdf = mini_pdf(tmp_path / "deck.pdf", (PAGE_PT, PAGE_PT, PAGE_PT))
    page_image(tmp_path / "page-001.png")
    page_image(tmp_path / "page-002.png", flat=True)
    page_image(tmp_path / "page-003.png", (100, 100))

    findings = unrendered(deck(tmp_path, pdf=pdf))

    assert [f.page for f in findings] == [2, 3]
    assert {f.severity for f in findings} == {Severity.WARNING}
