"""Runtime-owned approval round-trip for protected tool calls.

The permission gate reaches this broker when a call lands on the ask tier.
The broker mints an ``approval_id``, emits ``approval.request`` to the client,
and blocks that tool call until the user answers. The broker grants nothing
itself: it carries the human's choice back -- once, for this session, or a
prefix rule to persist -- and the gate is what remembers or writes.
``suggested_pattern`` on the request is the prefix the gate found safe to
offer; a client that shows no editor for it simply never sends ``allow_always``.

The request carries what a prompt is drawn from, beside the action line the
gate already wrote: ``kind`` picks the layout, ``family`` the wording,
``origin`` names who is asking, and ``evidence`` is the tool's own account of
the call. The broker forwards them as given.

A request has no working deadline: it stays open until a person answers it,
the connection goes away (``cancel_all``) or the turn is cancelled. The default
``hard_timeout_s`` is a day -- a floor nobody present ever reaches, there so
that "waits forever" is not a state the runtime can be in -- and a host may set
a shorter ceiling; when it fires the outcome is a deny nobody chose. Every
backend outcome emits ``approval.closed`` in ``finally``, so a client can retire
an overlay whatever ended the request; the client matches the id before
clearing, so a delayed close cannot dismiss a newer request.

``pending`` lists the requests still open, as they were sent: a page that
reloaded lost its sheets, and this is how it draws them again. WHICH of them a
caller may see is the transport's to decide, not this broker's -- the requests
are kept whole here and the RPC boundary filters by who owns the conversation
(``rpc/methods/approval.py``).

A grant that writes a rule leaves a receipt. The gate persists AFTER this
broker's answer has already reached the client (measured: the respond call
returns first), so a client asking to undo cannot ask about "the rule matching
this text" -- at that moment there may be no rule yet, or there may be one the
person wrote themselves long ago. ``record_grant`` is how the gate says what its
write actually did, and ``written_pattern`` is how an undo waits for that word
and learns whether this answer is the reason a rule is on disk.

One question per conversation at a time. Both surfaces draw one prompt per
conversation and replace it when another lands, so a second request sent while
the first is open would take the first off the screen with nobody having
answered it -- and with no deadline behind it, that call would wait forever. A
background sub-agent asking while the main agent's prompt is up is the ordinary
way this happens. The second request is sent once the first is answered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

from raven.contracts.permissions import ApprovalChoice, ApprovalOutcome

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]


#: The floor. A day, not an hour: a person may step away from an open prompt
#: for an afternoon and find it still waiting; nobody waits a day on one.
DEFAULT_HARD_TIMEOUT_S = 24 * 3600.0


#: How long an undo waits for the gate to report what its write did. The gate
#: persists microseconds after the answer, so this is a ceiling on a race rather
#: than a poll interval; past it the undo answers "nothing of mine is on disk".
GRANT_RECEIPT_TIMEOUT_S = 5.0


@dataclass
class _Grant:
    """What one answer's rule-writing did, once the gate has said so."""

    #: Set when the gate reports; until then an undo waits on it.
    reported: asyncio.Event
    pattern: str = ""
    #: True only when this grant is the reason the rule is on disk. A rule the
    #: person already had is not this prompt's to take away.
    written: bool = False


@dataclass
class _PendingApproval:
    conversation_id: str
    future: asyncio.Future[tuple[str, str, str]]
    #: The ``approval.request`` params as sent, so ``pending`` can hand a fresh
    #: page the very frame it missed.
    params: dict[str, Any]


class ApprovalBroker:
    """Coordinate one-shot client approvals without delegating authority to the model."""

    def __init__(
        self,
        send_frame: SendFrame,
        *,
        hard_timeout_s: float | None = DEFAULT_HARD_TIMEOUT_S,
    ) -> None:
        if hard_timeout_s is not None and hard_timeout_s <= 0:
            raise ValueError("hard_timeout_s must be positive when set")
        self._send_frame = send_frame
        self._hard_timeout_s = hard_timeout_s
        self._pending: dict[str, _PendingApproval] = {}
        # The queue behind each conversation's one prompt.
        # ponytail: one lock per conversation ever asked, never reclaimed. A
        # gateway serving thousands of conversations holds thousands of empty
        # locks; drop one when its queue drains if that ever shows up in a heap.
        self._lanes: dict[str, asyncio.Lock] = {}
        # What each answered grant's write did, for the undo. An entry appears
        # when the answer is a persisted grant and is dropped once an undo has
        # read it, so a second undo of the same grant finds nothing.
        self._grants: dict[str, _Grant] = {}

    async def await_approval(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        command: str,
        description: str,
        suggested_pattern: str = "",
        kind: str = "",
        family: str = "",
        origin: str = "",
        origin_name: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> ApprovalOutcome:
        """Wait for an approval decision and fail closed on every error path.

        A grant comes back as the human chose it: once, for this session, or
        with the pattern to persist. User denial, the host's ceiling if it has
        one, connection failure, and broker cancellation all resolve to a deny;
        ``DENY_STOP`` is the one choice that additionally ends the turn, and
        only a human's click can produce it. Exceptions are contained here
        because an approval transport failure must never turn into tool
        execution.
        """
        async with self._lanes.setdefault(conversation_id, asyncio.Lock()):
            return await self._ask(
                conversation_id=conversation_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                command=command,
                description=description,
                suggested_pattern=suggested_pattern,
                kind=kind,
                family=family,
                origin=origin,
                origin_name=origin_name,
                evidence=evidence,
            )

    async def _ask(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        tool_call_id: str,
        command: str,
        description: str,
        suggested_pattern: str,
        kind: str,
        family: str,
        origin: str,
        origin_name: str,
        evidence: dict[str, Any] | None,
    ) -> ApprovalOutcome:
        approval_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        close_reason = "cancelled"
        request_sent = False
        params = {
            "approval_id": approval_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "command": command,
            "description": description,
            "suggested_pattern": suggested_pattern,
            "kind": kind or "unknown",
            "family": family,
            "origin": {"kind": origin, "name": origin_name},
            "evidence": evidence if evidence is not None else {},
        }
        self._pending[approval_id] = _PendingApproval(
            conversation_id=conversation_id,
            future=future,
            params=params,
        )
        try:
            await self._send_frame({"jsonrpc": "2.0", "method": "approval.request", "params": params})
            request_sent = True
            choice, feedback, pattern = await asyncio.wait_for(future, self._hard_timeout_s)
            close_reason = choice
            try:
                answer = ApprovalChoice(choice)
            except ValueError:
                # Not a wire choice, so not a person's answer: `cancel_all` puts
                # the synthetic "cancelled" here during teardown, and a client
                # sending something outside the enum said nothing this side can
                # read. Both fail closed, and neither is a refusal.
                return ApprovalOutcome(choice=ApprovalChoice.DENY, answered=False)
            if answer is ApprovalChoice.ALLOW_ALWAYS:
                # The slot exists before the gate writes, so an undo that
                # arrives first has something to wait on rather than a miss.
                self._grants[approval_id] = _Grant(reported=asyncio.Event())
            return ApprovalOutcome(choice=answer, feedback=feedback, pattern=pattern, approval_id=approval_id)
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
                    # Whatever ended the request, the client hears it, so an
                    # overlay never outlives the question it was asking.
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

    def record_grant(self, approval_id: str, pattern: str, written: bool) -> None:
        """The gate's word on what its write did, for an undo to wait on.

        Ignored for an approval holding no receipt slot: a grant that was not
        ``allow_always``, or one whose undo has already read it.
        """
        grant = self._grants.get(approval_id)
        if grant is None:
            return
        grant.pattern, grant.written = pattern, written
        grant.reported.set()

    async def written_pattern(self, approval_id: str, *, timeout_s: float = GRANT_RECEIPT_TIMEOUT_S) -> str | None:
        """The rule this answer put on disk, or None when it put none there.

        Waits for the gate's word, because the answer reaches the client first.
        Reads the receipt once: a second undo of the same grant finds nothing,
        which is also what stops an undo from reaching a rule some later prompt
        wrote under the same text.
        """
        grant = self._grants.get(approval_id)
        if grant is None:
            return None
        try:
            await asyncio.wait_for(grant.reported.wait(), timeout_s)
        except TimeoutError:
            logger.warning("approval_broker: no grant receipt for {} after {}s", approval_id, timeout_s)
            return None
        finally:
            self._grants.pop(approval_id, None)
        return grant.pattern if grant.written else None

    def pending(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """The requests still waiting, as their ``approval.request`` params were sent.

        All of them, or one conversation's. Copies, so a caller cannot reach
        the broker's own record through the answer.
        """
        return [
            dict(p.params)
            for p in self._pending.values()
            if conversation_id is None or p.conversation_id == conversation_id
        ]

    def cancel_all(self) -> None:
        """Fail-close pending approvals during RPC teardown or TUI disconnect.

        "cancelled" is not a wire choice: the awaiting side maps it onto a deny
        while the closed-notification keeps naming the real cause.
        """
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(("cancelled", "", ""))


__all__ = ["DEFAULT_HARD_TIMEOUT_S", "GRANT_RECEIPT_TIMEOUT_S", "ApprovalBroker", "SendFrame"]
