"""An ACP sub-agent's `ask_user`, when it asks outside `elicitation/create`.

Raven-X routes its deep-research clarify through an extension of its own rather
than the spec method: the question leaves as a `session/update` whose
`sessionUpdate` is `ask_user_request`, and the answer goes back as a
`_raven/clarify_respond` request. That agent arms the route only when the client
declared `_meta.raven.askUser` at `initialize`, which is what
`CLIENT_CAPABILITIES` now carries -- unarmed, its tool falls back to ending the
turn on the questions, so this module is the difference between a clarify that
interrupts one turn and one that costs a whole round trip through the caller.

Separate from `elicitor.py` because the two shapes have nothing in common: a
notification out and a request back, against a request answered by its result.
What they do share is the far end -- the same `clarify.request` contract, and the
same per-conversation lock, because both reach one question broker that allows a
single pending question per conversation.

**Every question a run owns is answered, including the ones nobody can put to a
user.** The agent blocks its tool call on the reply for ten minutes before
falling back to the question's default, so silence here is not a no-op: it is
the stall the capability exists to avoid. A background turn and a finished run
both answer with an empty string, which the asking side already reads as "the
user did not answer; proceed with best judgment". A session nobody owns is the
one case that is routed and not answered -- raven cannot answer for a run that
is gone, and the agent falls back to its own timeout (see
`_SessionResponders.dispatch`). The window is a frame landing after the
backend's ``finally`` has detached the responder.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from raven.acp_client import autofill
from raven.acp_client.asker import attribute, current_ask, current_autofill, question_lock

UPDATE_METHOD = "session/update"

ASK_USER_UPDATE_KIND = "ask_user_request"
"""The `sessionUpdate` discriminator a question rides out on.

An extension kind rather than spec surface, mirrored from the agent side. A
client that does not know it ignores it as an unknown update, which is what makes
the capability declaration -- not the frame -- the thing that turns this on.
"""

CLARIFY_RESPOND_METHOD = "_raven/clarify_respond"
"""The extension method the answer goes back on.

Underscore-prefixed per ACP extensibility convention. Params: `requestId` (from
the update frame) or `sessionId`, plus `answer`. The agent replies
`{"delivered": bool}` and never errors on a stale handle, so there is nothing to
retry here.
"""

RESPOND_TIMEOUT_SECONDS = 30.0
"""Budget for handing one answer back.

Short because the agent resolves a pending future and returns -- there is no work
behind this call. A budget at all because the alternative is a task that outlives
the connection it was answering into.
"""

LOCK_WAIT_SECONDS = 600.0
"""How long a queued question waits for its conversation before giving up.

One question's worth of patience, matching what the holder can cost: the lock is
held across a single human round trip, which fail-safes at the broker's own 600s.
Giving up answers empty rather than waiting forever, because the asking side is
counting down the same budget and an answer that arrives after it has moved on is
worse than none.
"""


def clarify_responder(
    client: Any, *, timeout: float = RESPOND_TIMEOUT_SECONDS
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """The send half, bound to one connection."""

    async def respond(params: dict[str, Any]) -> None:
        await client.request(CLARIFY_RESPOND_METHOD, params, timeout=timeout)

    return respond


def is_ask_user_update(method: str, params: dict[str, Any]) -> bool:
    if method != UPDATE_METHOD:
        return False
    update = params.get("update")
    return isinstance(update, dict) and update.get("sessionUpdate") == ASK_USER_UPDATE_KIND


class AskUserResponder:
    """One run's answer to its agent's `ask_user` questions."""

    def __init__(self, agent: str, instance: str, respond: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._agent = agent
        self._instance = instance
        self._respond = respond
        # Read at construction, in the run's own context, for the reason
        # `Elicitor` documents: the frame that carries a question is dispatched
        # from the connection's read loop, whose ContextVars are a copy of
        # whichever turn first opened the connection -- which the pool then keeps
        # for the life of the process.
        self._asker, self._conversation_id = current_ask()
        # Read here rather than at question time for the reason above: an
        # autofill left over from the first turn would answer this turn's
        # question out of a conversation that is not this one.
        self._autofill = current_autofill()
        self._cancelled = False
        self._tasks: set[asyncio.Task] = set()

    def cancel(self) -> None:
        """Stop asking: the run whose questions these are has ended.

        The flag and the cancels do different work, the same split `Elicitor`
        makes. The cancels stop a question already put to a user, which nothing
        else in a turn's teardown reaches; the flag stops one that has not been
        put yet, because a frame can still be in flight when the prompt settles.
        """
        self._cancelled = True
        for task in list(self._tasks):
            task.cancel()

    def dispatch(self, params: dict[str, Any]) -> None:
        """Take one `ask_user_request`, answered off the caller's task.

        Notifications are dispatched inline on the connection's read loop, so
        awaiting a human here would stall every other session sharing this
        connection -- the same defect the request path was fixed for.
        """
        task = asyncio.create_task(self._answer(params))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, params: dict[str, Any]) -> None:
        update = params.get("update")
        update = update if isinstance(update, dict) else {}
        request_id = str(update.get("requestId") or "")
        session_id = str(params.get("sessionId") or "")
        question = str(update.get("question") or "").strip()
        choices = [str(c) for c in (update.get("choices") or []) if str(c).strip()]
        if not request_id and not session_id:
            # Nothing to answer into. Unlike every other early return below,
            # this one cannot even say so.
            logger.warning("acp agent {!r}: ask_user_request with no handle, dropped", self._agent)
            return
        try:
            answer = await self._ask(question, choices)
        except asyncio.CancelledError:
            # `cancel`, i.e. the turn was aborted. Answered rather than
            # propagated, for the same reason the empty answers below are: the
            # agent is holding a tool call open on this reply.
            answer = ""
        except Exception as exc:  # noqa: BLE001 - a declared capability must answer
            logger.warning("acp agent {!r}: ask_user round trip failed, answering empty: {}", self._agent, exc)
            answer = ""
        await self._deliver(request_id, session_id, answer)

    async def _ask(self, question: str, choices: list[str]) -> str:
        if not question:
            return ""
        asker, conversation_id = self._asker, self._conversation_id
        if asker is None or not conversation_id or self._cancelled:
            # No reachable user (a CRON or other background turn), or a run that
            # ended while this frame was in flight.
            return ""
        auto = self._autofill
        known = ""
        if auto is not None:
            question_obj = autofill.Question(key="", prompt=question, options=list(choices), required=True)
            resolution = (await auto.resolve([question_obj], agent=self._agent, instance=self._instance))[0]
            if resolution.status == "answer":
                if self._cancelled:
                    # Re-checked because `resolve` awaited a model call, and the
                    # run can have ended during it. The lock path below re-checks
                    # after its own wait for exactly this; skipping the lock must
                    # not also skip the check.
                    return ""
                # Answered without a round trip, so the lock is never taken and
                # no other session's question waits behind this one.
                return resolution.answer
            known = resolution.known
        lock = question_lock(conversation_id)
        try:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                await lock.acquire()
        except TimeoutError:
            logger.warning(
                "acp agent {!r}: conversation {} still busy after {}s, answering empty",
                self._agent,
                conversation_id,
                LOCK_WAIT_SECONDS,
            )
            return ""
        try:
            if self._cancelled:
                # Re-checked under the lock: waiting for it is where a run is
                # most likely to have ended underneath this question.
                return ""
            answer = await asker.ask(
                autofill.annotate(attribute(self._agent, self._instance, question), known),
                choices or None,
                conversation_id,
            )
        finally:
            lock.release()
        # `None` is `ask_direct`'s structurally-unavailable, `""` the broker's
        # own default on timeout or close. Both mean no answer, and the asking
        # side draws no distinction between them.
        return answer or ""

    async def _deliver(self, request_id: str, session_id: str, answer: str) -> None:
        params: dict[str, Any] = {"answer": answer}
        if request_id:
            params["requestId"] = request_id
        if session_id:
            params["sessionId"] = session_id
        try:
            await asyncio.shield(self._respond(params))
        except asyncio.CancelledError:
            # Shielded so a teardown that cancels this task still lets the
            # answer reach the wire: the agent is blocked on it, and a cancel
            # here would trade a fast empty answer for its full 600s fail-safe.
            raise
        except Exception as exc:  # noqa: BLE001 - nothing above this can recover
            logger.warning("acp agent {!r}: could not deliver an ask_user answer: {}", self._agent, exc)


def notification_dispatcher(
    name: str,
    route: Callable[[str, dict[str, Any]], Awaitable[None]],
    *,
    responders: Any = None,
) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    """The connection's `on_notification`: hand questions on, route everything.

    A question is routed as well as answered, not instead: the run's collector
    reads the same stream, and a frame it never sees is a turn whose record does
    not say a question was asked.
    """

    async def handle(method: str, params: dict[str, Any]) -> None:
        if responders is not None and is_ask_user_update(method, params):
            try:
                responders.dispatch(params)
            except Exception as exc:  # noqa: BLE001 - the read loop must keep reading
                logger.warning("acp agent {!r}: could not take an ask_user_request: {}", name, exc)
        await route(method, params)

    return handle


__all__ = [
    "ASK_USER_UPDATE_KIND",
    "CLARIFY_RESPOND_METHOD",
    "LOCK_WAIT_SECONDS",
    "RESPOND_TIMEOUT_SECONDS",
    "UPDATE_METHOD",
    "AskUserResponder",
    "clarify_responder",
    "is_ask_user_update",
    "notification_dispatcher",
]
