"""One PDF, page by page: its text, its tables, its figures.

The order of the four figure readers is the whole of the logic. A printed
figure laid in as twelve images must be recognised as one figure before any of
the twelve is written out on its own, and a screenshot sliced into bands must
be stitched before its bands are; so clusters run first, then stacks, then
whatever is left over individually, then the drawing clusters that no bitmap
accounts for.

Every one of the four attaches the page's own caption to what it emits. That is
not decoration: a figure without the label its caption gave it cannot be
checked against the claim a slide makes about it, and the run that cited "Fig.
4" under a page showing Fig. 5 is what this prevents.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from raven.ppt.contracts.sources import AssetKind, SourceAsset
from raven.ppt.services.ingest import assets as asset_meta
from raven.ppt.services.ingest.captions import Caption, caption_for, page_captions
from raven.ppt.services.ingest.figures import (
    ExtractedFigure,
    extract_vector_figures,
    figure_clusters,
    render_cluster,
    stitch_stack,
    strip_stacks,
    write_placement,
)
from raven.ppt.services.ingest.tables import ExtractedTable, extract_tables


@dataclass(frozen=True)
class PdfContent:
    """What one PDF yielded."""

    text: str
    assets: tuple[SourceAsset, ...]
    page_count: int


def read_pdf(pdf_path: Path, figures_dir: Path) -> PdfContent:
    """Extract page text, tables and figures from one PDF."""
    import fitz  # PyMuPDF, provided by the render extra

    parts: list[str] = [f"# Source: {pdf_path.name}\n"]
    found: list[SourceAsset] = []
    seen_xrefs: set[int] = set()
    with fitz.open(pdf_path) as doc:
        for page_index, page in enumerate(doc, start=1):
            prefix = figures_dir / f"{pdf_path.stem}_p{page_index:03d}"
            text = page.get_text("text").strip()
            bitmap_boxes, placements = _placements(doc, page, seen_xrefs)
            tables = extract_tables(page, prefix, page_text=text)
            captions = page_captions(page)
            for table in tables:
                found.append(_table_asset(table, pdf_path, page_index))
            parts.append(
                f"\n## [{pdf_path.name}] page {page_index}\n\n{text}\n"
                + "".join(_table_section(table) for table in tables)
            )
            for figure in _figures(page, prefix, placements):
                found.append(_figure_asset(figure, pdf_path, page_index, captions))
            occupied = [*bitmap_boxes, *(table.bbox for table in tables)]
            for figure in extract_vector_figures(page, prefix, occupied):
                found.append(_figure_asset(figure, pdf_path, page_index, captions))
        page_count = doc.page_count
    return PdfContent(text="".join(parts), assets=tuple(found), page_count=page_count)


def _figures(page, prefix: Path, placements: list[dict]) -> list[ExtractedFigure]:
    """The bitmap figures on one page, grouped before they are written."""
    emitted: list[ExtractedFigure] = []
    consumed: set[int] = set()
    for cluster in figure_clusters(placements):
        if len(cluster) < 2:
            continue
        out = prefix.with_name(f"{prefix.name}_fig{cluster[0]['xref']}.png")
        figure = render_cluster(page, cluster, out)
        if figure is None:
            continue
        consumed.update(id(member) for member in cluster)
        emitted.append(figure)
    for stack in strip_stacks([p for p in placements if id(p) not in consumed]):
        out = prefix.with_name(f"{prefix.name}_s{stack[0]['xref']}.png")
        figure = stitch_stack(stack, out)
        if figure is None:
            continue
        consumed.update(id(member) for member in stack)
        emitted.append(figure)
    for placement in placements:
        if id(placement) in consumed:
            continue
        out = prefix.with_name(f"{prefix.name}_x{placement['xref']}.{placement['ext']}")
        figure = write_placement(placement, out)
        if figure is not None:
            emitted.append(figure)
    return emitted


def _placements(doc, page, seen_xrefs: set[int]) -> tuple[list, list[dict]]:
    """Every embedded image on the page, with where it sits and how big it is.

    Two lists, because they answer different questions: every placement box is
    needed to tell a drawing cluster from a bitmap that already covers it, while
    only images not already extracted elsewhere in the document are decoded --
    a logo on forty pages is one figure.
    """
    import fitz

    page_area = page.rect.get_area()
    boxes = []
    placements: list[dict] = []
    for info in page.get_image_info(xrefs=True):
        bbox = fitz.Rect(info["bbox"])
        if not bbox.is_empty:
            boxes.append(bbox)
        xref = info.get("xref", 0)
        if not xref or xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            payload = doc.extract_image(xref)
        except (RuntimeError, ValueError):
            continue
        data = payload.get("image", b"")
        if not data:
            continue
        ext = payload.get("ext", "png")
        smask = int(payload.get("smask", 0) or 0)
        if smask:
            restored = restore_soft_mask(doc, xref, smask, data)
            if restored is None:
                # Without its mask the image is the opaque base, which for a
                # transparent figure is a black rectangle. Better no figure.
                continue
            data = restored
            ext = "png"
        placements.append(
            {
                "xref": xref,
                "bbox": (bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                "px_w": payload.get("width", 0),
                "px_h": payload.get("height", 0),
                "data": data,
                "ext": ext,
                "coverage": bbox.get_area() / page_area if page_area and not bbox.is_empty else 0.0,
            }
        )
    return boxes, placements


def restore_soft_mask(doc, xref: int, smask: int, base_data: bytes) -> bytes | None:
    """Recombine an image with its soft mask, or None if neither route works.

    Pillow first because it keeps the source encoding's colour handling; PyMuPDF
    second because Pillow cannot decode every stream a PDF may carry.
    """
    try:
        mask_payload = doc.extract_image(smask)
        return _compose_soft_mask_pillow(base_data, mask_payload["image"])
    except (KeyError, OSError, RuntimeError, ValueError):
        pass
    try:
        import fitz

        base = fitz.Pixmap(doc, xref)
        mask = fitz.Pixmap(doc, smask)
        return fitz.Pixmap(base, mask).tobytes("png")
    except (RuntimeError, ValueError):
        return None


def _compose_soft_mask_pillow(base_data: bytes, mask_data: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(base_data)) as base_img, Image.open(io.BytesIO(mask_data)) as mask_img:
        base = base_img.convert("RGBA")
        mask = mask_img.convert("L")
        if mask.size != base.size:
            mask = mask.resize(base.size)
        base.putalpha(mask)
        encoded = io.BytesIO()
        base.save(encoded, format="PNG")
        return encoded.getvalue()


def _figure_asset(figure: ExtractedFigure, pdf_path: Path, page_index: int, captions: list[Caption]) -> SourceAsset:
    caption = caption_for(figure.bbox, captions, AssetKind.FIGURE) if figure.bbox is not None else None
    return asset_meta.build(
        figure.path.stem,
        figure.path,
        asset_meta.measure(figure.path, figure.size, figure.coverage),
        kind=AssetKind.FIGURE,
        source_file=pdf_path.name,
        source_page=page_index,
        source_label=None if caption is None else caption.label,
        caption=None if caption is None else caption.prose,
        source_bbox=figure.bbox,
    )


def _table_asset(table: ExtractedTable, pdf_path: Path, page_index: int) -> SourceAsset:
    return asset_meta.build(
        table.path.stem,
        table.path,
        # A table has one region however many rows it prints; counting ink bands
        # in one reports its rows as panels.
        asset_meta.measure(table.path, table.size, table.coverage, panel_count=1),
        kind=AssetKind.TABLE,
        source_file=pdf_path.name,
        source_page=page_index,
        source_label=table.source_label,
        caption=table.caption.prose,
        source_bbox=table.bbox,
    )


def _table_section(table: ExtractedTable) -> str:
    """The table as markdown under the page text, so its numbers are indexed.

    The image is what goes on a slide; this is the text an author can read a
    figure off. A table that exists only as pixels is a table whose numbers a
    deck can only retype from memory.
    """
    return (
        f"\n### Source {table.source_label} ({table.path.stem})\n\nCaption: {table.caption.text}\n\n{table.markdown}\n"
    )
