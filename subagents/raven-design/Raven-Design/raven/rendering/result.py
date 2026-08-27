"""Convert render outcomes into agent-facing metadata and image blocks."""

from __future__ import annotations

import base64
import json
from pathlib import PurePosixPath
from typing import Any

from raven.rendering.models import Detection, RenderError, RenderOutcome, RenderToolOutput
from raven.utils.helpers import ContentPart, image_block, text_block

NON_CONTENT_WARNING_CODES = frozenset(
    {
        "browser_pdf_raster_fallback",
        "prototype_direct_browser",
        "prototype_office_backend",
        "prototype_pillow_animation_decode",
        "spreadsheet_views_normalized",
    }
)


def render_tool_result(outcome: RenderOutcome) -> RenderToolOutput:
    result: RenderToolOutput = {"dir": str(outcome.bundle_dir)}
    warnings = public_warning_codes(outcome.warnings)
    if warnings:
        result["warnings"] = warnings
    errors = page_error_details(outcome.warnings)
    if errors:
        result["errors"] = errors
    return result


def preview_tool_result(
    outcome: RenderOutcome,
    max_inline_bytes: int,
) -> list[ContentPart]:
    inline_bytes = 0
    emitted_records: list[dict[str, Any]] = []
    image_parts: list[ContentPart] = []
    for record in outcome.preview_records:
        relative_path = PurePosixPath(str(record.get("path") or ""))
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or not relative_path.parts
            or relative_path.parts[0] != "preview"
        ):
            raise RenderError("invalid_output", "Preview metadata contains an unsafe image path.")
        image_path = outcome.bundle_dir.joinpath(*relative_path.parts)
        if not image_path.is_file():
            raise RenderError("invalid_output", "Preview metadata points to a missing image.")
        if inline_bytes + image_path.stat().st_size > max_inline_bytes:
            break
        payload = image_path.read_bytes()
        if inline_bytes + len(payload) > max_inline_bytes:
            break
        inline_bytes += len(payload)
        image_parts.append(image_block(f"data:image/png;base64,{base64.b64encode(payload).decode('ascii')}"))
        emitted_records.append(record)
    metadata: dict[str, Any] = {
        "views": [
            preview_view(record, index, outcome.detection) for index, record in enumerate(emitted_records, start=1)
        ]
    }
    omitted = omitted_metadata(outcome, len(emitted_records))
    if omitted:
        metadata["omitted"] = omitted
    warnings = public_warning_codes(outcome.warnings)
    if warnings:
        metadata["warnings"] = warnings
    errors = page_error_details(outcome.warnings)
    if errors:
        metadata["errors"] = errors
    return [text_block(json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)), *image_parts]


def preview_view(
    record: dict[str, Any],
    image_index: int,
    detection: Detection,
) -> dict[str, Any]:
    view: dict[str, Any] = {"image": image_index}
    if record.get("action"):
        view["action"] = record["action"]
        status = record.get("status")
        if status and status != "ok":
            view["status"] = status
        changed = record.get("changed_pixel_ratio")
        if changed is not None:
            view["changed_pixel_ratio"] = changed
        return view
    sheet_name = str(record.get("sheet_name") or "").strip()
    if sheet_name:
        view["sheet"] = sheet_name
        cell_range = record.get("visible_range") or record.get("used_range")
        if cell_range:
            view["range"] = cell_range
    elif record.get("at_ms") is not None:
        view["at_ms"] = record["at_ms"]
    elif detection.format == "pptx" and record.get("slide") is not None:
        view["slide"] = record["slide"]
    elif record.get("page") is not None:
        key = "slide" if detection.format == "pptx" else "page"
        view[key] = record["page"]
    return view


def omitted_metadata(
    outcome: RenderOutcome,
    emitted_count: int,
) -> dict[str, int]:
    omitted: dict[str, int] = {}
    missing_images = max(0, outcome.preview_candidate_count - emitted_count)
    if missing_images:
        omitted["images"] = missing_images
    if outcome.detection.format == "xlsx":
        hidden_sheets = int(outcome.detection.metadata.get("hidden_sheet_count") or 0)
        if hidden_sheets:
            omitted["hidden_sheets"] = hidden_sheets
    return omitted


def public_warning_codes(warnings: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for warning in warnings:
        code = warning.get("code")
        if isinstance(code, str) and code not in NON_CONTENT_WARNING_CODES and code not in codes:
            codes.append(code)
    return codes


_ERROR_DETAIL_CODES = (
    "browser_page_errors",
    "suspicious_page_text",
    "animation_not_running",
    "action_no_visible_response",
)


def page_error_details(warnings: list[dict[str, Any]]) -> list[str]:
    """Verbatim defect lines for the model to act on.

    A bare warning code is not actionable; the actual text (an uncaught
    TypeError, "page text contains 'NaN'", "the animation is not
    running") is what lets the author locate and fix the defect.
    """
    lines: list[str] = []
    for warning in warnings:
        if warning.get("code") in _ERROR_DETAIL_CODES:
            details = warning.get("details")
            if isinstance(details, list):
                lines.extend(str(d)[:300] for d in details[:5])
    return lines[:10]
