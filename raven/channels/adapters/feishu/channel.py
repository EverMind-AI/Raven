"""Feishu/Lark adapter — receives events over a lark-oapi WebSocket long
connection and sends replies via the lark Open API."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import lark_oapi as lark
from loguru import logger

from raven.channels.adapters.feishu import cards, content
from raven.channels.base import ChannelBase
from raven.channels.errors import transient_network
from raven.channels.media import save_media_bytes
from raven.channels.transcribe import transcribe_audio
from raven.config.schema import FeishuConfig

_MSG_TYPE_LABEL = {"image": "[image]", "audio": "[audio]", "file": "[file]", "sticker": "[sticker]"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
_PLAYABLE_EXTS = {".opus", ".mp4", ".mov", ".avi"}
_FILE_TYPE = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}
_DEDUP_CAP = 1000


class FeishuChannel(ChannelBase):
    """Feishu bot over a WebSocket long connection — no public IP / webhook."""

    name = "feishu"
    display_name = "Feishu"

    config: FeishuConfig

    def __init__(self, config: FeishuConfig):
        super().__init__(config)
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._native_stt_disabled = False

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        self._running = True
        self._loop = asyncio.get_running_loop()

        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self._on_message_sync)
        for method in (
            "register_p2_im_message_reaction_created_v1",
            "register_p2_im_message_message_read_v1",
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
        ):
            register = getattr(builder, method, None)
            if callable(register):
                builder = register(self._ignore_event)

        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=builder.build(),
            log_level=lark.LogLevel.INFO,
        )
        self._ws_thread = threading.Thread(target=self._run_ws_supervised, daemon=True)
        self._ws_thread.start()

        logger.info("Feishu bot started (WebSocket long connection, no public IP needed)")
        while self._running:
            await asyncio.sleep(1)

    def _run_ws_supervised(self) -> None:
        """Drive the lark WebSocket client on a dedicated event loop.

        lark_oapi grabs a module-level ``loop = asyncio.get_event_loop()``;
        giving this thread its own idle loop (and pointing lark's module at
        it) avoids clashing with the already-running main loop. Reconnects
        with a fixed backoff until the channel is stopped.
        """
        import lark_oapi.ws.client as lark_ws

        ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_loop)
        lark_ws.loop = ws_loop
        try:
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning("Feishu WebSocket error: {}", e)
                if self._running:
                    time.sleep(5)
        finally:
            ws_loop.close()

    async def stop(self) -> None:
        # lark.ws.Client has no stop(); dropping references + exit closes it.
        self._running = False
        logger.info("Feishu bot stopped")

    # ── group addressing ──────────────────────────────────────────────

    def _is_bot_mentioned(self, message: Any) -> bool:
        if "@_all" in (message.content or ""):
            return True
        for mention in getattr(message, "mentions", None) or []:
            mid = getattr(mention, "id", None)
            if mid and not getattr(mid, "user_id", None) and (getattr(mid, "open_id", None) or "").startswith("ou_"):
                return True
        return False

    def _addressed_to_bot(self, message: Any) -> bool:
        return self.config.group_policy == "open" or self._is_bot_mentioned(message)

    # ── reactions (ack UX) ────────────────────────────────────────────

    async def _react(self, message_id: str, emoji_type: str) -> None:
        if not self._client:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._react_sync, message_id, emoji_type)

    def _react_sync(self, message_id: str, emoji_type: str) -> None:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message_reaction.create(request)
            if not response.success():
                logger.warning("Failed to add reaction: code={}, msg={}", response.code, response.msg)
        except Exception as e:
            logger.warning("Error adding reaction: {}", e)

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._client:
            logger.warning("Feishu client not initialized")
            return
        receive_id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
        loop = asyncio.get_running_loop()
        try:
            for path in media or []:
                await self._send_one_media(loop, receive_id_type, chat_id, path)
            if content and content.strip():
                await self._send_text(loop, receive_id_type, chat_id, content)
        except Exception as e:
            if transient_network(e):
                raise  # requests-level drop/timeout inside the executor: retryable
            logger.error("Error sending Feishu message: {}", e)

    async def _send_one_media(self, loop, receive_id_type, chat_id, path) -> None:
        if not os.path.isfile(path):
            logger.warning("Media file not found: {}", path)
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in _IMAGE_EXTS:
            key = await loop.run_in_executor(None, self._upload_image_sync, path)
            if key:
                await self._post(loop, receive_id_type, chat_id, "image", {"image_key": key})
        else:
            key = await loop.run_in_executor(None, self._upload_file_sync, path)
            if key:
                kind = "media" if ext in _PLAYABLE_EXTS else "file"
                await self._post(loop, receive_id_type, chat_id, kind, {"file_key": key})

    async def _send_text(self, loop, receive_id_type, chat_id, text) -> None:
        fmt = cards.detect_format(text)
        if fmt == "text":
            await self._post_raw(loop, receive_id_type, chat_id, "text", cards.text_payload(text))
        elif fmt == "post":
            await self._post_raw(loop, receive_id_type, chat_id, "post", cards.post_payload(text))
        else:
            for payload in cards.card_payloads(text):
                await self._post_raw(loop, receive_id_type, chat_id, "interactive", payload)

    async def _post(self, loop, receive_id_type, chat_id, msg_type, body: dict) -> None:
        await self._post_raw(loop, receive_id_type, chat_id, msg_type, json.dumps(body, ensure_ascii=False))

    async def _post_raw(self, loop, receive_id_type, chat_id, msg_type, content_json: str) -> None:
        await loop.run_in_executor(None, self._send_message_sync, receive_id_type, chat_id, msg_type, content_json)

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content_json: str) -> bool:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content_json)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Failed to send Feishu {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return False
            return True
        except Exception as e:
            logger.error("Error sending Feishu {} message: {}", msg_type, e)
            return False

    # ── media upload / download (lark SDK, per-adapter) ───────────────

    def _upload_image_sync(self, path: str) -> str | None:
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(CreateImageRequestBody.builder().image_type("message").image(f).build())
                    .build()
                )
                response = self._client.im.v1.image.create(request)
                if response.success():
                    return response.data.image_key
                logger.error("Failed to upload image: code={}, msg={}", response.code, response.msg)
        except Exception as e:
            logger.error("Error uploading image {}: {}", path, e)
        return None

    def _upload_file_sync(self, path: str) -> str | None:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(_FILE_TYPE.get(ext, "stream"))
                        .file_name(os.path.basename(path))
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.file.create(request)
                if response.success():
                    return response.data.file_key
                logger.error("Failed to upload file: code={}, msg={}", response.code, response.msg)
        except Exception as e:
            logger.error("Error uploading file {}: {}", path, e)
        return None

    def _download_resource_sync(
        self, message_id: str, file_key: str, resource_type: str
    ) -> tuple[bytes | None, str | None]:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu only accepts 'image' or 'file'; audio rides the file endpoint.
        api_type = "file" if resource_type == "audio" else resource_type
        try:
            request = (
                GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(api_type).build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                data = response.file
                return (data.read() if hasattr(data, "read") else data), response.file_name
            logger.error("Failed to download {}: code={}, msg={}", resource_type, response.code, response.msg)
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
        return None, None

    async def _download_media(
        self, msg_type: str, content_json: dict, message_id: str | None
    ) -> tuple[str | None, str]:
        loop = asyncio.get_running_loop()
        data = filename = None
        if msg_type == "image":
            key = content_json.get("image_key")
            if key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_resource_sync, message_id, key, "image"
                )
                filename = filename or f"{key[:16]}.jpg"
        elif msg_type in ("audio", "file", "media"):
            key = content_json.get("file_key")
            if key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_resource_sync, message_id, key, msg_type
                )
                filename = filename or key[:16]
                if msg_type == "audio" and not filename.endswith(".opus"):
                    filename = f"{filename}.opus"
        if data and filename:
            path = save_media_bytes("feishu", data, filename)
            return str(path), f"[{msg_type}: {path.name}]"
        return None, f"[{msg_type}: download failed]"

    # ── transcription ─────────────────────────────────────────────────

    async def _transcribe(self, path: str) -> str:
        """Transcribe a voice file, preferring Feishu's own speech-to-text
        (no external key) and falling back to the base Whisper provider.

        Native STT is skipped once it has definitively failed this session
        (no permission / unavailable on the tenant's plan), so we don't pay
        its latency on every subsequent voice message."""
        if not self._native_stt_disabled:
            loop = asyncio.get_running_loop()
            native = await loop.run_in_executor(None, self._lark_stt_sync, path)
            if native:
                return native
        return await transcribe_audio(path, self.transcription_api_key, channel=self.name)

    def _lark_stt_sync(self, path: str) -> str | None:
        """One-shot recognition via Feishu's file_recognize API. Feishu voice
        messages are opus, which the API accepts directly (no transcoding).
        Returns ``None`` on any failure so the caller can fall back."""
        from lark_oapi.api.speech_to_text.v1 import (
            FileConfig,
            FileRecognizeSpeechRequest,
            FileRecognizeSpeechRequestBody,
            Speech,
        )

        try:
            audio_b64 = base64.b64encode(Path(path).read_bytes()).decode()
            request = (
                FileRecognizeSpeechRequest.builder()
                .request_body(
                    FileRecognizeSpeechRequestBody.builder()
                    .speech(Speech.builder().speech(audio_b64).build())
                    .config(
                        FileConfig.builder().format("opus").engine_type("16k_auto").file_id(uuid.uuid4().hex).build()
                    )
                    .build()
                )
                .build()
            )
            # file_recognize is QPS-limited (code 99991400); the limit is
            # transient, so retry a couple of times with backoff before
            # giving up and falling back to Whisper.
            for attempt in range(3):
                response = self._client.speech_to_text.v1.speech.file_recognize(request)
                if response.success() and response.data and response.data.recognition_text:
                    return response.data.recognition_text
                code = getattr(response, "code", None)
                if code == 99991400 and attempt < 2:
                    time.sleep(1.0 + attempt)
                    continue
                self._disable_native_stt(code, getattr(response, "msg", ""))
                break
        except Exception as e:
            # Likely transient (network) — fall back but keep native enabled.
            logger.warning("Feishu native STT failed, falling back to Whisper: {}", e)
        return None

    def _disable_native_stt(self, code: int | None, msg: str) -> None:
        """Turn off native STT for this session after a definitive failure and
        emit a single actionable hint. Operators read this to know whether to
        grant a scope, upgrade the Feishu plan, or rely on the Whisper key."""
        self._native_stt_disabled = True
        if code == 99991672:  # app lacks the speech_to_text:speech scope
            logger.warning(
                "Feishu native STT disabled: the app lacks the 'speech_to_text:speech' "
                "permission. Grant it in the Feishu developer console (then publish a "
                "new app version) for key-free transcription; using Whisper meanwhile. ({})",
                msg,
            )
        elif code == 99991400:  # rate / availability limit
            logger.warning(
                "Feishu native STT disabled: file_recognize is rate-limited or "
                "unavailable on this tenant's plan (the API requires a paid Feishu "
                "plan). Using Whisper instead — set providers.groq.api_key to enable "
                "it, or upgrade the Feishu plan for key-free transcription. ({})",
                msg,
            )
        else:
            logger.warning("Feishu native STT disabled (code={}, msg={}); using Whisper.", code, msg)

    # ── inbound ───────────────────────────────────────────────────────

    def _on_message_sync(self, data: Any) -> None:
        """Bridge the lark WS thread back onto the main event loop."""
        if not self._running:
            # lark.ws.Client has no stop(): the socket may outlive stop() and
            # keep delivering. Drop zombie deliveries here so a stopped (or
            # restarted) instance can never publish again.
            return
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: Any) -> None:
        try:
            message = data.event.message
            sender = data.event.sender
            message_id = message.message_id
            if message_id in self._seen:
                return
            self._seen[message_id] = None
            while len(self._seen) > _DEDUP_CAP:
                self._seen.popitem(last=False)
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_type = message.chat_type
            msg_type = message.message_type
            if chat_type == "group" and not self._addressed_to_bot(message):
                return
            if not self.is_allowed(sender_id):  # reject before react / media download
                return

            await self._react(message_id, self.config.react_emoji)

            content_text, media_paths = await self._extract(msg_type, message, message_id)
            if not content_text and not media_paths:
                return

            reply_to = message.chat_id if chat_type == "group" else sender_id
            await self.intake.publish(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content_text,
                media=media_paths,
                metadata={"message_id": message_id, "chat_type": chat_type, "msg_type": msg_type},
            )
        except Exception as e:
            logger.error("Error processing Feishu message: {}", e)

    async def _extract(self, msg_type: str, message: Any, message_id: str) -> tuple[str, list[str]]:
        try:
            payload = json.loads(message.content) if message.content else {}
        except json.JSONDecodeError:
            payload = {}
        parts: list[str] = []
        media: list[str] = []

        if msg_type == "text":
            if text := payload.get("text"):
                parts.append(text)
        elif msg_type == "post":
            text, image_keys = content.extract_post(payload)
            if text:
                parts.append(text)
            for key in image_keys:
                path, label = await self._download_media("image", {"image_key": key}, message_id)
                if path:
                    media.append(path)
                parts.append(label)
        elif msg_type in ("image", "audio", "file", "media"):
            path, label = await self._download_media(msg_type, payload, message_id)
            if path:
                media.append(path)
            if msg_type == "audio" and path:
                if transcription := await self._transcribe(path):
                    label = f"[transcription: {transcription}]"
            parts.append(label)
        elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
            if text := content.extract_share_card(payload, msg_type):
                parts.append(text)
        else:
            parts.append(_MSG_TYPE_LABEL.get(msg_type, f"[{msg_type}]"))

        return ("\n".join(parts) if parts else ""), media

    @staticmethod
    def _ignore_event(_data: Any) -> None:
        """No-op sink for reaction/read/p2p-enter events (silences SDK noise)."""
