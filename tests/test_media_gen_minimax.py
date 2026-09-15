"""MiniMax image-to-video request mapping, polling, and credential boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from raven.agent.tools import media_gen as media_gen_module
from raven.agent.tools.media_gen import VideoGenerateTool
from raven.config.schema import MediaToolConfig

_VIDEO_BYTES = b"generated-video"


def _patch_minimax_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
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
async def test_minimax_v2_image_to_video_uses_regional_endpoint(
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
                "content": [
                    {"type": "text", "text": "A ship crosses the horizon"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://images.example/frame.png"},
                        "role": "first_frame",
                    },
                ],
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

    _patch_minimax_client(monkeypatch, handler)
    tool = VideoGenerateTool(
        MediaToolConfig(api_key="test-key", api_base=configured_base, model="MiniMax-H3"),
        workspace=tmp_path,
    )

    result = json.loads(
        await tool.execute(
            prompt="A ship crosses the horizon",
            first_frame_image="https://images.example/frame.png",
            params={"duration": 6, "ratio": "16:9", "ignored": True},
        )
    )

    assert result["success"] is True
    assert result["model"] == "MiniMax-H3"
    assert Path(result["path"]).read_bytes() == _VIDEO_BYTES
    assert len(requests) == 3


async def test_minimax_v1_image_to_video_retrieves_generated_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_base = "https://api.minimaxi.com"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert str(request.url) == f"{expected_base}/v1/video_generation"
            assert json.loads(request.content) == {
                "model": "I2V-01",
                "prompt": "Clouds gather above a mountain",
                "first_frame_image": "data:image/png;base64,aW1hZ2U=",
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

    _patch_minimax_client(monkeypatch, handler)
    tool = VideoGenerateTool(
        MediaToolConfig(
            api_key="test-key",
            api_base="https://api.minimaxi.com/v1/video_generation",
            model="I2V-01",
        ),
        workspace=tmp_path,
    )

    result = json.loads(
        await tool.execute(
            prompt="Clouds gather above a mountain",
            first_frame_image="data:image/png;base64,aW1hZ2U=",
            params={"duration": 6, "resolution": "720P", "ratio": "16:9"},
        )
    )

    assert result["success"] is True
    assert result["model"] == "I2V-01"
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

    _patch_minimax_client(monkeypatch, handler)
    tool = VideoGenerateTool(
        MediaToolConfig(api_key="test-key", model="MiniMax-H3"),
        workspace=tmp_path,
    )

    result = json.loads(await tool.execute(prompt="A prompt", first_frame_image="https://images.example/frame.png"))

    assert result["error"] == "video job status=failed"
    assert result["detail"]["code"] == "1026"


@pytest.mark.parametrize("params", [{"duration": 3}, {"duration": 16}, {"duration": True}, {"resolution": "720P"}])
async def test_minimax_rejects_invalid_v2_options(tmp_path, params):
    tool = VideoGenerateTool(MediaToolConfig(model="MiniMax-H3", api_key="test-key"), workspace=tmp_path)
    result = json.loads(
        await tool.execute(prompt="Animate waves", first_frame_image="https://images.example/a.png", params=params)
    )
    assert "error" in result


@pytest.mark.parametrize("image", [None, "", "/tmp/image.png"])
async def test_minimax_requires_image_source(tmp_path, image):
    tool = VideoGenerateTool(MediaToolConfig(model="I2V-01", api_key="test-key"), workspace=tmp_path)
    result = json.loads(await tool.execute(prompt="Animate waves", first_frame_image=image))
    assert "error" in result


async def test_minimax_rejects_empty_v2_prompt(tmp_path):
    tool = VideoGenerateTool(MediaToolConfig(model="MiniMax-H3", api_key="test-key"), workspace=tmp_path)
    result = json.loads(await tool.execute(prompt=" ", first_frame_image="https://images.example/a.png"))
    assert "error" in result


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"base_resp": {"status_code": 1004, "status_msg": "invalid credential"}},
    ],
)
async def test_minimax_submit_errors(tmp_path, monkeypatch, payload):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    _patch_minimax_client(monkeypatch, handler)
    tool = VideoGenerateTool(MediaToolConfig(model="I2V-01", api_key="test-key"), workspace=tmp_path)
    result = json.loads(await tool.execute(prompt="Animate waves", first_frame_image="https://images.example/a.png"))
    assert "error" in result
    assert len(requests) == 1


async def test_minimax_poll_timeout(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "task"})
        return httpx.Response(200, json={"task": {"status": "running"}})

    _patch_minimax_client(monkeypatch, handler)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(media_gen_module.asyncio, "sleep", no_sleep)
    tool = VideoGenerateTool(MediaToolConfig(model="MiniMax-H3", api_key="test-key"), workspace=tmp_path)
    tool._POLL_TIMEOUT_S = 1
    result = json.loads(await tool.execute(prompt="Animate waves", first_frame_image="https://images.example/a.png"))
    assert "timeout" in result["error"]


def test_minimax_explicit_credentials_and_environment(monkeypatch):
    from raven.config.schema import Config, live_media_tool_config

    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    cfg = Config()
    cfg.tools.media.video.model = "MiniMax-H3"
    video = cfg.effective_media_config().video
    assert video.api_key == ""
    assert VideoGenerateTool(video).api_key == "test-minimax-key"
    assert live_media_tool_config({"model": "I2V-01"}, {"apiKey": "test-unrelated-key"}).api_key == ""
