"""Agent-facing facades for persistent rendering and visual previews."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from raven.contracts.tool import Tool, ToolResult
from raven_design.rendering.models import DEFAULT_VIEWPORT, RenderError, RenderRequest
from raven_design.rendering.service import RenderService


def _path_schema(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "description": description,
        "minLength": 1,
        "maxLength": 4096,
    }


class _RenderTool(Tool):
    def __init__(self, service: RenderService, default_capture_duration_seconds: float) -> None:
        self.service = service
        self.default_capture_duration_seconds = default_capture_duration_seconds
        self.timeout_seconds = float(
            getattr(
                service,
                "tool_timeout_seconds",
                service.config.timeout_seconds + 15,
            )
        )

    def _common_properties(self) -> dict[str, Any]:
        config = self.service.config
        return {
            "path": _path_schema("Source HTML, SVG, Office, or animated-image file."),
            "motion_mode": {
                "type": "string",
                "enum": ["auto", "static", "dynamic"],
                "default": "auto",
            },
            "capture_duration_seconds": {
                "type": "number",
                "minimum": config.min_motion_seconds,
                "maximum": config.max_motion_seconds,
                "default": self.default_capture_duration_seconds,
            },
            "page_range": {
                "type": "string",
                "description": "Optional one-based page selection such as 1-3,5.",
                "minLength": 1,
                "maxLength": 200,
            },
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {
                        "type": "integer",
                        "minimum": config.min_viewport_width,
                        "maximum": config.max_viewport_width,
                    },
                    "height": {
                        "type": "integer",
                        "minimum": config.min_viewport_height,
                        "maximum": config.max_viewport_height,
                    },
                },
                "required": ["width", "height"],
                "additionalProperties": False,
            },
            "asset_root": _path_schema("Optional root for local HTML or SVG assets; the source must be inside it."),
            "scale": {
                "type": "number",
                "minimum": 1,
                "maximum": 4,
                "default": 1,
                "description": (
                    "Device scale factor for HTML/SVG screen captures: 2 doubles the pixel "
                    "size of preview/ and screen images. The PDF stays vector; scaled viewport must fit max_side_pixels."
                ),
            },
            "actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "description": (
                    "Replay user actions on the page (HTML/SVG only) and screenshot after "
                    "each step: verify that controls actually respond. Each step is exactly "
                    'one of {"click": "<css selector>"}, {"hover": "<css selector>"}, '
                    '{"fill": {"selector": "<css selector>", "value": "<text>"}}, or '
                    '{"wait_ms": <50-3000>}. The result reports per-step status and how much '
                    "the page visibly changed; a successful action with zero change means the "
                    "control is wired to nothing."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "click": {"type": "string", "minLength": 1, "maxLength": 200},
                        "hover": {"type": "string", "minLength": 1, "maxLength": 200},
                        "fill": {
                            "type": "object",
                            "properties": {
                                "selector": {"type": "string", "minLength": 1, "maxLength": 200},
                                "value": {"type": "string", "maxLength": 500},
                            },
                            "required": ["selector", "value"],
                            "additionalProperties": False,
                        },
                        "wait_ms": {"type": "integer", "minimum": 50, "maximum": 3000},
                    },
                    "minProperties": 1,
                    "maxProperties": 1,
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _viewport(viewport: dict[str, int] | None) -> tuple[int, int]:
        if viewport is None:
            return DEFAULT_VIEWPORT
        return viewport["width"], viewport["height"]

    def display_call(self, args: dict[str, Any]) -> str | None:
        path = args.get("path")
        return str(path) if path else None

    @staticmethod
    def _error(exc: RenderError) -> str:
        return exc.as_tool_error()


class RenderFileTool(_RenderTool):
    @property
    def name(self) -> str:
        return "render_file"

    @property
    def description(self) -> str:
        return (
            "Render HTML, SVG, DOCX, PPTX, XLSX, GIF, APNG, or animated WebP "
            "into a persistent bundle containing document.pdf, pages/, and preview/. "
            "Use native file reading for text, CSV, PDF, or static images."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        properties = self._common_properties()
        properties.update(
            {
                "output_dir": _path_schema("Directory that will receive a uniquely named render bundle."),
                "capture_at_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": self.service.config.max_motion_seconds,
                    "description": "Snapshot time; defaults to the end of the capture window.",
                },
            }
        )
        return {
            "type": "object",
            "properties": properties,
            "required": ["path", "output_dir"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        path: str,
        output_dir: str,
        motion_mode: str = "auto",
        capture_duration_seconds: float | None = None,
        capture_at_seconds: float | None = None,
        page_range: str | None = None,
        viewport: dict[str, int] | None = None,
        asset_root: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        scale: float = 1.0,
    ) -> str | ToolResult:
        duration = (
            self.default_capture_duration_seconds if capture_duration_seconds is None else capture_duration_seconds
        )
        capture_at = duration if capture_at_seconds is None else capture_at_seconds
        width, height = self._viewport(viewport)
        try:
            result = await self.service.render(
                RenderRequest(
                    path=Path(path),
                    output_dir=Path(output_dir),
                    motion_mode=motion_mode,
                    capture_duration_seconds=duration,
                    capture_at_seconds=capture_at,
                    page_range=page_range,
                    viewport_width=width,
                    viewport_height=height,
                    asset_root=Path(asset_root) if asset_root else None,
                    actions=tuple(actions) if actions else None,
                    scale=scale,
                )
            )
            return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except RenderError as exc:
            return self._error(exc)


class PreviewFileTool(_RenderTool):
    @property
    def name(self) -> str:
        return "preview_file"

    @property
    def description(self) -> str:
        return (
            "Visually inspect HTML, SVG, DOCX, PPTX, XLSX, GIF, APNG, or animated "
            "WebP. Returns compact view metadata followed by the selected preview images."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        properties = self._common_properties()
        properties.update(
            {
                "output_dir": _path_schema(
                    "Optional directory for retaining the normalized bundle; omitted previews are ephemeral."
                ),
                "max_previews": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self.service.config.max_preview_count,
                    "default": self.service.config.default_preview_count,
                },
            }
        )
        return {
            "type": "object",
            "properties": properties,
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        path: str,
        output_dir: str | None = None,
        max_previews: int | None = None,
        motion_mode: str = "auto",
        capture_duration_seconds: float | None = None,
        page_range: str | None = None,
        viewport: dict[str, int] | None = None,
        asset_root: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        scale: float = 1.0,
    ) -> str | ToolResult:
        duration = (
            self.default_capture_duration_seconds if capture_duration_seconds is None else capture_duration_seconds
        )
        width, height = self._viewport(viewport)
        try:
            parts = await self.service.preview(
                path=Path(path),
                output_dir=Path(output_dir) if output_dir else None,
                max_previews=max_previews,
                motion_mode=motion_mode,
                capture_duration_seconds=duration,
                page_range=page_range,
                viewport_width=width,
                viewport_height=height,
                asset_root=Path(asset_root) if asset_root else None,
                actions=tuple(actions) if actions else None,
                scale=scale,
            )
            metadata = next(
                (
                    part.get("text")
                    for part in parts
                    if part.get("type") == "text" and isinstance(part.get("text"), str)
                ),
                '{"views":[]}',
            )
            return ToolResult(
                model_text=metadata,
                display_text=metadata,
                blocks=parts,
            )
        except asyncio.CancelledError:
            raise
        except RenderError as exc:
            return self._error(exc)
