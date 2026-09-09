"""Unit tests for image generation and reference-image editing."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from raven.agent.tools import media_gen as media_gen_module
from raven.agent.tools.base import ToolResult
from raven.agent.tools.media_gen import ImageGenerateTool
from raven.config.schema import MediaToolConfig

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z5QAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


def _patch_client(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*_args, **kwargs):
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout", 30.0),
        )

    monkeypatch.setattr(media_gen_module.httpx, "AsyncClient", factory)


def _tool(
    tmp_path: Path,
    *,
    api_style: str = "openrouter_images",
    allow_model_override: bool = True,
    api_base: str = "https://openrouter.ai/api/v1",
    bypass_proxy: bool = False,
) -> ImageGenerateTool:
    return ImageGenerateTool(
        MediaToolConfig(
            api_key="sk-test",
            api_base=api_base,
            model="openai/gpt-image-2",
            api_style=api_style,
            allow_model_override=allow_model_override,
            bypass_proxy=bypass_proxy,
        ),
        workspace=tmp_path,
    )


def _metadata(result: ToolResult) -> dict:
    return json.loads(result.model_text)


def test_image_api_style_accepts_camel_case_and_round_trips() -> None:
    config = MediaToolConfig.model_validate({"apiStyle": "images", "bypassProxy": True})

    assert config.api_style == "images"
    assert config.bypass_proxy is True
    assert config.model_dump(by_alias=True)["apiStyle"] == "images"
    assert config.model_dump(by_alias=True)["bypassProxy"] is True


def test_generation_options_match_each_api_style(tmp_path: Path) -> None:
    openrouter_schema = _tool(tmp_path).to_schema()["function"]["parameters"]["properties"]
    images_schema = _tool(tmp_path, api_style="images").to_schema()["function"]["parameters"]["properties"]
    chat_schema = _tool(tmp_path, api_style="chat_modalities").to_schema()["function"]["parameters"]["properties"]

    assert {"quality", "aspect_ratio"} <= openrouter_schema.keys()
    assert "size" not in openrouter_schema
    assert "output_format" not in openrouter_schema
    assert {"size", "quality", "output_format"} <= images_schema.keys()
    assert "aspect_ratio" not in images_schema
    assert {"size", "quality", "output_format", "aspect_ratio"}.isdisjoint(chat_schema)


def test_locked_model_is_hidden_from_the_agent_schema(tmp_path: Path) -> None:
    properties = _tool(tmp_path, allow_model_override=False).to_schema()["function"]["parameters"]["properties"]

    assert "model" not in properties


async def test_locked_model_rejects_direct_override(tmp_path: Path) -> None:
    result = await _tool(tmp_path, allow_model_override=False).execute(
        prompt="A test image",
        model="google/gemini-2.5-flash-image",
    )

    assert isinstance(result, str)
    assert "model override is disabled" in json.loads(result)["error"]


async def test_openrouter_images_generation_posts_dedicated_payload(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": PNG_B64, "media_type": "image/png"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 196, "total_tokens": 208, "cost": 0.00594},
            },
        )

    _patch_client(monkeypatch, handler)
    result = await _tool(tmp_path).execute(
        prompt="A black circle on white",
        quality="low",
        aspect_ratio="1:1",
    )

    assert isinstance(result, ToolResult)
    metadata = _metadata(result)
    assert captured == {
        "url": "https://openrouter.ai/api/v1/images",
        "body": {
            "model": "openai/gpt-image-2",
            "prompt": "A black circle on white",
            "n": 1,
            "quality": "low",
            "aspect_ratio": "1:1",
        },
    }
    assert metadata["success"] is True
    assert metadata["operation"] == "generation"
    assert metadata["usage"]["cost"] == 0.00594
    output = Path(metadata["paths"][0])
    assert output.read_bytes() == PNG_BYTES
    assert [block["type"] for block in result.blocks or []] == ["text", "image_url"]


async def test_openrouter_images_edit_sends_local_reference(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(PNG_BYTES)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": PNG_B64, "media_type": "image/png"}]},
        )

    _patch_client(monkeypatch, handler)
    result = await _tool(tmp_path).execute(
        prompt="Change the circle from black to red",
        images=[str(source)],
        quality="low",
    )

    assert isinstance(result, ToolResult)
    metadata = _metadata(result)
    assert metadata["operation"] == "edit"
    assert captured["model"] == "openai/gpt-image-2"
    assert captured["prompt"] == "Change the circle from black to red"
    assert captured["n"] == 1
    assert captured["quality"] == "low"
    reference = captured["input_references"][0]
    assert reference["type"] == "image_url"
    assert reference["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    assert Path(metadata["paths"][0]).read_bytes() == PNG_BYTES


async def test_images_api_generation_posts_openai_payload(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})

    _patch_client(monkeypatch, handler)
    result = await _tool(
        tmp_path,
        api_style="images",
        api_base="https://images.test/v1",
    ).execute(
        prompt="A black circle on white",
        size="1024x1024",
        quality="low",
        output_format="png",
    )

    assert isinstance(result, ToolResult)
    assert captured == {
        "url": "https://images.test/v1/images/generations",
        "body": {
            "model": "openai/gpt-image-2",
            "prompt": "A black circle on white",
            "n": 1,
            "size": "1024x1024",
            "quality": "low",
            "output_format": "png",
        },
    }
    assert _metadata(result)["operation"] == "generation"


async def test_images_api_edit_posts_multipart_references(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(PNG_BYTES)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers["content-type"]
        captured["body"] = request.content
        return httpx.Response(200, json={"data": [{"b64_json": PNG_B64}]})

    _patch_client(monkeypatch, handler)
    result = await _tool(
        tmp_path,
        api_style="images",
        api_base="https://images.test/v1",
    ).execute(
        prompt="Change both circles to red",
        images=[str(source), f"data:image/png;base64,{PNG_B64}"],
        quality="low",
    )

    assert isinstance(result, ToolResult)
    assert captured["url"] == "https://images.test/v1/images/edits"
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    assert captured["body"].count(b'name="image"') == 2
    assert b'name="model"' in captured["body"]
    assert b"openai/gpt-image-2" in captured["body"]
    assert b'name="prompt"' in captured["body"]
    assert b"Change both circles to red" in captured["body"]
    assert b'filename="source.png"' in captured["body"]
    assert b'filename="image-2.png"' in captured["body"]
    assert _metadata(result)["operation"] == "edit"


async def test_images_api_rejects_remote_edit_reference(tmp_path: Path) -> None:
    result = await _tool(tmp_path, api_style="images").execute(
        prompt="Edit this image",
        images=["https://example.test/source.png"],
    )

    assert isinstance(result, str)
    assert "HTTP(S) input images are not supported" in json.loads(result)["error"]


def test_bypass_proxy_disables_environment_proxy(tmp_path: Path) -> None:
    tool = _tool(tmp_path, api_style="images", bypass_proxy=True)

    options = tool._client_options(timeout=30.0)

    assert options["trust_env"] is False
    assert "proxy" not in options


async def test_chat_modalities_generation_remains_supported(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
                            ]
                        }
                    }
                ]
            },
        )

    _patch_client(monkeypatch, handler)
    result = await _tool(tmp_path, api_style="chat_modalities").execute(prompt="A test image")

    assert isinstance(result, ToolResult)
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["body"]["modalities"] == ["image", "text"]
    assert _metadata(result)["operation"] == "generation"


async def test_mismatched_response_mime_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={"data": [{"b64_json": PNG_B64, "media_type": "image/jpeg"}]},
        ),
    )

    result = await _tool(tmp_path).execute(prompt="A test image")

    assert isinstance(result, str)
    assert not isinstance(result, ToolResult)
    assert "MIME type does not match" in json.loads(result)["error"]
