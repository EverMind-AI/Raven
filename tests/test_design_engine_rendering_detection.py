"""Unit tests for render input detection and document validation."""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image

from raven_design.rendering.detection import detect_file
from raven_design.rendering.models import RenderConfig, RenderError
from raven_design.rendering.pdf import parse_page_range, validate_pdf_structure


def test_detects_runtime_and_source_motion_signals(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    source.write_text(
        """
        <!doctype html>
        <style>@keyframes spin { to { rotate: 1turn; } }</style>
        <canvas></canvas>
        <script>
        requestAnimationFrame(() => {});
        document.body.addEventListener("click", () => {});
        </script>
        """,
        encoding="utf-8",
    )

    detection = detect_file(source)

    assert detection.format == "html"
    assert detection.family == "browser"
    assert {
        "css_animation",
        "canvas",
        "request_animation_frame",
        "user_interaction",
    } <= set(detection.motion_signals)


def test_detects_svg_from_content_and_svgz(tmp_path: Path) -> None:
    disguised = tmp_path / "drawing.data"
    disguised.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 40"><rect width="80" height="40"/></svg>',
        encoding="utf-8",
    )
    compressed = tmp_path / "drawing.svgz"
    with gzip.open(compressed, "wb") as handle:
        handle.write(b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"/>')

    assert detect_file(disguised).metadata["view_box"] == "0 0 80 40"
    assert detect_file(compressed).format == "svg"


def test_large_svg_is_parsed_without_truncating_xml(tmp_path: Path) -> None:
    source = tmp_path / "large.svg"
    source.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><desc>' + "x" * 1_100_000 + "</desc></svg>",
        encoding="utf-8",
    )

    detection = detect_file(source, max_expanded_bytes=2_000_000)

    assert detection.format == "svg"
    assert detection.metadata["element_count"] == 2


def test_detects_xlsx_structure_and_hidden_sheets(tmp_path: Path) -> None:
    source = tmp_path / "workbook.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "xl/workbook.xml",
            """
            <workbook xmlns="urn:sheet"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <bookViews><workbookView activeTab="0"/></bookViews>
              <sheets>
                <sheet name="Data" sheetId="1" r:id="rId1"/>
                <sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/>
              </sheets>
            </workbook>
            """,
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
              <Relationship Id="rId2" Target="worksheets/sheet2.xml"/>
            </Relationships>
            """,
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="urn:sheet"><dimension ref="A1:Q62"/></worksheet>',
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            '<worksheet xmlns="urn:sheet"><dimension ref="A1"/></worksheet>',
        )

    detection = detect_file(source)

    assert detection.format == "xlsx"
    assert detection.metadata["visible_sheet_count"] == 1
    assert detection.metadata["hidden_sheet_count"] == 1
    assert detection.metadata["sheets"][0]["used_range"] == "A1:Q62"


def test_detects_pptx_source_order_and_hidden_slides(tmp_path: Path) -> None:
    source = tmp_path / "deck.pptx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "ppt/presentation.xml",
            """
            <p:presentation
                xmlns:p="urn:presentation"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <p:sldIdLst>
                <p:sldId id="10" r:id="rId2"/>
                <p:sldId id="11" r:id="rId1"/>
              </p:sldIdLst>
            </p:presentation>
            """,
        )
        archive.writestr(
            "ppt/_rels/presentation.xml.rels",
            """
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="slides/slide1.xml"/>
              <Relationship Id="rId2" Target="slides/slide2.xml"/>
            </Relationships>
            """,
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="urn:presentation"><p:cSld/></p:sld>',
        )
        archive.writestr(
            "ppt/slides/slide2.xml",
            '<p:sld xmlns:p="urn:presentation" show="0"><p:transition/></p:sld>',
        )

    detection = detect_file(source)

    assert detection.metadata["hidden_slide_count"] == 1
    assert [slide["xml_path"] for slide in detection.metadata["slides"]] == [
        "ppt/slides/slide2.xml",
        "ppt/slides/slide1.xml",
    ]
    assert detection.metadata["transition_count"] == 1


def test_zip_path_traversal_and_expansion_limit_are_rejected(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.docx"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../word/document.xml", "<document/>")
    oversized = tmp_path / "oversized.docx"
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("word/document.xml", "x" * 1024)

    with pytest.raises(RenderError) as unsafe:
        detect_file(traversal)
    with pytest.raises(RenderError) as expanded:
        detect_file(oversized, max_expanded_bytes=32)

    assert unsafe.value.code == "unsafe_input"
    assert expanded.value.code == "resource_limit_exceeded"


@pytest.mark.parametrize(
    ("name", "part"),
    [
        ("broken.docx", "word/document.xml"),
        ("broken.pptx", "ppt/presentation.xml"),
        ("broken.xlsx", "xl/workbook.xml"),
    ],
)
def test_malformed_required_ooxml_parts_are_rejected(
    tmp_path: Path,
    name: str,
    part: str,
) -> None:
    source = tmp_path / name
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(part, "<broken")

    with pytest.raises(RenderError) as raised:
        detect_file(source)

    assert raised.value.code == "unsafe_input"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, [0, 1, 2, 3, 4]),
        ("1-3,5", [0, 1, 2, 4]),
        ("3,1,3", [2, 0]),
    ],
)
def test_page_range(value: str | None, expected: list[int]) -> None:
    assert parse_page_range(value, 5) == expected


def test_text_pdf_and_static_images_use_native_readers(tmp_path: Path) -> None:
    sources = {
        "note.txt": b"plain text",
        "ready.pdf": b"%PDF-1.7\n",
    }
    for name, payload in sources.items():
        path = tmp_path / name
        path.write_bytes(payload)
        with pytest.raises(RenderError) as raised:
            detect_file(path)
        assert raised.value.code == "native_read_preferred"
    image = tmp_path / "image.png"
    Image.new("RGB", (10, 10), "red").save(image)
    with pytest.raises(RenderError) as raised:
        detect_file(image)
    assert raised.value.code == "native_read_preferred"


def test_pdfium_rejects_structurally_invalid_generated_pdf(tmp_path: Path) -> None:
    pytest.importorskip("pypdfium2")
    source = tmp_path / "invalid.pdf"
    source.write_bytes(b"%PDF-1.7\nnot-a-document")

    with pytest.raises(RenderError) as raised:
        validate_pdf_structure(
            source,
            RenderConfig(chrome_path=None, libreoffice_path=None),
        )

    assert raised.value.code == "invalid_output"
