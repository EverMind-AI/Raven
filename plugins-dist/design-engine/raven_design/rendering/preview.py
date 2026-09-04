"""Build compact representative previews from rendered pages and frames."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from raven_design.rendering.bundle import BundlePaths
from raven_design.rendering.keyframes import select_keyframes
from raven_design.rendering.models import (
    AdapterResult,
    RenderConfig,
    RenderError,
)
from raven_design.rendering.util import evenly_spaced, file_record, make_preview_image, safe_stem


@dataclass(frozen=True)
class PreviewBuild:
    records: list[dict[str, Any]]
    candidate_count: int


class PreviewBuilder:
    def __init__(self, config: RenderConfig) -> None:
        self.config = config

    def validate_limit(self, limit: int) -> None:
        if not 1 <= limit <= self.config.max_preview_count:
            raise RenderError(
                "invalid_parameters",
                f"max_previews must be between 1 and {self.config.max_preview_count}.",
            )

    def materialize(
        self,
        bundle_root: Path,
        page_records: list[dict[str, Any]],
        adapter_result: AdapterResult,
        limit: int,
    ) -> PreviewBuild:
        self.validate_limit(limit)
        raw_candidates = adapter_result.preview_candidates or page_records
        candidates = [
            {"path": candidate} if isinstance(candidate, str) else dict(candidate) for candidate in raw_candidates
        ]
        candidate_count = max(
            len(candidates),
            int(adapter_result.rendered.get("spreadsheet_viewport_count") or 0),
            int(adapter_result.rendered.get("frame_count") or 0),
        )
        selected = select_preview_candidates(candidates, limit, bundle_root)
        preview_dir = BundlePaths(bundle_root).preview
        preview_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        for position, candidate in enumerate(selected):
            source = bundle_root / candidate["path"]
            if not source.is_file():
                continue
            target = preview_dir / preview_filename(position, candidate, source)
            make_preview_image(source, target, self.config.preview_max_edge)
            record = file_record(target, bundle_root)
            record["mime"] = "image/png"
            record.update({key: value for key, value in candidate.items() if key != "path" and not key.startswith("_")})
            records.append(record)
        return PreviewBuild(records, candidate_count)


def select_preview_candidates(
    candidates: list[dict[str, Any]],
    limit: int,
    bundle_root: Path | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    if candidates[0].get("at_ms") is not None and bundle_root is not None:
        return select_keyframes(candidates, bundle_root, limit)
    if len(candidates) <= limit:
        return candidates
    if candidates[0].get("sheet_name"):
        return _select_spreadsheet_candidates(candidates, limit)
    return evenly_spaced(candidates, limit)


def _select_spreadsheet_candidates(
    candidates: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    first_views: list[dict[str, Any]] = []
    extra_views: list[dict[str, Any]] = []
    seen_sheets: set[str] = set()
    for candidate in candidates:
        sheet_name = str(candidate.get("sheet_name") or "")
        if sheet_name and sheet_name not in seen_sheets:
            seen_sheets.add(sheet_name)
            first_views.append(candidate)
        else:
            extra_views.append(candidate)
    if len(first_views) >= limit:
        selected = evenly_spaced(first_views, limit)
    else:
        selected = first_views + evenly_spaced(
            extra_views,
            limit - len(first_views),
        )
    selected_ids = {id(candidate) for candidate in selected}
    return [candidate for candidate in candidates if id(candidate) in selected_ids]


def preview_filename(
    position: int,
    metadata: dict[str, Any],
    source: Path,
) -> str:
    kind = metadata.get("kind")
    if metadata.get("sheet_name"):
        label = safe_stem(str(metadata["sheet_name"]))
        if kind == "sheet_viewport":
            anchor = safe_stem(str(metadata.get("anchor_cell") or "viewport"))
            label = f"{label}__{anchor}"
        elif kind == "sheet_overview":
            label = f"{label}__overview"
        elif metadata.get("sheet_page"):
            label = f"{label}__page-{int(metadata['sheet_page']):04d}"
    elif metadata.get("at_ms") is not None:
        label = f"at-{int(metadata['at_ms']):06d}ms"
    else:
        label = safe_stem(source.stem)
    return f"{position:02d}-{label}.png"
