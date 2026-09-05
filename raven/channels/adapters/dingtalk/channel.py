"""DingTalk channel — dingtalk-stream Stream Mode in, REST API out.

This module is orchestration only: it parses inbound events (via
:mod:`.parsing`) and drives the transport (:class:`.api.DingTalkAPI`). Group
chat ids carry a ``group:`` prefix so replies route back to the room.
"""

import asyncio
import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from dingtalk_stream import (
    AckMessage,
    CallbackHandler,
    CallbackMessage,
    Credential,
    DingTalkStreamClient,
)
from dingtalk_stream.chatbot import ChatbotMessage
from loguru import logger

from raven.channels.adapters.dingtalk import parsing
from raven.channels.adapters.dingtalk.api import DingTalkAPI
from raven.channels.base import ChannelBase
from raven.channels.media import save_media_bytes
from raven.config.schema import DingTalkConfig

_RECONNECT_DELAY_S = 5
_REPLY_TITLE = "Raven Reply"


class DingTalkCallbackHandler(CallbackHandler):
    """Stream-SDK callback: parse one ChatbotMessage and hand it to the channel."""

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        try:
            chatbot_msg = ChatbotMessage.from_dict(message.data)
            parsed = parsing.parse_inbound(chatbot_msg, message.data)
            if not self.channel.is_allowed(parsed.sender_id):  # reject before file download
                return AckMessage.STATUS_OK, "OK"

            text = parsed.text
            file_paths: list[str] = []
            for req in parsed.media:
                if saved := await self.channel._download_dingtalk_file(
                    req.download_code, req.filename, parsed.sender_uid
                ):
                    file_paths.append(saved)
                    text = text or req.placeholder
            text = parsing.append_files_footer(text, file_paths)

            if not text:
                logger.warning(
                    "Received empty or unsupported message type: {}",
                    getattr(chatbot_msg, "message_type", None),
                )
                return AckMessage.STATUS_OK, "OK"

            logger.info("Received DingTalk message from {} ({}): {}", parsed.sender_name, parsed.sender_id, text)
            self.channel._spawn(
                self.channel._on_message(
                    text,
                    parsed.sender_id,
                    parsed.sender_name,
                    parsed.conversation_type,
                    parsed.conversation_id,
                    file_paths,
                )
            )
            return AckMessage.STATUS_OK, "OK"
        except Exception as e:
            logger.error("Error processing DingTalk message: {}", e)
            # Ack OK regardless, otherwise the server keeps retrying the frame.
            return AckMessage.STATUS_OK, "Error"


class DingTalkChannel(ChannelBase):
    config: DingTalkConfig
    name = "dingtalk"
    display_name = "DingTalk"

    def __init__(self, config: DingTalkConfig):
        super().__init__(config)
        self._stream: DingTalkStreamClient | None = None
        self._api = DingTalkAPI(config.client_id, config.client_secret)
        self._background_tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        """Run a coroutine detached, holding a ref so it survives GC."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.client_id or not self.config.client_secret:
            logger.error("DingTalk client_id and client_secret not configured")
            return

        self._running = True
        await self._api.open()
        self._stream = DingTalkStreamClient(Credential(self.config.client_id, self.config.client_secret))
        self._stream.register_callback_handler(ChatbotMessage.TOPIC, DingTalkCallbackHandler(self))
        logger.info("DingTalk bot started in Stream Mode (client_id={})", self.config.client_id)

        while self._running:
            try:
                await self._stream.start()
            except Exception as e:
                logger.warning("DingTalk stream error: {}", e)
            if self._running:
                logger.info("Reconnecting DingTalk stream in {}s...", _RECONNECT_DELAY_S)
                await asyncio.sleep(_RECONNECT_DELAY_S)

    async def stop(self) -> None:
        self._running = False
        # Cancel and await in-flight downloads BEFORE closing the http client
        # they use; awaiting also keeps the cancellations from dying unobserved.
        tasks = list(self._background_tasks)
        self._background_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # The stream SDK exposes no stop(); closing its websocket unblocks the
        # stream.start() await so the reconnect loop can see _running=False.
        ws = getattr(self._stream, "websocket", None)
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        await self._api.close()

    # ── inbound ───────────────────────────────────────────────────────

    async def _download_dingtalk_file(self, download_code: str, filename: str, sender_id: str) -> str | None:
        """Fetch an inbound attachment and persist it via the shared media sink,
        which is traversal-safe (sanitizes the name) and collision-safe."""
        data = await self._api.download_file(download_code)
        if data is None:
            return None
        path = await asyncio.to_thread(save_media_bytes, "dingtalk", data, f"{sender_id}_{filename}")
        logger.info("dingtalk file saved: {}", path)
        return str(path)

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        conversation_type: str | None = None,
        conversation_id: str | None = None,
        file_paths: list[str] | None = None,
    ) -> None:
        """Hand a parsed message to the spine via Intake, which enforces the
        allow_from permission check before submitting the turn."""
        try:
            await self.intake.publish(
                sender_id=sender_id,
                chat_id=parsing.resolve_chat_id(conversation_type, conversation_id, sender_id),
                content=str(content),
                media=file_paths or None,
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                    "conversation_type": conversation_type,
                },
            )
        except Exception as e:
            logger.error("Error publishing DingTalk message: {}", e)

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not await self._api.access_token():
            return
        if content and content.strip():
            await self._reply_markdown(chat_id, content.strip())
        for media_ref in media or []:
            if await self._send_media_ref(chat_id, media_ref):
                continue
            logger.error("DingTalk media send failed for {}", media_ref)
            # Tell the user instead of dropping the attachment silently.
            name = parsing.guess_filename(media_ref, parsing.guess_upload_type(media_ref))
            await self._reply_markdown(chat_id, f"[Attachment send failed: {name}]")

    async def _reply_markdown(self, chat_id: str, text: str) -> bool:
        return await self._api.send(chat_id, "sampleMarkdown", {"text": text, "title": _REPLY_TITLE})

    async def _send_media_ref(self, chat_id: str, media_ref: str) -> bool:
        if not (media_ref := (media_ref or "").strip()):
            return True

        upload_type = parsing.guess_upload_type(media_ref)
        # An image already on a public URL can be sent by reference, no upload.
        if upload_type == "image" and parsing.is_http_url(media_ref):
            if await self._api.send(chat_id, "sampleImageMsg", {"photoURL": media_ref}):
                return True
            logger.warning("DingTalk image url send failed, trying upload fallback: {}", media_ref)

        data, filename, content_type = await self._read_media_bytes(media_ref)
        if not data:
            logger.error("DingTalk media read failed: {}", media_ref)
            return False

        filename = filename or parsing.guess_filename(media_ref, upload_type)
        file_type = Path(filename).suffix.lower().lstrip(".") or (
            mimetypes.guess_extension(content_type or "") or ".bin"
        ).lstrip(".")
        if file_type == "jpeg":
            file_type = "jpg"
        mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        media_id = await self._api.upload_media(upload_type, filename, data, mime)
        if not media_id:
            return False

        if upload_type == "image":
            # Verified in production: sampleImageMsg accepts a media_id in photoURL.
            if await self._api.send(chat_id, "sampleImageMsg", {"photoURL": media_id}):
                return True
            logger.warning("DingTalk image media_id send failed, falling back to file: {}", media_ref)

        return await self._api.send(
            chat_id, "sampleFile", {"mediaId": media_id, "fileName": filename, "fileType": file_type}
        )

    async def _read_media_bytes(self, media_ref: str) -> tuple[bytes | None, str | None, str | None]:
        """Read an outbound media ref (remote URL or local path) into bytes."""
        if not media_ref:
            return None, None, None

        if parsing.is_http_url(media_ref):
            data, content_type = await self._api.fetch_remote(media_ref)
            if data is None:
                return None, None, None
            return data, parsing.guess_filename(media_ref, parsing.guess_upload_type(media_ref)), content_type

        try:
            if media_ref.startswith("file://"):
                local_path = Path(unquote(urlparse(media_ref).path))
            else:
                local_path = Path(os.path.expanduser(media_ref))
            if not local_path.is_file():
                logger.warning("DingTalk media file not found: {}", local_path)
                return None, None, None
            data = await asyncio.to_thread(local_path.read_bytes)
            return data, local_path.name, mimetypes.guess_type(local_path.name)[0]
        except Exception as e:
            logger.error("DingTalk media read error ref={} err={}", media_ref, e)
            return None, None, None
