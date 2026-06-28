"""Inbound intake — the framework service that turns a channel's raw inbound
into a permission-checked spine ``TurnRequest`` submitted to the scheduler.

Injected into adapters (composition, not inheritance): deny-by-policy, then
submit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from raven.auth.allowlist import is_allowed
from raven.spine import ChatType, Media, Origin, Source, TurnRequest


class Intake:
    """Per-channel inbound gate + submitter. One instance per channel.

    ``allow_check`` overrides the default allowlist policy for channels with
    bespoke permission rules (e.g. Telegram's ``<id>|<username>`` matching).

    A permitted message is handed to the spine dispatch (wired via
    ``set_submit``) as a ``TurnRequest`` — channel metadata rides
    ``Source.extras``.
    """

    def __init__(
        self,
        channel_name: str,
        config: Any,
        allow_check: Callable[[str], bool] | None = None,
    ):
        self.channel_name = channel_name
        self.config = config
        self._allow_check = allow_check
        self._submit: Callable[[TurnRequest], Awaitable[None]] | None = None

    def set_submit(self, submit: Callable[[TurnRequest], Awaitable[None]]) -> None:
        """Wire the spine inbound dispatch (gateway). ``publish`` submits a
        TurnRequest through it. The dispatch is control-aware (it intercepts
        /stop and /restart) — Intake stays a dumb gate + builder; the
        control/cancel logic lives where the scheduler and agent are (the
        gateway)."""
        self._submit = submit

    def is_allowed(self, sender_id: str) -> bool:
        """Deny-by-default allowlist check (``*`` = all; empty = deny all),
        unless a custom ``allow_check`` was supplied."""
        if self._allow_check is not None:
            return self._allow_check(sender_id)
        return is_allowed(self.channel_name, sender_id, getattr(self.config, "allow_from", None))

    async def publish(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        """Check permission, then submit the message to the spine dispatch as a
        TurnRequest."""
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender {} on channel {}. "
                "Add them to allowFrom list in config to grant access.",
                sender_id, self.channel_name,
            )
            return

        if self._submit is None:
            logger.error(
                "Intake for channel {} has no spine dispatch wired; dropping "
                "inbound from {}", self.channel_name, sender_id,
            )
            return

        meta = metadata or {}
        await self._submit(
            TurnRequest(
                origin=Origin.USER,
                source=Source(
                    channel=self.channel_name,
                    chat_id=str(chat_id),
                    sender_id=str(sender_id),
                    # Not load-bearing for processing (channels read the real
                    # chat_type from metadata, which rides extras); a best-effort
                    # shape for the spine. group when the channel says so, else DM.
                    chat_type=ChatType.GROUP if meta.get("chat_type") == "group" else ChatType.DM,
                    extras=meta,
                ),
                text=content,
                media=[
                    Media(path=p, mime="application/octet-stream", kind="file")
                    for p in (media or [])
                ],
                # session_key_override -> conversation: run_turn's cid is
                # `conversation or channel:chat_id`.
                conversation=session_key,
            )
        )
