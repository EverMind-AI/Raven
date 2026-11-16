"""Tests for the Groq transcription provider's missing-key and rejected-key behaviour."""

import asyncio

import httpx
from loguru import logger

from raven.providers.transcription import GroqTranscriptionProvider, transcription_api_key


def _captured_warnings() -> tuple[list[str], int]:
    said: list[str] = []
    sink_id = logger.add(lambda message: said.append(message.record["message"]), level="WARNING")
    return said, sink_id


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions"))


def _client_returning(response: httpx.Response):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, *args, **kwargs):
            return response

    return _Client


def test_a_missing_key_returns_a_diagnosis_naming_the_fix(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    provider = GroqTranscriptionProvider(api_key=None)

    said, sink_id = _captured_warnings()
    try:
        text = asyncio.run(provider.transcribe("/no/such/audio.ogg"))
    finally:
        logger.remove(sink_id)

    assert "raven provider set groq --api-key" in text
    assert "restart the gateway" in text
    assert said and "restart the gateway" in said[0]


def test_a_rejected_key_returns_a_diagnosis_naming_the_fix(monkeypatch, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"audio")
    provider = GroqTranscriptionProvider(api_key="bad")

    monkeypatch.setattr("raven.providers.transcription.httpx.AsyncClient", _client_returning(_response(401)))
    said, sink_id = _captured_warnings()
    try:
        text = asyncio.run(provider.transcribe(audio))
    finally:
        logger.remove(sink_id)

    assert "raven provider set groq --api-key <NEW-KEY>" in text
    assert "restart the gateway" in text
    assert said and "rejected the API key" in said[0]


def test_a_server_error_still_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"audio")
    provider = GroqTranscriptionProvider(api_key="k")

    monkeypatch.setattr("raven.providers.transcription.httpx.AsyncClient", _client_returning(_response(500)))
    said, sink_id = _captured_warnings()
    try:
        text = asyncio.run(provider.transcribe(audio))
    finally:
        logger.remove(sink_id)

    assert text == ""
    assert not any("rejected the API key" in message for message in said)


def test_a_configured_key_still_reports_a_missing_file_as_empty(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    provider = GroqTranscriptionProvider(api_key="k")

    said, sink_id = _captured_warnings()
    try:
        text = asyncio.run(provider.transcribe("/no/such/audio.ogg"))
    finally:
        logger.remove(sink_id)

    assert text == ""
    assert not any("not configured" in message for message in said)


def test_the_env_key_counts_as_configured(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    provider = GroqTranscriptionProvider(api_key=None)

    assert asyncio.run(provider.transcribe("/no/such/audio.ogg")) == ""


def test_the_configured_provider_key_is_the_one_channels_transcribe_with():
    class _Groq:
        api_key = "cfg-key"

    class _Config:
        providers = {"groq": _Groq()}

    assert transcription_api_key(_Config()) == "cfg-key"
