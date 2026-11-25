"""Audio transcription helper for channels — a thin wrapper over the Groq
provider (:mod:`raven.providers.transcription`).

An empty ``api_key`` is passed through so the provider can fall back to the
``GROQ_API_KEY`` env var and decide for itself; a missing or rejected key comes
back as a diagnosis on the result, and every other failure as an empty result.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from raven.providers.transcription import TranscriptionResult


def transcription_marker(result: TranscriptionResult) -> str:
    """The conversation marker for one attempt: the transcript when there is
    one, else the one-line diagnosis labelled as unavailable, else "" so the
    caller keeps its media marker."""
    if result.text:
        return f"[transcription: {result.text}]"
    if result.diagnosis:
        return f"[transcription unavailable: {result.diagnosis}]"
    return ""


async def transcribe_audio(file_path: str | Path, api_key: str = "", *, channel: str = "") -> TranscriptionResult:
    try:
        from raven.providers.transcription import GroqTranscriptionProvider

        provider = GroqTranscriptionProvider(api_key=api_key or None)
        return await provider.transcribe(file_path)
    except Exception as e:
        logger.warning("{}: audio transcription failed: {}", channel or "channel", e)
        return TranscriptionResult()
