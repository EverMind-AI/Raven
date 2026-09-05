"""Email channel — orchestration over the IMAP/SMTP mailbox and pure parsers.

Polls for unread mail (:meth:`start`), tracks reply threading state, and sends
replies (:meth:`send`). Transport lives in :mod:`.mailbox`; parsing in
:mod:`.parsing`.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date
from email.message import EmailMessage
from typing import Any

from loguru import logger

from raven.channels.adapters.email import parsing
from raven.channels.adapters.email.mailbox import EmailMailbox
from raven.channels.base import ChannelBase
from raven.config.schema import EmailConfig

_MAX_SEEN_UIDS = 100_000


class EmailChannel(ChannelBase):
    """Email channel: poll IMAP for unread mail in, reply over SMTP out."""

    config: EmailConfig
    name = "email"
    display_name = "Email"

    def __init__(self, config: EmailConfig):
        super().__init__(config)
        self._stop_event = asyncio.Event()
        self._mailbox = EmailMailbox(config)
        self._last_subject: dict[str, str] = {}
        self._last_message_id: dict[str, str] = {}
        self._seen_uids: set[str] = set()
        self._seen_queue: deque[str] = deque()

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.consent_granted:
            logger.warning(
                "Email channel disabled: consent_granted is false. "
                "Set channels.email.consentGranted=true after explicit user permission."
            )
            return
        if not self._validate_config():
            return

        self._running = True
        self._stop_event = asyncio.Event()  # fresh per start (restart-safe)
        logger.info("Starting Email channel (IMAP polling mode)...")
        poll_seconds = max(5, int(self.config.poll_interval_seconds))
        while self._running:
            try:
                for item in await asyncio.to_thread(self._fetch_new_messages):
                    await self._process_item(item)
            except Exception as e:
                logger.error("Email polling error: {}", e)
            try:
                # Sleep until the next poll, but wake immediately on stop().
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _process_item(self, item: dict[str, Any]) -> None:
        """Gate, record reply-threading state, and publish one parsed mail.

        The allowlist check runs before _last_subject/_last_message_id are
        recorded so a denied sender cannot poison the reply threading used by
        send(). (The IMAP ``\\Seen`` flag is set in bulk at fetch time, before
        the sender is known — per-sender skipping there isn't possible.)
        """
        sender = item["sender"]
        if not self.is_allowed(sender):
            return
        if subject := item.get("subject", ""):
            self._last_subject[sender] = subject
        if message_id := item.get("message_id", ""):
            self._last_message_id[sender] = message_id
        await self.intake.publish(
            sender_id=sender,
            chat_id=sender,
            content=item["content"],
            metadata=item.get("metadata", {}),
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    # ── inbound ───────────────────────────────────────────────────────

    def _fetch_new_messages(self) -> list[dict[str, Any]]:
        """Fetch unread mail, drop already-seen UIDs, and parse each (blocking)."""
        items: list[dict[str, Any]] = []
        for uid, raw in self._mailbox.search_fetch(("UNSEEN",), mark_seen=self.config.mark_seen, limit=0):
            if uid and uid in self._seen_uids:
                continue
            item = parsing.parse_message(raw, self.config.max_body_chars, uid=uid)
            if item is None:
                continue
            if uid:
                self._remember_uid(uid)
            items.append(item)
        return items

    def fetch_messages_between_dates(self, start_date: date, end_date: date, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch messages in [start_date, end_date) — used for historical
        summarization (e.g. 'yesterday')."""
        if end_date <= start_date:
            return []
        criteria = ("SINCE", parsing.format_imap_date(start_date), "BEFORE", parsing.format_imap_date(end_date))
        items: list[dict[str, Any]] = []
        for uid, raw in self._mailbox.search_fetch(criteria, mark_seen=False, limit=max(1, int(limit))):
            if item := parsing.parse_message(raw, self.config.max_body_chars, uid=uid):
                items.append(item)
        return items

    def _remember_uid(self, uid: str) -> None:
        """Track a processed UID, FIFO-capped. mark_seen is the primary dedup;
        this is a safety net against re-processing within a session."""
        if uid in self._seen_uids:
            return
        self._seen_uids.add(uid)
        self._seen_queue.append(uid)
        while len(self._seen_queue) > _MAX_SEEN_UIDS:
            self._seen_uids.discard(self._seen_queue.popleft())

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self.config.consent_granted:
            logger.warning("Skip email send: consent_granted is false")
            return
        if not self.config.smtp_host:
            logger.warning("Email channel SMTP host not configured")
            return
        to_addr = chat_id.strip()
        if not to_addr:
            logger.warning("Email channel missing recipient address")
            return

        # auto_reply_enabled gates only *automatic* replies (to someone who
        # mailed us), not proactive sends.
        is_reply = to_addr in self._last_subject
        if is_reply and not self.config.auto_reply_enabled:
            logger.info("Skip automatic email reply to {}: auto_reply_enabled is false", to_addr)
            return

        subject = parsing.reply_subject(
            self._last_subject.get(to_addr, "Raven reply"), self.config.subject_prefix or "Re: "
        )

        email_msg = EmailMessage()
        email_msg["From"] = self.config.from_address or self.config.smtp_username or self.config.imap_username
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject
        email_msg.set_content(content or "")
        if in_reply_to := self._last_message_id.get(to_addr):
            email_msg["In-Reply-To"] = in_reply_to
            email_msg["References"] = in_reply_to

        try:
            await asyncio.to_thread(self._mailbox.smtp_send, email_msg)
        except Exception as e:
            logger.error("Error sending email to {}: {}", to_addr, e)
            raise

    def _validate_config(self) -> bool:
        missing = [
            name
            for name, value in (
                ("imap_host", self.config.imap_host),
                ("imap_username", self.config.imap_username),
                ("imap_password", self.config.imap_password),
                ("smtp_host", self.config.smtp_host),
                ("smtp_username", self.config.smtp_username),
                ("smtp_password", self.config.smtp_password),
            )
            if not value
        ]
        if missing:
            logger.error("Email channel not configured, missing: {}", ", ".join(missing))
            return False
        return True
