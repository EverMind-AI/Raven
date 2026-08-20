"""WhatsApp channel — talks to a Node.js (baileys) bridge over WebSocket.

The bridge handles the WhatsApp Web protocol; this channel connects to it,
relays outbound sends, and parses inbound frames. Process/build/token concerns
live in :mod:`.bridge`; pure sender/content parsing in :mod:`.parsing`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import OrderedDict

from loguru import logger

from raven import __logo__
from raven.channels.adapters.whatsapp import bridge, parsing
from raven.channels.base import ChannelBase
from raven.channels.contract import Capabilities
from raven.channels.errors import transient_network
from raven.channels.media import safe_name
from raven.config.schema import WhatsAppConfig

_MAX_PROCESSED_IDS = 1000


class WhatsAppChannel(ChannelBase):
    """WhatsApp channel backed by a local Node.js bridge over WebSocket."""

    config: WhatsAppConfig
    name = "whatsapp"
    display_name = "WhatsApp"
    capabilities = Capabilities(interactive_login=True)  # QR pairing via the bridge

    def __init__(self, config: WhatsAppConfig):
        super().__init__(config)
        self._ws = None
        self._connected = False
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._lid_to_phone: dict[str, str] = {}
        self._bridge_token: str | None = None

    def _effective_bridge_token(self) -> str:
        """Resolve the bridge token, minting a local secret on first use."""
        if self._bridge_token is None:
            configured = self.config.bridge_token.strip()
            self._bridge_token = configured or bridge.load_or_create_bridge_token(bridge.bridge_token_path())
        return self._bridge_token

    # ── login (interactive QR via the bridge) ─────────────────────────

    async def login(self, force: bool = False) -> bool:
        from raven.config.paths import get_runtime_subdir

        try:
            bridge_dir = bridge.ensure_bridge_dir()
        except RuntimeError as e:
            logger.error("Bridge setup failed: {}", e)
            return False
        if not shutil.which("npm"):
            logger.error("npm not found. Please install Node.js.")
            return False

        logger.info(f"{__logo__} Starting WhatsApp bridge for QR login...")
        return bridge.run_login(bridge_dir, self._effective_bridge_token(), str(get_runtime_subdir("whatsapp-auth")))

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        import websockets

        logger.info("Connecting to WhatsApp bridge at {}...", self.config.bridge_url)
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.config.bridge_url) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "auth", "token": self._effective_bridge_token()}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")
                    async for frame in ws:
                        try:
                            await self._handle_bridge_message(frame)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("WhatsApp bridge connection error: {}", e)
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return
        text = content
        media = media or []
        if media:
            # The bridge send protocol is text-only; surface the dropped
            # attachments to the user instead of losing them silently.
            logger.warning("WhatsApp bridge send is text-only; {} attachment(s) not sent", len(media))
            notes = "\n".join(
                f"[Attachment not sent: {safe_name(m)}]" for m in media if isinstance(m, str) and m.strip()
            )
            text = f"{text}\n{notes}".strip()
        try:
            await self._ws.send(json.dumps({"type": "send", "to": chat_id, "text": text}, ensure_ascii=False))
        except Exception as e:
            if transient_network(e):
                raise  # ws drop: let manager._send_with_retry back off and retry
            logger.error("Error sending WhatsApp message: {}", e)

    # ── inbound ───────────────────────────────────────────────────────

    async def _handle_bridge_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")
        if msg_type == "message":
            await self._on_inbound(data)
        elif msg_type == "status":
            status = data.get("status")
            logger.info("WhatsApp status: {}", status)
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
        elif msg_type == "qr":
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")
        elif msg_type == "error":
            logger.error("WhatsApp bridge error: {}", data.get("error"))

    async def _on_inbound(self, data: dict) -> None:
        if parsing.should_skip_group(
            data.get("isGroup", False), self.config.group_policy, data.get("wasMentioned", False)
        ):
            return

        phone_id, lid_id, sender_id = parsing.classify_sender(
            data.get("pn", ""), data.get("sender", ""), self._lid_to_phone
        )
        if not self.is_allowed(sender_id):
            return

        message_id = data.get("id", "")
        if message_id:
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None
            while len(self._processed_message_ids) > _MAX_PROCESSED_IDS:
                self._processed_message_ids.popitem(last=False)

        if phone_id and lid_id:
            self._lid_to_phone[lid_id] = phone_id
        logger.info("WhatsApp sender phone={} lid={} -> {}", phone_id or "(none)", lid_id or "(none)", sender_id)

        media_paths = data.get("media") or []
        await self.intake.publish(
            sender_id=sender_id,
            chat_id=data.get("sender", ""),  # full LID/JID, used for replies
            content=parsing.build_inbound_content(data.get("content", ""), media_paths),
            media=media_paths,
            metadata={
                "message_id": message_id,
                "timestamp": data.get("timestamp"),
                "is_group": data.get("isGroup", False),
            },
        )
