"""media_gen tool: where the provider key is allowed to travel.

Every URL the video path fetches -- the poll target and the content URL --
arrives inside a provider response, so "where the response said to go" is what
decided where the ``Authorization`` header went. The decision was a string
prefix test against ``api_base``, which is host-substitutable as soon as
``api_base`` is configured without a path component (a self-hosted proxy):
``https://api.mycorp.example.attacker.test/`` starts with
``https://api.mycorp.example`` and collected the key.
"""

from __future__ import annotations

import base64
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from raven.agent.tools import media_gen
from raven.agent.tools.media_gen import ImageGenerateTool, SpeechGenerateTool, VideoGenerateTool


def _tool(api_base: str) -> VideoGenerateTool:
    """``api_base`` is resolved at call time from the tool's config, so the
    config is where the test states it."""
    return VideoGenerateTool(SimpleNamespace(api_base=api_base, model=""))


@pytest.mark.parametrize(
    "base,url,same",
    [
        # A base with no path -- the case the prefix test got wrong.
        ("https://api.mycorp.example", "https://api.mycorp.example/v1/videos/x", True),
        ("https://api.mycorp.example", "https://api.mycorp.example.attacker.test/steal", False),
        ("https://api.mycorp.example", "https://api.mycorp.example-evil.test/steal", False),
        # The shipped default, where prefix and origin already agreed.
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/videos/x", True),
        ("https://openrouter.ai/api/v1", "https://cdn.example.com/signed", False),
        # A default port written out is the same origin; a scheme change is not.
        ("https://api.mycorp.example", "https://api.mycorp.example:443/v1/x", True),
        ("https://api.mycorp.example", "http://api.mycorp.example/v1/x", False),
        ("https://api.mycorp.example", "not a url at all", False),
    ],
)
def test_only_the_api_origin_counts_as_the_api(base: str, url: str, same: bool) -> None:
    assert _tool(base)._is_api_origin(url) is same


def test_credentials_are_withheld_from_every_other_origin() -> None:
    tool = _tool("https://api.mycorp.example")
    headers = {"Authorization": "Bearer sk-test"}

    assert tool._api_headers_for("https://api.mycorp.example/v1/x", headers) == headers
    assert tool._api_headers_for("https://api.mycorp.example.attacker.test/x", headers) is None


def test_a_confined_tool_reads_input_images_only_under_the_workspace(tmp_path) -> None:
    """The file tools honour restrict_to_workspace; an image the model names by
    path is the same read and gets the same rule."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    inside = workspace / "in.png"
    inside.write_bytes(b"\x89PNG\r\n")
    outside = tmp_path / "out.png"
    outside.write_bytes(b"\x89PNG\r\n")
    tool = ImageGenerateTool(
        SimpleNamespace(api_base="https://x.test", model=""), workspace=workspace, restrict_to_workspace=True
    )

    assert tool._image_part(str(inside))["image_url"]["url"].startswith("data:image/png;base64,")
    with pytest.raises(PermissionError):
        tool._image_part(str(outside))


def test_an_unconfined_tool_reads_the_paths_it_is_given(tmp_path) -> None:
    outside = tmp_path / "out.png"
    outside.write_bytes(b"\x89PNG\r\n")
    tool = ImageGenerateTool(SimpleNamespace(api_base="https://x.test", model=""), workspace=tmp_path / "ws")
    assert tool._image_part(str(outside))["image_url"]["url"].startswith("data:image/png;base64,")


# ── the image path: which endpoint a model is sent to ──

_PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
_B64 = base64.b64encode(_PNG).decode()


def _image_tool(monkeypatch, handler, *, model: str, workspace: Path, api_base: str = "https://openrouter.ai/api/v1"):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(media_gen.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=transport))
    return ImageGenerateTool(SimpleNamespace(api_base=api_base, model=model, api_key="k"), workspace=workspace)


async def test_a_dedicated_image_model_goes_to_the_images_api_with_its_references(monkeypatch, tmp_path) -> None:
    """gpt-image-2 answers 404 on chat/completions whatever the modalities say;
    OpenRouter serves it only through ``/images``, where edit inputs travel as
    ``input_references``."""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    ref = tmp_path / "logo.png"
    ref.write_bytes(_PNG)
    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=tmp_path / "ws")
    out = json.loads(await tool.execute("a poster", images=[str(ref)], aspect_ratio="3:4", quality="high"))
    assert out["success"] and Path(out["paths"][0]).read_bytes() == _PNG
    path, body = seen[0]
    assert path == "/api/v1/images"
    assert (body["model"], body["aspect_ratio"], body["quality"]) == ("openai/gpt-image-2", "3:4", "high")
    reference = body["input_references"][0]
    assert reference["type"] == "image_url" and reference["image_url"]["url"].startswith("data:image/png;base64,")


async def test_a_confined_tool_accepts_an_output_dir_inside_the_bound_session(monkeypatch, tmp_path) -> None:
    """The session directory a turn binds may live outside agent home; the
    fence has to admit it, or a confined task cannot keep its images in its
    own folder -- the case output_dir exists for."""
    from raven.agent import workdir

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    home, session = tmp_path / "home", tmp_path / "session"
    home.mkdir()
    session.mkdir()
    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=home)
    tool._restrict_to_workspace = True
    with workdir.bind(session):
        out = json.loads(await tool.execute("a mark", output_dir="assets/generated"))
        assert Path(out["paths"][0]).parent == (session / "assets/generated").resolve()
        with pytest.raises(PermissionError):
            tool._output_path("png", str(tmp_path / "elsewhere"))


async def test_output_dir_places_the_image_where_the_caller_asks(monkeypatch, tmp_path) -> None:
    """A task confined to one folder needs its images inside that folder, not in
    the workspace-level generated/ scratch; a confined tool still refuses a
    directory outside the workspace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": _B64, "media_type": "image/png"}]})

    ws = tmp_path / "ws"
    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=ws)
    out = json.loads(await tool.execute("a mark", output_dir="task/assets/generated"))
    assert Path(out["paths"][0]).parent == (ws / "task/assets/generated").resolve()
    tool._restrict_to_workspace = True
    with pytest.raises(PermissionError):
        tool._output_path("png", str(tmp_path / "elsewhere"))


async def test_an_openai_compatible_base_takes_a_size_and_edits_by_multipart(monkeypatch, tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(
        monkeypatch, handler, model="gpt-image-2", workspace=tmp_path / "ws", api_base="https://gw.test/v1"
    )
    assert json.loads(await tool.execute("a poster", aspect_ratio="3:4"))["success"]
    generation = seen[0]
    assert generation.url.path == "/v1/images/generations"
    assert json.loads(generation.content)["size"] == "1152x1536"

    ref = tmp_path / "logo.png"
    ref.write_bytes(_PNG)
    assert json.loads(await tool.execute("blend the logo in", images=[str(ref)]))["success"]
    edit = seen[1]
    assert edit.url.path == "/v1/images/edits"
    assert edit.headers["content-type"].startswith("multipart/form-data")
    assert b'name="image[]"' in edit.content and _PNG in edit.content and b"gpt-image-2" in edit.content


async def test_a_reference_pointing_inward_is_refused_before_anything_is_fetched(monkeypatch, tmp_path) -> None:
    """``images`` is model-controlled and may name any URL. The multipart edit
    path is the one that fetches such a URL from this process, so it goes
    through the guarded fetch: a link-local or private target is refused, and
    nothing is read from it or uploaded to the provider."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(
        monkeypatch, handler, model="gpt-image-2", workspace=tmp_path / "ws", api_base="https://gw.test/v1"
    )
    out = json.loads(await tool.execute("blend it in", images=["http://169.254.169.254/latest/meta-data/"]))
    assert "refused" in out["error"]
    assert seen == []


async def test_a_chat_refusal_for_an_image_only_model_falls_back_to_the_images_api(monkeypatch, tmp_path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            message = (
                "vendor/pictures-only is an image generation model and cannot be used with the "
                "chat/completions endpoint. Use the /api/v1/images endpoint instead."
            )
            return httpx.Response(404, json={"error": {"message": message, "code": 404}})
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model="vendor/pictures-only", workspace=tmp_path / "ws")
    assert json.loads(await tool.execute("a poster"))["success"]
    assert seen == ["/api/v1/chat/completions", "/api/v1/images"]


async def test_a_chat_routed_image_model_still_takes_chat_completions(monkeypatch, tmp_path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert json.loads(request.content)["modalities"] == ["image", "text"]
        message = {"images": [{"image_url": {"url": "data:image/png;base64," + _B64}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    tool = _image_tool(monkeypatch, handler, model="google/gemini-2.5-flash-image", workspace=tmp_path / "ws")
    assert json.loads(await tool.execute("a poster"))["success"]
    assert seen == ["/api/v1/chat/completions"]


# ── the speech path: what the stream carries, and what the file becomes ──


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def _audio_delta(*, data: bytes | None = None, transcript: str | None = None) -> dict:
    audio: dict[str, str] = {}
    if data is not None:
        audio["data"] = base64.b64encode(data).decode()
    if transcript is not None:
        audio["transcript"] = transcript
    return {"choices": [{"delta": {"audio": audio}}]}


def _speech_tool(monkeypatch: pytest.MonkeyPatch, body: str, status: int = 200) -> SpeechGenerateTool:
    """A speech tool whose HTTP client answers with ``body`` and nothing else."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, content=body.encode()))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        media_gen.httpx,
        "AsyncClient",
        lambda *_a, **_kw: real_client(transport=transport),
    )
    return SpeechGenerateTool(SimpleNamespace(api_base="https://api.test", model="", api_key="k"))


async def test_the_pcm_chunks_concatenate_and_the_transcript_joins(monkeypatch) -> None:
    tool = _speech_tool(
        monkeypatch,
        _sse(
            _audio_delta(data=b"\x01\x02", transcript="hello "),
            _audio_delta(data=b"\x03\x04"),
            {"choices": [{"delta": {}}]},
            _audio_delta(transcript="there"),
        ),
    )

    pcm, transcript = await tool._stream_audio_pcm({"model": "m"})

    assert pcm == b"\x01\x02\x03\x04"
    assert transcript == "hello there"


async def test_a_line_that_is_not_an_event_is_skipped_rather_than_fatal(monkeypatch) -> None:
    tool = _speech_tool(
        monkeypatch,
        ": keep-alive\n\n" + "data: {not json\n\n" + _sse(_audio_delta(data=b"\x05\x06")),
    )

    pcm, transcript = await tool._stream_audio_pcm({"model": "m"})

    assert pcm == b"\x05\x06"
    assert transcript == ""


async def test_an_error_status_raises_with_the_body_loaded(monkeypatch) -> None:
    tool = _speech_tool(monkeypatch, '{"error": "no such model"}', status=404)

    with pytest.raises(httpx.HTTPStatusError):
        await tool._stream_audio_pcm({"model": "m"})


def test_the_wav_declares_the_rate_the_model_emits(tmp_path: Path) -> None:
    """OpenRouter hands over raw pcm16 with no header; these three numbers are
    the whole of what makes it playable."""
    tool = SpeechGenerateTool(SimpleNamespace(api_base="https://api.test", model=""))
    out = tmp_path / "speech.wav"

    tool._write_wav(out, b"\x00\x01" * 240)

    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 24_000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 240


def test_a_config_callable_is_read_per_call_not_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live form: the loop passes a reader over the config file, so a key
    added or rotated there serves the next call with no re-registration."""
    from raven.config.schema import MediaToolConfig

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    holder = {"cfg": MediaToolConfig()}
    tool = ImageGenerateTool(lambda: holder["cfg"])
    assert tool.api_key == ""

    holder["cfg"] = MediaToolConfig(api_key="sk-added-later", model="some/model")
    assert tool.api_key == "sk-added-later"
    assert tool._model(None) == "some/model"


def test_a_plain_config_section_stays_a_snapshot() -> None:
    from raven.config.schema import MediaToolConfig

    tool = ImageGenerateTool(MediaToolConfig(api_key="sk-static"))
    assert tool.api_key == "sk-static"


@pytest.mark.parametrize(
    "model",
    [
        "bytedance-seed/seedream-5-0-lite",
        "bytedance-seed/seedream-5-0-pro",
        "qwen/qwen-image-3",
        "qwen/qwen-image-3-pro",
        "x-ai/grok-imagine-image-2.0",
        "black-forest-labs/flux.2-pro",
        "recraft/recraft-v4-vector",
        "vendor/mai-image-2",
        "vendor/krea-1",
        "vendor/riverflow-2",
        "vendor/muse-image",
    ],
)
async def test_image_families_route_directly_without_gpt_quality(monkeypatch, tmp_path, model):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path)
    result = json.loads(await tool.execute("a circle", quality="high", aspect_ratio="9:16"))
    assert result["success"]
    assert len(seen) == 1 and seen[0].url.path == "/api/v1/images"
    body = json.loads(seen[0].content)
    assert "quality" not in body
    if any(name in model for name in ("seedream", "qwen-image", "grok-imagine")):
        assert body["aspect_ratio"] == "9:16"
    else:
        assert "aspect_ratio" not in body


@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize(
    "mime,extension,data",
    [
        ("image/jpeg", ".jpg", b"\xff\xd8\xffpixels"),
        ("image/svg+xml", ".svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
        ("image/webp", ".webp", b"RIFFpixelsWEBP"),
        ("image/png", ".png", _PNG),
    ],
)
async def test_image_output_uses_the_response_mime(monkeypatch, tmp_path, chat, mime, extension, data):
    encoded = base64.b64encode(data).decode()

    def handler(request):
        if chat:
            message = {"images": [{"image_url": {"url": f"data:{mime};base64,{encoded}"}}]}
            return httpx.Response(200, json={"choices": [{"message": message}]})
        return httpx.Response(200, json={"data": [{"b64_json": encoded, "media_type": mime}]})

    model = "google/gemini-3.1-flash-image" if chat else "openai/gpt-image-2"
    tool = _image_tool(monkeypatch, handler, model=model, workspace=tmp_path)
    result = json.loads(await tool.execute("a circle"))
    path = Path(result["paths"][0])
    assert path.suffix == extension and path.read_bytes() == data


async def test_image_selection_is_live_and_settings_win(monkeypatch, tmp_path):
    from raven.config.schema import MediaToolConfig

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    tool = _image_tool(monkeypatch, handler, model="", workspace=tmp_path)
    holder = {"config": MediaToolConfig(api_key="k")}
    tool._config_source = lambda: holder["config"]
    await tool.execute("a circle")
    assert seen[-1]["model"] == "openai/gpt-image-2.5-sunburst"
    assert seen[-1]["quality"] == "medium"
    holder["config"] = MediaToolConfig(api_key="k", model="openai/gpt-image-2", quality="low")
    await tool.execute("a circle")
    assert seen[-1]["quality"] == "low"
    await tool.execute("a circle", quality="high")
    assert seen[-1]["quality"] == "low"
    await tool.execute("a circle", model="qwen/qwen-image-3")
    assert seen[-1]["model"] == "openai/gpt-image-2"
    holder["config"] = MediaToolConfig(api_key="k", model="qwen/qwen-image-3")
    await tool.execute("a circle", model="openai/gpt-image-2", quality="high")
    assert seen[-1]["model"] == "qwen/qwen-image-3" and "quality" not in seen[-1]
    holder["config"].quality = ""
    await tool.execute("a circle")
    assert "quality" not in seen[-1]
    holder["config"] = MediaToolConfig(api_key="k", quality="")
    await tool.execute("a circle")
    assert seen[-1]["model"] == "openai/gpt-image-2.5-sunburst" and "quality" not in seen[-1]


async def test_borrowed_image_selection_updates_without_recreating_tool(monkeypatch, tmp_path):
    from raven.config.schema import MediaToolConfig

    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"data": [{"b64_json": _B64}]})

    source = tmp_path / "host.json"

    def configure(base, key, model, quality):
        source.write_text(
            json.dumps(
                {
                    "tools": {
                        "media": {
                            "image": {
                                "apiBase": base,
                                "apiKey": key,
                                "model": model,
                                "quality": quality,
                            }
                        }
                    }
                }
            )
        )

    configure("https://custom.example/v1", "custom-key", "openai/gpt-image-2", "low")
    tool = _image_tool(monkeypatch, handler, model="", workspace=tmp_path)
    tool._config_static = MediaToolConfig(api_key="worker-key", selection_config=str(source))
    result = json.loads(await tool.execute("a circle", model="qwen/qwen-image-3", quality="high"))
    assert result["model"] == "openai/gpt-image-2" and result["quality"] == "low"
    assert seen[-1][:2] == ("https://custom.example/v1/images/generations", "Bearer custom-key")
    assert seen[-1][2]["quality"] == "low"
    configure("https://openrouter.ai/api/v1", "router-key", "openai/gpt-image-2", "low")
    await tool.execute("a circle", quality="high")
    assert seen[-1][:2] == ("https://openrouter.ai/api/v1/images", "Bearer router-key")
    assert seen[-1][2]["quality"] == "low"
    configure("https://openrouter.ai/api/v1", "rotated-key", "qwen/qwen-image-3", "")
    result = json.loads(await tool.execute("a circle", model="openai/gpt-image-2", quality="high"))
    assert result["model"] == "qwen/qwen-image-3" and result["quality"] == ""
    assert "quality" not in seen[-1][2]
    source.write_text("{")
    await tool.execute("a circle")
    assert seen[-1][2]["model"] == "qwen/qwen-image-3"


async def test_image_provider_refusal_preserves_reason(monkeypatch, tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None},
                        "finish_reason": "content_filter",
                        "native_finish_reason": "IMAGE_RECITATION",
                    }
                ]
            },
        )

    tool = _image_tool(monkeypatch, handler, model="google/gemini-3.1-flash-image", workspace=tmp_path)
    result = json.loads(await tool.execute("a circle"))
    assert result["error"] == "image generation refused"
    assert result["native_finish_reason"] == "IMAGE_RECITATION"
    assert result["retryable"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("route", ["chat", "openrouter", "generation", "edit", "fallback"])
@pytest.mark.parametrize("cost", [None, 0, 0.12])
async def test_image_usage_reaches_runtime_tracker_and_telemetry(monkeypatch, tmp_path, route, cost):
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import EngineWiring, ToolWiring
    from raven.config.schema import MediaGenConfig, MediaToolConfig
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.token_wise.registry import StrategyRegistry
    from raven.token_wise.usage_tracker import UsageTracker

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    model = "google/test-image" if route in ("chat", "fallback") else "openai/gpt-image-2"
    base = "https://openrouter.ai/api/v1" if route in ("openrouter", "fallback") else "https://gateway.test/v1"
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if route == "fallback" and request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                404, json={"error": {"message": "This image model is not available on chat/completions; use /images"}}
            )
        chat = request.url.path.endswith("/chat/completions")
        data = (
            {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + _B64}}]}}]}
            if chat
            else {"data": [{"b64_json": _B64}]}
        )
        if cost is not None:
            data["usage"] = {
                ("prompt_tokens" if chat or route in ("openrouter", "fallback") else "input_tokens"): 100,
                ("completion_tokens" if chat or route in ("openrouter", "fallback") else "output_tokens"): 20,
                ("prompt_tokens_details" if chat or route in ("openrouter", "fallback") else "input_tokens_details"): {
                    "cached_tokens": 60,
                    "cache_write_tokens": 10,
                },
                "cost": cost,
            }
        return httpx.Response(200, json=data)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    tracker = UsageTracker()
    agent = AgentLoop(
        provider=LiteLLMProvider(default_model="test"),
        workspace=tmp_path / "workspace",
        engine=EngineWiring(strategies=StrategyRegistry([tracker])),
        tools=ToolWiring(
            media_config=MediaGenConfig(image=MediaToolConfig(api_key="test", api_base=base, model=model))
        ),
    )
    tool = agent.tools.get("image_generate")
    refs = None
    if route == "edit":
        reference = tmp_path / "reference.png"
        reference.write_bytes(_PNG)
        refs = [str(reference)]
    result = json.loads(await tool.execute("a circle", images=refs))
    assert result.get("success"), result
    rows = [
        json.loads(line) for p in (tmp_path / "telemetry").glob("usage-*.jsonl") for line in p.read_text().splitlines()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == model and row["cost_usd"] == cost
    assert row["input_tokens"] == (30 if cost is not None else None)
    assert row["output_tokens"] == (20 if cost is not None else None)
    assert row["cache_read_tokens"] == (60 if cost is not None else None)
    assert row["cache_write_tokens"] == (10 if cost is not None else None)
    assert tracker.snapshot().cost_usd == cost


async def test_image_usage_is_recorded_before_file_write_failure(monkeypatch, tmp_path):
    from raven.token_wise.usage_tracker import UsageTracker

    def handler(request):
        return httpx.Response(200, json={"data": [{"b64_json": _B64}], "usage": {"cost": 0.2}})

    tracker = UsageTracker(telemetry_dir=tmp_path / "telemetry")
    tool = _image_tool(monkeypatch, handler, model="openai/gpt-image-2", workspace=tmp_path)

    async def record(usage):
        await tracker.after_llm_call({}, usage)

    tool._usage_recorder = record

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(OSError, match="disk full"):
        await tool.execute("a circle")
    assert tracker.snapshot().cost_usd == 0.2
    assert tracker.snapshot().calls == 1


@pytest.mark.parametrize("prompt,completion", [(0, 4175), (16, 272)])
def test_images_endpoint_accepts_openrouter_chat_style_usage(prompt, completion):
    from raven.providers.usage import image_usage

    usage = image_usage(
        {
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "cost": 0.04,
            }
        },
        "images",
    )
    assert usage["input_tokens"] == prompt
    assert usage["output_tokens"] == completion
    assert usage["cost_usd"] == 0.04
    assert usage["cache_read_tokens"] is None
    assert usage["cache_write_tokens"] is None
