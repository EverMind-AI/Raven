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


def _mock_images(monkeypatch, requested: list[httpx.Request]) -> None:
    """Answer every /images post with one PNG, recording the request."""
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


def test_a_host_edit_after_assembly_reaches_the_generator(monkeypatch, tmp_path: Path) -> None:
    """SessionTool keeps this tool for the life of the process, so a section
    read once at assembly would serve a rotated key and a changed model until
    the product restarts. A section carrying selectionConfig is re-resolved
    per call against the host file."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    host = tmp_path / "config.json"
    host.write_text(json.dumps({"tools": {"media": {"image": {"apiKey": "sk-boot", "model": "vendor/boot"}}}}))
    tool = PptGenerateImageTool(
        tmp_path,
        MediaToolConfig.model_validate({"apiKey": "sk-boot", "model": "vendor/boot", "selectionConfig": str(host)}),
    )
    assert (tool.api_key, tool.model) == ("sk-boot", "vendor/boot")

    host.write_text(json.dumps({"tools": {"media": {"image": {"apiKey": "sk-rotated", "model": "vendor/new"}}}}))
    assert (tool.api_key, tool.model) == ("sk-rotated", "vendor/new")


def test_a_section_without_a_selection_stays_the_one_assembly_handed_over(monkeypatch, tmp_path: Path) -> None:
    """The launcher writes selectionConfig only where the host file is what
    answers -- a borrowed key lives in the rendered config alone, and an empty
    key in a present host section is a revocation, so re-resolving one would
    erase it every call."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tool = PptGenerateImageTool(tmp_path, MediaToolConfig(api_key="sk-borrowed"))
    assert tool.api_key == "sk-borrowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "asked", "sent"),
    [
        ({"apiKey": "k", "quality": "low"}, None, "low"),
        ({"apiKey": "k", "quality": "low"}, "high", "low"),
        ({"apiKey": "k", "quality": ""}, "high", None),
        ({"apiKey": "k"}, None, "high"),
    ],
)
async def test_the_configured_quality_travels_on_the_request(
    monkeypatch, tmp_path: Path, section: dict, asked: str | None, sent: str | None
) -> None:
    """The host's own setting outranks the call argument, the shared media
    tool's precedence, and an explicitly empty one means the provider's
    default -- which travels as no `quality` field rather than an empty
    string. Asserted on the outgoing body, because the copied section was
    already right while the request was not."""
    requested: list[httpx.Request] = []
    _mock_images(monkeypatch, requested)
    tool = PptGenerateImageTool(tmp_path, MediaToolConfig.model_validate(section))

    call: dict = {"project": "talk", "prompt": "a plain blue square, no text", "filename": "sq"}
    if asked is not None:
        call["quality"] = asked
    assert json.loads(await tool.execute(**call))["ok"] is True

    body = json.loads(requested[0].content)
    assert body.get("quality") == sent


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
    """OpenRouter's gpt-image models refuse `background: transparent` and paint a
    checkerboard when asked in words, so the tool asks for a green screen and keys it out
    itself; the caller learns how much was keyed. This case drives the shipped default,
    so it follows whichever id `_OPENROUTER_MODEL` names."""
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
