"""QQ channel — botpy SDK (WebSocket) for C2C, group, and direct messages.

Orchestration only: the botpy Client subclass routes events to this channel,
which applies the pure routing in :mod:`.parsing` and replies via the SDK API.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, NamedTuple

import botpy
import httpx
from botpy.errors import ServerError
from botpy.message import C2CMessage, GroupMessage
from loguru import logger

from raven.channels.adapters.qq import parsing
from raven.channels.base import ChannelBase
from raven.channels.errors import transient_network
from raven.channels.media import save_media_bytes
from raven.config.schema import QQConfig

_RECONNECT_DELAY_S = 5
_DEDUP_CAP = 1000
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class _Fetched(NamedTuple):
    """A saved attachment, or why there is not one.

    ``reason`` is what the agent is told. Refusing an oversized attachment is
    not a failure, and labelling it as one sends the model looking for a
    network fault that is not there -- and an operator after it.
    """

    path: str | None
    reason: str = ""


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Build a botpy Client subclass that forwards events to *channel*."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # Disable botpy's file log (default botpy.log fails on a read-only
            # fs); Raven logs through loguru.
            super().__init__(intents=intents, ext_handlers=False)

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: "GroupMessage"):
            await channel._on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            await channel._on_message(message, is_group=False)

    return _Bot


class QQChannel(ChannelBase):
    """QQ channel using the botpy SDK over WebSocket."""

    config: QQConfig
    name = "qq"
    display_name = "QQ"

    def __init__(self, config: QQConfig):
        super().__init__(config)
        self._client: "botpy.Client | None" = None
        self._http: "httpx.AsyncClient | None" = None
        self._processed_ids: deque[str] = deque(maxlen=_DEDUP_CAP)
        self._msg_seq: int = 1
        self._chat_type_cache: dict[str, str] = {}

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return
        self._running = True
        self._client = _make_bot_class(self)()
        self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        logger.info("QQ bot started (C2C & Group supported)")
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            if self._running:
                logger.info("Reconnecting QQ bot in {}s...", _RECONNECT_DELAY_S)
                await asyncio.sleep(_RECONNECT_DELAY_S)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("QQ bot stopped")

    # ── inbound ───────────────────────────────────────────────────────

    async def _download_attachment(self, att: Any) -> _Fetched:
        """Fetch one attachment to the media dir, returning its local path.

        QQ serves attachment urls without a scheme, and they expire, so the
        bytes have to be pulled while the event is being handled rather than
        handed to the agent as a link.
        """
        url = getattr(att, "url", None)
        if not url or self._http is None:
            return _Fetched(None, "download failed")
        # Checked before the request, the way discord and dingtalk do it:
        # `resp.content` buffers the whole body, so an oversized attachment has
        # to be refused rather than measured.
        size = getattr(att, "size", None) or 0
        if size > _MAX_ATTACHMENT_BYTES:
            logger.warning("QQ attachment too large ({} bytes), skipping", size)
            return _Fetched(None, "too large")
        if url.startswith("//"):
            url = f"https:{url}"
        elif not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        try:
            resp = await self._fetch_attachment(url)
            if resp is None:
                return _Fetched(None, "blocked")
            resp.raise_for_status()
            # Inside the try: a write failure (full disk, unwritable media dir)
            # must degrade to the same "download failed" label as a fetch
            # failure, not escape to the caller and take the whole message down.
            return _Fetched(str(save_media_bytes(self.name, resp.content, getattr(att, "filename", None))))
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as e:
            logger.warning("QQ attachment download failed ({}): {}", url, e)
            return _Fetched(None, "download failed")

    async def _fetch_attachment(self, url: str) -> "httpx.Response | None":
        """Fetch an attachment URL through the guarded fetch.

        The URL arrives in an inbound message event, so it is the sender's to
        choose, and ``allow_from`` defaults to everyone: without a check the
        gateway would GET whatever address a message named and hand the body to
        the agent -- a read primitive against anything the host can reach.
        """
        from raven.security.network import guarded_fetch

        assert self._http is not None  # noqa: S101 - the caller checked
        return await guarded_fetch(self._http, url, what="QQ attachment")

    async def _on_message(self, data: "C2CMessage | GroupMessage", is_group: bool = False) -> None:
        try:
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            chat_id, user_id, chat_type = parsing.resolve_route(data, is_group)
            # Ahead of any download: an attachment from a sender who is not
            # allowed must not be fetched just to be thrown away. Returning here
            # skips the rejection Intake.publish would otherwise log, and that
            # line is how an operator diagnoses "why is the bot ignoring me".
            if not self.is_allowed(user_id):
                logger.warning(
                    "Access denied for sender {} on channel {}. Add them to allowFrom list in config to grant access.",
                    user_id,
                    self.name,
                )
                return

            parts: list[str] = []
            text = parsing.clean_content(data)
            if text:
                parts.append(text)

            media: list[str] = []
            for att in getattr(data, "attachments", None) or []:
                kind = (getattr(att, "content_type", None) or "file").split("/", 1)[0]
                saved = await self._download_attachment(att)
                if saved.path is None:
                    filename = getattr(att, "filename", None)
                    parts.append(f"[{kind}: {filename} - {saved.reason}]" if filename else f"[{kind}: {saved.reason}]")
                    continue
                media.append(saved.path)
                parts.append(f"[{kind}: {saved.path}]")

            # A message carrying only an image used to be dropped here, so the
            # agent never learned it had been sent one.
            if not parts:
                return

            self._chat_type_cache[chat_id] = chat_type
            await self.intake.publish(
                sender_id=user_id,
                chat_id=chat_id,
                content="\n".join(parts),
                media=media or None,
                metadata={"message_id": data.id},
            )
        except Exception:
            logger.exception("Error handling QQ message")

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._client:
            logger.warning("QQ client not initialized")
            return
        # Bump the per-message sequence number so QQ's API doesn't dedup replies.
        self._msg_seq += 1
        try:
            chat_type = self._chat_type_cache.get(chat_id, "c2c")
            if chat_type == "group":
                await self._client.api.post_group_message(
                    group_openid=chat_id,
                    msg_type=2,
                    markdown={"content": content},
                    msg_id=None,
                    msg_seq=self._msg_seq,
                )
            elif chat_type == "guild_dm":
                # Guild DMs reply through the DM session (post_dms); the C2C
                # endpoint rejects guild user ids. post_dms has no msg_seq.
                await self._client.api.post_dms(
                    guild_id=chat_id,
                    content=content,
                    msg_id=None,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=chat_id,
                    msg_type=2,
                    markdown={"content": content},
                    msg_id=None,
                    msg_seq=self._msg_seq,
                )
        except Exception as e:
            if isinstance(e, ServerError) or transient_network(e):
                raise  # 5xx / network drop: let manager._send_with_retry back off
            logger.error("Error sending QQ message: {}", e)
