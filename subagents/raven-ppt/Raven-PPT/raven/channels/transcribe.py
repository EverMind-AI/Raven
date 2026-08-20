"""Audio transcription helper for channels — a thin wrapper over the Groq
provider (:mod:`raven.providers.transcription`).

An empty ``api_key`` is passed through so the provider can fall back to the
``GROQ_API_KEY`` env var and decide for itself; returns "" on any failure.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger


async def transcribe_audio(file_path: str | Path, api_key: str = "", *, channel: str = "") -> str:
    try:
        from raven.providers.transcription import GroqTranscriptionProvider

        provider = GroqTranscriptionProvider(api_key=api_key or None)
        return await provider.transcribe(file_path)
    except Exception as e:
        logger.warning("{}: audio transcription failed: {}", channel or "channel", e)
        return ""
