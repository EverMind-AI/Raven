"""Turns one `elicitation/create` into questions a human answers, and back.

Separate from `raven.agent.acp.elicitation` on purpose: that module decides what a
schema means and is pure, this one holds the awaits -- the broker round trip, the
per-conversation lock -- and is the only part that needs a running loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from loguru import logger

from raven.agent.acp import autofill, elicitation
from raven.agent.acp.asker import attribute, current_ask, current_autofill, question_lock

LOCK_WAIT_SECONDS = 600.0
"""How long a queued form waits for its conversation before declining.

Bounded because the holder is one human round trip per field and each of those
fail-safes only at `QuestionBroker.await_question`'s own 600s, so an unattended
multi-field form would otherwise park every other agent's question in that
conversation for a multiple of that. Set to one question's worth of patience
rather than less, so a form queued behind one a user is still answering is not
cut off while that answer is on its way.
"""


def _retracted() -> bool:
    """Whether this task carries a cancellation something has already swallowed.

    `QuestionBroker.await_question` answers a cancellation with the question's
    default rather than propagating one, so a retracted call arrives in `_one` as
    an empty answer -- indistinguishable from a user's skip. Nothing calls
    `uncancel`, so the request count outlives that catch and is the only trace
    left that the call was cancelled at all.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _question(ask: elicitation.Ask, field: elicitation.Field) -> str:
    """One field's question in the sub-agent's own words, unattributed.

    Shared by the user's prompt and the resolver's, so the two are never asked
    slightly different things about the same field.
    """
    return ask.message if field.prompt == ask.message else f"{ask.message} - {field.prompt}"


def _off_enum(value: Any, options: list[str]) -> bool:
    """Whether `value` was not among `options`, by item if `value` is a list.

    `coerce` returns a `list[str]` for an array field, and that list is never
    itself equal to one of `options`' strings, so testing membership on the
    whole value would call every multi-select answer "Other" regardless of
    what it actually contained.
    """
    if isinstance(value, list):
        return any(item not in options for item in value)
    return value not in options


class Elicitor:
    """One run's answer to its agent's questions."""

    def __init__(self, agent: str, instance: str, dialect: Any | None = None) -> None:
        self._agent = agent
        self._instance = instance
        self._dialect = dialect
        # Read at construction, in the run's own context. An elicitation is
        # answered on a task the connection's read loop creates, and that loop
        # carries a copy of the ContextVars of whichever turn first launched the
        # connection -- which the pool then keeps for the life of the process.
        # Read there, every later turn's question would go to the first turn's
        # user, or be declined forever if that turn had no user at all.
        self._asker, self._conversation_id = current_ask()
        # Read here rather than at question time for the reason above: an
        # autofill left over from the first turn would answer this turn's
        # questions out of a conversation that is not this one.
        self._autofill = current_autofill()
        self._cancelled = False
        self._tasks: set[asyncio.Task] = set()

    def cancel(self) -> None:
        """Stop answering: the run whose questions these are has ended.

        Both halves do different work. The cancel is what stops the waiting: a
        question already put to the user stands for the broker's whole budget,
        and nothing else in a turn's teardown reaches the task it waits on. The
        flag is what stops the *form*: `QuestionBroker.await_question` answers a
        cancellation with the question's default rather than propagating one, so
        the cancel alone arrives in `_one` as an empty answer -- a user's skip --
        and the form would go on to put its next field to a user whose run is
        gone.
        """
        self._cancelled = True
        for task in list(self._tasks):
            task.cancel()

    def _prefix(self, message: str) -> str:
        return attribute(self._agent, self._instance, message)

    async def elicit(self, params: dict[str, Any]) -> dict[str, Any]:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            return await self._elicit(params)
        except asyncio.CancelledError:
            # The turn was aborted, which is what `cancel` means. Answered rather
            # than propagated: an unanswered request leaves the agent's turn
            # pending for the life of the session.
            return elicitation.cancel()
        except Exception as exc:  # noqa: BLE001 - a declared capability must answer
            logger.warning("acp agent {!r}: elicitation failed, declining: {}", self._agent, exc)
            return elicitation.decline()
        finally:
            if task is not None:
                self._tasks.discard(task)

    async def _elicit(self, params: dict[str, Any]) -> dict[str, Any]:
        ask = elicitation.parse_request(params)
        if ask is None or ask.mode != "form":
            # `url` is never advertised, and an unknown mode must not be rendered
            # as a known one, so neither is answerable here.
            return elicitation.decline()
        fields = elicitation.fields(ask.schema)
        if self._dialect is not None:
            fields = self._dialect.pair_fields(fields)
        if not fields:
            return elicitation.decline()
        asker, conversation_id = self._asker, self._conversation_id
        if asker is None or not conversation_id:
            return elicitation.decline()

        if self._cancelled:
            # Cancelled before the first question was put -- a late
            # `elicitation/create`, or a run that ended while this form queued.
            return elicitation.cancel()

        resolutions = await self._resolve(fields, ask)
        content: dict[str, Any] = {}
        pending: list[tuple[elicitation.Field, autofill.Resolution]] = []
        for field, resolution in zip(fields, resolutions, strict=True):
            if resolution.status == "answer":
                ok, value = elicitation.coerce(field, resolution.answer)
                if ok:
                    content[field.name] = value
                    continue
                # An answer the schema cannot hold is no answer. Asked rather
                # than retried: `_one`'s retry budget exists for a user who
                # mistyped, and spending it on the resolver would leave a
                # genuinely confused user with fewer attempts than they have now.
                logger.debug("question autofill: {!r} does not fit {}; asking", resolution.answer, field.name)
                resolution = autofill.Resolution(status="defer")
            pending.append((field, resolution))
        if not pending:
            if self._cancelled or _retracted():
                # The same check the per-field loop makes after every answer,
                # for the same reason: `_resolve` awaits a model call, and a run
                # that ended underneath it has no one left to accept content
                # for. Skipping the lock must not also skip this.
                return elicitation.cancel()
            # Answered in full, so the lock is never taken at all: a form nobody
            # has to see cannot park another agent's question behind it for
            # LOCK_WAIT_SECONDS.
            return elicitation.accept(content)

        # Held for the whole form, so one agent's multi-field form is never
        # interleaved with another's.
        lock = question_lock(conversation_id)
        try:
            async with asyncio.timeout(LOCK_WAIT_SECONDS):
                await lock.acquire()
        except TimeoutError:
            logger.warning(
                "acp agent {!r}: conversation {} still busy after {}s, declining",
                self._agent,
                conversation_id,
                LOCK_WAIT_SECONDS,
            )
            return elicitation.decline()
        try:
            # What the user is being taken through, so a frontend can show the
            # rest of the form rather than one question at a time.
            batch = [{"question": f.prompt} for f, _ in pending]
            for index, (field, resolution) in enumerate(pending):
                status, value = await self._one(
                    asker, conversation_id, ask, field, resolution.known, index, len(pending), batch
                )
                if self._cancelled or _retracted():
                    # Checked after each answer rather than only before the
                    # first: the run can end mid-form, and both the next
                    # question and content assembled for a run that is gone are
                    # things to stop rather than deliver. Two conditions because
                    # there are two ways to lose the reader -- the run ending,
                    # which `cancel` marks, and the agent retracting this one
                    # request, which reaches here only as a cancelled task.
                    return elicitation.cancel()
                if status in ("unavailable", "invalid"):
                    # `unavailable`: no round trip happened, so nothing was put
                    # to anybody and accepting content the user never saw would
                    # be a lie. `invalid`: the answers never fitted the schema,
                    # and content that does not match it is not acceptable
                    # either. Both decline the whole form.
                    return elicitation.decline()
                if status == "skip":
                    if field.required:
                        return elicitation.decline()
                    continue
                # A typed answer that is not one of the offered options is the
                # user using the "Other" box, which the adapter reads from the
                # paired property rather than from this one.
                off_enum = bool(field.custom_name and field.options and _off_enum(value, field.options))
                if off_enum and field.required:
                    # The enum property's own schema allows only its offered
                    # consts, so no "Other" answer can satisfy that and
                    # `required` at once. Writing just the custom key would
                    # still hand back content missing a key the schema
                    # requires -- exactly what `invalid` exists to prevent.
                    return elicitation.decline()
                if off_enum:
                    content[field.custom_name] = value
                else:
                    content[field.name] = value
            return elicitation.accept(content)
        finally:
            lock.release()

    async def _resolve(self, fields: list[elicitation.Field], ask: elicitation.Ask) -> list[autofill.Resolution]:
        """What raven can answer of this form, or a defer for every field.

        The whole form in one call rather than one call per field: the questions
        of a form are usually about one decision, and a resolver shown only the
        field in front of it cannot see the rest of that decision.
        """
        if self._autofill is None:
            return autofill.defer_all(fields)
        questions = [
            autofill.Question(
                key=field.name,
                prompt=_question(ask, field),
                options=list(field.options),
                required=field.required,
            )
            for field in fields
        ]
        return await self._autofill.resolve(questions, agent=self._agent, instance=self._instance)

    async def _one(
        self,
        asker: Any,
        conversation_id: str,
        ask: elicitation.Ask,
        field: elicitation.Field,
        known: str,
        index: int,
        total: int,
        batch: list[dict[str, str]],
    ) -> tuple[str, Any]:
        """One field's value. Status is `ok`, `skip`, `invalid`, or `unavailable`.

        Four rather than a boolean because the spec answers each differently and
        they are genuinely different facts: a skip is the user's decision about
        an optional field, `invalid` is an answer that never fit its schema,
        and `unavailable` means the round trip could not happen at all.
        """
        prompt = autofill.annotate(self._prefix(_question(ask, field)), known)
        # A paired field accepts an off-enum answer, because that is what its
        # free-text sibling is for; the enum check would reject it.
        probe = replace(field, options=[]) if field.custom_name else field
        for _ in range(elicitation.MAX_FIELD_RETRIES + 1):
            answer = await asker.ask(
                prompt, field.options or None, conversation_id, index=index, total=total, batch=batch
            )
            if answer is None:
                # `ask_direct`'s "structurally unavailable": no broker, or no
                # conversation. Nothing was put to anybody.
                return ("unavailable", None)
            if not answer.strip():
                # The broker's default on timeout or EOF, and also what the
                # sheet's close button sends. A decision, not an invalid answer
                # to re-ask.
                return ("skip", None)
            ok, value = elicitation.coerce(probe, answer)
            if ok:
                return ("ok", value)
        # Out of retries. Not a skip: the user did answer, and no answer fitted,
        # so there is no content for this field and none can be invented.
        return ("invalid", None)


__all__ = ["LOCK_WAIT_SECONDS", "Elicitor"]
