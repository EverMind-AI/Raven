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
