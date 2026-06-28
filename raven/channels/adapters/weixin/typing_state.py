"""Typing-indicator subsystem for the weixin channel.

Owns the per-chat ticket cache (TTL'd with jittered refresh; exponential
backoff on failure while still serving the stale ticket) and the keepalive
tasks. Talks to the wire only through the injected ``post`` callable — it
never holds the channel. Ticket state round-trips through ``snapshot`` /
``restore`` so the channel persists it with the rest of its account state.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from loguru import logger

from raven.channels.adapters.weixin import protocol as p

PostFn = Callable[..., Awaitable[dict]]


class TypingIndicator:
    """Per-chat typing tickets + keepalive tasks for the iLink API."""

    def __init__(self, post: PostFn):
        self._post = post
        self._tasks: dict[str, asyncio.Task] = {}
        self._tickets: dict[str, dict[str, Any]] = {}

    # ── ticket-state persistence hooks ────────────────────────────────

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return self._tickets

    def restore(self, tickets: dict[str, dict[str, Any]]) -> None:
        self._tickets = tickets

    # ── ticket policy ─────────────────────────────────────────────────

    async def ticket_for(self, user_id: str, context_token: str = "") -> str:
        now = time.time()
        entry = self._tickets.get(user_id)
        if entry and now < float(entry.get("next_fetch_at", 0)):
            return str(entry.get("ticket", "") or "")

        data = await self._post("ilink/bot/getconfig", {
            "ilink_user_id": user_id, "context_token": context_token or None,
        })
        if data.get("ret", 0) == 0:
            ticket = str(data.get("typing_ticket", "") or "")
            self._tickets[user_id] = {
                "ticket": ticket, "ever_succeeded": True,
                "next_fetch_at": now + random.random() * p.TYPING_TICKET_TTL_S,
                "retry_delay_s": p.CONFIG_CACHE_INITIAL_RETRY_S,
            }
            return ticket

        prev = float(entry.get("retry_delay_s", p.CONFIG_CACHE_INITIAL_RETRY_S)) if entry else p.CONFIG_CACHE_INITIAL_RETRY_S
        delay = min(prev * 2, p.CONFIG_CACHE_MAX_RETRY_S)
        if entry:
            entry.update(next_fetch_at=now + delay, retry_delay_s=delay)
            return str(entry.get("ticket", "") or "")
        self._tickets[user_id] = {
            "ticket": "", "ever_succeeded": False,
            "next_fetch_at": now + p.CONFIG_CACHE_INITIAL_RETRY_S,
            "retry_delay_s": p.CONFIG_CACHE_INITIAL_RETRY_S,
        }
        return ""

    # ── wire + keepalive ──────────────────────────────────────────────

    async def _send(self, user_id: str, ticket: str, status: int) -> None:
        if ticket:
            await self._post("ilink/bot/sendtyping", {
                "ilink_user_id": user_id, "typing_ticket": ticket, "status": status,
            })

    async def _keepalive(self, user_id: str, ticket: str) -> None:
        # Cancellation is the only exit: the old stop Event was always set
        # together with task.cancel(), so it carried no extra meaning.
        while True:
            await asyncio.sleep(p.TYPING_KEEPALIVE_INTERVAL_S)
            with suppress(Exception):
                await self._send(user_id, ticket, p.TYPING_STATUS_TYPING)

    async def start(self, chat_id: str, context_token: str = "") -> None:
        if not chat_id:
            return
        await self.stop(chat_id, clear_remote=False)
        try:
            ticket = await self.ticket_for(chat_id, context_token)
            if not ticket:
                return
            await self._send(chat_id, ticket, p.TYPING_STATUS_TYPING)
        except Exception as e:
            logger.debug("typing start failed for {}: {}", chat_id, e)
            return
        self._tasks[chat_id] = asyncio.create_task(self._keepalive(chat_id, ticket))

    async def stop(self, chat_id: str, *, clear_remote: bool) -> None:
        task = self._tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if not clear_remote:
            return
        entry = self._tickets.get(chat_id)
        ticket = str(entry.get("ticket", "") or "") if isinstance(entry, dict) else ""
        if ticket:
            with suppress(Exception):
                await self._send(chat_id, ticket, p.TYPING_STATUS_CANCEL)

    async def stop_all(self, *, clear_remote: bool = False) -> None:
        for chat_id in list(self._tasks):
            await self.stop(chat_id, clear_remote=clear_remote)
