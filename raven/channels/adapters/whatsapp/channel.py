"""WhatsApp channel — talks to a Node.js (baileys) bridge over WebSocket.

The bridge handles the WhatsApp Web protocol; this channel connects to it,
relays outbound sends, and parses inbound frames. Process/build/token concerns
live in :mod:`.bridge`; pure sender/content parsing in :mod:`.parsing`.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Any

from loguru import logger

from raven import __logo__
from raven.channels.adapters.whatsapp import bridge, parsing
from raven.channels.base import ChannelBase
from raven.channels.contract import Capabilities
from raven.channels.errors import transient_network
from raven.channels.media import safe_name

_MAX_PROCESSED_IDS = 1000
_RECONNECT_SECONDS = 5
_BRIDGE_READY_SECONDS = 30.0
_LOGIN_POLL_SECONDS = 0.5


class WhatsAppChannel(ChannelBase):
    """WhatsApp channel backed by a local Node.js bridge over WebSocket."""

    config: Any
    name = "whatsapp"
    display_name = "WhatsApp"
    capabilities = Capabilities(interactive_login=True)  # QR pairing via the bridge

    def __init__(self, config: Any):
        super().__init__(config)
        self._ws = None
        self._connected = False
        self._bridge_up = False
        self._bridge_proc: asyncio.subprocess.Process | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._lid_to_phone: dict[str, str] = {}
        self._bridge_token: str | None = None
        # Login QR (when the bridge emits one) exposed for the web UI to render.
        self.pending_qr: str | None = None

    @property
    def connected(self) -> bool:
        """Whether the bridge reports a paired session, as opposed to the
        channel task merely running (which is true before the QR is scanned)."""
        return self._connected

    def _auth_dir(self) -> str:
        from raven.config.paths import get_runtime_subdir

        return str(get_runtime_subdir("whatsapp-auth"))

    def _effective_bridge_token(self) -> str:
        """Resolve the bridge token, minting a local secret on first use."""
        if self._bridge_token is None:
            configured = self.config.bridge_token.strip()
            self._bridge_token = configured or bridge.load_or_create_bridge_token(bridge.bridge_token_path())
        return self._bridge_token

    # ── login (interactive QR via the bridge) ─────────────────────────

    async def login(self, force: bool = False) -> bool:
        """Pair by QR from a terminal: run the same bridge the gateway runs and
        wait until it reports a paired session (the bridge prints the code)."""
        logger.info(f"{__logo__} Starting WhatsApp bridge for QR login...")
        task = asyncio.create_task(self.start())
        try:
            while not task.done() and not self._connected:
                await asyncio.sleep(_LOGIN_POLL_SECONDS)
            paired = self._connected
        finally:
            await self.stop()
            task.cancel()
            (outcome,) = await asyncio.gather(task, return_exceptions=True)
            if isinstance(outcome, Exception):
                logger.error("WhatsApp bridge client failed: {}", outcome)
        return paired

    # ── lifecycle ─────────────────────────────────────────────────────

    async def _ensure_bridge_process(self) -> bool:
        """Make sure something serves ``bridge_url``, starting the bridge as our
        own child when the URL is local and nothing listens there.

        False means the bridge is not usable on this machine (no node, no bridge
        source, a failed build, a child that never listens), which retrying
        would only repeat, or that the channel was stopped while it came up.
        """
        if not bridge.is_local_bridge(self.config.bridge_url):
            return True
        if self._bridge_proc is not None and self._bridge_proc.returncode is None:
            return True
        host, port = bridge.bridge_endpoint(self.config.bridge_url)
        if await bridge.port_is_open(host, port):
            return True
        try:
            bridge_dir = await asyncio.to_thread(bridge.ensure_bridge_dir)
            self._bridge_proc = await bridge.spawn_bridge(
                bridge_dir, self._effective_bridge_token(), self._auth_dir(), port
            )
        except Exception as e:
            logger.error("Cannot run the WhatsApp bridge: {}", e)
            return False
        logger.info("Started the WhatsApp bridge on port {} (pid {})", port, self._bridge_proc.pid)
        try:
            listening = await bridge.wait_for_port(host, port, _BRIDGE_READY_SECONDS)
        except asyncio.CancelledError:
            await self._discard_bridge_process()
            raise
        if not listening:
            logger.error("The WhatsApp bridge never listened on port {}; giving up", port)
            await self._discard_bridge_process()
            return False
        if not self._running:
            # stop() ran while the build/spawn was in flight, so it found no
            # process to stop; this child is ours to clean up or it outlives
            # the gateway holding the WhatsApp session.
            await self._discard_bridge_process()
            return False
        return True

    async def _discard_bridge_process(self) -> None:
        if self._bridge_proc is not None:
            await bridge.terminate_bridge(self._bridge_proc)
            self._bridge_proc = None

    async def start(self) -> None:
        import websockets

        logger.info("Connecting to WhatsApp bridge at {}...", self.config.bridge_url)
        self._running = True
        while self._running:
            if not await self._ensure_bridge_process():
                self._running = False
                return
            if not self._running:
                return  # stopped while the bridge came up, which can take minutes
            try:
                async with websockets.connect(self.config.bridge_url) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "auth", "token": self._effective_bridge_token()}))
                    self._bridge_up = True
                    logger.info("Connected to WhatsApp bridge")
                    async for frame in ws:
                        try:
                            await self._handle_bridge_message(frame)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("WhatsApp bridge connection error: {}", e)
            finally:
                self._bridge_up = False
                self._connected = False
                self._ws = None
            if self._running:
                logger.info("Reconnecting in {} seconds...", _RECONNECT_SECONDS)
                await asyncio.sleep(_RECONNECT_SECONDS)

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        self._bridge_up = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        await self._discard_bridge_process()

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._ws or not self._bridge_up:
            # Raised, not swallowed: the delivery hub retries the send and,
            # when the bridge stays down, counts the reply as dropped.
            raise ConnectionError("WhatsApp bridge is not connected")
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
                raise  # ws drop: let the delivery hub back off and retry
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
                self.pending_qr = None
            elif status == "disconnected":
                self._connected = False
        elif msg_type == "qr":
            self.pending_qr = data.get("qr") or data.get("code")
            logger.info("Scan the QR code (shown in the web UI or the bridge terminal) to connect WhatsApp")
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
