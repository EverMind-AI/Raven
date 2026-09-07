"""Integration coverage for real browser rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PIL")
pytest.importorskip("fitz")
pytest.importorskip("playwright")

from PIL import Image

from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import ToolOutput
from raven_design.rendering.models import RenderConfig, RenderRequest
from raven_design.rendering.paths import RenderPathPolicy
from raven_design.rendering.pdf import image_difference
from raven_design.rendering.service import RenderService
from raven_design.tools.render import PreviewFileTool, RenderFileTool


def _service(tmp_path: Path) -> RenderService:
    config = RenderConfig.discover(
        timeout_seconds=60,
        browser_ready_seconds=3,
        max_motion_seconds=2,
    )
    if not config.chrome_path:
        pytest.skip("Chromium is unavailable")
    return RenderService(
        config,
        path_policy=RenderPathPolicy(
            workspace=tmp_path,
            media_root=tmp_path,
            runtime_root=tmp_path / "runtime",
            restrict_to_workspace=True,
        ),
        backend="direct",
        max_concurrency=1,
    )


def _assert_bundle(bundle: Path) -> None:
    assert {item.name for item in bundle.iterdir()} == {
        "document.pdf",
        "pages",
        "preview",
    }
    assert (bundle / "document.pdf").stat().st_size > 0
    assert list((bundle / "pages").glob("*.png"))
    assert list((bundle / "preview").glob("*.png"))


@pytest.mark.asyncio
async def test_static_html_produces_minimal_bundle(tmp_path: Path) -> None:
    source = tmp_path / "static.html"
    source.write_text(
        """
        <!doctype html>
        <html><body style="margin:0;background:#102030;color:white">
        <main style="width:640px;height:360px;display:grid;place-items:center">
        <h1>Render test</h1></main></body></html>
        """,
        encoding="utf-8",
    )
    service = _service(tmp_path)

    result = await service.render(
        RenderRequest(
            path=source,
            output_dir=tmp_path / "output",
            motion_mode="static",
            capture_duration_seconds=0.25,
            viewport_width=640,
            viewport_height=360,
            scale=2,
        )
    )

    assert set(result) == {"dir"}
    _assert_bundle(Path(result["dir"]))
    with Image.open(next((Path(result["dir"]) / "preview").glob("*.png"))) as image:
        assert image.size == (1280, 720)
    preview = await service.preview(
        path=source,
        output_dir=tmp_path / "preview-output",
        max_previews=1,
        motion_mode="static",
        capture_duration_seconds=0.25,
        viewport_width=640,
        viewport_height=360,
    )
    assert json.loads(preview[0]["text"])["views"] == [{"image": 1, "page": 1}]


@pytest.mark.asyncio
async def test_tool_registry_returns_real_render_and_multimodal_preview(tmp_path: Path) -> None:
    source = tmp_path / "tool.html"
    source.write_text(
        """
        <!doctype html>
        <html><body style="margin:0;background:#172554;color:#f8fafc">
        <main style="width:640px;height:360px;display:grid;place-items:center">
        <h1>Tool preview</h1></main></body></html>
        """,
        encoding="utf-8",
    )
    service = _service(tmp_path)
    registry = ToolRegistry()
    registry.register(RenderFileTool(service, 0.25))
    registry.register(PreviewFileTool(service, 0.25))

    rendered = await registry.execute(
        "render_file",
        {
            "path": str(source),
            "output_dir": str(tmp_path / "render-output"),
            "motion_mode": "static",
            "viewport": {"width": 640, "height": 360},
        },
    )
    _assert_bundle(Path(json.loads(str(rendered))["dir"]))

    previewed = await registry.execute(
        "preview_file",
        {
            "path": str(source),
            "output_dir": str(tmp_path / "preview-output"),
            "max_previews": 1,
            "motion_mode": "static",
            "viewport": {"width": 640, "height": 360},
        },
    )
    assert isinstance(previewed, ToolOutput)
    assert json.loads(str(previewed))["views"] == [{"image": 1, "page": 1}]
    assert previewed.blocks is not None
    assert [part["type"] for part in previewed.blocks] == ["text", "image_url"]


@pytest.mark.asyncio
async def test_dynamic_svg_returns_only_selected_keyframes(tmp_path: Path) -> None:
    source = tmp_path / "dynamic.svg"
    source.write_text(
        """
        <svg xmlns="http://www.w3.org/2000/svg" width="320" height="240">
          <rect width="320" height="240" fill="#081018"/>
          <circle cx="40" cy="120" r="24" fill="#4fd1c5">
            <animate attributeName="cx" from="40" to="280" dur=".5s"
                     repeatCount="indefinite"/>
          </circle>
        </svg>
        """,
        encoding="utf-8",
    )
    service = _service(tmp_path)

    parts = await service.preview(
        path=source,
        output_dir=tmp_path / "output",
        max_previews=4,
        capture_duration_seconds=0.5,
    )

    bundle = next((tmp_path / "output").iterdir())
    _assert_bundle(bundle)
    metadata = json.loads(parts[0]["text"])
    assert len(metadata["views"]) == len(parts) - 1
    assert len(metadata["views"]) >= 2
    assert all("at_ms" in view for view in metadata["views"])


@pytest.mark.asyncio
async def test_dynamic_page_matches_primary_preview_and_blocks_network(
    tmp_path: Path,
) -> None:
    source = tmp_path / "finite-animation.html"
    source.write_text(
        """
        <!doctype html>
        <html><body style="margin:0;background:#111827">
        <img src="https://example.invalid/tracker.png">
        <div id="box" style="width:320px;height:240px;background:#38bdf8;
             opacity:0;transition:opacity .5s linear"></div>
        <script>
        new WebSocket("wss://example.invalid/socket");
        requestAnimationFrame(() => {
          document.getElementById("box").style.opacity = "1";
        });
        </script>
        </body></html>
        """,
        encoding="utf-8",
    )
    service = _service(tmp_path)

    parts = await service.preview(
        path=source,
        output_dir=tmp_path / "output",
        max_previews=3,
        capture_duration_seconds=0.5,
        viewport_width=640,
        viewport_height=360,
    )

    bundle = next((tmp_path / "output").iterdir())
    metadata = json.loads(parts[0]["text"])
    assert "external_resource_blocked" in metadata["warnings"]
    primary = bundle / "preview" / "00-at-000500ms.png"
    page = bundle / "pages" / "page-0001.png"
    assert metadata["views"][0] == {"image": 1, "at_ms": 500}
    difference = image_difference(primary, page)
    assert difference["changed_pixel_ratio"] <= 0.01
    assert difference["mean_absolute_channel_delta"] <= 0.005
