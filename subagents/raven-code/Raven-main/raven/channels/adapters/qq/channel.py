"""QQ channel — botpy SDK (WebSocket) for C2C, group, and direct messages.

Orchestration only: the botpy Client subclass routes events to this channel,
which applies the pure routing in :mod:`.parsing` and replies via the SDK API.
"""

from __future__ import annotations

import asyncio
from collections import deque

import botpy
from botpy.errors import ServerError
from botpy.message import C2CMessage, GroupMessage
from loguru import logger

from raven.channels.adapters.qq import parsing
from raven.channels.base import ChannelBase
from raven.channels.errors import transient_network
from raven.config.schema import QQConfig

_RECONNECT_DELAY_S = 5
_DEDUP_CAP = 1000


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
        logger.info("QQ bot stopped")

    # ── inbound ───────────────────────────────────────────────────────

    async def _on_message(self, data: "C2CMessage | GroupMessage", is_group: bool = False) -> None:
        try:
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            content = parsing.clean_content(data)
            if not content:
                return

            chat_id, user_id, chat_type = parsing.resolve_route(data, is_group)
            self._chat_type_cache[chat_id] = chat_type
            await self.intake.publish(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
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
