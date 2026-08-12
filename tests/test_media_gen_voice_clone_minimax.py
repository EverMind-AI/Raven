"""Unit tests for MiniMax voice cloning."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from raven.agent.tools import media_gen as mg
from raven.agent.tools.media_gen import VoiceCloneTool
from raven.config.schema import Config, MediaToolConfig


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mg.httpx, "AsyncClient", factory)


async def test_voice_clone_uploads_source_and_creates_voice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source audio")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer unit-key"
        if request.url.path == "/v1/files/upload":
            assert request.url.host == "api.minimax.io"
            assert b'form-data; name="purpose"' in request.content
            assert b"voice_clone" in request.content
            assert b"source audio" in request.content
            return httpx.Response(
                200,
                json={"file": {"file_id": 101, "purpose": "voice_clone"}, "base_resp": {"status_code": 0}},
            )

        assert request.url == httpx.URL("https://api.minimax.io/v1/voice_clone")
        assert json.loads(request.content) == {
            "file_id": 101,
            "voice_id": "RavenVoice001",
            "model": "speech-2.8-hd",
            "accuracy": 0.8,
            "need_noise_reduction": True,
            "need_volume_normalization": True,
            "aigc_watermark": False,
            "text": "Preview this voice.",
            "text_validation": "Source transcript.",
        }
        return httpx.Response(200, json={"voice_id": "RavenVoice001", "base_resp": {"status_code": 0}})

    _patch_client(monkeypatch, handler)
    tool = VoiceCloneTool(MediaToolConfig(api_key="unit-key"), workspace=tmp_path)

    result = json.loads(
        await tool.execute(
            audio_path=str(source),
            voice_id="RavenVoice001",
            model="speech-2.8-hd",
            accuracy=0.8,
            need_noise_reduction=True,
            need_volume_normalization=True,
            text="Preview this voice.",
            text_validation="Source transcript.",
        )
    )

    assert result == {"success": True, "voice_id": "RavenVoice001", "model": "speech-2.8-hd"}
    assert len(requests) == 2


async def test_voice_clone_uses_cn_endpoint_and_prompt_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mp3"
    prompt = tmp_path / "prompt.m4a"
    source.write_bytes(b"source audio")
    prompt.write_bytes(b"prompt audio")
    purposes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.minimaxi.com"
        if request.url.path == "/v1/files/upload":
            purpose = "prompt_audio" if b"prompt_audio" in request.content else "voice_clone"
            purposes.append(purpose)
            file_id = 202 if purpose == "prompt_audio" else 101
            return httpx.Response(
                200,
                json={"file": {"file_id": file_id, "purpose": purpose}, "base_resp": {"status_code": 0}},
            )

        assert request.url.path == "/v1/voice_clone"
        body = json.loads(request.content)
        assert body["file_id"] == 101
        assert body["clone_prompt"] == {"prompt_audio": 202, "prompt_text": "A short prompt."}
        return httpx.Response(200, json={"base_resp": {"status_code": 0}})

    _patch_client(monkeypatch, handler)
    tool = VoiceCloneTool(
        MediaToolConfig(api_key="unit-key", api_base="https://api.minimaxi.com/v1"),
        workspace=tmp_path,
    )

    result = json.loads(
        await tool.execute(
            audio_path=str(source),
            voice_id="RavenVoice002",
            model="speech-2.6-hd",
            prompt_audio_path=str(prompt),
            prompt_text="A short prompt.",
        )
    )

    assert result["success"] is True
    assert result["voice_id"] == "RavenVoice002"
    assert purposes == ["voice_clone", "prompt_audio"]


async def test_voice_clone_returns_api_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source audio")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"base_resp": {"status_code": 1004, "status_msg": "Authentication failed"}})

    _patch_client(monkeypatch, handler)
    tool = VoiceCloneTool(MediaToolConfig(api_key="unit-key"), workspace=tmp_path)

    result = json.loads(await tool.execute(audio_path=str(source), voice_id="RavenVoice003"))

    assert result["error"] == "Authentication failed"


def test_voice_clone_inherits_minimax_provider_key() -> None:
    config = Config.model_validate(
        {
            "providers": {"minimax": {"apiKey": "unit-key"}},
            "tools": {"media": {"voiceClone": {"model": "speech-2.8-hd"}}},
        }
    )

    resolved = config.effective_media_config().voice_clone

    assert resolved.api_key == "unit-key"
