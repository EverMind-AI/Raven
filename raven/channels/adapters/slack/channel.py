"""Slack channel — slack_sdk Socket Mode in, Web API out.

Orchestration only: receives events over the Socket Mode websocket, applies the
pure decisions from :mod:`.parsing`, and replies via the async Web client.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from slack_sdk.errors import SlackApiError, SlackClientNotConnectedError
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from raven.channels.adapters.slack import parsing
from raven.channels.base import ChannelBase
from raven.channels.errors import transient_network
from raven.config.schema import SlackConfig


def _transient_slack(err: Exception) -> bool:
    """Network drops, rate limits (429) and 5xx are worth a manager retry."""
    if transient_network(err) or isinstance(err, SlackClientNotConnectedError):
        return True
    if isinstance(err, SlackApiError):
        status = getattr(err.response, "status_code", None)
        return status == 429 or (status is not None and status >= 500)
    return False


class SlackChannel(ChannelBase):
    """Slack channel using Socket Mode."""

    config: SlackConfig
    name = "slack"
    display_name = "Slack"

    def __init__(self, config: SlackConfig):
        super().__init__(config)
        self._stop_event = asyncio.Event()
        self._web_client: AsyncWebClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._bot_user_id: str | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot/app token not configured")
            return
        if self.config.mode != "socket":
            logger.error("Unsupported Slack mode: {}", self.config.mode)
            return

        self._running = True
        self._stop_event = asyncio.Event()   # fresh per start (restart-safe)
        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token, web_client=self._web_client
        )
        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info("Slack bot connected as {}", self._bot_user_id)
        except Exception as e:
            logger.warning("Slack auth_test failed: {}", e)

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()
        await self._stop_event.wait()

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                logger.warning("Slack socket close failed: {}", e)
            self._socket_client = None

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._web_client:
            logger.warning("Slack client not running")
            return

        try:
            media = media or []
            # Slack rejects empty text; send a single blank line when there's
            # neither text nor files, but keep media-only messages media-only.
            if content or not media:
                await self._web_client.chat_postMessage(
                    channel=chat_id,
                    text=parsing.to_mrkdwn(content) if content else " ",
                )
            for media_path in media:
                try:
                    await self._web_client.files_upload_v2(
                        channel=chat_id, file=media_path
                    )
                except Exception as e:
                    if _transient_slack(e):
                        raise
                    logger.error("Failed to upload file {}: {}", media_path, e)
        except Exception as e:
            if _transient_slack(e):
                raise  # let manager._send_with_retry back off and retry
            logger.error("Error sending Slack message: {}", e)

    # ── inbound ───────────────────────────────────────────────────────

    async def _on_socket_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        if req.type != "events_api":
            return
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        event = (req.payload or {}).get("event") or {}
        event_type = event.get("type")
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")
        text = event.get("text") or ""

        if event.get("subtype"):
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return
        if parsing.is_duplicate_mention(event_type, text, self._bot_user_id):
            return
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""
        if not parsing.sender_permitted(self.config, sender_id, chat_id, channel_type):
            return
        if not self.is_allowed(sender_id):   # allow_from gate before the :eyes: react
            return
        if channel_type != "im" and not parsing.should_respond_in_channel(
            self.config, event_type, text, chat_id, self._bot_user_id
        ):
            return

        text = parsing.strip_bot_mention(text, self._bot_user_id)

        thread_ts = event.get("thread_ts")
        if self.config.reply_in_thread and not thread_ts:
            thread_ts = event.get("ts")

        await self._react(chat_id, event.get("ts"))

        session_key = f"slack:{chat_id}:{thread_ts}" if thread_ts and channel_type != "im" else None
        try:
            await self.intake.publish(
                sender_id=sender_id, chat_id=chat_id, content=text,
                metadata={"slack": {"event": event, "thread_ts": thread_ts, "channel_type": channel_type}},
                session_key=session_key,
            )
        except Exception:
            logger.exception("Error handling Slack message from {}", sender_id)

    async def _react(self, chat_id: str, ts: str | None) -> None:
        """Best-effort :eyes: acknowledgement on the triggering message."""
        if not self._web_client or not ts:
            return
        try:
            await self._web_client.reactions_add(channel=chat_id, name=self.config.react_emoji, timestamp=ts)
        except Exception as e:
            logger.debug("Slack reactions_add failed: {}", e)
