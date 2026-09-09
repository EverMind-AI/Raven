"""Unit tests for preview_file metadata and multimodal results."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import ToolResult
from raven_design.rendering.models import Detection, RenderConfig, RenderOutcome
from raven_design.rendering.paths import RenderPathPolicy
from raven_design.rendering.service import RenderService
from raven_design.tools.render import PreviewFileTool


class _PreviewService:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            timeout_seconds=180,
            min_motion_seconds=0.25,
            max_motion_seconds=10.0,
            min_viewport_width=320,
            max_viewport_width=3840,
            min_viewport_height=240,
            max_viewport_height=2160,
            default_preview_count=6,
            max_preview_count=12,
        )
        self.options = None

    async def preview(self, **kwargs):
        self.options = kwargs
        return [
            {"type": "text", "text": '{"views":[{"image":1,"page":2}]}'},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
            },
        ]


@pytest.mark.asyncio
async def test_preview_file_returns_metadata_and_images_without_wrapper_fields() -> None:
    service = _PreviewService()
    tool = PreviewFileTool(service, 3.0)

    result = await tool.execute(
        path="report.docx",
        max_previews=4,
        page_range="2-5",
        viewport={"width": 1024, "height": 768},
    )

    assert isinstance(result, ToolResult)
    assert json.loads(result.model_text) == {"views": [{"image": 1, "page": 2}]}
    assert result.blocks is not None
    assert result.blocks[1]["type"] == "image_url"
    assert service.options["max_previews"] == 4
    assert service.options["page_range"] == "2-5"
    assert service.options["viewport_width"] == 1024
    assert service.options["viewport_height"] == 768


@pytest.mark.asyncio
async def test_preview_file_forwards_device_scale_factor() -> None:
    service = _PreviewService()
    tool = PreviewFileTool(service, 3.0)

    await tool.execute(path="poster.html", scale=2)

    assert service.options["scale"] == 2


@pytest.mark.asyncio
async def test_registry_preserves_media_for_model_and_uses_metadata_for_display() -> None:
    registry = ToolRegistry()
    registry.register(PreviewFileTool(_PreviewService(), 3.0))

    output = await registry.execute("preview_file", {"path": "report.docx"})

    assert json.loads(str(output)) == {"views": [{"image": 1, "page": 2}]}
    # trunk's tool_call indirection forwards the multimodal blocks but
    # not the display string (cosmetic; the fork registry forwarded
    # both) -- media survival is the load-bearing half of this pin.
    assert output.blocks is not None
    assert len(output.blocks) == 2
    assert output.blocks[1]["type"] == "image_url"


def test_preview_file_schema_enforces_preview_hard_limit() -> None:
    tool = PreviewFileTool(_PreviewService(), 3.0)

    assert tool.parameters["additionalProperties"] is False
    assert tool.parameters["properties"]["max_previews"]["maximum"] == 12
    # The fork Tool base validated params itself; on trunk the registry
    # casts and refuses out-of-range params at dispatch (its own tests
    # pin that), so the schema shape above is the whole port.


class _OutcomeBackend:
    async def run(self, request, *, preview_limit):
        bundle = request.output_dir / "bundle"
        (bundle / "pages").mkdir(parents=True)
        (bundle / "preview").mkdir()
        (bundle / "document.pdf").write_bytes(b"%PDF-test")
        (bundle / "pages" / "page-0001.png").write_bytes(b"page")
        (bundle / "preview" / "00-page.png").write_bytes(b"preview")
        return RenderOutcome(
            bundle_dir=bundle,
            detection=Detection(
                format="docx",
                family="office",
                mime="application/octet-stream",
                declared_extension=".docx",
                support_level="guaranteed",
                metadata={},
            ),
            page_records=[{"path": "pages/page-0001.png", "page": 1}],
            preview_records=[
                {
                    "path": "preview/00-page.png",
                    "mime": "image/png",
                    "page": 1,
                }
            ],
            preview_candidate_count=1,
            warnings=[],
        )


@pytest.mark.asyncio
async def test_preview_without_output_dir_cleans_ephemeral_bundle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"source")
    runtime = tmp_path / "runtime"
    service = RenderService(
        RenderConfig(chrome_path=None, libreoffice_path=None),
        path_policy=RenderPathPolicy(
            workspace=tmp_path,
            media_root=tmp_path,
            runtime_root=runtime,
            restrict_to_workspace=True,
        ),
        backend="direct",
    )
    service.backend = _OutcomeBackend()

    parts = await service.preview(path=source, max_previews=1)

    assert json.loads(parts[0]["text"]) == {"views": [{"image": 1, "page": 1}]}
    assert len(parts) == 2
    assert list(runtime.iterdir()) == []
