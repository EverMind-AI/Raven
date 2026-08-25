"""Let the agent ask its user a question, over a protocol with no method for it.

``ask_user`` is a tool: the model calls it, the runtime emits a
``clarify.request`` notification and blocks the tool call until somebody answers.
Over ACP that notification has nowhere to go, and the shape of the failure is the
worst kind -- the turn does not error, it *stalls*, for the ten minutes the
broker waits before falling back to the question's default. A client shows a
spinner the whole time and then a reply that ignores what it asked.

ACP has no "ask a question" method, so there are two routes and the client's
declared capabilities pick between them:

* **``elicitation/create``**, when the client declared form support. This is the
  right fit: a message plus a schema describing one field, answered with a value.
* **``session/request_permission``** otherwise. A worse fit, and the reason it is
  needed at all: ``RequestPermissionRequest.toolCall`` is *required*, so a bare
  question has to arrive wearing a tool call it does not have. The synthesised
  one is marked in ``_meta`` rather than disguised, and the options are the
  question's own choices -- which only works because ``ask_user`` usually has
  them.

**A free-text question with no elicitation cannot be answered.** A permission
response carries an option id and nothing else, so there is no channel for typed
text. Rather than inventing an answer or hanging, the question is put on the wire
as an ordinary agent message -- the person sees what was asked and can answer it
in their next prompt -- and the tool falls back to its default. Said out loud in
the compatibility matrix, because a silently defaulted question reads as an agent
that did not listen.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import uuid4

from loguru import logger

from raven.acp import protocol, redact
from raven.acp.capabilities import ClientCapabilities
from raven.acp.outbound import DEFAULT_REQUEST_TIMEOUT_S, OutboundRequests
from raven.acp.updates import UpdateTranslator

# The field name the elicitation form asks for and the answer is read back from.
# One field, because ``ask_user`` asks one thing.
ANSWER_FIELD = "answer"

# The notification the runtime emits when ``ask_user`` fires.
CLARIFY_METHOD = "clarify.request"


class AcpQuestions:
    """Serve ``clarify.request`` by asking the client, and answer the broker.

    The broker is the runtime's ``QuestionBroker``; ``reply`` on it is what
    unblocks the waiting tool call. Answering it is not optional -- the broker
    will eventually fall back to the default, but ten minutes late, and the tool
    call is what a person is watching.
    """

    def __init__(
        self,
        *,
        outbound: OutboundRequests,
        translator: UpdateTranslator,
        emit: Any,
        broker: Any = None,
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._outbound = outbound
        self._translator = translator
        # Optional at construction because this object has to exist before the
        # RPC stack does: the stack's ``send_frame`` *is* the sink this hangs off,
        # so the hook must be in place before the first frame can arrive on it --
        # and the broker only exists once the stack is built. Until then a
        # question is declined rather than dropped into a half-built object.
        self._broker = broker
        self._emit = emit
        self._timeout_s = timeout_s
        self.client = ClientCapabilities()
        # Kept so the connection can wait for them at shutdown rather than
        # cancelling a round trip that is about to answer.
        self._tasks: set[asyncio.Task[None]] = set()
        self.routes: dict[str, int] = {}

    def set_broker(self, broker: Any) -> None:
        """Bind the runtime's ask-user broker, once the stack that owns it exists."""
        self._broker = broker

    def set_client(self, client: ClientCapabilities) -> None:
        """Record what the client declared, at the handshake.

        Late-bound because the questions object is built with the connection and
        the capabilities arrive with ``initialize`` -- and re-initialising is
        allowed, so this can happen more than once.
        """
        self.client = client

    def handle(self, method: str, params: Any) -> bool:
        """Take a ``clarify.request``, reporting whether it was taken.

        The method check lives here rather than at the hook, so the one place that
        knows what this serves is the place that decides. The hook is offered
        *every* non-event notification -- ``approval.request``, ``mcp.status`` and
        the rest -- and a handler that read their params without checking would
        eventually misfire on one that happened to carry the same keys.

        Returns rather than awaits, and the round trip runs on its own task. The
        caller is ``send_frame`` -- the same sink the emitter streams a turn
        through -- and blocking it for the minutes a person takes to answer would
        stall every other frame on the connection behind this one.
        """
        if method != CLARIFY_METHOD or self._broker is None or not isinstance(params, dict):
            return False
        request_id = params.get("request_id")
        conversation_id = params.get("conversation_id")
        question = params.get("question")
        if not isinstance(request_id, str) or not request_id or not isinstance(question, str) or not question:
            return False
        session_id = self._session_for(conversation_id if isinstance(conversation_id, str) else "")
        if session_id is None:
            # A question from a turn no ACP session owns -- a cron turn sharing
            # this process. Not ours to answer, and the broker's own default
            # applies.
            return False
        choices = [c for c in (params.get("choices") or ()) if isinstance(c, str) and c]
        task = asyncio.create_task(self._ask(request_id, session_id, question, choices))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def drain(self) -> None:
        """Wait for in-flight questions, then give up on what is left.

        Called at shutdown. The wait is short because the client is already gone
        by then; what it buys is that a round trip which has *just* been answered
        gets to deliver that answer to the broker instead of being cancelled one
        step short.
        """
        pending = [task for task in tuple(self._tasks) if not task.done()]
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=1.0)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    # -- the two routes ---------------------------------------------------

    async def _ask(self, request_id: str, session_id: str, question: str, choices: list[str]) -> None:
        """Ask, then answer the broker exactly once.

        Every path answers it, including every failure path. The broker treats an
        unanswered question as "wait longer", so a route that gave up silently
        would be indistinguishable from a person who has not decided yet.
        """
        answer: str | None = None
        try:
            if self.client.elicitation_form:
                answer = await self._via_elicitation(session_id, question, choices)
                self._count("elicitation")
            elif choices:
                answer = await self._via_permission(session_id, question, choices)
                self._count("permission")
            else:
                # Nothing can carry typed text. Show the question rather than
                # swallow it, and let the default stand.
                self._say(session_id, question)
                self._count("shown-only")
        except asyncio.CancelledError:
            self._count("cancelled")
            # Answered anyway, and with the fallback: the tool call is still
            # blocked, and a cancelled question is not a reason to leave it that
            # way for the rest of the broker's timeout.
            with contextlib.suppress(Exception):
                self._broker.reply(request_id, "")
            raise
        except Exception:
            logger.exception("acp: asking the client failed")
            self._count("error")
        if not self._broker.reply(request_id, answer or ""):
            # Already resolved: the broker timed out, or the turn was cancelled
            # and it fail-safed. Not an error, and worth a line only because a
            # steady stream of these means the deadline here is too long.
            logger.debug("acp: the question was already resolved when the answer arrived")

    async def _via_elicitation(self, session_id: str, question: str, choices: list[str]) -> str | None:
        """The route that fits: a message plus a one-field schema.

        The field is an enum when the question has choices and a plain string
        otherwise, which is the whole reason this route is preferred -- it is the
        only one that can carry an answer nobody listed in advance.
        """
        field: dict[str, Any] = {"type": "string", "description": "Your answer"}
        if choices:
            field["enum"] = choices
        result = await self._outbound.call(
            "elicitation/create",
            {
                "message": redact.redact(question),
                "mode": "form",
                "sessionId": session_id,
                "requestedSchema": {
                    "type": "object",
                    "properties": {ANSWER_FIELD: field},
                    "required": [ANSWER_FIELD],
                },
            },
            timeout=self._timeout_s,
        )
        return self._read_elicitation(result, choices)

    def _read_elicitation(self, result: Any, choices: list[str]) -> str | None:
        """Read the form's answer, believing only a value of a usable type.

        ``decline`` and ``cancel`` both mean no answer, and they are not errors:
        a person is allowed to dismiss a question. ``None`` then flows out as the
        tool's default.

        A value outside the enum is refused rather than passed through. The
        content is typed loosely by the schema (a string, a number, a bool, a
        list), and a client that answered a multiple-choice question with
        something not on the list is a client whose answer cannot be acted on.
        """
        if not isinstance(result, dict) or result.get("action") != "accept":
            return None
        content = result.get("content")
        if not isinstance(content, dict):
            return None
        value = content.get(ANSWER_FIELD)
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if not isinstance(value, str) or not value:
            return None
        if choices and value not in choices:
            logger.warning("acp: the elicitation answer was not one of the offered choices")
            return None
        return value

    async def _via_permission(self, session_id: str, question: str, choices: list[str]) -> str | None:
        """The route that does not fit, made honest.

        ``toolCall`` is required, so the question arrives wearing one. It is
        marked in ``_meta`` as synthesised: a client that renders permission
        prompts differently from questions can tell them apart, and one that does
        not still shows the question and its choices.

        Option ids are minted here and the answer is matched against them, for
        the same reason a real permission is: an id from an earlier request, or
        one the client invented, must not select an answer nobody chose.
        """
        offered = {f"choice-{index}-{uuid4().hex}": choice for index, choice in enumerate(choices[:MAX_CHOICES])}
        result = await self._outbound.call(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": f"ask-{uuid4().hex}",
                    "title": redact.redact(question),
                    # ``other``, not ``think``: the kinds describe what a tool
                    # does, and this one is not a tool doing anything. A client
                    # choosing an icon from it should get the neutral one.
                    "kind": "other",
                    "status": "pending",
                },
                "options": [
                    # ``allow_once`` for every choice. The kinds describe
                    # authorisation and there is none here; using ``reject_once``
                    # for some would tell a client one of the answers is a
                    # refusal, which is not something this layer can know.
                    {"optionId": option_id, "name": choice, "kind": "allow_once"}
                    for option_id, choice in offered.items()
                ],
                "_meta": {"raven.synthesisedToolCall": True, "raven.kind": "question"},
            },
            timeout=self._timeout_s,
        )
        if not isinstance(result, dict):
            return None
        outcome = result.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
            return None
        return offered.get(outcome.get("optionId"))

    # -- plumbing ---------------------------------------------------------

    def _session_for(self, conversation_id: str) -> str | None:
        from raven.spine import session_of

        if not conversation_id:
            return None
        session = self._translator.get(session_of(conversation_id))
        return None if session is None else session.session_id

    def _say(self, session_id: str, text: str) -> None:
        self._emit(
            protocol.notification(
                "session/update",
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": redact.redact(text)},
                    },
                },
            )
        )

    def _count(self, route: str) -> None:
        self.routes[route] = self.routes.get(route, 0) + 1


# A permission prompt is a list of buttons. Past a handful it stops being a
# choice and becomes a menu nobody reads, and ``ask_user`` is capped upstream
# anyway -- this is the backstop for a caller that is not.
MAX_CHOICES = 8


__all__ = ["ANSWER_FIELD", "CLARIFY_METHOD", "MAX_CHOICES", "AcpQuestions"]
