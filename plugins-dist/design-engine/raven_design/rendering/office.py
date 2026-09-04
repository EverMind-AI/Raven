"""Route Office documents through available rendering backends."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from raven_design.rendering.bundle import BundlePaths
from raven_design.rendering.models import (
    AdapterResult,
    Detection,
    RenderConfig,
    RenderError,
    RenderRequest,
)
from raven_design.rendering.spreadsheet import SpreadsheetRenderer
from raven_design.rendering.util import command_version

OFFICE_FORMATS = frozenset({"docx", "pptx", "xlsx"})
_LOG_TAIL_LENGTH = 1000


class OfficeBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def render(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
        request: RenderRequest,
    ) -> AdapterResult: ...


class OfficeRouter:
    def __init__(
        self,
        order: tuple[str, ...],
        backends: dict[str, OfficeBackend],
        *,
        allow_cloud: bool = False,
    ) -> None:
        self.order = order
        self.backends = backends
        self.allow_cloud = allow_cloud

    def render(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
        request: RenderRequest,
    ) -> AdapterResult:
        for name in self.order:
            if name == "graph" and not self.allow_cloud:
                continue
            backend = self.backends.get(name)
            if backend is not None and backend.available():
                return backend.render(source, bundle_root, detection, request)
        raise RenderError(
            "renderer_unavailable",
            "No configured Office backend is available.",
            details={"requested_backends": list(self.order)},
        )


class LibreOfficeBackend:
    name = "libreoffice"

    def __init__(self, config: RenderConfig) -> None:
        self.config = config
        self.spreadsheet = SpreadsheetRenderer(config)

    def available(self) -> bool:
        return bool(self.config.libreoffice_path)

    def render(
        self,
        source: Path,
        bundle_root: Path,
        detection: Detection,
        _request: RenderRequest,
    ) -> AdapterResult:
        executable = self.config.libreoffice_path
        if not executable:
            raise RenderError(
                "renderer_unavailable",
                "No local Office conversion backend is available.",
                details={"backend": self.name},
            )
        if detection.format not in OFFICE_FORMATS:
            raise RenderError(
                "unsupported_format",
                f"The Office backend does not support {detection.format}.",
            )
        worker = bundle_root / ".worker" / "libreoffice"
        input_dir = worker / "input"
        output_dir = worker / "output"
        profile_dir = worker / "profile"
        input_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        profile_dir.mkdir(parents=True)
        staged_source = input_dir / source.name
        shutil.copy2(source, staged_source)
        command = [
            executable,
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(staged_source),
        ]
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError(
                "render_timeout",
                "The Office conversion exceeded its time limit.",
                retryable=True,
                details={"backend": self.name},
            ) from exc
        candidates = list(output_dir.glob("*.pdf"))
        if len(candidates) != 1 or not candidates[0].is_file():
            raise RenderError(
                "conversion_failed",
                "LibreOffice did not produce exactly one PDF.",
                details={
                    "backend": self.name,
                    "exit_code": result.returncode,
                    "stdout": result.stdout.strip()[-_LOG_TAIL_LENGTH:],
                    "stderr": result.stderr.strip()[-_LOG_TAIL_LENGTH:],
                },
            )
        target = BundlePaths(bundle_root).document
        candidates[0].replace(target)
        canonical_duration_ms = round((time.monotonic() - started) * 1000)
        warnings: list[dict[str, object]] = []
        metadata = detection.metadata
        if metadata.get("has_macros"):
            warnings.append(
                {
                    "code": "macros_disabled",
                    "message": "Office macros were not executed.",
                }
            )
        if detection.format == "pptx" and (metadata.get("transition_count") or metadata.get("timing_tree_count")):
            warnings.append(
                {
                    "code": "powerpoint_motion_not_rendered",
                    "message": (
                        "PowerPoint transitions or animations were detected but the Office PDF output is static."
                    ),
                }
            )
        if metadata.get("external_link_count"):
            warnings.append(
                {
                    "code": "external_links_not_updated",
                    "message": "External Office links were not refreshed.",
                }
            )
        artifacts: dict[str, object] = {}
        preview_candidates: list[dict[str, object]] = []
        spreadsheet_rendered: dict[str, object] = {}
        if detection.format == "xlsx":
            try:
                spreadsheet_result = self.spreadsheet.render(
                    source,
                    bundle_root,
                    detection,
                )
                if spreadsheet_result.normalized_pdf is not None:
                    try:
                        spreadsheet_result.normalized_pdf.replace(target)
                    except OSError as exc:
                        raise RenderError(
                            "publish_failed",
                            "The normalized spreadsheet PDF could not be promoted.",
                        ) from exc
                artifacts["spreadsheet"] = spreadsheet_result.artifacts
                preview_candidates.extend(spreadsheet_result.preview_candidates)
                spreadsheet_rendered.update(spreadsheet_result.rendered)
                warnings.extend(spreadsheet_result.warnings)
            except RenderError as exc:
                spreadsheet_rendered["spreadsheet_views_available"] = False
                warnings.append(
                    {
                        "code": "spreadsheet_views_failed",
                        "message": ("The canonical workbook PDF was created, but specialized per-sheet views failed."),
                        "details": exc.as_dict(),
                    }
                )
        return AdapterResult(
            renderer={
                "adapter": "office",
                "backend": self.name,
                "engine_version": command_version(executable),
                "format": detection.format,
                "isolated": False,
                "network_policy": "backend-default",
            },
            rendered={
                "conversion_duration_ms": canonical_duration_ms,
                "backend_exit_code": result.returncode,
                **spreadsheet_rendered,
            },
            motion={
                "classification": "static_observed",
                "confidence": "container_semantics",
                "observed_ms": 0,
                "signals": [],
                "changed_frame_pairs": 0,
            },
            artifacts=artifacts,
            preview_candidates=preview_candidates,
            warnings=warnings,
        )
