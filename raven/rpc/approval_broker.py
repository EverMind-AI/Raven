"""Runtime-owned approval round-trip for protected shell commands.

The permission gate reaches this broker when a call lands on the ask tier.
The broker mints an ``approval_id``, emits ``approval.request`` to the TUI, and
blocks that tool call until the user answers or the backend hard limit expires.
The broker grants nothing itself: it carries the human's choice back -- once,
for this session, or a prefix rule to persist -- and the gate is what remembers
or writes. ``suggested_pattern`` on the request is the prefix the gate found
safe to offer; a client that shows no editor for it simply never sends
``allow_always``.

Timeouts have two layers:

* ``visible_timeout_s`` is serialized as ``expires_at``. The TUI owns the
  countdown and fail-closes its overlay at that deadline.
* ``hard_timeout_s`` is the backend ceiling. It is slightly longer so a choice
  made before the visible deadline can survive event-loop or RPC transport lag.

The frontend timer is not authoritative cleanup: the process may be suspended,
the socket may disconnect, or a notification may arrive late. Therefore every
backend outcome emits ``approval.closed`` in ``finally``. The frontend matches
the id before clearing, so a delayed close cannot dismiss a newer request.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

from raven.contracts.permissions import ApprovalChoice, ApprovalOutcome

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _PendingApproval:
    conversation_id: str
    future: asyncio.Future[tuple[str, str, str]]


class ApprovalBroker:
    """Coordinate one-shot TUI approvals without delegating authority to the model."""

    def __init__(
        self,
        send_frame: SendFrame,
        *,
        visible_timeout_s: float = 30.0,
        hard_timeout_s: float = 35.0,
    ) -> None:
        if visible_timeout_s <= 0 or hard_timeout_s < visible_timeout_s:
            raise ValueError("approval timeouts must satisfy 0 < visible <= hard")
        # The frontend expires first; the extra backend window lets a response
        # chosen before the visible deadline survive transport/event-loop lag.
        self._send_frame = send_frame
        self._visible_timeout_s = visible_timeout_s
        self._hard_timeout_s = hard_timeout_s
        self._pending: dict[str, _PendingApproval] = {}

    async def await_approval(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        command: str,
        description: str,
        suggested_pattern: str = "",
    ) -> ApprovalOutcome:
        """Wait for an approval decision and fail closed on every error path.

        A grant comes back as the human chose it: once, for this session, or
        with the pattern to persist. User denial, visible timeout forwarded by
        the TUI, backend timeout, connection failure, and broker cancellation
        all resolve to a deny; ``DENY_STOP`` is the one choice that additionally
        ends the turn, and only a human's click can produce it. Exceptions are
        contained here because an approval transport failure must never turn
        into tool execution or leave the agent loop waiting indefinitely.
        """
        approval_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        created_at = time.time()
        close_reason = "cancelled"
        request_sent = False
        self._pending[approval_id] = _PendingApproval(
            conversation_id=conversation_id,
            future=future,
        )
        try:
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "approval.request",
                    "params": {
                        "approval_id": approval_id,
                        "conversation_id": conversation_id,
                        "turn_id": turn_id,
                        "tool_call_id": tool_call_id,
                        "command": command,
                        "description": description,
                        "action_digest": hashlib.sha256(command.encode()).hexdigest(),
                        "suggested_pattern": suggested_pattern,
                        "created_at": created_at,
                        "expires_at": created_at + self._visible_timeout_s,
                    },
                }
            )
            request_sent = True
            choice, feedback, pattern = await asyncio.wait_for(future, self._hard_timeout_s)
            close_reason = choice
            try:
                return ApprovalOutcome(choice=ApprovalChoice(choice), feedback=feedback, pattern=pattern)
            except ValueError:
                # Not a wire choice, so not a person's answer: `cancel_all` puts
                # the synthetic "cancelled" here during teardown, and a client
                # sending something outside the enum said nothing this side can
                # read. Both fail closed, and neither is a refusal.
                return ApprovalOutcome(choice=ApprovalChoice.DENY, answered=False)
        except TimeoutError:
            close_reason = "timeout"
            # Still a deny -- failing closed is the point -- but nobody said so.
            return ApprovalOutcome(choice=ApprovalChoice.DENY, answered=False)
        except Exception:
            close_reason = "error"
            logger.exception("approval_broker: request failed for {}", approval_id)
            return ApprovalOutcome(choice=ApprovalChoice.DENY, answered=False)
        finally:
            self._pending.pop(approval_id, None)
            if request_sent:
                try:
                    # Frontend timers are best-effort (a suspended terminal may
                    # never fire), so every backend outcome closes the overlay.
                    await self._send_frame(
                        {
                            "jsonrpc": "2.0",
                            "method": "approval.closed",
                            "params": {
                                "approval_id": approval_id,
                                "conversation_id": conversation_id,
                                "reason": close_reason,
                            },
                        }
                    )
                except Exception:
                    logger.exception("approval_broker: close notification failed for {}", approval_id)

    def resolve(
        self,
        approval_id: str,
        choice: str,
        *,
        conversation_id: str,
        feedback: str = "",
        pattern: str = "",
    ) -> bool:
        """Resolve a live request only when both opaque id and conversation match.

        Returning ``False`` for stale, duplicate, cross-conversation, or invalid
        responses makes late UI input harmless and keeps resolution idempotent.
        ``feedback`` rides along on a refusal for the model to read; it is
        clipped rather than refused, because a long sentence is still an answer.
        ``allow_always`` without a pattern is not an answer this side can act
        on, so it is refused here rather than turned into a plain allow.
        """
        if choice not in {"allow", "allow_session", "allow_always", "deny", "deny_stop"}:
            return False
        if choice == "allow_always" and not pattern.strip():
            return False
        pending = self._pending.get(approval_id)
        if pending is None or pending.conversation_id != conversation_id or pending.future.done():
            return False
        pending.future.set_result((choice, feedback[:2000], pattern.strip()[:500]))
        return True

    def cancel_all(self) -> None:
        """Fail-close pending approvals during RPC teardown or TUI disconnect.

        "cancelled" is not a wire choice: the awaiting side maps it onto a deny
        while the closed-notification keeps naming the real cause.
        """
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(("cancelled", "", ""))


__all__ = ["ApprovalBroker", "SendFrame"]
