"""Reading one PDF: transparency, and images the document reuses."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from raven_ppt.services.ingest import pdf as pdf_mod
from raven_ppt.services.ingest.pdf import read_pdf, restore_soft_mask

fitz = pytest.importorskip("fitz", reason="render extra (pymupdf) not installed")


def _alpha_pdf(path: Path) -> None:
    from PIL import Image, ImageDraw

    rgba = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).rectangle((100, 80, 400, 320), fill=(255, 255, 255, 255))
    stream = io.BytesIO()
    rgba.save(stream, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(56, 100, 556, 500), stream=stream.getvalue())
    doc.save(path)
    doc.close()


def test_a_soft_mask_falls_back_to_pymupdf_when_pillow_cannot_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pillow keeps the source encoding's colour handling, so it is tried first,
    but it cannot decode every stream a PDF may carry."""
    from PIL import Image

    source = tmp_path / "alpha.pdf"
    _alpha_pdf(source)

    with fitz.open(source) as doc:
        xref = doc[0].get_image_info(xrefs=True)[0]["xref"]
        payload = doc.extract_image(xref)
        smask = int(payload.get("smask", 0) or 0)
        assert smask

        def fail_decode(base_data: bytes, mask_data: bytes) -> bytes:
            raise OSError("decoder unavailable")

        monkeypatch.setattr(pdf_mod, "_compose_soft_mask_pillow", fail_decode)
        restored = restore_soft_mask(doc, xref, smask, payload["image"])

    assert restored is not None
    with Image.open(io.BytesIO(restored)) as image:
        assert image.convert("RGBA").getpixel((0, 0))[3] == 0
        assert image.convert("RGBA").getpixel((image.width // 2, image.height // 2))[3] == 255


def test_a_mask_that_cannot_be_restored_does_not_yield_the_black_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without its mask a transparent figure is an opaque black rectangle, and
    nothing downstream can tell that from a deliberate one."""

    class BrokenDocument:
        def extract_image(self, xref: int):
            raise RuntimeError("mask unavailable")

    def fail_pixmap(*args, **kwargs):
        raise RuntimeError("pixmap unavailable")

    monkeypatch.setattr(fitz, "Pixmap", fail_pixmap)
    assert restore_soft_mask(BrokenDocument(), 4, 5, b"raw black base") is None


def test_an_unrestorable_mask_leaves_the_figure_out_entirely(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "alpha.pdf"
    _alpha_pdf(source)
    monkeypatch.setattr(pdf_mod, "restore_soft_mask", lambda *args, **kwargs: None)
    figures = tmp_path / "figures"
    figures.mkdir()

    content = read_pdf(source, figures)

    assert content.assets == ()
    assert list(figures.iterdir()) == []


def test_an_image_reused_across_pages_is_one_figure(tmp_path: Path) -> None:
    """A logo on forty pages is not forty figures."""
    from PIL import Image

    logo = Image.new("RGB", (300, 220))
    logo.putdata([((x * 7 + y * 13) % 256, (x * 3) % 256, (y * 5) % 256) for y in range(220) for x in range(300)])
    buffer = io.BytesIO()
    logo.save(buffer, format="PNG")

    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), "A page of prose about the product.")
        page.insert_image(fitz.Rect(72, 100, 372, 320), stream=buffer.getvalue())
    source = tmp_path / "brochure.pdf"
    doc.save(source)
    doc.close()
    figures = tmp_path / "figures"
    figures.mkdir()

    content = read_pdf(source, figures)

    assert len(content.assets) == 1
    assert content.assets[0].source_page == 1
    assert content.page_count == 3
    assert content.text.count("## [brochure.pdf] page ") == 3
