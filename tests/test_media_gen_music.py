"""Unit tests for the MiniMax music_generate tool.

Hermetic: an httpx.MockTransport feeds canned responses for the
``/music_generation`` POST and the audio downloads, so no real network or key
is touched. Covers the url/hex output formats, cover reference audio, streamed
hex, and error response parsing (``base_resp.status_code`` / ``data.status``).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from raven.agent.tools import media_gen as mg
from raven.agent.tools.media_gen import MusicGenerateTool
from raven.config.schema import MediaToolConfig


def _patch(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mg.httpx, "AsyncClient", factory)


@pytest.fixture
def tool(tmp_path: Path) -> MusicGenerateTool:
    return MusicGenerateTool(MediaToolConfig(api_key="mk-test"), workspace=tmp_path)


async def test_url_output_downloads_audio(tool: MusicGenerateTool, monkeypatch) -> None:
    mp3_bytes = b"ID3\x00\x00fake mp3"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/music_generation"):
            assert request.url.host == "api.minimax.io"
            body = json.loads(request.content)
            assert body["model"] == "music-3.0"
            assert body["output_format"] == "url"
            return httpx.Response(
                200,
                json={
                    "base_resp": {"status_code": 0},
                    "data": {"status": 2, "audio": ["https://cdn.example.com/track.mp3"]},
                },
            )
        if "cdn.example.com" in str(request.url):
            return httpx.Response(200, content=mp3_bytes)
        return httpx.Response(404)

    _patch(monkeypatch, handler)
    result = json.loads(await tool.execute(prompt="indie folk"))

    assert result["success"] is True
    assert result["model"] == "music-3.0"
    path = Path(result["paths"][0])
    assert path.read_bytes() == mp3_bytes
    assert path.suffix == ".mp3"


async def test_hex_output_decodes_audio(tool: MusicGenerateTool, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/music_generation")
        body = json.loads(request.content)
        assert body["output_format"] == "hex"
        assert body["audio_setting"] == {"format": "mp3"}
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": "ffd8ff"}})

    _patch(monkeypatch, handler)
    result = json.loads(await tool.execute(prompt="lofi", output_format="hex", audio_setting={"format": "mp3"}))

    assert result["success"] is True
    assert Path(result["paths"][0]).read_bytes() == bytes.fromhex("ffd8ff")


async def test_default_region_endpoint_is_global(tool: MusicGenerateTool, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.minimax.io"
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": "abcd"}})

    _patch(monkeypatch, handler)
    result = json.loads(await tool.execute(prompt="ambient", output_format="hex"))
    assert result["success"] is True


async def test_cn_region_endpoint_from_api_base(tool: MusicGenerateTool, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.minimaxi.com"
        body = json.loads(request.content)
        assert body["aigc_watermark"] == 1
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": "abcd"}})

    _patch(monkeypatch, handler)
    tool._config = MediaToolConfig(api_key="mk-test", api_base="https://api.minimaxi.com/v1")
    result = json.loads(await tool.execute(prompt="ambient", output_format="hex", aigc_watermark=1))
    assert result["success"] is True


async def test_cover_model_accepts_local_reference_audio(tool: MusicGenerateTool, monkeypatch, tmp_path: Path) -> None:
    ref = tmp_path / "ref.mp3"
    ref.write_bytes(b"\x00\x01\x02")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": "abcd"}})

    _patch(monkeypatch, handler)
    result = json.loads(
        await tool.execute(model="music-cover", prompt="rock cover", audio_url=str(ref), output_format="hex")
    )

    assert seen["model"] == "music-cover"
    assert seen.get("audio_url") is None
    assert base64.b64decode(seen["audio_base64"]) == b"\x00\x01\x02"
    assert result["success"] is True


async def test_stream_hex_concatenates_chunks(tool: MusicGenerateTool, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["output_format"] == "hex"
        events = (
            'data: {"data": {"status": 1, "audio": "ff"}}\n\n'
            'data: {"data": {"status": 1, "audio": "d8"}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=events.encode())

    _patch(monkeypatch, handler)
    # output_format is forced to hex when stream is set.
    result = json.loads(await tool.execute(prompt="synthwave", stream=True, output_format="url"))

    assert result["success"] is True
    assert Path(result["paths"][0]).read_bytes() == bytes.fromhex("ffd8")


async def test_api_error_status_code(tool: MusicGenerateTool, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"base_resp": {"status_code": 1004, "status_msg": "Authentication failed"}, "data": {}}
        )

    _patch(monkeypatch, handler)
    result = json.loads(await tool.execute(prompt="x"))
    assert result["error"] == "Authentication failed"


async def test_in_progress_status_reports_no_audio(tool: MusicGenerateTool, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"status": 1}})

    _patch(monkeypatch, handler)
    result = json.loads(await tool.execute(prompt="x"))
    assert result["error"] == "music generation status=1"


async def test_http_error_is_formatted(tool: MusicGenerateTool, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    _patch(monkeypatch, handler)
    result = json.loads(await tool.execute(prompt="x"))
    assert "HTTP 500" in result["error"]


async def test_no_key_error(tool: MusicGenerateTool, monkeypatch) -> None:
    tool._config = MediaToolConfig()
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    result = json.loads(await tool.execute(prompt="x"))
    assert "no API key" in result["error"]


async def test_requires_some_input(tool: MusicGenerateTool) -> None:
    result = json.loads(await tool.execute())
    assert "provide a prompt, lyrics, or a reference audio" in result["error"]
