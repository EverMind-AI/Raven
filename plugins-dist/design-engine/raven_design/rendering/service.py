"""Shared service coordinating rendering, persistence, and previews."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from raven.contracts.tool import ContentPart
from raven_design.rendering.backend import BoxLiteRenderBackend, DirectRenderBackend, RenderExecutionBackend
from raven_design.rendering.models import (
    DEFAULT_CAPTURE_DURATION_SECONDS,
    DEFAULT_VIEWPORT,
    RenderConfig,
    RenderError,
    RenderOutcome,
    RenderRequest,
    RenderToolOutput,
)
from raven_design.rendering.paths import RenderPathPolicy
from raven_design.rendering.result import preview_tool_result, render_tool_result


class RenderService:
    def __init__(
        self,
        config: RenderConfig | None = None,
        *,
        path_policy: RenderPathPolicy | None = None,
        backend: str = "auto",
        worker_image: str = "",
        worker_cpus: int = 2,
        worker_memory_mib: int = 4096,
        worker_create_timeout_seconds: int = 300,
        max_concurrency: int = 2,
    ) -> None:
        self.config = config or RenderConfig.discover()
        workspace = Path.cwd().resolve()
        self.path_policy = path_policy or RenderPathPolicy(
            workspace=workspace,
            media_root=workspace,
            runtime_root=Path(tempfile.gettempdir()) / "raven-render-runtime",
        )
        self.backend_name = backend
        self.backend = self._build_backend(
            backend,
            worker_image=worker_image,
            worker_cpus=worker_cpus,
            worker_memory_mib=worker_memory_mib,
            worker_create_timeout_seconds=worker_create_timeout_seconds,
        )
        startup_budget = worker_create_timeout_seconds if backend in {"auto", "boxlite"} else 0
        self.tool_timeout_seconds = startup_budget + self.config.timeout_seconds + 30
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @classmethod
    def from_tool_config(
        cls,
        tool_config: Any,
        *,
        workspace: Path,
        media_root: Path,
        runtime_root: Path,
        restrict_to_workspace: bool,
    ) -> RenderService:
        binary_overrides = {
            name: value
            for name, value in {
                "chrome_path": tool_config.chrome_path,
                "libreoffice_path": tool_config.libreoffice_path,
                "ffmpeg_path": tool_config.ffmpeg_path,
                "ffprobe_path": tool_config.ffprobe_path,
                "qpdf_path": tool_config.qpdf_path,
            }.items()
            if value
        }
        config = RenderConfig.discover(
            **binary_overrides,
            allow_network=tool_config.allow_network,
            network_allowlist=tuple(tool_config.network_allowlist),
            office_backend_order=tuple(tool_config.office_backend_order),
            allow_cloud_office=tool_config.allow_cloud_office,
            raster_dpi=tool_config.raster_dpi,
            preview_max_edge=tool_config.preview_max_edge,
            max_input_bytes=tool_config.max_input_bytes,
            max_asset_bytes=tool_config.max_asset_bytes,
            max_output_bytes=tool_config.max_output_bytes,
            max_pages=tool_config.max_pages,
            max_side_pixels=tool_config.max_side_pixels,
            max_total_pixels=tool_config.max_total_pixels,
            max_inline_preview_bytes=tool_config.max_inline_preview_bytes,
            default_preview_count=tool_config.default_preview_count,
            max_preview_count=tool_config.max_preview_count,
            max_motion_seconds=tool_config.max_capture_duration_seconds,
            max_animation_frames=tool_config.max_animation_frames,
            motion_fps=tool_config.motion_fps,
            timeout_seconds=tool_config.timeout_seconds,
            browser_ready_seconds=tool_config.browser_ready_seconds,
            spreadsheet_viewport_width=tool_config.spreadsheet_viewport_width,
            spreadsheet_viewport_height=tool_config.spreadsheet_viewport_height,
            spreadsheet_max_viewports=tool_config.spreadsheet_max_viewports,
            locale=tool_config.locale,
            timezone=tool_config.timezone,
        )
        return cls(
            config,
            path_policy=RenderPathPolicy(
                workspace=workspace,
                media_root=media_root,
                runtime_root=runtime_root,
                restrict_to_workspace=restrict_to_workspace,
            ),
            backend=tool_config.backend,
            worker_image=tool_config.worker_image,
            worker_cpus=tool_config.worker_cpus,
            worker_memory_mib=tool_config.worker_memory_mib,
            worker_create_timeout_seconds=tool_config.worker_create_timeout_seconds,
            max_concurrency=tool_config.max_concurrency,
        )

    async def render(
        self,
        request: RenderRequest,
        *,
        preview_limit: int | None = None,
    ) -> RenderToolOutput:
        outcome = await self.render_outcome(
            request,
            preview_limit=preview_limit,
        )
        return render_tool_result(outcome)

    async def render_outcome(
        self,
        request: RenderRequest,
        *,
        preview_limit: int | None = None,
    ) -> RenderOutcome:
        limit = self.config.default_preview_count if preview_limit is None else preview_limit
        self._validate_preview_limit(limit)
        async with self._semaphore:
            return await self.backend.run(request, preview_limit=limit)

    async def preview(
        self,
        *,
        path: Path,
        output_dir: Path | None = None,
        max_previews: int | None = None,
        motion_mode: str = "auto",
        capture_duration_seconds: float = DEFAULT_CAPTURE_DURATION_SECONDS,
        page_range: str | None = None,
        viewport_width: int = DEFAULT_VIEWPORT[0],
        viewport_height: int = DEFAULT_VIEWPORT[1],
        asset_root: Path | None = None,
        actions: tuple[dict[str, Any], ...] | None = None,
    ) -> list[ContentPart]:
        limit = self.config.default_preview_count if max_previews is None else max_previews
        self._validate_preview_limit(limit)
        with ExitStack() as stack:
            destination = output_dir
            internal_output = False
            if destination is None:
                self.path_policy.runtime_root.mkdir(parents=True, exist_ok=True)
                temporary = stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="raven-render-preview-",
                        dir=self.path_policy.runtime_root,
                    )
                )
                destination = Path(temporary)
                internal_output = True
            outcome = await self.render_outcome(
                RenderRequest(
                    path=path,
                    output_dir=destination,
                    motion_mode=motion_mode,
                    capture_duration_seconds=capture_duration_seconds,
                    capture_at_seconds=capture_duration_seconds,
                    page_range=page_range,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    asset_root=asset_root,
                    internal_output=internal_output,
                    actions=actions,
                ),
                preview_limit=limit,
            )
            return preview_tool_result(
                outcome,
                self.config.max_inline_preview_bytes,
            )

    def _build_backend(
        self,
        backend: str,
        *,
        worker_image: str,
        worker_cpus: int,
        worker_memory_mib: int,
        worker_create_timeout_seconds: int,
    ) -> RenderExecutionBackend:
        if backend == "direct":
            return DirectRenderBackend(self.config, self.path_policy)
        if backend in {"auto", "boxlite"}:
            return BoxLiteRenderBackend(
                self.config,
                self.path_policy,
                image=worker_image,
                cpus=worker_cpus,
                memory_mib=worker_memory_mib,
                create_timeout_seconds=worker_create_timeout_seconds,
            )
        raise ValueError(f"Unsupported render backend: {backend}")

    def _validate_preview_limit(self, limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= self.config.max_preview_count:
            raise RenderError(
                "invalid_parameters",
                f"max_previews must be between 1 and {self.config.max_preview_count}.",
            )
