"""Let the agent ask its user a question, over a protocol with no method for it.

``ask_user`` is a tool: the model calls it, the runtime hands the question to a
:class:`~raven.tui_rpc.question_broker.QuestionBroker` and blocks the tool call
until somebody answers. Every other surface binds that broker to its own
transport. This one had nothing to bind it to, so ``ask_user`` answered
``"Error: ask_user not configured (no question broker)"`` and the turn carried on
without ever asking -- a deck built on a guess where a question was available.

ACP has no "ask a question" method, so there are two routes and the client's
declared capabilities pick between them:

* **``elicitation/create``**, when the client declared form support. The right
  fit: a message plus a schema describing one field, answered with a value.
* **``session/request_permission``** otherwise. A worse fit, and the reason it is
  needed at all: ``RequestPermissionRequest.toolCall`` is *required*, so a bare
  question has to arrive wearing a tool call it does not have. The synthesised one
  is marked in ``_meta`` rather than disguised, and the options are the question's
  own choices -- which only works because ``ask_user`` usually has them.

**A free-text question with no elicitation cannot be answered.** A permission
response carries an option id and nothing else, so there is no channel for typed
text. Rather than inventing an answer or hanging, the question is put on the wire
as an ordinary agent message -- the person sees what was asked and can answer it
in their next prompt -- and the tool falls back to its default.

Two deliberate differences from the reference implementation, both because this
package is smaller rather than because the reasoning differs:

* **Nothing is redacted.** There is no ``redact`` module here, and the text of a
  question is agent-authored text of exactly the kind ``AcpOutlet`` already puts
  on the wire unaltered. Routing it through this surface is not a new exposure,
  and inventing a scrubber for one call site would be a worse answer than the
  parity.
* **There is no ``clarify.closed``.** Nothing in this fork emits it, so a
  retraction hook keyed on it would be dead code. Cancellation arrives through
  ``session/cancel`` instead, which is what :meth:`cancel` serves -- and
  cancelling the ask is what makes ``OutboundRequests`` retract the request, so
  the client learns to take the form down either way.
"""

from __future__ import annotations

import asyncio
import contextlib
from functools import partial
from typing import Any
from uuid import uuid4

from loguru import logger

from raven.acp import protocol
from raven.acp.capabilities import ClientCapabilities
from raven.acp.outbound import OutboundRequests

# The field name the elicitation form asks for and the answer is read back from.
# One field, because ``ask_user`` asks one thing.
ANSWER_FIELD = "answer"

# The notification the broker emits when ``ask_user`` fires. Not a wire method:
# it is the broker's own contract with whatever transport it was given, and this
# surface is that transport.
CLARIFY_METHOD = "clarify.request"

# A permission prompt is a list of buttons. Past a handful it stops being a choice
# and becomes a menu nobody reads.
MAX_CHOICES = 8

HUMAN_ANSWER_TIMEOUT_S = 570.0
"""How long to wait for the client to bring back an answer.

Below the broker's own 600s, and that ordering is the whole of the number. The
reference implementation waits *longer* than the broker, which it can afford
because the runtime there emits ``clarify.closed`` when a question expires and it
cancels the round trip on that. Nothing in this fork emits that notification, so
ordering is the only mechanism left: if this side outlived the broker, the broker
would give up first, the tool would proceed on its default, and the client would
keep a form on screen for the remaining minutes with no answer anybody could
still use. Ending first means this side is the one that gives up, which retracts
the request through ``OutboundRequests`` and answers the broker in the same
breath.

The thirty seconds of margin are for the client's own dispatch. Long, because what
is on the other side is a person reading a question; finite, because an outbound
call that never resolves is a turn that never ends.
"""


class AcpQuestions:
    """Serve the broker's ``clarify.request`` by asking the client.

    One per connection: the outbound channel it asks over is the connection's, and
    every session on it maps to a session id the same way. Answering the broker is
    not optional -- it will eventually fall back to the question's default, but ten
    minutes late, and the tool call is what a person is watching.
    """

    def __init__(
        self,
        *,
        outbound: OutboundRequests,
        sessions: Any,
        emit: Any,
        timeout_s: float = HUMAN_ANSWER_TIMEOUT_S,
    ) -> None:
        self._outbound = outbound
        self._sessions = sessions
        self._emit = emit
        self._timeout_s = timeout_s
        self.client = ClientCapabilities()
        # Kept so the connection can wait for them at shutdown rather than
        # cancelling a round trip that is about to answer.
        self._tasks: set[asyncio.Task[None]] = set()
        # The same tasks by session, so ``session/cancel`` can reach the one round
        # trip that session is holding. A set cannot answer that.
        self._asking: dict[str, set[asyncio.Task[None]]] = {}
        self.routes: dict[str, int] = {}

    def set_client(self, client: ClientCapabilities) -> None:
        """Record what the client declared, at the handshake.

        Late-bound because this object is built with the connection and the
        capabilities arrive with ``initialize`` -- and re-initialising is allowed,
        so this can happen more than once.
        """
        self.client = client

    def new_broker(self) -> Any:
        """A :class:`QuestionBroker` for one session, bound to this surface.

        Built here rather than by the caller because the two halves refer to each
        other: the broker emits its notification through the sink, and the sink
        resolves the tool call through :meth:`reply` on that same broker. Tying
        the knot in one place keeps it out of every caller, and the box is what
        lets the sink name a broker that does not exist yet when it is defined.

        One per session, because the broker keys its pending questions by
        conversation id and a session is one conversation.
        """
        from raven.tui_rpc.question_broker import QuestionBroker

        box: list[Any] = []

        async def sink(frame: dict[str, Any]) -> None:
            self.handle(box[0], frame.get("method"), frame.get("params"))

        broker = QuestionBroker(send_frame=sink)
        box.append(broker)
        return broker

    def handle(self, broker: Any, method: Any, params: Any) -> bool:
        """Take a ``clarify.request``, reporting whether it was taken.

        Returns rather than awaits, and the round trip runs on its own task: the
        caller is the broker's emit, which runs inside the tool call, and the tool
        call is what the round trip has to outlive.
        """
        if method != CLARIFY_METHOD or not isinstance(params, dict):
            return False
        request_id = params.get("request_id")
        question = params.get("question")
        if not isinstance(request_id, str) or not request_id:
            return False
        if not isinstance(question, str) or not question:
            return False
        conversation_id = params.get("conversation_id")
        session_id = conversation_id if isinstance(conversation_id, str) else ""
        if not session_id or self._sessions.get(session_id) is None:
            # A question from a turn no session on this connection owns. Not ours
            # to answer, and the broker's own default applies.
            return False
        choices = [c for c in (params.get("choices") or ()) if isinstance(c, str) and c]
        task = asyncio.create_task(self._ask(broker, request_id, session_id, question, choices))
        self._tasks.add(task)
        self._asking.setdefault(session_id, set()).add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(partial(self._forget, session_id))
        return True

    def cancel(self, session_id: str) -> int:
        """Stop asking whatever this session is asking, returning how many.

        Called from ``session/cancel``. Cancelling the task is what reaches the
        client: ``OutboundRequests`` retracts a request it stops waiting for.
        Without this the client keeps a form on screen for the rest of *its*
        budget, and the answer a person then types lands in a request the broker
        resolved minutes ago.
        """
        asking = self._asking.get(session_id) or set()
        live = [task for task in asking if not task.done()]
        for task in live:
            task.cancel()
        return len(live)

    def _forget(self, session_id: str, task: asyncio.Task[None]) -> None:
        asking = self._asking.get(session_id)
        if asking is None:
            return
        asking.discard(task)
        if not asking:
            self._asking.pop(session_id, None)

    async def drain(self) -> None:
        """Wait for in-flight questions, then give up on what is left.

        Called at shutdown. The wait is short because the client is already gone by
        then; what it buys is that a round trip which has *just* been answered gets
        to deliver that answer to the broker instead of being cancelled one step
        short.
        """
        pending = [task for task in tuple(self._tasks) if not task.done()]
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=1.0)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    # -- the three routes -------------------------------------------------

    async def _ask(self, broker: Any, request_id: str, session_id: str, question: str, choices: list[str]) -> None:
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
                broker.reply(request_id, "")
            raise
        except Exception:
            logger.exception("acp: asking the client failed")
            self._count("error")
        if not broker.reply(request_id, answer or ""):
            # Already resolved: the broker timed out, or the turn was cancelled and
            # it fail-safed. Not an error, and worth a line only because a steady
            # stream of these means the deadline here is too long.
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
                "message": question,
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

        ``decline`` and ``cancel`` both mean no answer, and they are not errors: a
        person is allowed to dismiss a question. ``None`` then flows out as the
        tool's default.

        A value outside the enum is refused rather than passed through. The content
        is typed loosely by the schema, and a client that answered a
        multiple-choice question with something not on the list is a client whose
        answer cannot be acted on.
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

        ``toolCall`` is required, so the question arrives wearing one. It is marked
        in ``_meta`` as synthesised: a client that renders permission prompts
        differently from questions can tell them apart, and one that does not still
        shows the question and its choices.

        Option ids are minted here and the answer is matched against them, for the
        same reason a real permission is: an id from an earlier request, or one the
        client invented, must not select an answer nobody chose.
        """
        offered = {f"choice-{index}-{uuid4().hex}": choice for index, choice in enumerate(choices[:MAX_CHOICES])}
        result = await self._outbound.call(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": f"ask-{uuid4().hex}",
                    "title": question,
                    # ``other``, not ``think``: the kinds describe what a tool does,
                    # and this one is not a tool doing anything. A client choosing
                    # an icon from it should get the neutral one.
                    "kind": "other",
                    "status": "pending",
                },
                "options": [
                    # ``allow_once`` for every choice. The kinds describe
                    # authorisation and there is none here; using ``reject_once``
                    # for some would tell a client one of the answers is a refusal,
                    # which is not something this layer can know.
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

    def _say(self, session_id: str, text: str) -> None:
        self._emit(
            protocol.session_update(
                session_id,
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
            )
        )

    def _count(self, route: str) -> None:
        self.routes[route] = self.routes.get(route, 0) + 1


__all__ = ["ANSWER_FIELD", "CLARIFY_METHOD", "MAX_CHOICES", "AcpQuestions"]
