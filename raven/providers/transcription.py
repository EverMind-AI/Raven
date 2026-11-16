"""Voice transcription provider using Groq."""

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from loguru import logger

_NO_KEY_DIAGNOSIS = (
    "Groq API key not configured for transcription. "
    "Run: raven provider set groq --api-key <KEY>, then restart the gateway."
)

_INVALID_KEY_DIAGNOSIS = (
    "Groq rejected the API key for transcription. "
    "Run: raven provider set groq --api-key <NEW-KEY>, then restart the gateway."
)


@dataclass(frozen=True)
class TranscriptionResult:
    """One attempt's outcome: the transcribed text, or why there is no text."""

    text: str = ""
    diagnosis: str = ""


def transcription_api_key(config) -> str:
    """The key channels transcribe voice with: the configured groq provider's."""
    groq = config.providers.get("groq")
    return getattr(groq, "api_key", "") or ""


class GroqTranscriptionProvider:
    """
    Voice transcription provider using Groq's Whisper API.

    Groq offers extremely fast transcription with a generous free tier.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = "https://api.groq.com/openai/v1/audio/transcriptions"

    async def transcribe(self, file_path: str | Path) -> TranscriptionResult:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.

        Returns:
            The transcribed text, or a one-line diagnosis naming the fix when no
            API key is configured or the key is rejected. A transport failure
            returns neither.
        """
        if not self.api_key:
            logger.warning(_NO_KEY_DIAGNOSIS)
            return TranscriptionResult(diagnosis=_NO_KEY_DIAGNOSIS)

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return TranscriptionResult()

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {
                        "file": (path.name, f),
                        "model": (None, "whisper-large-v3"),
                    }
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                    }

                    response = await client.post(self.api_url, headers=headers, files=files, timeout=60.0)

                    response.raise_for_status()
                    data = response.json()
                    return TranscriptionResult(text=data.get("text") or "")

        except Exception as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 401:
                logger.warning(_INVALID_KEY_DIAGNOSIS)
                return TranscriptionResult(diagnosis=_INVALID_KEY_DIAGNOSIS)
            logger.error("Groq transcription error: {}", e)
            return TranscriptionResult()
