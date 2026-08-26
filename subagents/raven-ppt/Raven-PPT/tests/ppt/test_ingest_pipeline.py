"""End to end: a directory of materials becomes text, a read record, and assets.

Fixtures are built here, page by page, so each test pins the one source shape
it is about. Real papers and brochures never enter the tests.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from raven.ppt.contracts.findings import Severity
from raven.ppt.contracts.sources import AssetKind
from raven.ppt.services.ingest import READ_FILE, ingest_materials, load_catalogue

fitz = pytest.importorskip("fitz", reason="render extra (pymupdf) not installed")


def _report_pdf(noise_png, path: Path, *, with_figure: bool, caption: str | None = None) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "The M4 chip delivers 38 TOPS.")
    page.insert_text((72, 130), "Revenue was $30,972 million, up 14%.")
    if with_figure:
        page.insert_image(fitz.Rect(72, 200, 472, 500), stream=noise_png(400, 300))
    if caption is not None:
        page.insert_text((72, 520), caption, fontsize=11)
    doc.save(path)
    doc.close()


def _ruled_table(page, *, label: str, caption_y: float, table_y: float) -> None:
    x0, col_w, row_h = 72, 150, 36
    page.insert_text((x0, caption_y), f"{label}. Benchmark results.", fontsize=11)
    rows = [
        ("Method", "Accuracy", "Latency"),
        ("Baseline", "72.4", "18.2"),
        ("Variant", "78.9", "15.1"),
        ("Proposed", "83.7", "11.4"),
    ]
    for row in (0, 1, len(rows)):
        page.draw_line(
            fitz.Point(x0, table_y + row * row_h),
            fitz.Point(x0 + 3 * col_w, table_y + row * row_h),
            color=(0, 0, 0),
        )
    for row_index, values in enumerate(rows):
        for col_index, value in enumerate(values):
            page.insert_text((x0 + col_index * col_w + 8, table_y + row_index * row_h + 24), value, fontsize=10)


# --- text and the read record ------------------------------------------------


def test_a_pdf_and_a_markdown_file_become_one_material_text(tmp_path: Path, noise_png) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    _report_pdf(noise_png, materials / "report.pdf", with_figure=True)
    (materials / "notes.md").write_text("Launch year 2024. Codename Sequoia.", encoding="utf-8")

    outcome = ingest_materials(materials, tmp_path / "out")

    text = outcome.materials_path.read_text(encoding="utf-8")
    assert "page 1" in text and "M4 chip" in text and "Codename Sequoia" in text
    assert outcome.page_count == 1
    assert outcome.source_files == ("notes.md", "report.pdf")
    # What a tool reports without re-reading and re-counting the file itself.
    assert outcome.text_chars == sum(
        len(line.strip()) for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # The figures the sources print survive into the text a page quotes from.
    assert "30,972" in text and "14%" in text and "TOPS" in text


def test_what_was_read_is_recorded_beside_the_materials(tmp_path: Path, noise_png) -> None:
    """A later stage reads the file, not the return value."""
    materials = tmp_path / "materials"
    materials.mkdir()
    _report_pdf(noise_png, materials / "report.pdf", with_figure=False)

    outcome = ingest_materials(materials, tmp_path / "out")

    record = json.loads((outcome.materials_path.parent / READ_FILE).read_text(encoding="utf-8"))
    assert record["sources"] == ["report.pdf"]
    assert record["stated_chars"] == outcome.text_chars


def test_materials_with_nothing_supported_are_refused(tmp_path: Path) -> None:
    empty = tmp_path / "materials"
    empty.mkdir()
    (empty / "deck.key").write_bytes(b"unsupported")

    with pytest.raises(FileNotFoundError, match="no supported materials"):
        ingest_materials(empty, tmp_path / "out")


def test_an_html_source_contributes_only_visible_text(tmp_path: Path) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "report.html").write_text(
        "<html><style>.x{display:none}</style><script>bad=999</script>"
        "<body><h1>Market update</h1><p>Revenue reached 42 million in 2026.</p></body></html>",
        encoding="utf-8",
    )

    outcome = ingest_materials(materials, tmp_path / "out")

    text = outcome.materials_path.read_text(encoding="utf-8")
    assert "Revenue reached 42 million" in text
    assert "999" not in text


def test_image_only_sources_come_back_with_a_warning_not_silent_empty_text(tmp_path: Path, noise_png) -> None:
    """A scan has no text layer, so a deck written from it stands on nothing.
    Left unsaid, that reads the same as a source with plenty to say."""
    materials = tmp_path / "materials"
    materials.mkdir()
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(width=612, height=792)
        page.insert_image(fitz.Rect(72, 100, 472, 400), stream=noise_png(400, 300))
    doc.save(materials / "scan.pdf")
    doc.close()

    outcome = ingest_materials(materials, tmp_path / "out")

    (finding,) = outcome.findings
    assert finding.kind == "text_layer"
    assert finding.severity is Severity.WARNING
    assert "image-only" in finding.message
    assert finding.detail["pages"] == 2


def test_a_source_with_text_reports_no_findings(tmp_path: Path, noise_png) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    _report_pdf(noise_png, materials / "report.pdf", with_figure=False)

    assert ingest_materials(materials, tmp_path / "out").findings == ()


# --- figures ----------------------------------------------------------------


def test_a_figure_is_extracted_with_the_provenance_a_citation_needs(tmp_path: Path, noise_png) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    _report_pdf(noise_png, materials / "with_fig.pdf", with_figure=True, caption="Figure 3. A noisy block.")

    outcome = ingest_materials(materials, tmp_path / "out")

    (asset,) = outcome.assets
    assert asset.kind is AssetKind.FIGURE
    assert asset.source_file == "with_fig.pdf"
    assert asset.source_page == 1
    assert asset.source_label == "Figure 3"
    assert asset.caption == "Figure 3. A noisy block."
    assert asset.path.stat().st_size >= 8_000
    assert asset.aspect == pytest.approx(asset.width_px / asset.height_px)

    catalogue = load_catalogue(outcome.catalogue_path, figures_dir=asset.path.parent)
    assert catalogue[asset.asset_id].source_label == "Figure 3"


def test_a_figure_the_page_never_captions_arrives_unlabelled(tmp_path: Path, noise_png) -> None:
    """Not an error -- a paper prints figures without numbering them -- but the
    label has to be absent rather than guessed."""
    materials = tmp_path / "materials"
    materials.mkdir()
    _report_pdf(noise_png, materials / "with_fig.pdf", with_figure=True)

    (asset,) = ingest_materials(materials, tmp_path / "out").assets

    assert asset.source_label is None and asset.caption is None


def test_a_vector_plot_is_rendered_and_a_full_page_frame_is_not(tmp_path: Path) -> None:
    """Academic-PDF blind spot: plots are drawing commands, not embedded
    bitmaps. Large drawing clusters render to PNG; a decorative page frame
    does not qualify."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(fitz.Rect(100, 100, 400, 300), color=(0, 0, 0), width=1)
    for index in range(5):
        left = 120 + index * 50
        page.draw_rect(fitz.Rect(left, 280 - index * 30, left + 28, 280), color=(0, 0, 1), fill=(0, 0, 1))
    page.draw_line(fitz.Point(110, 280), fitz.Point(390, 280), color=(0, 0, 0), width=1)
    page.insert_text((110, 330), "Figure 2. Bars.", fontsize=11)
    frame_page = doc.new_page(width=612, height=792)
    frame_page.draw_rect(fitz.Rect(6, 6, 606, 786), color=(0, 0, 0), width=2)
    materials = tmp_path / "materials"
    materials.mkdir()
    doc.save(materials / "paper.pdf")
    doc.close()

    outcome = ingest_materials(materials, tmp_path / "out")

    (asset,) = outcome.assets
    assert "_vec" in asset.asset_id and asset.path.suffix == ".png"
    assert asset.source_page == 1
    assert asset.source_label == "Figure 2"
    assert min(asset.width_px, asset.height_px) >= 160


def test_a_letterboxed_bitmap_lands_already_trimmed(tmp_path: Path, noise_png) -> None:
    """Full chain: a web-print export's letterboxed screenshot arrives in
    figures/ trimmed, and the recorded size is the trimmed one."""
    from PIL import Image

    banner = Image.new("RGB", (2000, 400), (0, 0, 0))
    with Image.open(io.BytesIO(noise_png(600, 240))) as content:
        banner.paste(content, (700, 80))
    buffer = io.BytesIO()
    banner.save(buffer, format="PNG")

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Web print export with a letterboxed screenshot.")
    page.insert_image(fitz.Rect(56, 100, 556, 200), stream=buffer.getvalue())
    materials = tmp_path / "materials"
    materials.mkdir()
    doc.save(materials / "web.pdf")
    doc.close()

    (asset,) = ingest_materials(materials, tmp_path / "out").assets

    assert 600 <= asset.width_px <= 690 and 240 <= asset.height_px <= 270
    with Image.open(asset.path) as on_disk:
        assert on_disk.size == (asset.width_px, asset.height_px)


def test_a_soft_masked_figure_keeps_its_transparency(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    rgba = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
    pixels = rgba.load()
    for y in range(40, 380):
        for x in range(80, 420):
            if ((x - 250) / 170) ** 2 + ((y - 210) / 170) ** 2 <= 1:
                pixels[x, y] = ((x * 7 + y * 3) % 220 + 20, (x * 3) % 180 + 30, (y * 5) % 190 + 25, 255)
    draw = ImageDraw.Draw(rgba)
    for row in range(8):
        draw.rectangle((150, 100 + row * 24, 350, 110 + row * 24), fill=(255, 255, 255, 255))
    stream = io.BytesIO()
    rgba.save(stream, format="PNG")

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Transparent architecture figure.")
    page.insert_image(fitz.Rect(56, 100, 556, 500), stream=stream.getvalue())
    materials = tmp_path / "materials"
    materials.mkdir()
    doc.save(materials / "alpha.pdf")
    doc.close()

    (asset,) = ingest_materials(materials, tmp_path / "out").assets

    with Image.open(asset.path) as extracted:
        assert extracted.mode == "RGBA"
        assert extracted.getpixel((0, 0))[3] == 0
        assert extracted.getpixel((extracted.width // 2, extracted.height // 2))[3] == 255


def test_a_piece_of_an_extracted_region_never_reaches_the_catalogue(tmp_path: Path, noise_png) -> None:
    """A logo printed inside a table is inside a region already extracted whole.
    The predecessor handed it over graded "avoid"; four of the five assets it
    graded that way on one paper were exactly this."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    _ruled_table(page, label="Table 1", caption_y=96, table_y=120)
    page.insert_image(fitz.Rect(430, 200, 470, 240), stream=noise_png(200, 200))
    materials = tmp_path / "materials"
    materials.mkdir()
    doc.save(materials / "paper.pdf")
    doc.close()

    outcome = ingest_materials(materials, tmp_path / "out")

    (asset,) = outcome.assets
    assert asset.kind is AssetKind.TABLE
    # Dropped from the catalogue and from disk: a file left behind is an orphan
    # a figure listing still finds.
    assert [path.name for path in sorted((tmp_path / "out" / "figures").iterdir())] == [asset.path.name]


# --- tables -----------------------------------------------------------------


def test_a_printed_table_is_registered_as_an_asset_and_as_markdown(tmp_path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    _ruled_table(page, label="Table 1", caption_y=96, table_y=120)
    materials = tmp_path / "materials"
    materials.mkdir()
    doc.save(materials / "paper.pdf")
    doc.close()

    outcome = ingest_materials(materials, tmp_path / "out")

    (asset,) = outcome.assets
    assert asset.kind is AssetKind.TABLE
    assert asset.source_label == "Table 1"
    assert asset.caption == "Table 1. Benchmark results."
    assert asset.panel_count == 1
    text = outcome.materials_path.read_text(encoding="utf-8")
    assert f"Source Table 1 ({asset.asset_id})" in text
    assert "Baseline" in text and "83.7" in text
    assert "11.4" in text


def test_two_stacked_tables_keep_separate_labels_and_regions(tmp_path: Path) -> None:
    """One paper page had Table 1 running to y=264 while Table 2's caption began
    at y=249, so Table 1's image carried Table 2's header rows."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    _ruled_table(page, label="Table 1", caption_y=96, table_y=112)
    _ruled_table(page, label="Table 2", caption_y=340, table_y=356)
    materials = tmp_path / "materials"
    materials.mkdir()
    doc.save(materials / "paper.pdf")
    doc.close()

    outcome = ingest_materials(materials, tmp_path / "out")

    tables = [asset for asset in outcome.assets if asset.kind is AssetKind.TABLE]
    assert [asset.source_label for asset in tables] == ["Table 1", "Table 2"]
    assert tables[0].source_bbox[3] <= tables[1].source_bbox[1]


# --- supplied images --------------------------------------------------------


def test_a_supplied_image_is_registered_with_where_it_was_downloaded_from(tmp_path: Path) -> None:
    from PIL import Image

    materials = tmp_path / "downloads"
    materials.mkdir()
    Image.new("RGB", (320, 180), (35, 90, 140)).save(materials / "source.png")
    out = tmp_path / "out"
    out.mkdir()
    (out / "sources.jsonl").write_text(
        json.dumps(
            {
                "url": "https://example.com/source.png",
                "final_url": "https://cdn.example.com/source.png",
                "path": str(materials / "source.png"),
                "content_sha256": hashlib.sha256((materials / "source.png").read_bytes()).hexdigest(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    (asset,) = ingest_materials(materials, out).assets

    assert asset.kind is AssetKind.IMAGE
    assert asset.source_url == "https://cdn.example.com/source.png"
    assert asset.source_file == "source.png"
    assert asset.path.name == "source.png"
    # Not a region of any page, so coverage says nothing about it.
    assert asset.page_coverage == 0.0
    assert asset.source_bbox is None


def test_a_supplied_image_takes_the_caption_its_fetch_recorded(tmp_path: Path) -> None:
    """A picture off a web page has no caption in its bytes: the words sit in the HTML
    beside it, so whatever fetched it is the only thing that ever held both."""
    from PIL import Image

    materials = tmp_path / "downloads"
    materials.mkdir()
    Image.new("RGB", (320, 180), (35, 90, 140)).save(materials / "source.png")
    out = tmp_path / "out"
    out.mkdir()
    (out / "sources.jsonl").write_text(
        json.dumps(
            {
                "url": "https://example.com/source.png",
                "path": str(materials / "source.png"),
                "content_sha256": hashlib.sha256((materials / "source.png").read_bytes()).hexdigest(),
                "caption": "Figure 2: extraction and update phases",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    (asset,) = ingest_materials(materials, out).assets

    assert asset.caption == "Figure 2: extraction and update phases"
    assert load_catalogue(out / "figures.json", figures_dir=out / "figures")[asset.asset_id].caption == asset.caption


def test_a_papers_own_figure_caption_is_not_replaced_by_the_download_it_arrived_in(tmp_path: Path, noise_png) -> None:
    """A recorded caption describes the file, and a paper is not one figure. Letting it
    through would print one line under every figure the paper prints, over the caption
    each of those pages actually carries."""
    materials = tmp_path / "downloads"
    materials.mkdir()
    _report_pdf(noise_png, materials / "paper.pdf", with_figure=True, caption="Figure 3. A noisy block.")
    out = tmp_path / "out"
    out.mkdir()
    (out / "sources.jsonl").write_text(
        json.dumps(
            {
                "url": "https://example.com/paper.pdf",
                "path": str(materials / "paper.pdf"),
                "content_sha256": hashlib.sha256((materials / "paper.pdf").read_bytes()).hexdigest(),
                "caption": "the Mem0 paper",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    (asset,) = ingest_materials(materials, out).assets

    assert asset.caption == "Figure 3. A noisy block."


def test_two_supplied_images_with_one_stem_stay_separately_referenceable(tmp_path: Path) -> None:
    from PIL import Image

    materials = tmp_path / "materials"
    (materials / "a").mkdir(parents=True)
    (materials / "b").mkdir()
    Image.new("RGB", (320, 180), (120, 20, 20)).save(materials / "a" / "same.png")
    Image.new("RGB", (320, 180), (20, 20, 120)).save(materials / "b" / "same.jpg")

    outcome = ingest_materials(materials, tmp_path / "out")

    ids = {asset.asset_id for asset in outcome.assets}
    assert len(ids) == 2
    assert all(asset.path.is_file() for asset in outcome.assets)


def test_a_file_that_is_not_an_image_never_reaches_the_figure_store(tmp_path: Path) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "bad.png").write_bytes(b"not an image")
    out = tmp_path / "out"

    with pytest.raises(OSError):
        ingest_materials(materials, out)

    assert list((out / "figures").iterdir()) == []


def _screenshot_pdf(path: Path, screenshot: Path) -> None:
    from PIL import Image

    with Image.open(screenshot) as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(0, 0, 612, 792), stream=buffer.getvalue())
    doc.save(path)
    doc.close()


def test_a_whole_page_asset_points_at_the_blocks_cut_from_it(tmp_path: Path, product_page) -> None:
    """A real run placed twelve page screenshots whole -- the browser chrome on
    every slide -- while their blocks sat unused and nothing said to use them."""
    materials = tmp_path / "materials"
    materials.mkdir()
    _screenshot_pdf(materials / "web.pdf", product_page(tmp_path / "page.png"))

    outcome = ingest_materials(materials, tmp_path / "out")

    whole = [asset for asset in outcome.assets if asset.page_coverage >= 0.6]
    blocks = [asset for asset in outcome.assets if asset.asset_id.rsplit("_", 1)[-1].startswith("b")]
    assert len(whole) == 1 and len(blocks) == 2
    note = " ".join(whole[0].concerns)
    assert "whole-page screenshot" in note
    for block in blocks:
        assert block.asset_id in note
        # The block inherits the page's provenance; it is the same source page.
        assert block.source_file == whole[0].source_file
        assert block.source_page == whole[0].source_page
