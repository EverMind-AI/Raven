"""Multimodal generation tools.

``image_generate`` supports three explicit API styles:

- ``chat_modalities`` posts to ``{base}/chat/completions`` and reads
  ``message.images[].image_url.url`` data URIs.
- ``openrouter_images`` posts generation and reference-image edits to
  ``{base}/images`` and reads ``data[].b64_json``.
- ``images`` uses the OpenAI Images API: ``{base}/images/generations`` for
  generation and multipart ``{base}/images/edits`` for reference-image edits.

``text_to_speech`` uses chat-completions output modalities:

- ``text_to_speech`` → ``modalities:["audio","text"]`` + ``audio:{voice,format}``.
  OpenRouter only returns audio when ``stream:true`` AND only as raw ``pcm16``
  in that mode, so we stream, concatenate the base64 ``delta.audio.data`` chunks,
  and wrap the PCM (24 kHz / mono / 16-bit) into a WAV with the stdlib ``wave``
  module — zero binary dependency. mp3/opus/flac are transcoded from that WAV
  only when ``ffmpeg`` is on PATH; otherwise we keep the WAV and say so.
  Default model ``openai/gpt-audio-mini``.

Video uses a separate async endpoint (NOT chat-completions):

- ``video_generate``  → ``POST {base}/videos`` with ``{model, prompt}`` → ``202``
  ``{id, polling_url, status}`` → poll ``polling_url`` until ``completed`` (URLs
  to download) or ``failed`` (with ``error``). Default model
  ``kwaivgi/kling-v3.0-std`` (Kling v3 Standard). Requires postpaid billing /
  credits enabled on the OpenRouter account.

Generated files are written under ``<workspace>/<output_subdir>`` and the path
is returned so the agent can forward it with the ``message`` tool's ``media``
field. A denied request (HTTP 403) hints at setting ``tools.media.proxy``.

See demos/skill_retrieval/skills/image-gen/SKILL.md for the image recipe.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import shutil
import uuid
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from raven.agent.tools.base import Tool, ToolResult
from raven.utils.helpers import detect_image_mime, image_block, text_block

if TYPE_CHECKING:
    from raven.config.schema import MediaToolConfig

_DEFAULT_BASE = "https://openrouter.ai/api/v1"

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

_OPENROUTER_ASPECT_RATIOS = [
    "1:1",
    "3:2",
    "2:3",
    "4:3",
    "3:4",
    "16:9",
    "9:16",
    "21:9",
    "auto",
]


class _OpenRouterMediaTool(Tool):
    """Shared base: config/env resolution, chat-completions call, file output."""

    default_model = ""

    def __init__(
        self,
        config: "MediaToolConfig | None" = None,
        *,
        workspace: Path | None = None,
        proxy: str | None = None,
        output_subdir: str = "generated",
    ):
        self._config = config
        self._workspace = Path(workspace) if workspace else Path.cwd()
        self._proxy = proxy
        self._output_subdir = output_subdir

    # ── config resolution (at call time, so env/config edits are picked up) ──

    @staticmethod
    def _resolve_key(config: "MediaToolConfig | None") -> str:
        """The credential chain in one place: this tool's section, then the
        shared environment variable.

        A static method so :meth:`has_key` can ask it without an instance --
        this base is abstract, and the callers deciding whether a capability
        works hold a config and no tool.
        """
        cfg_key = getattr(config, "api_key", "") if config else ""
        return cfg_key or os.environ.get("OPENROUTER_API_KEY", "")

    @property
    def api_key(self) -> str:
        return self._resolve_key(self._config)

    @classmethod
    def is_configured(cls, config: "MediaToolConfig | None") -> bool:
        """Whether this deployment asked for the tool at all.

        A model *or* a key, matching what ``AgentLoop`` registers on: the key
        alone would let an OpenRouter credential set for chat quietly switch on
        three tools that bill per call, and the model alone would miss the
        deployment that names no model and relies on ``default_model``.

        A classmethod because the caller deciding whether to offer the tool has
        a config and no instance, and because the answer has to be askable
        without building one.
        """
        if config is None:
            return False
        return bool(config.api_key or config.model)

    @classmethod
    def has_key(cls, config: "MediaToolConfig | None") -> bool:
        """Whether a credential resolves for this tool.

        A different question from :meth:`is_configured`, which answers whether
        the deployment asked for the tool at all: a section naming only a model
        is registered and offered to the model, and then every call returns
        :meth:`_no_key_error`. Asked of the tool because only the tool consults
        both the section and ``OPENROUTER_API_KEY``, so a caller reading the
        config alone answers wrong for every deployment that exports it.
        """
        return bool(cls._resolve_key(config))

    @property
    def api_base(self) -> str:
        cfg_base = getattr(self._config, "api_base", "") if self._config else ""
        return (cfg_base or _DEFAULT_BASE).rstrip("/")

    def _model(self, override: str | None) -> str:
        cfg_model = getattr(self._config, "model", "") if self._config else ""
        return override or cfg_model or self.default_model

    @property
    def bypass_proxy(self) -> bool:
        return bool(getattr(self._config, "bypass_proxy", False)) if self._config else False

    def _client_options(self, *, timeout: float) -> dict[str, Any]:
        options: dict[str, Any] = {"timeout": timeout}
        if self.bypass_proxy:
            options["trust_env"] = False
        else:
            options["proxy"] = self._proxy
        return options

    def _output_path(self, ext: str) -> Path:
        out_dir = self._workspace / self._output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{self.name}-{uuid.uuid4().hex[:12]}.{ext}"

    def _no_key_error(self) -> str:
        return json.dumps(
            {
                "error": (
                    f"{self.name}: no API key configured. Set it in "
                    f"~/.raven/config.json under tools.media.*.apiKey, "
                    f"providers.openrouter.apiKey, or export OPENROUTER_API_KEY, "
                    f"then restart the gateway."
                )
            },
            ensure_ascii=False,
        )

    async def _chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to chat/completions and return the assistant message dict.

        Raises ``httpx.HTTPStatusError`` on non-2xx so callers can format it.
        """
        async with httpx.AsyncClient(**self._client_options(timeout=180.0)) as client:
            r = await client.post(
                f"{self.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        return (data.get("choices") or [{}])[0].get("message") or {}

    def _format_http_error(self, e: httpx.HTTPStatusError) -> str:
        body = e.response.text[:400]
        hint = ""
        if e.response.status_code == 403:
            hint = (
                " | Request denied (HTTP 403). You can route media calls through a "
                "proxy via tools.media.proxy (or HTTPS_PROXY) and retry."
            )
        logger.error("{} HTTP {}: {}", self.name, e.response.status_code, body)
        return json.dumps({"error": f"HTTP {e.response.status_code}: {body}{hint}"}, ensure_ascii=False)


class ImageGenerateTool(_OpenRouterMediaTool):
    """Generate or edit images through the configured OpenRouter image API."""

    name = "image_generate"
    default_model = "google/gemini-2.5-flash-image"  # Nano Banana
    timeout_seconds = 360.0
    description = (
        "Generate an image from a text prompt, or edit supplied reference images. "
        "Saves the result under the workspace and returns both file paths and images "
        "for direct visual inspection."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to create"},
            "model": {
                "type": "string",
                "description": (
                    "Optional OpenRouter image model override, e.g. "
                    "'google/gemini-2.5-flash-image' (Nano Banana) or "
                    "'google/gemini-3.1-flash-image-preview' (Nano Banana 2)"
                ),
            },
            "images": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
                "description": (
                    "Optional input images to edit/vary: local file paths, http(s) URLs, or data: URIs (max 6)"
                ),
            },
            "size": {
                "type": "string",
                "pattern": "^(auto|[1-9][0-9]*x[1-9][0-9]*)$",
                "description": "Optional output size for the OpenAI Images API: 'auto' or WIDTHxHEIGHT.",
            },
            "quality": {
                "type": "string",
                "enum": ["low", "medium", "high", "auto"],
                "description": "Optional image rendering quality.",
            },
            "output_format": {
                "type": "string",
                "enum": ["png", "webp", "jpeg"],
                "description": "Optional output format for the OpenAI Images API.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": _OPENROUTER_ASPECT_RATIOS,
                "description": "Optional output aspect ratio for the OpenRouter Images API.",
            },
        },
        "required": ["prompt"],
    }

    @property
    def api_style(self) -> str:
        return getattr(self._config, "api_style", "chat_modalities") if self._config else "chat_modalities"

    @property
    def allow_model_override(self) -> bool:
        return bool(getattr(self._config, "allow_model_override", True)) if self._config else True

    def to_schema(self) -> dict[str, Any]:
        schema = super().to_schema()
        function = {**schema["function"]}
        parameters = {**function["parameters"]}
        properties = {**parameters["properties"]}
        if not self.allow_model_override:
            properties.pop("model", None)
        if self.api_style != "images":
            properties.pop("size", None)
            properties.pop("output_format", None)
        if self.api_style != "openrouter_images":
            properties.pop("aspect_ratio", None)
        if self.api_style == "chat_modalities":
            properties.pop("quality", None)
        parameters["properties"] = properties
        function["parameters"] = parameters
        return {**schema, "function": function}

    def _image_part(self, ref: str) -> dict[str, Any]:
        """Build an OpenAI-style image_url content part from a path/URL/data URI."""
        if ref.startswith(("http://", "https://", "data:")):
            return image_block(ref)
        path = Path(ref).expanduser()
        data = path.read_bytes()
        mime = _EXT_MIME.get(path.suffix.lower(), "image/png")
        b64 = base64.b64encode(data).decode("ascii")
        return image_block(f"data:{mime};base64,{b64}")

    async def _images(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(**self._client_options(timeout=300.0)) as client:
            r = await client.post(
                f"{self.api_base}/images",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    def _edit_image(self, ref: str, index: int) -> tuple[str, bytes, str]:
        if ref.startswith(("http://", "https://")):
            raise ValueError("HTTP(S) input images are not supported by apiStyle=images")
        if ref.startswith("data:"):
            header, separator, encoded = ref.partition(",")
            if not separator or ";base64" not in header.lower() or not header.lower().startswith("data:image/"):
                raise ValueError("input image data URI must contain a base64-encoded image")
            declared_mime = header[5:].split(";", 1)[0]
            data, mime = self._decode_image(encoded, declared_mime)
            return f"image-{index}.{_MIME_EXT[mime]}", data, mime

        path = Path(ref).expanduser()
        data = path.read_bytes()
        mime = detect_image_mime(data)
        if mime not in _MIME_EXT:
            raise ValueError(f"input image has unsupported or missing magic bytes: {path}")
        return path.name, data, mime

    async def _images_api_outputs(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> list[tuple[str, str | None]]:
        outputs: list[tuple[str, str | None]] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                outputs.append((encoded, item.get("media_type")))
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            if url.startswith("data:"):
                header, separator, encoded = url.partition(",")
                if separator and ";base64" in header.lower() and header.lower().startswith("data:image/"):
                    outputs.append((encoded, header[5:].split(";", 1)[0]))
                continue
            if url.startswith(("http://", "https://")):
                response = await client.get(url)
                response.raise_for_status()
                outputs.append(
                    (
                        base64.b64encode(response.content).decode("ascii"),
                        response.headers.get("content-type"),
                    )
                )
        return outputs

    @staticmethod
    def _decode_image(encoded: str, declared_mime: str | None) -> tuple[bytes, str]:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image payload contains invalid base64") from exc
        detected_mime = detect_image_mime(data)
        if detected_mime not in _MIME_EXT:
            raise ValueError("image payload has unsupported or missing magic bytes")
        if declared_mime:
            normalized = declared_mime.split(";", 1)[0].strip().lower()
            if normalized in {"image/jpg", "image/pjpeg"}:
                normalized = "image/jpeg"
            if normalized != detected_mime:
                raise ValueError("image MIME type does not match its bytes")
        return data, detected_mime

    def _save_result(
        self,
        *,
        model_id: str,
        operation: str,
        images: list[tuple[str, str | None]],
        usage: dict[str, Any] | None = None,
    ) -> str | ToolResult:
        if not images:
            return json.dumps({"error": "no image returned", "model": model_id}, ensure_ascii=False)

        decoded: list[tuple[bytes, str]] = []
        try:
            for encoded, declared_mime in images:
                decoded.append(self._decode_image(encoded, declared_mime))
        except ValueError as exc:
            return json.dumps({"error": str(exc), "model": model_id}, ensure_ascii=False)

        paths: list[str] = []
        blocks = []
        for data, mime in decoded:
            path = self._output_path(_MIME_EXT[mime])
            path.write_bytes(data)
            paths.append(str(path))
            blocks.append(image_block(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"))

        result: dict[str, Any] = {
            "success": True,
            "operation": operation,
            "model": model_id,
            "paths": paths,
        }
        if usage:
            result["usage"] = usage
        model_text = json.dumps(result, ensure_ascii=False)
        logger.info("image_generate: {} {} image(s) via {} -> {}", operation, len(paths), model_id, paths)
        return ToolResult(
            model_text=model_text,
            display_text=f"{operation}: {len(paths)} image(s) -> {', '.join(paths)}",
            blocks=[text_block(model_text), *blocks],
        )

    async def _execute_openrouter_images(
        self,
        *,
        prompt: str,
        model_id: str,
        images: list[str],
        quality: str | None,
        aspect_ratio: str | None,
    ) -> str | ToolResult:
        payload: dict[str, Any] = {"model": model_id, "prompt": prompt, "n": 1}
        if quality:
            payload["quality"] = quality
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if images:
            payload["input_references"] = [self._image_part(ref) for ref in images[:6]]

        data = await self._images(payload)
        encoded = [
            (item.get("b64_json", ""), item.get("media_type"))
            for item in (data.get("data") or [])
            if isinstance(item, dict) and item.get("b64_json")
        ]
        return self._save_result(
            model_id=model_id,
            operation="edit" if images else "generation",
            images=encoded,
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
        )

    async def _execute_images_api(
        self,
        *,
        prompt: str,
        model_id: str,
        images: list[str],
        size: str | None,
        quality: str | None,
        output_format: str | None,
    ) -> str | ToolResult:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        options = {
            key: value
            for key, value in {
                "size": size,
                "quality": quality,
                "output_format": output_format,
            }.items()
            if value is not None
        }
        async with httpx.AsyncClient(
            follow_redirects=True,
            **self._client_options(timeout=300.0),
        ) as client:
            if images:
                files = [("image", self._edit_image(ref, index)) for index, ref in enumerate(images[:6], start=1)]
                response = await client.post(
                    f"{self.api_base}/images/edits",
                    headers=headers,
                    data={"model": model_id, "prompt": prompt, **options},
                    files=files,
                )
            else:
                response = await client.post(
                    f"{self.api_base}/images/generations",
                    headers=headers,
                    json={"model": model_id, "prompt": prompt, "n": 1, **options},
                )
            response.raise_for_status()
            data = response.json()
            encoded = await self._images_api_outputs(client, data)
        return self._save_result(
            model_id=model_id,
            operation="edit" if images else "generation",
            images=encoded,
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
        )

    async def _execute_chat_modalities(
        self,
        *,
        prompt: str,
        model_id: str,
        images: list[str],
    ) -> str | ToolResult:
        if images:
            content: Any = [{"type": "text", "text": prompt}]
            for ref in images[:6]:
                content.append(self._image_part(ref))
        else:
            content = prompt

        msg = await self._chat(
            {
                "model": model_id,
                "messages": [{"role": "user", "content": content}],
                "modalities": ["image", "text"],
            }
        )
        encoded: list[tuple[str, str | None]] = []
        for item in msg.get("images") or []:
            url = (item.get("image_url") or {}).get("url", "")
            header, separator, payload = url.partition(",")
            if not separator or not header.lower().startswith("data:image/") or ";base64" not in header.lower():
                continue
            encoded.append((payload, header[5:].split(";", 1)[0]))
        if not encoded:
            note = msg.get("content") or msg.get("refusal") or ""
            return json.dumps(
                {"error": "no image returned", "model": model_id, "note": note[:300]},
                ensure_ascii=False,
            )
        return self._save_result(
            model_id=model_id,
            operation="edit" if images else "generation",
            images=encoded,
        )

    async def execute(
        self,
        prompt: str,
        model: str | None = None,
        images: list[str] | None = None,
        size: str | None = None,
        quality: str | None = None,
        output_format: str | None = None,
        aspect_ratio: str | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        if not self.api_key:
            return self._no_key_error()

        configured_model = self._model(None)
        if not self.allow_model_override and model is not None and model != configured_model:
            return json.dumps(
                {"error": f"model override is disabled; configured model is {configured_model}"},
                ensure_ascii=False,
            )
        model_id = self._model(model)
        refs = images or []
        if self.api_style != "images" and (size is not None or output_format is not None):
            return json.dumps(
                {"error": "size and output_format require apiStyle=images"},
                ensure_ascii=False,
            )
        if self.api_style == "chat_modalities" and quality is not None:
            return json.dumps({"error": "quality requires an Images API style"}, ensure_ascii=False)
        if self.api_style != "openrouter_images" and aspect_ratio is not None:
            return json.dumps({"error": "aspect_ratio requires apiStyle=openrouter_images"}, ensure_ascii=False)

        try:
            if self.api_style == "images":
                return await self._execute_images_api(
                    prompt=prompt,
                    model_id=model_id,
                    images=refs,
                    size=size,
                    quality=quality,
                    output_format=output_format,
                )
            if self.api_style == "openrouter_images":
                return await self._execute_openrouter_images(
                    prompt=prompt,
                    model_id=model_id,
                    images=refs,
                    quality=quality,
                    aspect_ratio=aspect_ratio,
                )
            return await self._execute_chat_modalities(prompt=prompt, model_id=model_id, images=refs)
        except httpx.HTTPStatusError as e:
            return self._format_http_error(e)
        except OSError as e:
            return json.dumps({"error": f"could not read input image: {e}"}, ensure_ascii=False)
        except Exception as e:
            logger.error("image_generate error: {}", e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class SpeechGenerateTool(_OpenRouterMediaTool):
    """Synthesize speech from text via an OpenRouter audio model (gpt-audio)."""

    name = "text_to_speech"
    default_model = "openai/gpt-audio-mini"
    description = (
        "Synthesize speech audio from text. Saves an audio file under the "
        "workspace and returns its path; forward it with the `message` tool's "
        "`media` field."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to speak"},
            "voice": {
                "type": "string",
                "description": "Voice name (e.g. alloy, echo, fable, onyx, nova, shimmer)",
                "default": "alloy",
            },
            "format": {
                "type": "string",
                "enum": ["wav", "mp3", "opus", "flac"],
                "default": "wav",
                "description": (
                    "Output container. 'wav' is always available (zero deps); "
                    "mp3/opus/flac require ffmpeg on PATH and fall back to wav if absent."
                ),
            },
            "model": {
                "type": "string",
                "description": "Optional OpenRouter audio model override (e.g. openai/gpt-audio)",
            },
        },
        "required": ["text"],
    }

    # OpenRouter streams audio as raw pcm16; OpenAI's gpt-audio emits it at
    # 24 kHz, mono, 16-bit — the params needed to wrap it into a valid WAV.
    _PCM_RATE = 24_000
    _PCM_CHANNELS = 1
    _PCM_SAMPWIDTH = 2

    async def execute(
        self,
        text: str,
        voice: str = "alloy",
        format: str = "wav",
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not self.api_key:
            return self._no_key_error()

        model_id = self._model(model)
        payload = {
            "model": model_id,
            # Instruct verbatim narration — gpt-audio is conversational, so an
            # explicit directive keeps it from ad-libbing a reply.
            "messages": [
                {
                    "role": "user",
                    "content": f"Read the following text aloud verbatim, adding nothing:\n\n{text}",
                }
            ],
            "modalities": ["audio", "text"],
            # OpenRouter rejects audio output unless stream=true, and in stream
            # mode only the raw pcm16 format is accepted — we wrap/transcode after.
            "stream": True,
            "audio": {"voice": voice, "format": "pcm16"},
        }

        try:
            pcm, transcript = await self._stream_audio_pcm(payload)
        except httpx.HTTPStatusError as e:
            return self._format_http_error(e)
        except Exception as e:
            logger.error("text_to_speech error: {}", e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        if not pcm:
            return json.dumps(
                {"error": "no audio returned", "model": model_id, "note": transcript[:300]},
                ensure_ascii=False,
            )

        wav_path = self._output_path("wav")
        self._write_wav(wav_path, pcm)
        out_path, out_fmt, note = await self._maybe_transcode(wav_path, format)

        logger.info("text_to_speech: via {} -> {}", model_id, out_path)
        result: dict[str, Any] = {
            "success": True,
            "model": model_id,
            "path": str(out_path),
            "format": out_fmt,
        }
        if transcript:
            result["transcript"] = transcript
        if note:
            result["note"] = note
        return json.dumps(result, ensure_ascii=False)

    async def _stream_audio_pcm(self, payload: dict[str, Any]) -> tuple[bytes, str]:
        """Stream a chat-completions audio response → (pcm16 bytes, transcript).

        Concatenates the base64 ``delta.audio.data`` chunks (and any transcript
        text) from the SSE stream. Raises ``httpx.HTTPStatusError`` on non-2xx so
        ``execute`` can format it the same way as the image path.
        """
        pcm = bytearray()
        transcript_parts: list[str] = []
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(proxy=self._proxy, timeout=180.0) as client:
            async with client.stream("POST", f"{self.api_base}/chat/completions", headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    await r.aread()  # load body so .text/raise_for_status carry the error
                    r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    audio = delta.get("audio") or {}
                    if chunk := audio.get("data"):
                        pcm += base64.b64decode(chunk)
                    if t := audio.get("transcript"):
                        transcript_parts.append(t)
        return bytes(pcm), "".join(transcript_parts)

    def _write_wav(self, path: Path, pcm: bytes) -> None:
        """Wrap raw pcm16 bytes into a WAV container (stdlib, no deps)."""
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self._PCM_CHANNELS)
            w.setsampwidth(self._PCM_SAMPWIDTH)
            w.setframerate(self._PCM_RATE)
            w.writeframes(pcm)

    async def _maybe_transcode(self, wav_path: Path, fmt: str) -> tuple[Path, str, str]:
        """Transcode the WAV to ``fmt`` via ffmpeg. Returns (path, format, note).

        wav needs nothing; other formats need ffmpeg and fall back to wav with a
        note when it is missing or the conversion fails.
        """
        if fmt == "wav":
            return wav_path, "wav", ""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return wav_path, "wav", f"ffmpeg not on PATH; saved as wav instead of {fmt}"
        out = wav_path.with_suffix(f".{fmt}")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-i",
            str(wav_path),
            str(out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not out.exists():
            logger.warning("ffmpeg transcode to {} failed: {}", fmt, err.decode("utf-8", "replace")[:200])
            return wav_path, "wav", f"ffmpeg transcode to {fmt} failed; saved as wav"
        wav_path.unlink(missing_ok=True)
        return out, fmt, ""


def _find_video_url(obj: Any) -> str | None:
    """Recursively find the first downloadable video URL / data URI in a response.

    The exact completed-response shape for OpenRouter's /videos endpoint is not
    publicly pinned down, so we search defensively rather than hard-code a path.
    """
    if isinstance(obj, str):
        s = obj
        if s.startswith("data:video") or (
            s.startswith(("http://", "https://"))
            and any(ext in s.lower() for ext in (".mp4", ".webm", ".mov", "video"))
        ):
            return s
        return None
    if isinstance(obj, dict):
        # Prefer obvious keys first.
        for key in ("url", "video_url", "download_url", "video", "output", "result"):
            if key in obj and (found := _find_video_url(obj[key])):
                return found
        for v in obj.values():
            if found := _find_video_url(v):
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            if found := _find_video_url(item):
                return found
        return None
    return None


def _pick_video_url(status: dict[str, Any]) -> str | None:
    """Choose the downloadable content URL from a completed /videos response.

    OpenRouter's completed shape exposes the rendered video under
    ``signed_urls`` / ``unsigned_urls`` (lists). We prefer those explicitly:
    the job's own ``polling_url`` also lives under ``/videos/`` and would
    otherwise be matched by ``_find_video_url``'s "video" substring heuristic,
    yielding the status JSON instead of the clip. For any other (undocumented)
    provider shape we fall back to a defensive scan that excludes the
    polling/status URL.
    """
    for key in ("signed_urls", "unsigned_urls"):
        urls = status.get(key)
        if isinstance(urls, list) and urls and isinstance(urls[0], str):
            return urls[0]
    scannable = {k: v for k, v in status.items() if k != "polling_url"}
    return _find_video_url(scannable)


class VideoGenerateTool(_OpenRouterMediaTool):
    """Generate a video from a text prompt via Kling on OpenRouter (async)."""

    name = "video_generate"
    default_model = "kwaivgi/kling-v3.0-std"  # Kling v3 Standard
    description = (
        "Generate a short video from a text prompt (async; takes a while). Saves "
        "the video under the workspace and returns its path; forward it with the "
        "`message` tool's `media` field."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the video"},
            "model": {
                "type": "string",
                "description": "Optional OpenRouter video model override (default kwaivgi/kling-v3.0-std)",
            },
            "params": {
                "type": "object",
                "description": (
                    "Optional extra provider params merged into the request, e.g. "
                    '{"duration": 5, "aspect_ratio": "16:9"} (3-15s; 16:9/9:16/1:1)'
                ),
            },
        },
        "required": ["prompt"],
    }

    _POLL_INTERVAL_S = 6.0
    _POLL_TIMEOUT_S = 600.0
    # Backstop above the 600s polling cap; the poll loop gives up first.
    timeout_seconds = 660.0

    async def execute(
        self,
        prompt: str,
        model: str | None = None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        if not self.api_key:
            return self._no_key_error()

        model_id = self._model(model)
        body: dict[str, Any] = {"model": model_id, "prompt": prompt}
        if params:
            body.update(params)

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(proxy=self._proxy, timeout=120.0) as client:
                # 1) submit job
                r = await client.post(f"{self.api_base}/videos", headers=headers, json=body)
                r.raise_for_status()
                job = r.json()
                poll_url = job.get("polling_url") or f"{self.api_base}/videos/{job.get('id')}"

                # 2) poll
                status = await self._poll(client, poll_url, headers)
                state = status.get("status")
                if state != "completed":
                    return json.dumps(
                        {
                            "error": f"video job status={state}",
                            "detail": status.get("error") or status,
                            "model": model_id,
                        },
                        ensure_ascii=False,
                    )

                # 3) locate + download the video
                video_url = _pick_video_url(status)
                if not video_url:
                    return json.dumps(
                        {"error": "completed but no video URL found", "raw": status},
                        ensure_ascii=False,
                    )
                if video_url.startswith("data:"):
                    data = base64.b64decode(video_url.split(",", 1)[1])
                else:
                    # OpenRouter returns "unsigned" content URLs on its own host
                    # that need the same Authorization header as the API. Only
                    # send it to OpenRouter — never leak the key to a third-party
                    # (pre-signed CDN/S3) host.
                    dl_headers = headers if video_url.startswith(self.api_base) else None
                    dl = await client.get(video_url, headers=dl_headers, timeout=180.0)
                    dl.raise_for_status()
                    data = dl.content
        except httpx.HTTPStatusError as e:
            return self._format_http_error(e)
        except Exception as e:
            logger.error("video_generate error: {}", e)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        path = self._output_path("mp4")
        path.write_bytes(data)
        logger.info("video_generate: {} bytes via {} -> {}", len(data), model_id, path)
        return json.dumps({"success": True, "model": model_id, "path": str(path)}, ensure_ascii=False)

    async def _poll(self, client: httpx.AsyncClient, poll_url: str, headers: dict[str, str]) -> dict[str, Any]:
        """Poll until the job leaves the pending/processing state or times out."""
        waited = 0.0
        while waited < self._POLL_TIMEOUT_S:
            r = await client.get(poll_url, headers=headers)
            r.raise_for_status()
            job = r.json()
            if job.get("status") not in ("pending", "processing", "queued", "in_progress"):
                return job
            await asyncio.sleep(self._POLL_INTERVAL_S)
            waited += self._POLL_INTERVAL_S
        return {"status": "timeout"}
