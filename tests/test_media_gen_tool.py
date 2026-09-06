from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from raven.agent.tools import media_gen
from raven.agent.tools.media_gen import ImageGenerateTool
from raven.config.schema import Config, ImageToolConfig


def _patch_client(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(media_gen.httpx, "AsyncClient", factory)


async def test_minimax_image_generation_uses_global_endpoint_and_base64(tmp_path: Path, monkeypatch) -> None:
    image = b"generated-image"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.minimax.io/v1/image_generation"
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload == {
            "model": "image-01",
            "prompt": "a lighthouse",
            "response_format": "base64",
            "n": 2,
            "prompt_optimizer": True,
            "aspect_ratio": "16:9",
            "seed": 7,
        }
        return httpx.Response(
            200,
            json={
                "data": {"image_base64": [base64.b64encode(image).decode("ascii")]},
                "metadata": {"success_count": 1, "failed_count": 0},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            },
        )

    _patch_client(monkeypatch, handler)
    config = ImageToolConfig(provider="minimax", api_key="test-key", model="image-01")
    tool = ImageGenerateTool(config, workspace=tmp_path)

    result = json.loads(
        await tool.execute(
            prompt="a lighthouse",
            aspect_ratio="16:9",
            response_format="base64",
            seed=7,
            n=2,
            prompt_optimizer=True,
        )
    )

    assert result["success"] is True
    assert result["model"] == "image-01"
    assert result["metadata"] == {"success_count": 1, "failed_count": 0}
    assert Path(result["paths"][0]).read_bytes() == image


async def test_minimax_image_generation_uses_cn_endpoint_and_downloads_url(tmp_path: Path, monkeypatch) -> None:
    image_url = "https://example.test/generated.png"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://api.minimaxi.com/v1/image_generation":
            return httpx.Response(
                200,
                json={
                    "data": {"image_urls": [image_url]},
                    "metadata": {"success_count": 1, "failed_count": 0},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                },
            )
        assert request.url == image_url
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=b"downloaded-image")

    _patch_client(monkeypatch, handler)
    config = ImageToolConfig(provider="minimax", region="cn", api_key="test-key")
    result = json.loads(await ImageGenerateTool(config, workspace=tmp_path).execute(prompt="a garden"))

    assert result["success"] is True
    assert result["model"] == "image-01"
    assert Path(result["paths"][0]).read_bytes() == b"downloaded-image"


async def test_minimax_image_generation_surfaces_api_error(tmp_path: Path, monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={"base_resp": {"status_code": 2013, "status_msg": "invalid parameters"}},
        ),
    )
    config = ImageToolConfig(provider="minimax", api_key="test-key")

    result = json.loads(await ImageGenerateTool(config, workspace=tmp_path).execute(prompt="test"))

    assert result == {"error": "invalid parameters", "status_code": 2013, "model": "image-01"}


def test_effective_media_config_resolves_minimax_key() -> None:
    config = Config.model_validate(
        {
            "providers": {"minimax": {"apiKey": "provider-key"}},
            "tools": {
                "media": {
                    "image": {"provider": "minimax", "region": "cn"},
                }
            },
        }
    )

    image = config.effective_media_config().image

    assert image.api_key == "provider-key"
    assert image.model == "image-01"
    assert image.provider == "minimax"
    assert image.region == "cn"
