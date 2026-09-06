from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from raven.agent.tools import media_gen as media_gen_module
from raven.agent.tools.media_gen import VideoGenerateTool
from raven.config.schema import Config, MediaToolConfig

_VIDEO_BYTES = b"generated-video"


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(media_gen_module.httpx, "AsyncClient", factory)


@pytest.mark.parametrize(
    ("configured_base", "expected_base"),
    [
        ("", "https://api.minimax.io"),
        ("https://api.minimaxi.com/v2/video_generation", "https://api.minimaxi.com"),
    ],
)
async def test_minimax_v2_text_to_video_uses_regional_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_base: str,
    expected_base: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert str(request.url) == f"{expected_base}/v2/video_generation"
            assert json.loads(request.content) == {
                "model": "MiniMax-H3",
                "content": [{"type": "text", "text": "A ship crosses the horizon"}],
                "resolution": "2K",
                "duration": 6,
                "ratio": "16:9",
            }
            return httpx.Response(200, json={"task_id": "task-v2"})
        if request.url.host in {"api.minimax.io", "api.minimaxi.com"}:
            assert str(request.url) == f"{expected_base}/v2/query/video_generation/task-v2"
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-v2",
                        "status": "succeeded",
                        "content": {"url": "https://cdn.example/video.mp4"},
                    }
                },
            )
        assert request.headers.get("authorization") is None
        return httpx.Response(200, content=_VIDEO_BYTES)

    _patch_client(monkeypatch, handler)
    tool = VideoGenerateTool(
        MediaToolConfig(api_key="test-key", api_base=configured_base, model="MiniMax-H3"),
        workspace=tmp_path,
    )

    result = json.loads(
        await tool.execute(
            prompt="A ship crosses the horizon",
            params={"duration": 6, "ratio": "16:9", "ignored": True},
        )
    )

    assert result["success"] is True
    assert result["model"] == "MiniMax-H3"
    assert Path(result["path"]).read_bytes() == _VIDEO_BYTES
    assert len(requests) == 3


async def test_minimax_v1_text_to_video_retrieves_generated_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_base = "https://api.minimaxi.com"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert str(request.url) == f"{expected_base}/v1/video_generation"
            assert json.loads(request.content) == {
                "model": "T2V-01",
                "prompt": "Clouds gather above a mountain",
                "duration": 6,
                "resolution": "720P",
            }
            return httpx.Response(
                200,
                json={"task_id": "task-v1", "base_resp": {"status_code": 0, "status_msg": "success"}},
            )
        if request.url.path == "/v1/query/video_generation":
            assert request.url.params["task_id"] == "task-v1"
            return httpx.Response(
                200,
                json={
                    "task_id": "task-v1",
                    "status": "Success",
                    "file_id": "file-v1",
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                },
            )
        if request.url.path == "/v1/files/retrieve":
            assert request.url.params["file_id"] == "file-v1"
            return httpx.Response(
                200,
                json={
                    "file": {"download_url": "https://cdn.example/video.mp4"},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                },
            )
        assert request.headers.get("authorization") is None
        return httpx.Response(200, content=_VIDEO_BYTES)

    _patch_client(monkeypatch, handler)
    tool = VideoGenerateTool(
        MediaToolConfig(
            api_key="test-key",
            api_base="https://api.minimaxi.com/v1/video_generation",
            model="T2V-01",
        ),
        workspace=tmp_path,
    )

    result = json.loads(
        await tool.execute(
            prompt="Clouds gather above a mountain",
            params={"duration": 6, "resolution": "720P", "ratio": "16:9"},
        )
    )

    assert result["success"] is True
    assert result["model"] == "T2V-01"
    assert Path(result["path"]).read_bytes() == _VIDEO_BYTES


async def test_minimax_v2_returns_task_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "failed-task"})
        return httpx.Response(
            200,
            json={
                "task": {
                    "id": "failed-task",
                    "status": "failed",
                    "error": {"code": "1026", "message": "video description contains sensitive content"},
                }
            },
        )

    _patch_client(monkeypatch, handler)
    tool = VideoGenerateTool(
        MediaToolConfig(api_key="test-key", model="MiniMax-H3"),
        workspace=tmp_path,
    )

    result = json.loads(await tool.execute(prompt="A prompt"))

    assert result["error"] == "video job status=failed"
    assert result["detail"]["code"] == "1026"


def test_minimax_video_reuses_provider_endpoint_and_key() -> None:
    config = Config.model_validate(
        {
            "providers": {
                "minimax": {
                    "apiKey": "test-provider-key",
                    "apiBase": "https://api.minimaxi.com/v1",
                }
            },
            "tools": {"media": {"video": {"model": "MiniMax-H3"}}},
        }
    )

    video = config.effective_media_config().video

    assert video.api_key == "test-provider-key"
    assert video.api_base == "https://api.minimaxi.com/v1"
