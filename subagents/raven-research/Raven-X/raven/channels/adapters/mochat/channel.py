"""Mochat adapter — Socket.IO primary transport with an HTTP polling fallback.

Subscribes to sessions (DMs) and panels (groups) over Socket.IO; when the
socket is down it falls back to per-target HTTP watch/poll workers. Inbound
events are deduped, cursor-tracked, optionally debounced, and mention-gated.
Pure decisions live in :mod:`.parsing`; the REST endpoints in :mod:`.api`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from raven.channels.adapters.mochat import parsing as mp
from raven.channels.adapters.mochat.api import MochatAPI
from raven.channels.adapters.mochat.cursors import CursorStore
from raven.channels.adapters.mochat.pipeline import Dedup, DelayBuffer
from raven.channels.adapters.mochat.transport import SocketTransport
from raven.channels.base import ChannelBase
from raven.channels.errors import retryable_http, transient_network
from raven.config.paths import get_runtime_subdir
from raven.config.schema import MochatConfig

# notify.* events the socket subscribes to; inbox.append is session-routed,
# the message.* family is panel-routed.
_NOTIFY_EVENTS = (
    "notify:chat.inbox.append",
    "notify:chat.message.add",
    "notify:chat.message.update",
    "notify:chat.message.recall",
    "notify:chat.message.delete",
)


class MochatChannel(ChannelBase):
    """Mochat channel: Socket.IO primary, HTTP polling fallback."""

    name = "mochat"
    display_name = "Mochat"

    config: MochatConfig

    def __init__(self, config: MochatConfig):
        super().__init__(config)
        self._api = MochatAPI(config)
        self._transport = SocketTransport(config, self._socket_handlers())
        self._ws_connected = self._ws_ready = False

        self._cursors = CursorStore(get_runtime_subdir("mochat"))

        self._session_set: set[str] = set()
        self._panel_set: set[str] = set()
        self._auto_discover_sessions = self._auto_discover_panels = False

        self._cold_sessions: set[str] = set()
        self._session_by_converse: dict[str, str] = {}

        self._dedup = Dedup()
        self._delays = DelayBuffer(
            delay_ms=lambda: self.config.reply_delay_ms,
            flush_cb=lambda *args: self._dispatch_entries(*args),
        )

        self._fallback_mode = False
        self._session_fallback_tasks: dict[str, asyncio.Task] = {}
        self._panel_fallback_tasks: dict[str, asyncio.Task] = {}
        self._refresh_task: asyncio.Task | None = None
        self._target_locks: dict[str, asyncio.Lock] = {}

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.claw_token:
            logger.error("Mochat claw_token not configured")
            return
        self._running = True
        await self._api.open()
        await self._cursors.load()
        self._seed_targets_from_config()
        await self._refresh_targets(subscribe_new=False)

        if not await self._transport.connect():
            await self._ensure_fallback_workers()

        self._refresh_task = asyncio.create_task(self._refresh_loop())
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        await self._stop_fallback_workers()
        await self._delays.cancel_all()
        await self._transport.close()
        await self._cursors.close()
        await self._api.close()
        self._ws_connected = self._ws_ready = False

    # ── config seeding ────────────────────────────────────────────────

    def _seed_targets_from_config(self) -> None:
        sessions, self._auto_discover_sessions = self._normalize_id_list(self.config.sessions)
        panels, self._auto_discover_panels = self._normalize_id_list(self.config.panels)
        self._session_set.update(sessions)
        self._panel_set.update(panels)
        self._cold_sessions.update(s for s in sessions if s not in self._cursors)

    @staticmethod
    def _normalize_id_list(values: list[str]) -> tuple[list[str], bool]:
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        return sorted({v for v in cleaned if v != "*"}), "*" in cleaned

    # ── socket events (decisions; the pipe itself lives in transport.py) ──

    def _socket_handlers(self) -> dict[str, Any]:
        """Event-handler table injected into SocketTransport. Routing and the
        connect/disconnect decisions stay here; the transport is a dumb pipe."""
        handlers: dict[str, Any] = {
            "connect": self._on_socket_connect,
            "disconnect": self._on_socket_disconnect,
            "connect_error": self._on_socket_connect_error,
            "claw.session.events": self._on_session_events,
            "claw.panel.events": self._on_panel_events,
        }
        for event_name in _NOTIFY_EVENTS:
            handlers[event_name] = self._make_notify_handler(event_name)
        return handlers

    async def _on_socket_connect_error(self, data: Any) -> None:
        logger.error("Mochat websocket connect error: {}", data)

    async def _on_session_events(self, payload: dict[str, Any]) -> None:
        await self._handle_watch_payload(payload, "session")

    async def _on_panel_events(self, payload: dict[str, Any]) -> None:
        await self._handle_watch_payload(payload, "panel")

    async def _on_socket_connect(self) -> None:
        self._ws_connected, self._ws_ready = True, False
        logger.info("Mochat websocket connected")
        self._ws_ready = await self._subscribe_all()
        await (self._stop_fallback_workers() if self._ws_ready else self._ensure_fallback_workers())

    async def _on_socket_disconnect(self) -> None:
        if not self._running:
            return
        self._ws_connected = self._ws_ready = False
        logger.warning("Mochat websocket disconnected")
        await self._ensure_fallback_workers()

    def _make_notify_handler(self, event_name: str):
        async def handler(payload: Any) -> None:
            if event_name == "notify:chat.inbox.append":
                await self._on_inbox_append(payload)
            else:
                await self._on_panel_notify(payload)

        return handler

    # ── subscription ──────────────────────────────────────────────────

    async def _subscribe_all(self) -> bool:
        ok_sessions = await self._subscribe_sessions(sorted(self._session_set))
        ok_panels = await self._subscribe_panels(sorted(self._panel_set))
        if self._auto_discover_sessions or self._auto_discover_panels:
            await self._refresh_targets(subscribe_new=True)
        return ok_sessions and ok_panels

    async def _subscribe_sessions(self, session_ids: list[str]) -> bool:
        if not session_ids:
            return True
        self._cold_sessions.update(s for s in session_ids if s not in self._cursors)
        ack = await self._transport.request(
            "com.claw.im.subscribeSessions",
            {
                "sessionIds": session_ids,
                "cursors": self._cursors.snapshot(),
                "limit": self.config.watch_limit,
            },
        )
        if not ack.get("result"):
            logger.error("Mochat subscribeSessions failed: {}", ack.get("message", "unknown error"))
            return False
        for item in self._ack_items(ack.get("data")):
            await self._handle_watch_payload(item, "session")
        return True

    async def _subscribe_panels(self, panel_ids: list[str]) -> bool:
        if not panel_ids and not self._auto_discover_panels:
            return True
        ack = await self._transport.request("com.claw.im.subscribePanels", {"panelIds": panel_ids})
        if not ack.get("result"):
            logger.error("Mochat subscribePanels failed: {}", ack.get("message", "unknown error"))
            return False
        return True

    @staticmethod
    def _ack_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        if isinstance(data, dict):
            sessions = data.get("sessions")
            if isinstance(sessions, list):
                return [i for i in sessions if isinstance(i, dict)]
            if "sessionId" in data:
                return [data]
        return []

    # ── target discovery / refresh ────────────────────────────────────

    async def _refresh_loop(self) -> None:
        interval_s = max(1.0, self.config.refresh_interval_ms / 1000.0)
        while self._running:
            await asyncio.sleep(interval_s)
            try:
                await self._refresh_targets(subscribe_new=self._ws_ready)
            except Exception as e:
                logger.warning("Mochat refresh failed: {}", e)
            if self._fallback_mode:
                await self._ensure_fallback_workers()

    async def _refresh_targets(self, subscribe_new: bool) -> None:
        if self._auto_discover_sessions:
            await self._refresh_sessions_directory(subscribe_new)
        if self._auto_discover_panels:
            await self._refresh_panels(subscribe_new)

    async def _refresh_sessions_directory(self, subscribe_new: bool) -> None:
        try:
            response = await self._api.list_sessions()
        except Exception as e:
            logger.warning("Mochat listSessions failed: {}", e)
            return
        sessions = response.get("sessions")
        if not isinstance(sessions, list):
            return
        new_ids: list[str] = []
        for entry in sessions:
            if not isinstance(entry, dict):
                continue
            sid = mp.str_field(entry, "sessionId")
            if not sid:
                continue
            if sid not in self._session_set:
                self._session_set.add(sid)
                new_ids.append(sid)
                if sid not in self._cursors:
                    self._cold_sessions.add(sid)
            if converse_id := mp.str_field(entry, "converseId"):
                self._session_by_converse[converse_id] = sid
        await self._on_new_targets(new_ids, subscribe_new, self._subscribe_sessions)

    async def _refresh_panels(self, subscribe_new: bool) -> None:
        try:
            response = await self._api.get_groups()
        except Exception as e:
            logger.warning("Mochat getWorkspaceGroup failed: {}", e)
            return
        panels = response.get("panels")
        if not isinstance(panels, list):
            return
        new_ids: list[str] = []
        for entry in panels:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if isinstance(kind, int) and kind != 0:
                continue
            pid = mp.str_field(entry, "id", "_id")
            if pid and pid not in self._panel_set:
                self._panel_set.add(pid)
                new_ids.append(pid)
        await self._on_new_targets(new_ids, subscribe_new, self._subscribe_panels)

    async def _on_new_targets(self, new_ids: list[str], subscribe_new: bool, subscribe) -> None:
        if not new_ids:
            return
        if self._ws_ready and subscribe_new:
            await subscribe(new_ids)
        if self._fallback_mode:
            await self._ensure_fallback_workers()

    # ── HTTP polling fallback ─────────────────────────────────────────

    async def _ensure_fallback_workers(self) -> None:
        if not self._running:
            return
        self._fallback_mode = True
        for sid in sorted(self._session_set):
            self._spawn_worker(self._session_fallback_tasks, sid, self._session_watch_worker)
        for pid in sorted(self._panel_set):
            self._spawn_worker(self._panel_fallback_tasks, pid, self._panel_poll_worker)

    def _spawn_worker(self, registry: dict[str, asyncio.Task], target_id: str, worker) -> None:
        existing = registry.get(target_id)
        if not existing or existing.done():
            registry[target_id] = asyncio.create_task(worker(target_id))

    async def _stop_fallback_workers(self) -> None:
        self._fallback_mode = False
        tasks = [*self._session_fallback_tasks.values(), *self._panel_fallback_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._session_fallback_tasks.clear()
        self._panel_fallback_tasks.clear()

    async def _session_watch_worker(self, session_id: str) -> None:
        while self._running and self._fallback_mode:
            try:
                payload = await self._api.watch_session(
                    session_id,
                    self._cursors.get(session_id),
                    self.config.watch_timeout_ms,
                    self.config.watch_limit,
                )
                await self._handle_watch_payload(payload, "session")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Mochat watch fallback error ({}): {}", session_id, e)
                await asyncio.sleep(max(0.1, self.config.retry_delay_ms / 1000.0))

    async def _panel_poll_worker(self, panel_id: str) -> None:
        sleep_s = max(1.0, self.config.refresh_interval_ms / 1000.0)
        while self._running and self._fallback_mode:
            try:
                response = await self._api.panel_messages(panel_id, min(100, max(1, self.config.watch_limit)))
                for message in reversed(response.get("messages") or []):
                    if not isinstance(message, dict):
                        continue
                    event = mp.make_synthetic_event(
                        message_id=str(message.get("messageId") or ""),
                        author=str(message.get("author") or ""),
                        content=message.get("content"),
                        meta=message.get("meta"),
                        group_id=str(response.get("groupId") or ""),
                        converse_id=panel_id,
                        timestamp=message.get("createdAt"),
                        author_info=message.get("authorInfo"),
                    )
                    await self._process_inbound_event(panel_id, event, "panel")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Mochat panel polling error ({}): {}", panel_id, e)
            await asyncio.sleep(sleep_s)

    # ── inbound: watch payloads ───────────────────────────────────────

    async def _handle_watch_payload(self, payload: dict[str, Any], target_kind: str) -> None:
        if not isinstance(payload, dict):
            return
        target_id = mp.str_field(payload, "sessionId")
        if not target_id:
            return
        async with self._lock_for(target_kind, target_id):
            is_session = target_kind == "session"
            cursor = payload.get("cursor")
            if is_session and isinstance(cursor, int) and cursor >= 0:
                self._cursors.mark(target_id, cursor)

            events = payload.get("events")
            if not isinstance(events, list):
                return
            # A cold session's first frame is a backlog drain — record it as
            # seen (cursor already advanced) but don't replay it to the agent.
            if is_session and target_id in self._cold_sessions:
                self._cold_sessions.discard(target_id)
                return

            for event in events:
                if not isinstance(event, dict):
                    continue
                seq = event.get("seq")
                if is_session and isinstance(seq, int) and seq > self._cursors.get(target_id):
                    self._cursors.mark(target_id, seq)
                if event.get("type") == "message.add":
                    await self._process_inbound_event(target_id, event, target_kind)

    def _lock_for(self, target_kind: str, target_id: str) -> asyncio.Lock:
        return self._target_locks.setdefault(f"{target_kind}:{target_id}", asyncio.Lock())

    # ── inbound: per-message processing ───────────────────────────────

    async def _process_inbound_event(self, target_id: str, event: dict[str, Any], target_kind: str) -> None:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        author = mp.str_field(payload, "author")
        if not author or (self.config.agent_user_id and author == self.config.agent_user_id):
            return
        if not self.is_allowed(author):
            return

        seen_key = f"{target_kind}:{target_id}"
        message_id = mp.str_field(payload, "messageId")
        if message_id and self._dedup.seen(seen_key, message_id):
            return

        was_mentioned = mp.resolve_was_mentioned(payload, self.config.agent_user_id)
        require_mention, use_delay = mp.mention_gate(
            self.config, target_kind, target_id, mp.str_field(payload, "groupId")
        )
        if require_mention and not was_mentioned and not use_delay:
            return

        entry = mp.build_entry(payload, event.get("timestamp"))
        if not use_delay:
            await self._dispatch_entries(target_id, target_kind, [entry], was_mentioned)
        elif was_mentioned:
            await self._delays.flush_now(seen_key, target_id, target_kind, entry)
        else:
            await self._delays.enqueue(seen_key, target_id, target_kind, entry)

    async def _dispatch_entries(
        self, target_id: str, target_kind: str, entries: list[mp.MochatBufferedEntry], was_mentioned: bool
    ) -> None:
        if not entries:
            return
        last = entries[-1]
        is_group = bool(last.group_id)
        await self.intake.publish(
            sender_id=last.author,
            chat_id=target_id,
            content=mp.build_buffered_body(entries, is_group) or "[empty message]",
            metadata={
                "message_id": last.message_id,
                "timestamp": last.timestamp,
                "is_group": is_group,
                "group_id": last.group_id,
                "sender_name": last.sender_name,
                "sender_username": last.sender_username,
                "target_kind": target_kind,
                "was_mentioned": was_mentioned,
                "buffered_count": len(entries),
            },
        )

    # ── inbound: notify.* events ──────────────────────────────────────

    async def _on_panel_notify(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        group_id = mp.str_field(payload, "groupId")
        panel_id = mp.str_field(payload, "converseId", "panelId")
        if not group_id or not panel_id:
            return
        if self._panel_set and panel_id not in self._panel_set:
            return
        event = mp.make_synthetic_event(
            message_id=str(payload.get("_id") or payload.get("messageId") or ""),
            author=str(payload.get("author") or ""),
            content=payload.get("content"),
            meta=payload.get("meta"),
            group_id=group_id,
            converse_id=panel_id,
            timestamp=payload.get("createdAt"),
            author_info=payload.get("authorInfo"),
        )
        await self._process_inbound_event(panel_id, event, "panel")

    async def _on_inbox_append(self, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return
        detail = payload.get("payload")
        if not isinstance(detail, dict) or mp.str_field(detail, "groupId"):
            return
        converse_id = mp.str_field(detail, "converseId")
        if not converse_id:
            return
        session_id = await self._resolve_session(converse_id)
        if not session_id:
            return
        event = mp.make_synthetic_event(
            message_id=str(detail.get("messageId") or payload.get("_id") or ""),
            author=str(detail.get("messageAuthor") or ""),
            content=str(detail.get("messagePlainContent") or detail.get("messageSnippet") or ""),
            meta={"source": "notify:chat.inbox.append", "converseId": converse_id},
            group_id="",
            converse_id=converse_id,
            timestamp=payload.get("createdAt"),
        )
        await self._process_inbound_event(session_id, event, "session")

    async def _resolve_session(self, converse_id: str) -> str | None:
        """Map a converseId to a sessionId, refreshing the directory once on a
        cache miss before giving up."""
        session_id = self._session_by_converse.get(converse_id)
        if not session_id:
            await self._refresh_sessions_directory(self._ws_ready)
            session_id = self._session_by_converse.get(converse_id)
        return session_id

    # ── cursor persistence ────────────────────────────────────────────

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self.config.claw_token:
            logger.warning("Mochat claw_token missing, skip send")
            return
        parts = [content.strip()] if content and content.strip() else []
        parts.extend(m for m in (media or []) if isinstance(m, str) and m.strip())
        content = "\n".join(parts).strip()
        if not content:
            return

        target = mp.resolve_target(chat_id)
        if not target.id:
            logger.warning("Mochat outbound target is empty")
            return
        is_panel = (target.is_panel or target.id in self._panel_set) and not target.id.startswith("session_")
        try:
            if is_panel:
                await self._api.send_panel(target.id, content)
            else:
                await self._api.send_session(target.id, content)
        except Exception as e:
            if retryable_http(e) or transient_network(e):
                raise  # let manager._send_with_retry back off and retry
            logger.error("Failed to send Mochat message: {}", e)
