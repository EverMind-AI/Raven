"""QuestionBroker — sync↔async ask-user round-trip keyed by conversation_id.

Generalizes :class:`ConfirmBroker` for the ``ask_user`` tool. The difference is
the key: a confirm mints its own ``request_id`` and that is the only handle the
caller has, whereas an ask_user round-trip is answered by an inbound channel
message whose only handle is the conversation_id. So the pending registry is
keyed by conversation_id; an internal request_id is still minted and carried in
the notification so a frontend that prefers to answer by request_id can.

Like ConfirmBroker it is transport-agnostic: constructed with a notification
emit callable (bound to ``RpcServer.send_frame`` in production), and every
fail-safe path (timeout, cancel, internal error, connection EOF via
:meth:`cancel_all`) resolves to the prompt's ``default`` rather than raising —
the agent loop must always get a string back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _PendingQuestion:
    future: asyncio.Future
    request_id: str
    default: str


class QuestionBroker:
    """Emits ``clarify.request`` notifications and awaits the answer.

    Keyed by conversation_id: at most one question may be pending per
    conversation, because a single turn is serial and cannot ask twice
    concurrently.
    """

    def __init__(self, send_frame: SendFrame) -> None:
        self._send_frame = send_frame
        self._pending: dict[str, _PendingQuestion] = {}
        # Reverse index request_id -> conversation_id so :meth:`reply` can
        # accept either handle.
        self._by_request: dict[str, str] = {}

    async def await_question(
        self,
        conversation_id: str,
        *,
        prompt: str,
        choices: list[str] | None = None,
        default: str = "",
        timeout_s: float = 600.0,
    ) -> str:
        """Emit a ``clarify.request`` and await the matching answer.

        Returns ``default`` on timeout, cancellation, EOF
        (:meth:`cancel_all`), or any internal error — never raises.

        A turn is serial, so a second pending question for the same
        conversation is a programming error: we drop the stale one (fail-safe
        to its default) and replace it, logging the overlap.
        """
        existing = self._pending.get(conversation_id)
        if existing is not None:
            logger.error(
                "question_broker: overlapping question for conversation {}; "
                "fail-safing the stale one",
                conversation_id,
            )
            self._by_request.pop(existing.request_id, None)
            if not existing.future.done():
                existing.future.set_result(existing.default)

        request_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[conversation_id] = _PendingQuestion(
            future=future, request_id=request_id, default=default
        )
        self._by_request[request_id] = conversation_id
        try:
            # ``clarify.request`` is the ui-tui frontend's existing multi-choice
            # prompt contract ({question, choices, request_id} -> ClarifyPrompt ->
            # clarify.respond); the broker reuses it. conversation_id is carried
            # for the gateway channel route (the frontend ignores extra keys).
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "clarify.request",
                    "params": {
                        "conversation_id": conversation_id,
                        "request_id": request_id,
                        "question": prompt,
                        "choices": choices or [],
                    },
                }
            )
            return await asyncio.wait_for(future, timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return default
        except Exception:  # noqa: BLE001 — fail-safe: the loop needs a string back
            logger.exception(
                "question_broker: await_question failed for {}", conversation_id
            )
            return default
        finally:
            # Only retract our own entry: an overlapping question may have
            # already replaced it under the same conversation_id.
            current = self._pending.get(conversation_id)
            if current is not None and current.request_id == request_id:
                self._pending.pop(conversation_id, None)
            self._by_request.pop(request_id, None)

    def pending_req(self, conversation_id: str) -> str | None:
        """Return the pending request_id for a conversation, else ``None``."""
        pending = self._pending.get(conversation_id)
        return pending.request_id if pending is not None else None

    def reply(self, key: str, answer: str) -> bool:
        """Resolve a pending question by conversation_id OR request_id.

        Idempotent: unknown key / already-resolved → ``False``.
        """
        conversation_id = key if key in self._pending else self._by_request.get(key)
        if conversation_id is None:
            return False
        pending = self._pending.get(conversation_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(answer)
        return True

    def cancel_all(self) -> None:
        """Fail-safe every pending question to its default (connection EOF)."""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(pending.default)


__all__ = ["QuestionBroker", "SendFrame"]
