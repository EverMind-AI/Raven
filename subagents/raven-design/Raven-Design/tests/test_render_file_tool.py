"""Unit tests for render_file configuration and Agent Loop registration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from raven.agent.loop import AgentLoop
from raven.agent.tools.render import RenderFileTool
from raven.config.schema import RenderToolConfig, ToolSearchConfig
from raven.providers.base import LLMProvider, LLMResponse


class _Provider(LLMProvider):
    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "test"


class _RenderService:
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
        self.requests: list[Any] = []

    async def render(self, request):
        self.requests.append(request)
        return {"dir": "/tmp/rendered", "warnings": ["macros_disabled"]}


@pytest.mark.asyncio
async def test_render_file_returns_compact_contract_and_forwards_options() -> None:
    service = _RenderService()
    tool = RenderFileTool(service, 3.0)

    result = await tool.execute(
        path="deck.pptx",
        output_dir="artifacts",
        motion_mode="dynamic",
        capture_duration_seconds=2.5,
        capture_at_seconds=1.25,
        page_range="1-3,5",
        viewport={"width": 1280, "height": 720},
        asset_root="assets",
    )

    assert result == '{"dir":"/tmp/rendered","warnings":["macros_disabled"]}'
    request = service.requests[0]
    assert request.path == Path("deck.pptx")
    assert request.output_dir == Path("artifacts")
    assert request.capture_duration_seconds == 2.5
    assert request.capture_at_seconds == 1.25
    assert request.page_range == "1-3,5"
    assert (request.viewport_width, request.viewport_height) == (1280, 720)
    assert request.asset_root == Path("assets")


@pytest.mark.asyncio
async def test_render_file_defaults_snapshot_to_capture_window_end() -> None:
    service = _RenderService()
    tool = RenderFileTool(service, 3.0)

    await tool.execute(
        path="animation.svg",
        output_dir="artifacts",
        capture_duration_seconds=2.5,
    )

    request = service.requests[0]
    assert request.capture_duration_seconds == 2.5
    assert request.capture_at_seconds == 2.5


def test_render_file_schema_is_closed() -> None:
    tool = RenderFileTool(_RenderService(), 3.0)

    assert tool.parameters["additionalProperties"] is False
    assert tool.parameters["properties"]["viewport"]["additionalProperties"] is False
    assert tool.validate_params({"path": "page.html", "output_dir": "out", "unexpected": True}) == [
        "unexpected unexpected"
    ]


def test_render_config_locks_default_and_hard_limits() -> None:
    config = RenderToolConfig()

    assert config.backend == "auto"
    assert config.worker_image == ""
    assert config.allow_network is False
    assert config.office_backend_order == ["onlyoffice", "libreoffice"]
    assert config.max_pages == 200
    assert config.raster_dpi == 144
    assert config.preview_max_edge == 2048
    assert config.default_preview_count == 6
    assert config.max_preview_count == 12
    assert config.default_capture_duration_seconds == 3
    assert config.max_capture_duration_seconds == 10
    with pytest.raises(ValidationError):
        RenderToolConfig(max_preview_count=13)
    with pytest.raises(ValidationError):
        RenderToolConfig(max_capture_duration_seconds=11)


def test_boxlite_tool_timeout_includes_worker_startup_budget(tmp_path: Path) -> None:
    loop = _loop(
        tmp_path,
        render=RenderToolConfig(
            timeout_seconds=180,
            worker_create_timeout_seconds=300,
        ),
    )

    assert loop.tools.get("render_file").timeout_seconds == 510
    assert loop.tools.get("preview_file").timeout_seconds == 510


def _loop(
    workspace: Path,
    *,
    render: RenderToolConfig,
    disabled_tools: list[str] | None = None,
    tool_search: ToolSearchConfig | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider=_Provider(),
        workspace=workspace,
        model="test",
        restrict_to_workspace=True,
        render_config=render,
        disabled_tools=disabled_tools,
        tool_search_config=tool_search,
    )


def test_agent_loop_registers_two_tools_with_one_service(tmp_path: Path) -> None:
    loop = _loop(tmp_path, render=RenderToolConfig())

    render_tool = loop.tools.get("render_file")
    preview_tool = loop.tools.get("preview_file")
    assert render_tool is not None
    assert preview_tool is not None
    assert render_tool.service is loop.render_service
    assert preview_tool.service is loop.render_service


def test_agent_loop_render_config_and_disabled_tools_are_honored(tmp_path: Path) -> None:
    disabled = _loop(tmp_path / "disabled", render=RenderToolConfig(enabled=False))
    filtered = _loop(
        tmp_path / "filtered",
        render=RenderToolConfig(),
        disabled_tools=["render_file", "preview_file"],
    )

    assert disabled.render_service is None
    assert not disabled.tools.has("render_file")
    assert not disabled.tools.has("preview_file")
    assert not filtered.tools.has("render_file")
    assert not filtered.tools.has("preview_file")


def test_render_tools_are_cataloged_but_not_always_visible(tmp_path: Path) -> None:
    loop = _loop(
        tmp_path,
        render=RenderToolConfig(),
        tool_search=ToolSearchConfig(enabled=True, compaction_threshold=1),
    )

    controller = loop.tool_search_controller
    controller.refresh()
    hits = controller.search("render html office preview", limit=10)
    names = {hit["name"] for hit in hits}
    assert {"render_file", "preview_file"} <= names
    assert "render_file" not in controller.visible_names()
    assert "preview_file" not in controller.visible_names()


@pytest.mark.asyncio
async def test_auto_backend_never_falls_back_to_host_rendering(tmp_path: Path) -> None:
    source = tmp_path / "page.html"
    source.write_text("<html><body>isolated</body></html>", encoding="utf-8")
    loop = _loop(tmp_path, render=RenderToolConfig(backend="auto", worker_image=""))

    result = await loop.tools.execute(
        "render_file",
        {"path": str(source), "output_dir": str(tmp_path / "out")},
    )

    payload = json.loads(str(result).removeprefix("Error: ").split("\n\n", 1)[0])
    assert payload["code"] == "renderer_unavailable"
