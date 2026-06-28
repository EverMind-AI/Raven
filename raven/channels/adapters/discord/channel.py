"""Discord adapter — receive over the Gateway WebSocket, reply over the REST API.

A thin client over Discord's raw Gateway protocol (HELLO/heartbeat/IDENTIFY,
MESSAGE_CREATE dispatch, RECONNECT/INVALID_SESSION) with a reconnect loop;
outbound goes through the v10 REST API. Gateway delivers each message once,
so no inbound dedup is needed.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import httpx
import websockets
from loguru import logger

from raven.channels.base import ChannelBase
from raven.channels.media import save_media_bytes
from raven.config.schema import DiscordConfig
from raven.utils.helpers import split_message

_API_BASE = "https://discord.com/api/v10"
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_MAX_MESSAGE_LEN = 2000

# Gateway opcodes
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10

# Close-code classification per the Gateway docs ("Reconnect" column):
# false -> fatal, stop instead of reconnect-looping (e.g. 4004 bad token);
# 4007/4009 are reconnectable but invalidate the session -> re-IDENTIFY.
_FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}
_NEW_SESSION_CLOSE_CODES = {4007, 4009}


class DiscordChannel(ChannelBase):
    """Discord channel over the Gateway WebSocket."""

    config: DiscordConfig
    name = "discord"
    display_name = "Discord"

    def __init__(self, config: DiscordConfig):
        super().__init__(config)
        self._ws: Any = None
        self._http: httpx.AsyncClient | None = None
        self._seq: int | None = None
        self._bot_user_id: str | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._session_id: str | None = None
        self._resume_url: str | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.token:
            logger.error("Discord bot token not configured")
            return
        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)
        while self._running:
            url = self._resume_url if self._can_resume() else self.config.gateway_url
            try:
                logger.info("Connecting to Discord gateway...")
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    await self._gateway_loop()
            except asyncio.CancelledError:
                break
            except websockets.exceptions.ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                if code in _FATAL_CLOSE_CODES:
                    logger.error(
                        "Discord gateway closed with fatal code {} ({}); not reconnecting",
                        code, e.rcvd.reason if e.rcvd else "",
                    )
                    self._running = False
                    break
                if code in _NEW_SESSION_CLOSE_CODES:
                    self._reset_session()
                logger.warning("Discord gateway closed (code {}); reconnecting", code)
                if self._running:
                    await asyncio.sleep(5)
            except Exception as e:
                logger.warning("Discord gateway error: {}", e)
                if self._running:
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── gateway ───────────────────────────────────────────────────────

    async def _gateway_loop(self) -> None:
        if not self._ws:
            return
        async for raw in self._ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from Discord gateway: {}", raw[:100])
                continue

            op, event, seq, payload = frame.get("op"), frame.get("t"), frame.get("s"), frame.get("d")
            if seq is not None:
                self._seq = seq

            if op == _OP_HELLO:
                await self._start_heartbeat((payload or {}).get("heartbeat_interval", 45000) / 1000)
                if self._can_resume():
                    await self._resume()
                else:
                    await self._identify()
            elif op == _OP_DISPATCH and event == "READY":
                self._bot_user_id = (payload.get("user") or {}).get("id")
                self._session_id = payload.get("session_id")
                self._resume_url = payload.get("resume_gateway_url")
                logger.info("Discord gateway READY (bot user {})", self._bot_user_id)
            elif op == _OP_DISPATCH and event == "RESUMED":
                logger.info("Discord gateway RESUMED (missed events replayed)")
            elif op == _OP_DISPATCH and event == "MESSAGE_CREATE":
                await self._on_message(payload)
            elif op == _OP_RECONNECT:
                logger.info("Discord gateway requested reconnect; resuming")
                break
            elif op == _OP_INVALID_SESSION:
                if not payload:   # d=false: session unrecoverable, re-identify
                    self._reset_session()
                    # Per the docs: wait a random 1-5s before the fresh
                    # IDENTIFY to avoid identify storms (and 4008s).
                    await asyncio.sleep(random.uniform(1, 5))
                logger.info("Discord session invalidated (resumable={})", bool(payload))
                break

    def _can_resume(self) -> bool:
        return bool(self._session_id and self._resume_url and self._seq is not None)

    def _reset_session(self) -> None:
        self._session_id = None
        self._resume_url = None

    async def _identify(self) -> None:
        if self._ws:
            # A fresh session: heartbeats must not carry the previous
            # session's sequence number.
            self._seq = None
            await self._ws.send(json.dumps({
                "op": _OP_IDENTIFY,
                "d": {
                    "token": self.config.token,
                    "intents": self.config.intents,
                    "properties": {"os": "raven", "browser": "raven", "device": "raven"},
                },
            }))

    async def _resume(self) -> None:
        if self._ws:
            await self._ws.send(json.dumps({
                "op": _OP_RESUME,
                "d": {
                    "token": self.config.token,
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            }))

    async def _start_heartbeat(self, interval_s: float) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        async def loop() -> None:
            # First beat after interval * jitter, per the Gateway docs, to
            # avoid stampeding the gateway on mass reconnects.
            await asyncio.sleep(interval_s * random.random())
            while self._running and self._ws:
                try:
                    await self._ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": self._seq}))
                except Exception as e:
                    logger.warning("Discord heartbeat failed: {}", e)
                    break
                await asyncio.sleep(interval_s)

        self._heartbeat_task = asyncio.create_task(loop())

    # ── inbound ───────────────────────────────────────────────────────

    async def _on_message(self, payload: dict[str, Any]) -> None:
        author = payload.get("author") or {}
        if author.get("bot"):
            return
        sender_id = str(author.get("id", ""))
        channel_id = str(payload.get("channel_id", ""))
        content = payload.get("content") or ""
        guild_id = payload.get("guild_id")
        if not sender_id or not channel_id or not self.is_allowed(sender_id):
            return
        if guild_id is not None and not self._addressed_in_group(payload, content):
            return

        parts = [content] if content else []
        media: list[str] = []
        for att in payload.get("attachments") or []:
            label = await self._fetch_attachment(att)
            parts.append(label.text)
            if label.path:
                media.append(label.path)

        await self._start_typing(channel_id)
        await self.intake.publish(
            sender_id=sender_id,
            chat_id=channel_id,
            content="\n".join(p for p in parts if p) or "[empty message]",
            media=media,
            metadata={
                "message_id": str(payload.get("id", "")),
                "guild_id": guild_id,
                "reply_to": (payload.get("referenced_message") or {}).get("id"),
            },
        )

    class _Attachment:
        __slots__ = ("text", "path")

        def __init__(self, text: str, path: str | None = None):
            self.text = text
            self.path = path

    async def _fetch_attachment(self, att: dict[str, Any]) -> _Attachment:
        url = att.get("url")
        filename = att.get("filename") or "attachment"
        size = att.get("size") or 0
        if not url or not self._http:
            return self._Attachment(f"[attachment: {filename}]")
        if size and size > _MAX_ATTACHMENT_BYTES:
            return self._Attachment(f"[attachment: {filename} - too large]")
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            path = save_media_bytes("discord", resp.content, filename)
            return self._Attachment(f"[attachment: {path}]", str(path))
        except Exception as e:
            logger.warning("Failed to download Discord attachment: {}", e)
            return self._Attachment(f"[attachment: {filename} - download failed]")

    def _addressed_in_group(self, payload: dict[str, Any], content: str) -> bool:
        """Group-channel gating: open responds to all; mention requires the bot
        to be @mentioned (via the mentions array or a <@id> token in content)."""
        policy = self.config.group_policy
        if policy == "open":
            return True
        if policy != "mention":
            return True
        mention_ids = [str(m.get("id")) for m in payload.get("mentions") or []]
        mentioned = bool(self._bot_user_id) and (
            self._bot_user_id in mention_ids
            or f"<@{self._bot_user_id}>" in content
            or f"<@!{self._bot_user_id}>" in content
        )
        if not mentioned:
            logger.debug(
                "Discord group msg not addressed to bot (bot_id={}, mentions={}, content={!r}); ignoring",
                self._bot_user_id, mention_ids, content[:120],
            )
        return mentioned

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._http:
            logger.warning("Discord HTTP client not initialized")
            return
        url = f"{_API_BASE}/channels/{chat_id}/messages"
        headers = {"Authorization": f"Bot {self.config.token}"}
        try:
            sent_media = False
            failed: list[str] = []
            for path in media or []:
                if await self._send_file(url, headers, path):
                    sent_media = True
                else:
                    failed.append(Path(path).name)

            chunks = split_message(content or "", _MAX_MESSAGE_LEN)
            if not chunks and failed and not sent_media:
                chunks = split_message(
                    "\n".join(f"[attachment: {name} - send failed]" for name in failed), _MAX_MESSAGE_LEN
                )
            for chunk in chunks:
                payload: dict[str, Any] = {"content": chunk}
                if not await self._post_retry(url, headers, json=payload):
                    break
        finally:
            await self._stop_typing(chat_id)

    async def _send_file(self, url: str, headers: dict[str, str], file_path: str) -> bool:
        path = Path(file_path)
        if not path.is_file():
            logger.warning("Discord file not found, skipping: {}", file_path)
            return False
        if path.stat().st_size > _MAX_ATTACHMENT_BYTES:
            logger.warning("Discord file too large (>20MB), skipping: {}", path.name)
            return False
        # Read into memory (<=20MB) so _post_retry can re-send on a 429/error
        # retry; a file handle would be at EOF on the second attempt.
        files = {"files[0]": (path.name, path.read_bytes(), "application/octet-stream")}
        ok = await self._post_retry(url, headers, files=files)
        if ok:
            logger.info("Discord file sent: {}", path.name)
        return ok

    async def _post_retry(self, url: str, headers: dict[str, str], **kwargs: Any) -> bool:
        """POST with up to 3 attempts, honoring Discord's 429 retry-after. True on success."""
        assert self._http is not None
        for attempt in range(3):
            try:
                resp = await self._http.post(url, headers=headers, **kwargs)
                if resp.status_code == 429:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                    logger.warning("Discord rate limited, retrying in {}s", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return True
            except Exception as e:
                if attempt == 2:
                    logger.error("Error in Discord POST: {}", e)
                else:
                    await asyncio.sleep(1)
        return False

    # ── typing ────────────────────────────────────────────────────────

    async def _start_typing(self, channel_id: str) -> None:
        await self._stop_typing(channel_id)

        async def loop() -> None:
            url = f"{_API_BASE}/channels/{channel_id}/typing"
            headers = {"Authorization": f"Bot {self.config.token}"}
            while self._running and self._http:
                try:
                    await self._http.post(url, headers=headers)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.debug("Discord typing failed for {}: {}", channel_id, e)
                    return
                await asyncio.sleep(8)

        self._typing_tasks[channel_id] = asyncio.create_task(loop())

    async def _stop_typing(self, channel_id: str) -> None:
        if task := self._typing_tasks.pop(channel_id, None):
            task.cancel()
