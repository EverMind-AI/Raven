"""WeCom (Enterprise WeChat) channel — receives events over a wecom_aibot_sdk
WebSocket long connection, replies via the bot's streaming API and sends files
through the SDK's chunked upload protocol (media_id reply/send)."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger
from wecom_aibot_sdk import WSClient, generate_req_id
from wecom_aibot_sdk.upload import WECOM_UPLOAD_MAX_BYTES

from raven.channels.base import ChannelBase
from raven.channels.contract import Capabilities
from raven.channels.errors import transient_network
from raven.channels.media import file_uri_to_path, safe_name, save_media_bytes

_MSG_TYPE_LABEL = {"image": "[image]", "voice": "[voice]", "file": "[file]", "mixed": "[mixed content]"}
_DEDUP_CAP = 1000
_FRAMES_CAP = 1000
_MAX_MEDIA_BYTES = WECOM_UPLOAD_MAX_BYTES  # the SDK's own upload cap; never drift from it


class WecomChannel(ChannelBase):
    """WeCom AI bot over a WebSocket long connection — no public IP / webhook."""

    capabilities = Capabilities(file_attachments=True)
    config: Any
    name = "wecom"
    display_name = "WeCom"

    def __init__(self, config: Any):
        super().__init__(config)
        self._client: Any = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        # The inbound frame is required to reply; keep the latest per chat,
        # LRU-capped so a long-lived bot in many chats doesn't leak frames.
        self._frames: OrderedDict[str, Any] = OrderedDict()

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.bot_id or not self.config.secret:
            logger.error("WeCom bot_id and secret not configured")
            return
        self._running = True

        self._client = WSClient(
            {
                "bot_id": self.config.bot_id,
                "secret": self.config.secret,
                "reconnect_interval": 1000,
                "max_reconnect_attempts": -1,
                "heartbeat_interval": 30000,
            }
        )
        self._client.on("connected", self._log_event("WeCom WebSocket connected"))
        self._client.on("authenticated", self._log_event("WeCom authenticated"))
        self._client.on("disconnected", self._log_event("WeCom WebSocket disconnected", "warning"))
        self._client.on("error", self._log_event("WeCom error", "error", body=True))
        for msg_type in ("text", "image", "voice", "file", "mixed"):
            self._client.on(f"message.{msg_type}", self._make_handler(msg_type))
        self._client.on("event.enter_chat", self._on_enter_chat)

        logger.info("WeCom bot started (WebSocket long connection, no public IP needed)")
        await self._client.connect_async()
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.disconnect()
        logger.info("WeCom bot stopped")

    @staticmethod
    def _log_event(message: str, level: str = "info", body: bool = False):
        async def handler(frame: Any) -> None:
            getattr(logger, level)("{}{}", message, f": {frame}" if body else "")

        return handler

    def _make_handler(self, msg_type: str):
        async def handler(frame: Any) -> None:
            await self._process(frame, msg_type)

        return handler

    # ── inbound ───────────────────────────────────────────────────────

    @staticmethod
    def _body(frame: Any) -> dict:
        if hasattr(frame, "body"):
            return frame.body or {}
        if isinstance(frame, dict):
            return frame.get("body", frame)
        return {}

    async def _on_enter_chat(self, frame: Any) -> None:
        """Greet a user who just opened the bot chat, if a welcome is set."""
        body = self._body(frame)
        chat_id = body.get("chatid", "") if isinstance(body, dict) else ""
        if chat_id and self.config.welcome_message:
            try:
                await self._client.reply_welcome(
                    frame, {"msgtype": "text", "text": {"content": self.config.welcome_message}}
                )
            except Exception as e:
                logger.error("Error handling enter_chat: {}", e)

    async def _process(self, frame: Any, msg_type: str) -> None:
        try:
            body = self._body(frame)
            if not isinstance(body, dict):
                logger.warning("WeCom: invalid body type {}", type(body))
                return

            msg_id = body.get("msgid") or f"{body.get('chatid', '')}_{body.get('sendertime', '')}"
            if msg_id in self._seen:
                return
            self._seen[msg_id] = None
            while len(self._seen) > _DEDUP_CAP:
                self._seen.popitem(last=False)

            from_info = body.get("from", {})
            sender_id = from_info.get("userid", "unknown") if isinstance(from_info, dict) else "unknown"
            chat_type = body.get("chattype", "single")
            chat_id = body.get("chatid", sender_id)  # single chat: chatid == sender
            if not self.is_allowed(sender_id):  # reject before media download in _extract
                return

            content = await self._extract(body, msg_type)
            if not content:
                return

            self._frames[chat_id] = frame
            self._frames.move_to_end(chat_id)
            while len(self._frames) > _FRAMES_CAP:
                self._frames.popitem(last=False)
            await self.intake.publish(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=None,  # media paths are embedded in content (broad model compatibility)
                metadata={"message_id": msg_id, "msg_type": msg_type, "chat_type": chat_type},
            )
        except Exception as e:
            logger.error("Error processing WeCom message: {}", e)

    async def _extract(self, body: dict, msg_type: str) -> str:
        parts: list[str] = []
        if msg_type == "text":
            if text := body.get("text", {}).get("content"):
                parts.append(text)
        elif msg_type == "voice":
            # WeCom transcribes voice server-side; use it directly (no Whisper).
            parts.append(f"[voice] {body['voice']['content']}" if body.get("voice", {}).get("content") else "[voice]")
        elif msg_type == "image":
            parts.append(await self._media_part(body.get("image", {}), "image"))
        elif msg_type == "file":
            info = body.get("file", {})
            parts.append(await self._media_part(info, "file", info.get("name")))
        elif msg_type == "mixed":
            for item in body.get("mixed", {}).get("item", []):
                if item.get("type") == "text":
                    if text := item.get("text", {}).get("content"):
                        parts.append(text)
                else:
                    parts.append(_MSG_TYPE_LABEL.get(item.get("type", ""), f"[{item.get('type')}]"))
        else:
            parts.append(_MSG_TYPE_LABEL.get(msg_type, f"[{msg_type}]"))
        return "\n".join(parts)

    async def _media_part(self, info: dict, kind: str, name: str | None = None) -> str:
        url, aes_key = info.get("url"), info.get("aeskey")
        if not (url and aes_key):
            return f"[{kind}: {name or kind}: download failed]"
        result = await self._download(url, aes_key, name)
        if not result:
            return f"[{kind}: {name or kind}: download failed]"
        path, display = result
        return f"[{kind}: {display}]\n[{kind.capitalize()}: source: {path}]"

    async def _download(self, url: str, aes_key: str, name: str | None) -> tuple[str, str] | None:
        """Return ``(saved_path, display_name)`` or ``None`` on failure. The
        saved path is content-hash-prefixed (collision-safe); the display name
        is the clean original filename for the human-readable label."""
        try:
            data, fname = await self._client.download_file(url, aes_key)
            if not data:
                logger.warning("WeCom: media download returned no data")
                return None
            path = save_media_bytes("wecom", data, name or fname)
            return str(path), safe_name(name or fname)
        except Exception as e:
            logger.error("Error downloading WeCom media: {}", e)
            return None

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._client:
            logger.warning("WeCom client not initialized")
            return
        content = (content or "").strip()
        media = [m for m in (media or []) if isinstance(m, str) and m.strip()]
        if content:
            await self._send_text(chat_id, content)
        for media_ref in media:
            if await self._send_media_ref(chat_id, media_ref):
                continue
            logger.error("WeCom media send failed for {}", media_ref)
            # Tell the user instead of dropping the attachment silently.
            await self._send_text(chat_id, f"[Attachment send failed: {self._media_display_name(media_ref)}]")

    async def _send_text(self, chat_id: str, text: str) -> None:
        frame = self._frames.get(chat_id)
        if not frame:
            logger.warning("No frame for chat {}, cannot reply", chat_id)
            return
        try:
            await self._client.reply_stream(frame, generate_req_id("stream"), text, finish=True)
        except Exception as e:
            if transient_network(e):
                raise  # ws drop / timeout: the frame is still cached, retry can succeed
            logger.error("Error sending WeCom message: {}", e)

    async def _send_media_ref(self, chat_id: str, media_ref: str) -> bool:
        path = self._resolve_media_path(media_ref)
        if not path:
            return False
        # In-band reply when the inbound frame is cached (same routing as the
        # text reply); otherwise the proactive send needs only the chatid.
        frame = self._frames.get(chat_id)
        try:
            if frame:
                await self._client.reply_media(frame, path)
            else:
                await self._client.send_media_message(chat_id, path)
            return True
        except Exception as e:
            if transient_network(e):
                raise  # ws drop / timeout: the hub retries the delivery
            logger.error("Error sending WeCom media ref={} err={}", media_ref, e)
            return False

    @staticmethod
    def _media_display_name(media_ref: str) -> str:
        """The name a failure notice shows: a ``file://`` ref is resolved first,
        so the user reads ``report.pdf`` rather than its percent-encoded tail."""
        if media_ref.startswith("file://"):
            resolved = file_uri_to_path(media_ref)
            if resolved is not None:
                return safe_name(str(resolved))
            try:
                return safe_name(unquote(urlparse(media_ref).path))
            except ValueError:
                return safe_name(media_ref)
        return safe_name(media_ref)

    @staticmethod
    def _resolve_media_path(media_ref: str) -> str | None:
        """Resolve an outbound media ref (local or file:// path) to an existing
        local path within the upload size cap, for the SDK upload protocol."""
        if media_ref.startswith("file://"):
            path = file_uri_to_path(media_ref)
            if path is None:
                logger.warning("WeCom media file not found: {}", media_ref)
                return None
        else:
            path = Path(os.path.expanduser(media_ref))
        if not path.is_file():
            logger.warning("WeCom media file not found: {}", path)
            return None
        try:
            size = path.stat().st_size
        except OSError:
            # Removed between the existence check and the size read: report it
            # as missing here rather than let it escape as a retryable failure.
            logger.warning("WeCom media file not found: {}", path)
            return None
        if size > _MAX_MEDIA_BYTES:
            logger.warning("WeCom media too large: {} bytes, cap {} (ref {})", size, _MAX_MEDIA_BYTES, media_ref)
            return None
        return str(path)
