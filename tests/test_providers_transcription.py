"""Tests for the Groq transcription provider's missing-key behaviour."""

import asyncio

from loguru import logger

from raven.providers.transcription import GroqTranscriptionProvider, transcription_api_key


def _captured_warnings() -> tuple[list[str], int]:
    said: list[str] = []
    sink_id = logger.add(lambda message: said.append(message.record["message"]), level="WARNING")
    return said, sink_id


def test_a_missing_key_returns_a_diagnosis_naming_the_fix(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    provider = GroqTranscriptionProvider(api_key=None)

    said, sink_id = _captured_warnings()
    try:
        text = asyncio.run(provider.transcribe("/no/such/audio.ogg"))
    finally:
        logger.remove(sink_id)

    assert "raven provider set groq --api-key" in text
    assert said and "raven provider set groq --api-key" in said[0]


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
