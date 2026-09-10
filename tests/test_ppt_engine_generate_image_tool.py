"""A generated slide visual becomes a figure the same turn it is created.

The generation rides the host's ``image_generate`` transport, so the tests drive
the same mocked HTTP the host tool's own tests drive and look at what the deck
adds around it: the figure, the stable file name a second ask writes over, the
green-screen cut-out, the references.

The name is the caller's and every call generates into it. That is the case
worth pinning rather than a cache: a second ask is the author saying the
picture was wrong, and it repeats the words of the first, so answering from
disk returned the rejected picture -- while writing over the name the page
already carries puts the new one on the page with no edit to the program.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import pytest

from raven.agent.tools.media_gen import ImageGenerateTool
from raven.config.schema import MediaToolConfig
from raven_ppt.contracts import Project
from raven_ppt.tools.generate_image import PptGenerateImageTool

OPENROUTER = "https://openrouter.ai/api/v1"


def _png(colour: str = "blue", size: tuple[int, int] = (800, 450)) -> bytes:
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", size, colour).save(stream, "PNG")
    return stream.getvalue()


def _jpeg() -> bytes:
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (640, 640), "orange").save(stream, "JPEG")
    return stream.getvalue()


def _subject_on_green() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (200, 200), (0, 255, 0))
    ImageDraw.Draw(image).ellipse((60, 60, 140, 140), fill=(200, 40, 40))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def _mock(monkeypatch, handler) -> list[httpx.Request]:
    requested: list[httpx.Request] = []

    def seen(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return handler(request)

    transport = httpx.MockTransport(seen)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requested


def _images_api(png: bytes):
    encoded = base64.b64encode(png).decode("ascii")
    return lambda request: httpx.Response(200, json={"data": [{"b64_json": encoded}]})


def _tool(tmp_path: Path, config: MediaToolConfig | None = None, **kwargs) -> PptGenerateImageTool:
    config = config or MediaToolConfig(api_key="test", api_base=OPENROUTER, model="openai/gpt-image-2")
    return PptGenerateImageTool(tmp_path, config=config, **kwargs)


@pytest.mark.asyncio
async def test_generated_image_enters_sources_and_returns_a_figure_id(monkeypatch, tmp_path: Path) -> None:
    requested = _mock(monkeypatch, _images_api(_png()))
    body = json.loads(
        await _tool(tmp_path).execute(
            project="talk", prompt="a blue bridge between two abstract memory nodes, no text", filename="memory bridge"
        )
    )

    assert body["ok"] is True
    assert body["model"] == "openai/gpt-image-2"
    assert body["figure_id"]
    assert (body["width"], body["height"]) == (800, 450)
    assert Path(body["path"]).parent == Project(workspace=tmp_path, slug="talk").sources_dir
    assert requested[0].url.path == "/api/v1/images"
    assert json.loads(requested[0].content)["aspect_ratio"] == "16:9"
    # The host tool's scratch copy does not linger beside the deck's own file.
    assert not any((Project(workspace=tmp_path, slug="talk").build_dir / ".generated").glob("*"))


@pytest.mark.asyncio
async def test_several_pictures_are_generated_together_and_ingested_once(monkeypatch, tmp_path: Path) -> None:
    """Nine pictures one call each cost a measured deck sixteen minutes of waiting and
    nine rewrites of the same catalogue. `prompts` asks for them together: every one is
    requested, each lands in sources with its own figure id, and the reply carries one
    result per picture."""
    requested = _mock(monkeypatch, _images_api(_png()))
    body = json.loads(
        await _tool(tmp_path).execute(
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
    assert {json.loads(r.content)["aspect_ratio"] for r in requested} == {"16:9", "3:2"}


@pytest.mark.asyncio
async def test_asking_again_generates_again_and_overwrites_the_file(monkeypatch, tmp_path: Path) -> None:
    """The ask this tool exists for is "that picture is not what I wanted, do it
    again", and the words that ask carries are the same words as the first time.
    Answering the second ask from the file the first one wrote hands back the very
    picture the author rejected, so a call generates. The name is the same on
    purpose: the page already names this file, so the new picture arrives without
    the script being edited."""
    requested = _mock(monkeypatch, _images_api(_png()))
    tool = _tool(tmp_path)
    first = json.loads(await tool.execute(project="talk", prompt="a lantern", filename="lantern"))
    second = json.loads(await tool.execute(project="talk", prompt="a lantern", filename="lantern"))

    assert len(requested) == 2, "the second ask reached the provider"
    assert second["path"] == first["path"], "and overwrote the file the page names"
    assert "cached" not in second
    assert (second["width"], second["height"]) == (800, 450)


@pytest.mark.asyncio
async def test_the_name_is_keyed_on_the_quality_the_host_sends_not_the_one_asked(monkeypatch, tmp_path: Path) -> None:
    """Settings' quality replaces the request's inside the host tool, so the deck
    names the file after the effective value: the same deck call before and after
    an operator switches from low to high writes two files rather than one, and the
    page can be pointed at the one it wants."""
    requested = _mock(monkeypatch, _images_api(_png()))
    section = {"now": MediaToolConfig(api_key="test", api_base=OPENROUTER, model="openai/gpt-image-2", quality="low")}
    tool = PptGenerateImageTool(tmp_path, config=lambda: section["now"])

    first = json.loads(await tool.execute(project="talk", prompt="a lantern", filename="lantern"))
    assert json.loads(requested[0].content)["quality"] == "low"

    section["now"] = MediaToolConfig(api_key="test", api_base=OPENROUTER, model="openai/gpt-image-2", quality="high")
    second = json.loads(await tool.execute(project="talk", prompt="a lantern", filename="lantern"))
    assert len(requested) == 2 and json.loads(requested[1].content)["quality"] == "high"
    assert second["path"] != first["path"], "a different effective quality is a different picture, so a different file"


@pytest.mark.asyncio
async def test_an_unset_quality_is_the_hosts_to_resolve_not_this_tools(monkeypatch, tmp_path: Path) -> None:
    """A deck asking for a picture without naming a quality must reach the provider
    the way the host's own `image_generate` reaches it. This tool used to declare
    `high` as its schema default, so the default model -- which the host sends with
    no quality at all -- was sent `high` from inside a deck and provider-default
    from outside it. Naming one still overrules the resolution; not naming one no
    longer means anything."""
    requested = _mock(monkeypatch, _images_api(_png()))
    default = MediaToolConfig(api_key="test", api_base=OPENROUTER)
    await _tool(tmp_path, config=default).execute(project="talk", prompt="a lantern", filename="lantern")

    sent = json.loads(requested[0].content)
    assert sent["model"] == ImageGenerateTool.default_model
    assert "quality" not in sent, "the host sends this model no quality, so neither does the deck"

    await _tool(tmp_path, config=default).execute(project="talk", prompt="a lantern", filename="lantern", quality="low")
    assert json.loads(requested[1].content)["quality"] == "low", "an explicit ask still carries"


@pytest.mark.asyncio
async def test_a_compatible_gateway_gets_a_frame_the_gpt_image_family_accepts(monkeypatch, tmp_path: Path) -> None:
    """Behind an OpenAI-compatible base the Images API takes a size, and gpt-image draws
    three frames: the deck's 16:9 is asked for as the landscape frame, and the reply says
    what came back so the author fits it to the box instead of assuming the ratio."""
    requested = _mock(monkeypatch, _images_api(_png(size=(1536, 1024))))
    tool = _tool(tmp_path, MediaToolConfig(api_key="k", api_base="https://gw.example/v1", model="gpt-image-2"))
    body = json.loads(await tool.execute(project="deck", prompt="a skyline", filename="skyline"))

    assert body["ok"] is True
    assert requested[0].url.path == "/v1/images/generations"
    sent = json.loads(requested[0].content)
    assert sent["model"] == "gpt-image-2" and sent["size"] == "1536x1024" and "aspect_ratio" not in sent
    assert (body["width"], body["height"]) == (1536, 1024)


@pytest.mark.asyncio
async def test_a_chat_routed_model_is_served_too_and_its_jpeg_becomes_a_png(monkeypatch, tmp_path: Path) -> None:
    """Not fixed to one model: a model the host configured that answers on chat/completions
    (Nano Banana and its kind) is driven the way image_generate drives it, and whatever it
    answers with is received as the PNG the deck's figures are."""
    data_uri = "data:image/jpeg;base64," + base64.b64encode(_jpeg()).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"images": [{"image_url": {"url": data_uri}}]}, "finish_reason": "stop"}]},
        )

    requested = _mock(monkeypatch, handler)
    tool = _tool(tmp_path, MediaToolConfig(api_key="k", api_base=OPENROUTER, model="google/gemini-3.1-flash-image"))
    body = json.loads(await tool.execute(project="deck", prompt="a warm section backdrop", filename="warm"))

    assert body["ok"] is True, body
    assert body["model"] == "google/gemini-3.1-flash-image"
    assert json.loads(requested[0].content)["modalities"] == ["image", "text"]
    assert Path(body["path"]).suffix == ".png" and Path(body["path"]).read_bytes()[:4] == b"\x89PNG"
    assert (body["width"], body["height"]) == (640, 640)


@pytest.mark.asyncio
async def test_references_ride_along_and_change_the_cache_key(monkeypatch, tmp_path: Path) -> None:
    """A template's own illustration, an earlier generation, a user's photograph: named as
    figure ids, source names or paths, they reach the model as input references, and a
    generation with a different reference is a different picture."""
    requested = _mock(monkeypatch, _images_api(_png()))
    deck = Project(workspace=tmp_path, slug="deck")
    deck.sources_dir.mkdir(parents=True)
    (deck.sources_dir / "template-cover.png").write_bytes(_png("red", (100, 100)))
    tool = _tool(tmp_path)

    first = json.loads(
        await tool.execute(
            project="deck", prompt="the skyline in this manner", filename="sky", references=["template-cover.png"]
        )
    )
    assert first["ok"] is True, first
    sent = json.loads(requested[0].content)
    assert len(sent["input_references"]) == 1
    assert sent["input_references"][0]["image_url"]["url"].startswith("data:image/png;base64,")

    (deck.sources_dir / "other.png").write_bytes(_png("green", (100, 100)))
    second = json.loads(
        await tool.execute(
            project="deck", prompt="the skyline in this manner", filename="sky", references=["other.png"]
        )
    )
    assert second["path"] != first["path"] and len(requested) == 2

    missing = json.loads(
        await tool.execute(project="deck", prompt="the skyline", filename="sky", references=["nowhere.png"])
    )
    assert missing["ok"] is False and "nowhere.png" in missing["error"]


@pytest.mark.asyncio
async def test_the_spend_is_reported_where_the_host_records_it(monkeypatch, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}],
                "usage": {"input_tokens": 40, "output_tokens": 1600, "total_tokens": 1640},
            },
        )

    _mock(monkeypatch, handler)
    recorded = []

    async def recorder(snapshot) -> None:
        recorded.append(snapshot)

    tool = _tool(tmp_path, usage_recorder=recorder)
    body = json.loads(await tool.execute(project="deck", prompt="a lantern", filename="lantern"))

    assert body["ok"] is True
    assert len(recorded) == 1 and recorded[0].model == "openai/gpt-image-2"


@pytest.mark.asyncio
async def test_without_a_key_the_reply_points_at_the_host_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tool = _tool(tmp_path, MediaToolConfig(model="openai/gpt-image-2"))
    body = json.loads(await tool.execute(project="deck", prompt="a lantern", filename="lantern"))
    assert body["ok"] is False and "tools.media.image.apiKey" in body["hint"]


@pytest.mark.asyncio
async def test_a_configured_host_tool_can_be_lent_as_is(monkeypatch, tmp_path: Path) -> None:
    """The assembly hands over the host's ImageGenerateTool rather than a config: one
    transport, one key resolution, one usage recorder for both faces."""
    _mock(monkeypatch, _images_api(_png()))
    media = ImageGenerateTool(
        MediaToolConfig(api_key="k", api_base=OPENROUTER, model="openai/gpt-image-2"), workspace=tmp_path
    )
    tool = PptGenerateImageTool(tmp_path, media)
    body = json.loads(await tool.execute(project="deck", prompt="a lantern", filename="lantern"))
    assert body["ok"] is True and body["model"] == "openai/gpt-image-2"


def test_key_out_green_clears_the_screen_and_keeps_white_and_the_subject() -> None:
    from PIL import Image

    from raven_ppt.tools.generate_image import key_out_green

    image = Image.new("RGB", (60, 60), (0, 255, 0))
    for x in range(20, 40):
        for y in range(20, 40):
            image.putpixel((x, y), (255, 255, 255))
    image.putpixel((30, 30), (180, 20, 20))
    stream = io.BytesIO()
    image.save(stream, "PNG")

    keyed, share = key_out_green(stream.getvalue())
    out = Image.open(io.BytesIO(keyed))
    assert out.getpixel((2, 2))[3] == 0
    assert out.getpixel((25, 25))[:3] == (255, 255, 255) and out.getpixel((25, 25))[3] == 255
    assert out.getpixel((30, 30))[:3] == (180, 20, 20)
    assert share > 0.8


@pytest.mark.asyncio
async def test_a_transparent_request_asks_for_a_green_screen_and_keys_it_out(monkeypatch, tmp_path: Path) -> None:
    """The image endpoints refuse `background: transparent` and paint a checkerboard when
    asked in words, so the tool asks for a green screen and keys it out itself; the caller
    learns how much was keyed."""
    from PIL import Image

    requested = _mock(monkeypatch, _images_api(_subject_on_green()))
    result = json.loads(
        await _tool(tmp_path, MediaToolConfig(api_key="k")).execute(
            project="deck", prompt="a night market stall", filename="stall.png", transparent=True
        )
    )

    body = json.loads(requested[0].content)
    assert "#00ff00" in body["prompt"] and "background" not in body
    assert result["transparent_share"] > 0.6
    saved = Image.open(result["path"])
    assert saved.mode == "RGBA" and saved.getpixel((2, 2))[3] == 0
