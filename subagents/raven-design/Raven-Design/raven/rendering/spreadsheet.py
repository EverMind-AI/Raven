"""Render spreadsheet sheets into bounded visual viewports."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageDraw, ImageFont

from raven.rendering.bundle import BundlePaths
from raven.rendering.models import Detection, RenderConfig, RenderError
from raven.rendering.spreadsheet_runtime import (
    run_spreadsheet_worker,
    spreadsheet_viewport_available,
    uno_python_path,
)
from raven.rendering.util import (
    crop_white_margin,
    file_record,
)


@dataclass
class SpreadsheetRenderResult:
    artifacts: dict[str, Any]
    preview_candidates: list[dict[str, Any]]
    rendered: dict[str, Any]
    warnings: list[dict[str, Any]]
    normalized_pdf: Path | None = None


@dataclass(frozen=True)
class _ProcessedSheets:
    sheets: list[dict[str, Any]]
    normalized_pdfs: list[tuple[Path, dict[str, Any]]]
    page_count: int
    warnings: list[dict[str, Any]]


def _cell_name(column: int, row: int) -> str:
    value = column + 1
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row + 1}"


def _visible_range_name(value: dict[str, int]) -> str:
    start = _cell_name(value["start_column"], value["start_row"])
    end = _cell_name(value["end_column"], value["end_row"])
    return start if start == end else f"{start}:{end}"


def _pdf_page_count(path: Path) -> int:
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise RenderError(
            "invalid_output",
            f"The per-sheet PDF cannot be opened: {path.name}",
        ) from exc
    try:
        if document.page_count < 1:
            raise RenderError(
                "invalid_output",
                f"The per-sheet PDF has no pages: {path.name}",
            )
        return document.page_count
    finally:
        document.close()


def _merge_normalized_sheet_pdfs(
    entries: list[tuple[Path, dict[str, Any]]],
    target: Path,
    max_pages: int,
) -> list[dict[str, Any]]:
    merged = fitz.open()
    page_map: list[dict[str, Any]] = []
    try:
        for path, sheet in entries:
            source = fitz.open(path)
            try:
                sheet_page_count = source.page_count
                if sheet_page_count < 1:
                    raise RenderError(
                        "invalid_output",
                        f"The normalized PDF for sheet {sheet['name']} has no pages.",
                    )
                if merged.page_count + sheet_page_count > max_pages:
                    raise RenderError(
                        "resource_limit_exceeded",
                        "The normalized workbook PDF exceeds the page limit.",
                        details={
                            "pages": merged.page_count + sheet_page_count,
                            "limit": max_pages,
                        },
                    )
                first_document_page = merged.page_count + 1
                merged.insert_pdf(source)
                for sheet_page in range(1, sheet_page_count + 1):
                    page_map.append(
                        {
                            "page": first_document_page + sheet_page - 1,
                            "sheet_index": sheet["index"],
                            "sheet_name": sheet["name"],
                            "sheet_page": sheet_page,
                            "sheet_page_count": sheet_page_count,
                            "used_range": sheet["used_range"],
                            "active": sheet["active"],
                        }
                    )
            finally:
                source.close()
        if merged.page_count < 1:
            raise RenderError(
                "invalid_output",
                "No normalized spreadsheet pages were available.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        merged.save(target, garbage=4, deflate=True)
    finally:
        merged.close()
    return page_map


def _render_pdf_page(
    page: fitz.Page,
    target: Path,
    config: RenderConfig,
    *,
    crop_to_content: bool,
) -> None:
    scale = config.raster_dpi / 72
    projected_width = page.rect.width * scale
    projected_height = page.rect.height * scale
    projected_max = max(projected_width, projected_height)
    if projected_max > config.max_side_pixels:
        scale *= config.max_side_pixels / projected_max
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
        colorspace=fitz.csRGB,
    )
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    if crop_to_content:
        image, _ = crop_white_margin(image)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "PNG", optimize=True)


def _add_sheet_header(path: Path, sheet: dict[str, Any]) -> None:
    with Image.open(path) as source:
        image = source.convert("RGB")
    header_height = 48
    canvas = Image.new("RGB", (image.width, image.height + header_height), "white")
    canvas.paste(image, (0, header_height))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=16)
    fill = "#dcfce7" if sheet["active"] else "#e5e7eb"
    tab_label = f"Sheet {sheet['index'] + 1}: {sheet['name']}"
    tab_width = min(image.width - 16, max(180, 14 + draw.textlength(tab_label, font=font)))
    draw.rounded_rectangle(
        (8, 8, 8 + tab_width, 43),
        radius=7,
        fill=fill,
        outline="#9ca3af",
    )
    draw.text((18, 16), tab_label, fill="#111827", font=font)
    range_label = f"used {sheet['used_range']}"
    range_width = draw.textlength(range_label, font=font)
    if 24 + tab_width + range_width < image.width:
        draw.text(
            (image.width - range_width - 12, 16),
            range_label,
            fill="#4b5563",
            font=font,
        )
    canvas.save(path, "PNG", optimize=True)


def _postprocess_sheet(
    sheet: dict[str, Any],
    bundle_root: Path,
    config: RenderConfig,
) -> tuple[dict[str, Any], int]:
    source_artifacts = sheet.pop("artifacts")
    source_viewports = sheet.pop("viewports", [])
    overview_pdf = Path(source_artifacts["overview_pdf"])
    pages_pdf = Path(source_artifacts["pages_pdf"])
    if not overview_pdf.is_file() or not pages_pdf.is_file():
        raise RenderError(
            "invalid_output",
            f"LibreOffice did not produce both views for sheet {sheet['name']}.",
        )
    sheet_dir = overview_pdf.parent
    page_count = _pdf_page_count(pages_pdf)
    if page_count > config.max_pages:
        raise RenderError(
            "resource_limit_exceeded",
            f"Sheet {sheet['name']} exceeds the per-sheet page limit.",
            details={
                "sheet": sheet["name"],
                "pages": page_count,
                "limit": config.max_pages,
            },
        )
    viewport_records: list[dict[str, Any]] = []
    for viewport in source_viewports:
        viewport_path = Path(viewport.pop("path"))
        if not viewport_path.is_file():
            continue
        record = file_record(viewport_path, bundle_root)
        record.update(
            {
                **viewport,
                "kind": "sheet_viewport",
                "sheet_index": sheet["index"],
                "sheet_name": sheet["name"],
                "sheet_state": "visible",
                "used_range": sheet["used_range"],
                "active": sheet["active"],
            }
        )
        viewport_records.append(record)
    overview_record = None
    if not viewport_records:
        overview_png = sheet_dir / "overview.png"
        overview_document = fitz.open(overview_pdf)
        try:
            _render_pdf_page(
                overview_document.load_page(0),
                overview_png,
                config,
                crop_to_content=True,
            )
        finally:
            overview_document.close()
        _add_sheet_header(overview_png, sheet)
        overview_record = file_record(overview_png, bundle_root)
        overview_record.update(
            {
                "kind": "sheet_overview",
                "sheet_index": sheet["index"],
                "sheet_name": sheet["name"],
                "sheet_state": "visible",
                "used_range": sheet["used_range"],
                "active": sheet["active"],
            }
        )
    sheet["artifacts"] = {
        "overview_image": overview_record,
        "pages_pdf": {
            "path": pages_pdf.relative_to(bundle_root).as_posix(),
            "page_count": page_count,
        },
        "viewports": viewport_records,
    }
    return sheet, page_count


def _spreadsheet_preview_candidates(
    sheets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    priority = sorted(
        (sheet for sheet in sheets if sheet.get("rendered")),
        key=lambda sheet: (
            not bool(sheet.get("active")),
            -int(sheet["source_structure"].get("drawing_count") or 0),
            sheet["index"],
        ),
    )
    candidates: list[dict[str, Any]] = []
    for sheet in priority:
        candidates.extend(_viewport_candidate(sheet, viewport) for viewport in sheet["artifacts"]["viewports"])
    for sheet in priority:
        if sheet["artifacts"]["viewports"]:
            continue
        overview = sheet["artifacts"]["overview_image"]
        if overview is None:
            continue
        candidates.append(
            {
                "path": overview["path"],
                "kind": "sheet_overview",
                "sheet_index": sheet["index"],
                "sheet_name": sheet["name"],
                "sheet_state": "visible",
                "used_range": sheet["used_range"],
                "active": sheet["active"],
            }
        )
    return candidates


def _viewport_candidate(
    sheet: dict[str, Any],
    viewport: dict[str, Any],
) -> dict[str, Any]:
    return {
        "path": viewport["path"],
        "kind": "sheet_viewport",
        "sheet_index": sheet["index"],
        "sheet_name": sheet["name"],
        "sheet_state": "visible",
        "anchor_cell": _cell_name(
            viewport["anchor_column"],
            viewport["anchor_row"],
        ),
        "visible_range": _visible_range_name(viewport["controller_visible_range"]),
        "zoom_percent": viewport["zoom_percent"],
        "used_range": sheet["used_range"],
        "active": sheet["active"],
    }


def _process_sheets(
    raw_sheets: list[dict[str, Any]],
    detection: Detection,
    bundle_root: Path,
    config: RenderConfig,
) -> _ProcessedSheets:
    source_sheets = {
        int(sheet["index"]): sheet
        for sheet in detection.metadata.get("sheets", [])
        if isinstance(sheet.get("index"), int)
    }
    processed: list[dict[str, Any]] = []
    normalized_pdfs: list[tuple[Path, dict[str, Any]]] = []
    page_count = 0
    warnings: list[dict[str, Any]] = []
    for sheet in raw_sheets:
        source_sheet = source_sheets.get(sheet["index"], {})
        sheet["source_structure"] = {
            key: source_sheet.get(key)
            for key in (
                "xml_path",
                "formula_count",
                "merge_count",
                "conditional_format_count",
                "drawing_count",
                "table_count",
                "auto_filter_range",
                "frozen_pane",
                "print_area",
                "page_setup",
            )
        }
        if sheet.get("rendered"):
            try:
                sheet, sheet_pages = _postprocess_sheet(
                    sheet,
                    bundle_root,
                    config,
                )
                page_count += sheet_pages
                normalized_pdfs.append(
                    (
                        bundle_root / sheet["artifacts"]["pages_pdf"]["path"],
                        sheet,
                    )
                )
            except RenderError as exc:
                sheet["rendered"] = False
                sheet["render_error"] = exc.message
        if sheet.get("render_error"):
            warnings.append(
                {
                    "code": "spreadsheet_sheet_render_failed",
                    "message": f"Sheet {sheet['name']} could not be rendered.",
                    "details": {
                        "sheet_index": sheet["index"],
                        "error": sheet["render_error"],
                    },
                }
            )
        if sheet.get("viewport_error"):
            warnings.append(
                {
                    "code": "spreadsheet_viewport_capture_failed",
                    "message": (f"The live Calc viewport for sheet {sheet['name']} could not be captured."),
                    "details": {
                        "sheet_index": sheet["index"],
                        "error": sheet["viewport_error"],
                    },
                }
            )
        processed.append(sheet)
    return _ProcessedSheets(
        sheets=processed,
        normalized_pdfs=normalized_pdfs,
        page_count=page_count,
        warnings=warnings,
    )


class SpreadsheetRenderer:
    def __init__(self, config: RenderConfig) -> None:
        self.config = config

    def available(self) -> bool:
        return bool(self.config.libreoffice_path and uno_python_path())

    def viewport_available(self) -> bool:
        return self.available() and spreadsheet_viewport_available()

    def render(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
    ) -> SpreadsheetRenderResult:
        python_path = uno_python_path()
        if not self.config.libreoffice_path or not python_path:
            return SpreadsheetRenderResult(
                artifacts={},
                preview_candidates=[],
                rendered={"spreadsheet_views_available": False},
                warnings=[
                    {
                        "code": "spreadsheet_views_unavailable",
                        "message": (
                            "The canonical workbook PDF was created, but the UNO "
                            "runtime needed for per-sheet views is unavailable."
                        ),
                    }
                ],
                normalized_pdf=None,
            )
        worker = run_spreadsheet_worker(
            source,
            bundle_root,
            python_path,
            self.config,
        )
        result = worker.payload
        worker_root = worker.worker_root
        capture_viewports = worker.capture_viewports
        started = worker.started_at
        processed = _process_sheets(
            result["sheets"],
            detection,
            bundle_root,
            self.config,
        )
        if not processed.normalized_pdfs:
            raise RenderError(
                "conversion_failed",
                "No visible spreadsheet sheet produced a usable view.",
            )
        normalized_pdf = worker_root / "normalized-document.pdf"
        normalized_page_map = _merge_normalized_sheet_pdfs(
            processed.normalized_pdfs,
            normalized_pdf,
            self.config.max_pages,
        )
        source_print_page_count = _pdf_page_count(
            BundlePaths(bundle_root).document,
        )
        visible_sheet_count = sum(bool(sheet["visible"]) for sheet in processed.sheets)
        hidden_sheet_count = sum(not bool(sheet["visible"]) for sheet in processed.sheets)
        rendered_sheet_count = len(processed.normalized_pdfs)
        viewport_count = sum(len(sheet.get("artifacts", {}).get("viewports", [])) for sheet in processed.sheets)
        document_layout = {
            "mode": "normalized_fit_width",
            "page_count": len(normalized_page_map),
            "pages": normalized_page_map,
            "source_print_page_count": source_print_page_count,
        }
        preview_candidates = _spreadsheet_preview_candidates(processed.sheets)
        warnings = processed.warnings
        warnings.append(
            {
                "code": "spreadsheet_views_normalized",
                "message": (
                    "document.pdf uses one-page-wide, vertically paginated views for "
                    "each visible Sheet. Intermediate per-Sheet artifacts are removed "
                    "before publication. Spreadsheet preview images come from readable "
                    "live Calc viewports."
                ),
            }
        )
        if not capture_viewports:
            warnings.append(
                {
                    "code": "spreadsheet_viewports_unavailable",
                    "message": (
                        "The X11 capture runtime is unavailable; preview falls back to normalized per-sheet exports."
                    ),
                }
            )
        return SpreadsheetRenderResult(
            artifacts={
                "sheet_count": result["sheet_count"],
                "visible_sheet_count": visible_sheet_count,
                "hidden_sheet_count": hidden_sheet_count,
                "rendered_sheet_count": rendered_sheet_count,
                "sheet_page_count": processed.page_count,
                "viewport_count": viewport_count,
                "document_layout": document_layout,
            },
            preview_candidates=preview_candidates,
            rendered={
                "spreadsheet_views_available": True,
                "spreadsheet_sheet_count": result["sheet_count"],
                "spreadsheet_visible_sheet_count": visible_sheet_count,
                "spreadsheet_rendered_sheet_count": rendered_sheet_count,
                "spreadsheet_sheet_page_count": processed.page_count,
                "spreadsheet_document_layout": "normalized_fit_width",
                "spreadsheet_source_print_page_count": source_print_page_count,
                "spreadsheet_viewport_count": viewport_count,
                "spreadsheet_view_duration_ms": round((time.monotonic() - started) * 1000),
            },
            warnings=warnings,
            normalized_pdf=normalized_pdf,
        )
