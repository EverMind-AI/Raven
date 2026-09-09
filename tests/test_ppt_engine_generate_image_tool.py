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
async def test_several_pictures_are_generated_together_and_ingested_once(monkeypatch, tmp_path: Path) -> None:
    """Nine pictures one call each cost a measured deck sixteen minutes of waiting and
    nine rewrites of the same catalogue. `prompts` asks for them together: every one is
    requested, each lands in sources with its own figure id, and the reply carries one
    result per picture."""
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
            prompts=[
                {"prompt": "a night market street, photographic", "filename": "market"},
                {"prompt": "a riverside promenade at dusk, photographic", "filename": "river", "aspect_ratio": "3:2"},
            ],
        )
    )

    assert body["ok"] is True
    assert len(requested) == 2
    assert len(body["results"]) == 2
    assert all(item["figure_id"] for item in body["results"])
    assert len({item["figure_id"] for item in body["results"]}) == 2
    assert json.loads(requested[1].content)["aspect_ratio"] == "3:2"


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


def _subject_on_green() -> bytes:
    """A teal figure on a green screen with a white window inside it, as a cut-out prompt returns;
    one row of its edge is blended with the screen the way anti-aliasing leaves it."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 300), (0, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 60, 300, 240), fill=(30, 120, 120))
    draw.rectangle((180, 130, 220, 170), fill=(250, 250, 250))
    draw.line((100, 60, 100, 240), fill=(20, 190, 70))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def test_key_out_green_clears_the_screen_and_keeps_white_and_the_subject() -> None:
    """The screen is what reads as screen green, wherever it is; white inside the subject
    stays, and the blended edge loses its green excess instead of standing as a fringe."""
    from PIL import Image

    from raven_ppt.tools.generate_image import key_out_green

    cleared, share = key_out_green(_subject_on_green())

    image = Image.open(io.BytesIO(cleared))
    assert image.mode == "RGBA"
    assert image.getpixel((5, 5))[3] == 0 and image.getpixel((395, 295))[3] == 0
    assert image.getpixel((200, 100))[3] == 255, "the subject is opaque"
    assert image.getpixel((200, 150))[3] == 255, "white inside the subject is not screen"
    edge = image.getpixel((100, 150))
    assert edge[3] in (0, 255) and (edge[3] == 0 or edge[1] <= max(edge[0], edge[2])), "no green fringe on the rim"
    assert 0.65 < share < 0.72


@pytest.mark.asyncio
async def test_a_transparent_request_asks_for_a_green_screen_and_keys_it_out(monkeypatch, tmp_path: Path) -> None:
    """OpenRouter's gpt-image-2 refuses `background: transparent` and paints a checkerboard
    when asked in words, so the tool asks for a green screen and keys it out itself; the
    caller learns how much was keyed."""
    from PIL import Image

    requested: list[httpx.Request] = []
    encoded = base64.b64encode(_subject_on_green()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    tool = PptGenerateImageTool(tmp_path, MediaToolConfig(api_key="k"))

    result = json.loads(
        await tool.execute(project="deck", prompt="a night market stall", filename="stall.png", transparent=True)
    )

    body = json.loads(requested[0].content)
    assert "#00ff00" in body["prompt"] and "background" not in body
    assert result["transparent_share"] > 0.6
    saved = Image.open(result["path"])
    assert saved.mode == "RGBA" and saved.getpixel((2, 2))[3] == 0
