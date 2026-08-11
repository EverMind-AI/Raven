"""Unit tests for MiniMax speech generation."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from raven.agent.tools import media_gen as mg
from raven.agent.tools.media_gen import SpeechGenerateTool
from raven.config.schema import Config, MediaToolConfig


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mg.httpx, "AsyncClient", factory)


async def test_minimax_global_hex_request_and_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.minimax.io/v1/t2a_v2")
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body == {
            "model": "speech-2.8-hd",
            "text": "Hello",
            "stream": False,
            "output_format": "hex",
            "audio_setting": {"format": "wav", "sample_rate": 32000},
            "voice_setting": {"voice_id": "English_Graceful_Lady", "speed": 1.1},
            "language_boost": "English",
            "pronunciation_dict": {"tone": ["Raven/(rei)ven"]},
            "voice_modify": {"pitch": 2},
            "subtitle_enable": True,
        }
        return httpx.Response(
            200,
            json={
                "data": {"audio": "52494646", "status": 2},
                "extra_info": {"audio_format": "wav"},
                "base_resp": {"status_code": 0},
            },
        )

    _patch_client(monkeypatch, handler)
    tool = SpeechGenerateTool(
        MediaToolConfig(api_key="test-key", model="speech-2.8-hd"),
        workspace=tmp_path,
    )
    result = json.loads(
        await tool.execute(
            text="Hello",
            voice_setting={"voice_id": "English_Graceful_Lady", "speed": 1.1},
            language_boost="English",
            pronunciation_dict={"tone": ["Raven/(rei)ven"]},
            audio_setting={"format": "wav", "sample_rate": 32000},
            voice_modify={"pitch": 2},
            subtitle_enable=True,
        )
    )

    assert result["success"] is True
    assert result["model"] == "speech-2.8-hd"
    path = Path(result["path"])
    assert path.suffix == ".wav"
    assert path.read_bytes() == bytes.fromhex("52494646")


async def test_minimax_cn_url_response_is_downloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio_bytes = b"ID3 mini audio"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.minimaxi.com":
            assert request.url.path == "/v1/t2a_v2"
            body = json.loads(request.content)
            assert body["model"] == "speech-2.8-hd"
            assert body["output_format"] == "url"
            return httpx.Response(
                200,
                json={
                    "data": {"audio": "https://cdn.example.com/speech.mp3", "status": 2},
                    "base_resp": {"status_code": 0},
                },
            )
        if request.url.host == "cdn.example.com":
            return httpx.Response(200, content=audio_bytes)
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    tool = SpeechGenerateTool(
        MediaToolConfig(
            api_key="test-key",
            api_base="https://api.minimaxi.com/v1",
        ),
        workspace=tmp_path,
    )
    result = json.loads(
        await tool.execute(
            text="Hello from the China endpoint",
            output_format="url",
            audio_setting={"format": "mp3"},
        )
    )

    assert result["success"] is True
    path = Path(result["path"])
    assert path.suffix == ".mp3"
    assert path.read_bytes() == audio_bytes


async def test_minimax_stream_concatenates_hex_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["output_format"] == "hex"
        events = (
            'data: {"data":{"audio":"0102","status":1},"base_resp":{"status_code":0}}\n\n'
            'data: {"data":{"audio":"0304","status":2},"base_resp":{"status_code":0}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=events.encode())

    _patch_client(monkeypatch, handler)
    tool = SpeechGenerateTool(
        MediaToolConfig(api_key="test-key", model="speech-2.6-hd"),
        workspace=tmp_path,
    )
    result = json.loads(
        await tool.execute(
            text="Stream this",
            stream=True,
            output_format="hex",
            audio_setting={"format": "mp3"},
        )
    )

    assert result["success"] is True
    assert Path(result["path"]).read_bytes() == bytes.fromhex("01020304")


async def test_minimax_api_error_uses_base_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": None,
                "base_resp": {"status_code": 1004, "status_msg": "Authentication failed"},
            },
        )

    _patch_client(monkeypatch, handler)
    tool = SpeechGenerateTool(
        MediaToolConfig(api_key="test-key", model="speech-02-hd"),
        workspace=tmp_path,
    )
    result = json.loads(await tool.execute(text="Hello"))

    assert result["error"] == "Authentication failed"


@pytest.mark.parametrize(
    "speech_config",
    [
        {"model": "speech-2.8-hd"},
        {"apiBase": "https://api.minimaxi.com/v1"},
    ],
)
def test_minimax_speech_inherits_provider_key(speech_config: dict[str, str]) -> None:
    config = Config.model_validate(
        {
            "providers": {"minimax": {"apiKey": "test-key"}},
            "tools": {"media": {"speech": speech_config}},
        }
    )

    resolved = config.effective_media_config().speech

    assert resolved.api_key == "test-key"
