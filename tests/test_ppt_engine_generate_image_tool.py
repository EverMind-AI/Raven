"""A generated slide visual becomes a figure the same turn it is created."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import pytest

from raven.config.schema import MediaToolConfig
from raven_ppt.contracts import Project
from raven_ppt.tools.generate_image import PptGenerateImageTool


def _png() -> bytes:
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (800, 450), "blue").save(stream, "PNG")
    return stream.getvalue()


@pytest.mark.asyncio
async def test_generated_image_enters_sources_and_returns_a_figure_id(monkeypatch, tmp_path: Path) -> None:
    requested: list[httpx.Request] = []
    encoded = base64.b64encode(_png()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    tool = PptGenerateImageTool(
        tmp_path,
        MediaToolConfig(api_key="test", api_base="https://openrouter.ai/api/v1", model="openai/gpt-image-2"),
    )

    body = json.loads(
        await tool.execute(
            project="talk",
            prompt="a blue bridge between two abstract memory nodes, no text",
            filename="memory bridge",
        )
    )

    assert body["ok"] is True
    assert body["model"] == "openai/gpt-image-2"
    assert body["figure_id"]
    assert Path(body["path"]).parent == Project(workspace=tmp_path, slug="talk").sources_dir
    assert requested[0].url.path == "/api/v1/images"
    sent = json.loads(requested[0].content)
    assert sent["aspect_ratio"] == "16:9" and sent["quality"] == "high"


@pytest.mark.asyncio
async def test_compatible_gateway_uses_images_generations(monkeypatch, tmp_path: Path) -> None:
    requested: list[httpx.Request] = []
    encoded = base64.b64encode(_png()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    tool = PptGenerateImageTool(
        tmp_path,
        MediaToolConfig(api_key="test", api_base="https://api.example.test/v1", model="gpt-image-2"),
    )

    body = json.loads(await tool.execute(project="talk", prompt="abstract memory system, no text", filename="map"))

    assert body["ok"] is True
    assert requested[0].url.path == "/v1/images/generations"
    assert json.loads(requested[0].content)["size"] == "1536x864"
