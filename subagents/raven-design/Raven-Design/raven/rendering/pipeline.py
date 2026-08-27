"""Normalize supported inputs into validated render bundles."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from raven.rendering.bundle import PUBLISHED_ENTRIES, BundlePaths, validate_published_bundle
from raven.rendering.detection import detect_file
from raven.rendering.models import (
    MOTION_MODES,
    AdapterResult,
    Detection,
    RenderAdapter,
    RenderConfig,
    RenderError,
    RenderOutcome,
    RenderRequest,
)
from raven.rendering.paths import RenderPathPolicy
from raven.rendering.pdf import PdfBackend, PyMuPdfBackend, image_difference
from raven.rendering.preview import PreviewBuilder
from raven.rendering.util import safe_stem

_JOB_ID_LENGTH = 12
_MAX_ACTIONS = 5
_MAX_SELECTOR_LENGTH = 200
_MAX_FILL_VALUE_LENGTH = 500
_ACTION_WAIT_RANGE_MS = (50, 3000)


def _validated_actions(
    actions: tuple[dict[str, Any], ...] | None,
) -> tuple[dict[str, Any], ...] | None:
    if actions is None:
        return None
    fail = RenderError(
        "invalid_parameters",
        f"actions must be a list of at most {_MAX_ACTIONS} steps, each exactly one of click/hover/fill/wait_ms.",
    )
    if not isinstance(actions, (list, tuple)) or not actions or len(actions) > _MAX_ACTIONS:
        raise fail
    validated: list[dict[str, Any]] = []
    for item in actions:
        if not isinstance(item, Mapping) or len(item) != 1:
            raise fail
        key, value = next(iter(item.items()))
        if key in ("click", "hover"):
            if not isinstance(value, str) or not 1 <= len(value) <= _MAX_SELECTOR_LENGTH:
                raise fail
        elif key == "fill":
            if (
                not isinstance(value, Mapping)
                or set(value) != {"selector", "value"}
                or not isinstance(value["selector"], str)
                or not 1 <= len(value["selector"]) <= _MAX_SELECTOR_LENGTH
                or not isinstance(value["value"], str)
                or len(value["value"]) > _MAX_FILL_VALUE_LENGTH
            ):
                raise fail
            value = dict(value)
        elif key == "wait_ms":
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not _ACTION_WAIT_RANGE_MS[0] <= value <= _ACTION_WAIT_RANGE_MS[1]
            ):
                raise fail
        else:
            raise fail
        validated.append({key: value})
    return tuple(validated)


@dataclass(frozen=True)
class _StageResult:
    pages: list[dict[str, Any]]
    previews: list[dict[str, Any]]
    preview_candidate_count: int
    warnings: list[dict[str, Any]]


class RenderPipeline:
    def __init__(
        self,
        config: RenderConfig,
        adapters: Mapping[str, RenderAdapter],
        previews: PreviewBuilder,
        path_policy: RenderPathPolicy | None = None,
        pdf_backend: PdfBackend | None = None,
    ) -> None:
        self.config = config
        self.adapters = dict(adapters)
        self.previews = previews
        self.path_policy = path_policy
        self.pdf = pdf_backend or PyMuPdfBackend()

    def run(
        self,
        request: RenderRequest,
        *,
        preview_limit: int,
    ) -> RenderOutcome:
        request = self._validate_request(request)
        detection = detect_file(
            request.path,
            max_expanded_bytes=self.config.max_asset_bytes,
            max_animation_frames=self.config.max_animation_frames,
            max_total_pixels=self.config.max_total_pixels,
        )
        final_dir, stage_dir = self._bundle_directories(
            request.output_dir,
            request.path.stem,
        )
        stage_dir.mkdir()
        try:
            outcome = self._render_stage(
                request,
                detection,
                stage_dir,
                preview_limit,
            )
            self._prune(stage_dir)
            validate_published_bundle(stage_dir, self.config.max_output_bytes)
            os.replace(stage_dir, final_dir)
            return RenderOutcome(
                bundle_dir=final_dir,
                detection=detection,
                page_records=outcome.pages,
                preview_records=outcome.previews,
                preview_candidate_count=outcome.preview_candidate_count,
                warnings=outcome.warnings,
            )
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir)

    def _render_stage(
        self,
        request: RenderRequest,
        detection: Detection,
        stage_dir: Path,
        preview_limit: int,
    ) -> _StageResult:
        paths = BundlePaths(stage_dir)
        adapter_result = self._render_with_adapter(
            request,
            detection,
            stage_dir,
        )
        self._require_pdf(paths.document)
        pdf_record, page_records = self.pdf.rasterize(
            paths.document,
            paths.pages,
            request.page_range,
            self.config,
            stage_dir,
            crop_to_content=detection.format == "xlsx",
        )
        pdf_record["fidelity"] = "vector_or_text_preserving"
        self._enrich_spreadsheet_pages(
            pdf_record,
            page_records,
            adapter_result,
        )
        self._enrich_presentation_pages(
            detection,
            pdf_record,
            page_records,
            adapter_result,
        )
        if detection.family == "browser":
            rebuilt = self._ensure_browser_pdf(
                paths,
                page_records,
                adapter_result,
            )
            if rebuilt:
                pdf_record, page_records = self.pdf.rasterize(
                    paths.document,
                    paths.pages,
                    request.page_range,
                    self.config,
                    stage_dir,
                )
            pdf_record["fidelity"] = adapter_result.rendered.pop(
                "pdf_fidelity",
                "vector_or_text_preserving",
            )
        preview_build = self.previews.materialize(
            stage_dir,
            page_records,
            adapter_result,
            preview_limit,
        )
        return _StageResult(
            pages=page_records,
            previews=preview_build.records,
            preview_candidate_count=preview_build.candidate_count,
            warnings=adapter_result.warnings,
        )

    def _render_with_adapter(
        self,
        request: RenderRequest,
        detection: Detection,
        stage_dir: Path,
    ) -> AdapterResult:
        adapter = self.adapters.get(detection.family)
        if adapter is None:
            raise RenderError(
                "unsupported_format",
                "No render adapter is available.",
            )
        if request.actions and detection.family != "browser":
            raise RenderError(
                "invalid_parameters",
                "actions only apply to browser-rendered documents (HTML/SVG).",
            )
        return adapter.render(
            request.path,
            stage_dir,
            detection,
            request,
        )

    def _ensure_browser_pdf(
        self,
        paths: BundlePaths,
        page_records: list[dict[str, Any]],
        adapter_result: AdapterResult,
    ) -> bool:
        if not page_records:
            return False
        reference = paths.root / adapter_result.rendered.get(
            "pdf_reference_path",
            "screen/viewport.png",
        )
        comparison = image_difference(reference, paths.root / page_records[0]["path"])
        adapter_result.rendered["pdf_vs_screen"] = comparison
        if (
            comparison["changed_pixel_ratio"] <= self.config.browser_changed_pixel_limit
            and comparison["mean_absolute_channel_delta"] <= self.config.browser_channel_delta_limit
        ):
            return False
        self.pdf.image_to_pdf(reference, paths.document)
        if paths.pages.exists():
            shutil.rmtree(paths.pages)
        adapter_result.rendered["pdf_fidelity"] = "raster_fallback"
        adapter_result.warnings.append(
            {
                "code": "browser_pdf_raster_fallback",
                "message": (
                    "Chromium PDF differed materially from the screen screenshot; "
                    "the snapshot PDF was rebuilt from the reference raster."
                ),
                "details": comparison,
            }
        )
        return True

    def _validate_request(self, request: RenderRequest) -> RenderRequest:
        if self.path_policy is not None:
            source = self.path_policy.resolve_source(request.path)
            output_dir = self.path_policy.resolve_output_dir(
                request.output_dir,
                internal=request.internal_output,
            )
            asset_root = self.path_policy.resolve_asset_root(request.asset_root)
        else:
            try:
                source = request.path.expanduser().resolve(strict=True)
            except FileNotFoundError as exc:
                raise RenderError(
                    "file_not_found",
                    "The source file does not exist.",
                ) from exc
            if not source.is_file():
                raise RenderError("unsafe_input", "The source must be a regular file.")
            output_dir = request.output_dir.expanduser().resolve()
            if output_dir == Path(output_dir.anchor):
                raise RenderError(
                    "path_not_allowed",
                    "The output directory is too broad.",
                )
            if output_dir.exists() and not output_dir.is_dir():
                raise RenderError(
                    "path_not_allowed",
                    "The output path is not a directory.",
                )
            asset_root = self._resolve_asset_root(request.asset_root)
        if source.stat().st_size > self.config.max_input_bytes:
            raise RenderError(
                "resource_limit_exceeded",
                "The source exceeds the input byte limit.",
            )
        if request.motion_mode not in MOTION_MODES:
            raise RenderError(
                "invalid_parameters",
                "motion_mode must be auto, static, or dynamic.",
            )
        if (
            not isinstance(request.capture_duration_seconds, (int, float))
            or isinstance(request.capture_duration_seconds, bool)
            or not isfinite(request.capture_duration_seconds)
            or not (
                self.config.min_motion_seconds <= request.capture_duration_seconds <= self.config.max_motion_seconds
            )
        ):
            raise RenderError(
                "invalid_parameters",
                "capture_duration_seconds is outside the allowed range.",
            )
        if (
            not isinstance(request.capture_at_seconds, (int, float))
            or isinstance(request.capture_at_seconds, bool)
            or not isfinite(request.capture_at_seconds)
            or not 0 <= request.capture_at_seconds <= request.capture_duration_seconds
        ):
            raise RenderError(
                "invalid_parameters",
                "capture_at_seconds must fall inside the capture window.",
            )
        if (
            not isinstance(request.viewport_width, int)
            or isinstance(request.viewport_width, bool)
            or not (self.config.min_viewport_width <= request.viewport_width <= self.config.max_viewport_width)
        ):
            raise RenderError(
                "invalid_parameters",
                "viewport width is outside the allowed range.",
            )
        if (
            not isinstance(request.viewport_height, int)
            or isinstance(request.viewport_height, bool)
            or not (self.config.min_viewport_height <= request.viewport_height <= self.config.max_viewport_height)
        ):
            raise RenderError(
                "invalid_parameters",
                "viewport height is outside the allowed range.",
            )
        actions = _validated_actions(request.actions)
        return RenderRequest(
            path=source,
            output_dir=output_dir,
            motion_mode=request.motion_mode,
            capture_duration_seconds=request.capture_duration_seconds,
            capture_at_seconds=request.capture_at_seconds,
            page_range=request.page_range,
            viewport_width=request.viewport_width,
            viewport_height=request.viewport_height,
            asset_root=asset_root,
            internal_output=request.internal_output,
            actions=actions,
        )

    @staticmethod
    def _resolve_asset_root(asset_root: Path | None) -> Path | None:
        if asset_root is None:
            return None
        try:
            resolved = asset_root.expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise RenderError(
                "path_not_allowed",
                "asset_root does not exist.",
            ) from exc
        if not resolved.is_dir():
            raise RenderError(
                "path_not_allowed",
                "asset_root must be a directory.",
            )
        return resolved

    @staticmethod
    def _bundle_directories(
        output_dir: Path,
        source_stem: str,
    ) -> tuple[Path, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex[:_JOB_ID_LENGTH]
        bundle_name = f"{safe_stem(source_stem)}-{job_id}"
        final_dir = output_dir / bundle_name
        stage_dir = output_dir / f".{bundle_name}.staging"
        if final_dir.exists() or stage_dir.exists():
            raise RenderError(
                "publish_failed",
                "The generated artifact path already exists.",
            )
        return final_dir, stage_dir

    @staticmethod
    def _require_pdf(pdf_path: Path) -> None:
        if not pdf_path.is_file() or not pdf_path.stat().st_size:
            raise RenderError(
                "invalid_output",
                "The renderer produced no non-empty PDF.",
            )

    @staticmethod
    def _prune(stage_dir: Path) -> None:
        paths = BundlePaths(stage_dir)
        for child in stage_dir.iterdir():
            if child.name in PUBLISHED_ENTRIES:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        if not paths.document.is_file():
            raise RenderError("invalid_output", "The final PDF is missing.")
        if not paths.pages.is_dir():
            raise RenderError(
                "invalid_output",
                "The final page images are missing.",
            )
        paths.preview.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _enrich_spreadsheet_pages(
        pdf_record: dict[str, Any],
        page_records: list[dict[str, Any]],
        adapter_result: AdapterResult,
    ) -> None:
        spreadsheet = adapter_result.artifacts.get("spreadsheet")
        if not isinstance(spreadsheet, dict):
            return
        layout = spreadsheet.get("document_layout")
        if not isinstance(layout, dict):
            return
        pages = layout.get("pages")
        if not isinstance(pages, list):
            return
        page_map = {
            item["page"]: item for item in pages if isinstance(item, dict) and isinstance(item.get("page"), int)
        }
        for record in page_records:
            metadata = page_map.get(record["page"])
            if metadata is not None:
                record.update(metadata)
        pdf_record["layout"] = layout.get("mode")
        source_page_count = layout.get("source_print_page_count")
        if isinstance(source_page_count, int):
            pdf_record["source_print_page_count"] = source_page_count

    @staticmethod
    def _enrich_presentation_pages(
        detection: Detection,
        pdf_record: dict[str, Any],
        page_records: list[dict[str, Any]],
        adapter_result: AdapterResult,
    ) -> None:
        if detection.format != "pptx":
            return
        slides = detection.metadata.get("slides")
        if not isinstance(slides, list):
            return
        visible = [slide for slide in slides if isinstance(slide, dict) and not slide.get("hidden")]
        page_count = pdf_record.get("page_count")
        if page_count == len(visible):
            exported = visible
        elif page_count == len(slides):
            exported = slides
        else:
            adapter_result.warnings.append(
                {
                    "code": "presentation_slide_mapping_uncertain",
                    "message": "The rendered PDF page count does not match the detected slide structure.",
                }
            )
            return
        for record in page_records:
            page = record.get("page")
            if isinstance(page, int) and 1 <= page <= len(exported):
                record["slide"] = int(exported[page - 1]["index"]) + 1
