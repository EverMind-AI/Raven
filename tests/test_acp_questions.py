"""``ask_user`` over a protocol with no method for asking.

The failure this replaces is worth naming: a ``clarify.request`` with nowhere to
go does not error, it *stalls*. The tool call stays blocked for the ten minutes
the broker waits before falling back to the question's default, so a client shows
a spinner and then a reply that ignores what it asked. Every test here therefore
checks two things -- that the client was asked, and that the broker was answered.

Which route is taken is the client's declaration, not a preference:
``elicitation/create`` in form mode can carry an answer nobody listed in advance;
``session/request_permission`` can only return an option id, and it needs a
synthesised ``toolCall`` to be legal at all.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.acp.capabilities import ClientCapabilities
from raven.acp.outbound import OutboundRequests
from raven.acp.questions import ANSWER_FIELD, CLARIFY_METHOD, MAX_CHOICES, AcpQuestions
from raven.acp.updates import AcpSession, UpdateTranslator
from tests.acp_schema import validate_def, validate_outbound


class _Broker:
    def __init__(self, accept: bool = True) -> None:
        self.answers: list[tuple[str, str]] = []
        self.accept = accept

    def reply(self, key: str, answer: str) -> bool:
        self.answers.append((key, answer))
        return self.accept


class _Client:
    """Answers the agent's request with whatever the test decided."""

    def __init__(self, answer=None, *, silent: bool = False) -> None:
        self.frames: list[dict] = []
        self.answer = answer
        self.silent = silent
        self.outbound: OutboundRequests | None = None

    def emit(self, frame: dict) -> None:
        self.frames.append(frame)
        if self.silent or self.outbound is None or "method" not in frame or "id" not in frame:
            return
        reply = {"jsonrpc": "2.0", "id": frame["id"]}
        reply.update(self.answer(frame) if callable(self.answer) else self.answer)
        asyncio.get_running_loop().call_soon(self.outbound.resolve, reply)

    def asked(self, method: str) -> dict:
        return next(f for f in self.frames if f.get("method") == method)

    @property
    def updates(self) -> list[dict]:
        return [f["params"]["update"] for f in self.frames if f.get("method") == "session/update"]


def _rig(answer=None, *, capabilities=None, silent: bool = False, accept: bool = True, timeout_s: float = 30.0):
    client = _Client(answer, silent=silent)
    outbound = OutboundRequests(emit=client.emit)
    client.outbound = outbound
    translator = UpdateTranslator(emit=client.emit)
    translator.add(AcpSession(session_id="acp:s1", session_key="acp:s1", cwd="/w", subscription_id="sub"))
    broker = _Broker(accept=accept)
    questions = AcpQuestions(
        outbound=outbound, translator=translator, broker=broker, emit=client.emit, timeout_s=timeout_s
    )
    questions.set_client(ClientCapabilities.from_params({"clientCapabilities": capabilities or {}}))
    return client, broker, questions


FORM = {"elicitation": {"form": {}}}


def _clarify(**overrides):
    params = {
        "conversation_id": "acp:s1",
        "request_id": "q1",
        "question": "Which database should I migrate?",
        "choices": ["postgres", "sqlite"],
    }
    params.update(overrides)
    return params


async def _settle(questions: AcpQuestions) -> None:
    await asyncio.sleep(0)
    await questions.drain()


class TestTheElicitationRoute:
    async def test_it_asks_with_a_one_field_schema_and_answers_the_broker(self):
        client, broker, questions = _rig(
            lambda f: {"result": {"action": "accept", "content": {ANSWER_FIELD: "postgres"}}},
            capabilities=FORM,
        )

        assert questions.handle(CLARIFY_METHOD, _clarify()) is True
        await _settle(questions)

        request = client.asked("elicitation/create")["params"]
        assert request["mode"] == "form"
        assert request["sessionId"] == "acp:s1"
        assert request["requestedSchema"]["properties"][ANSWER_FIELD]["enum"] == ["postgres", "sqlite"]
        assert broker.answers == [("q1", "postgres")]
        validate_def("CreateElicitationRequest", request)
        validate_outbound(client.asked("elicitation/create"))

    async def test_a_question_with_no_choices_asks_for_free_text(self):
        """The reason this route is preferred: it is the only one that can carry
        an answer nobody listed in advance."""
        client, broker, questions = _rig(
            lambda f: {"result": {"action": "accept", "content": {ANSWER_FIELD: "call it raven"}}},
            capabilities=FORM,
        )

        questions.handle(CLARIFY_METHOD, _clarify(choices=[]))
        await _settle(questions)

        field = client.asked("elicitation/create")["params"]["requestedSchema"]["properties"][ANSWER_FIELD]
        assert "enum" not in field
        assert broker.answers == [("q1", "call it raven")]

    @pytest.mark.parametrize("action", ["decline", "cancel"])
    async def test_dismissing_the_question_answers_the_broker_with_nothing(self, action):
        """A person is allowed to dismiss a question, and that is not an error --
        but the tool call is still blocked, so the broker must hear about it."""
        client, broker, questions = _rig(lambda f: {"result": {"action": action}}, capabilities=FORM)

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "")]

    @pytest.mark.parametrize(
        "result",
        [
            None,
            "yes",
            {},
            {"action": "accept"},
            {"action": "accept", "content": "postgres"},
            {"action": "accept", "content": {}},
            {"action": "accept", "content": {ANSWER_FIELD: None}},
            {"action": "accept", "content": {ANSWER_FIELD: True}},
            {"action": "accept", "content": {ANSWER_FIELD: ""}},
        ],
    )
    async def test_a_malformed_answer_falls_back_rather_than_being_forwarded(self, result):
        _, broker, questions = _rig(lambda f: {"result": result}, capabilities=FORM)

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "")]

    async def test_a_numeric_or_list_answer_is_rendered_as_text(self):
        """The content is typed loosely by the schema, and the tool takes a
        string."""
        _, broker, questions = _rig(
            lambda f: {"result": {"action": "accept", "content": {ANSWER_FIELD: ["a", "b"]}}},
            capabilities=FORM,
        )

        questions.handle(CLARIFY_METHOD, _clarify(choices=[]))
        await _settle(questions)

        assert broker.answers == [("q1", "a, b")]

    async def test_a_numeric_answer_is_rendered_as_text(self):
        """The schema types the content loosely -- a string, a number, a bool, a
        list -- and the tool takes a string."""
        _, broker, questions = _rig(
            lambda f: {"result": {"action": "accept", "content": {ANSWER_FIELD: 7}}}, capabilities=FORM
        )

        questions.handle(CLARIFY_METHOD, _clarify(choices=[]))
        await _settle(questions)

        assert broker.answers == [("q1", "7")]

    async def test_an_answer_outside_the_offered_choices_is_refused(self):
        """A client that answered a multiple-choice question with something not
        on the list gave an answer that cannot be acted on."""
        _, broker, questions = _rig(
            lambda f: {"result": {"action": "accept", "content": {ANSWER_FIELD: "mysql"}}},
            capabilities=FORM,
        )

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "")]

    async def test_a_client_that_declares_only_url_mode_does_not_get_the_form(self):
        """Reading the group as sufficient would route a question into a mode the
        client never claimed -- and ``url`` mode sends somebody to a web page."""
        client, broker, questions = _rig(
            lambda f: {
                "result": {"outcome": {"outcome": "selected", "optionId": f["params"]["options"][0]["optionId"]}}
            },
            capabilities={"elicitation": {"url": {}}},
        )

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert not any(f.get("method") == "elicitation/create" for f in client.frames)
        assert client.asked("session/request_permission")


class TestThePermissionRoute:
    def _pick_first(self, frame):
        return {"result": {"outcome": {"outcome": "selected", "optionId": frame["params"]["options"][0]["optionId"]}}}

    async def test_a_question_arrives_wearing_a_synthesised_tool_call(self):
        """``RequestPermissionRequest.toolCall`` is required and a bare question
        has none. Marked in ``_meta`` rather than disguised, so a client that
        renders questions differently can tell."""
        client, broker, questions = _rig(self._pick_first)

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        request = client.asked("session/request_permission")["params"]
        validate_def("RequestPermissionRequest", request)
        assert request["_meta"]["raven.synthesisedToolCall"] is True
        assert request["_meta"]["raven.kind"] == "question"
        assert request["toolCall"]["kind"] == "other", "the kinds describe tools, and this is not one"
        assert "Which database" in request["toolCall"]["title"]

    async def test_the_choices_become_the_options_and_the_pick_becomes_the_answer(self):
        client, broker, questions = _rig(self._pick_first)

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        options = client.asked("session/request_permission")["params"]["options"]
        assert [o["name"] for o in options] == ["postgres", "sqlite"]
        assert {o["kind"] for o in options} == {"allow_once"}, (
            "the kinds describe authorisation; calling one a rejection would invent a meaning"
        )
        assert broker.answers == [("q1", "postgres")]

    async def test_an_option_id_this_request_did_not_mint_selects_nothing(self):
        """Same rule as a real permission: a stale or invented id must not select
        an answer nobody chose."""
        _, broker, questions = _rig(
            lambda f: {"result": {"outcome": {"outcome": "selected", "optionId": "choice-0-fabricated"}}}
        )

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "")]

    @pytest.mark.parametrize(
        "result",
        [{"outcome": {"outcome": "cancelled"}}, {"outcome": {"outcome": "denied"}}, {"outcome": "selected"}, {}],
    )
    async def test_anything_but_a_selection_falls_back(self, result):
        _, broker, questions = _rig(lambda f: {"result": result})

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "")]

    @pytest.mark.parametrize("result", [None, "picked", 5, []])
    async def test_a_non_object_answer_falls_back(self, result):
        _, broker, questions = _rig(lambda f: {"result": result})

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "")]

    async def test_too_many_choices_are_capped(self):
        """Past a handful a prompt stops being a choice and becomes a menu nobody
        reads."""
        client, _, questions = _rig(self._pick_first)

        questions.handle(CLARIFY_METHOD, _clarify(choices=[f"option {n}" for n in range(MAX_CHOICES + 5)]))
        await _settle(questions)

        assert len(client.asked("session/request_permission")["params"]["options"]) == MAX_CHOICES


class TestTheUnaskableQuestion:
    async def test_free_text_with_no_elicitation_is_shown_rather_than_swallowed(self):
        """A permission response carries an option id and nothing else, so there
        is no channel for typed text. The person sees the question and can answer
        it in their next prompt."""
        client, broker, questions = _rig()

        questions.handle(CLARIFY_METHOD, _clarify(choices=[]))
        await _settle(questions)

        assert [u["content"]["text"] for u in client.updates] == ["Which database should I migrate?"]
        assert broker.answers == [("q1", "")]
        assert not any("request_permission" in str(f.get("method")) for f in client.frames)
        assert questions.routes == {"shown-only": 1}


class TestFailurePaths:
    async def test_a_silent_client_still_answers_the_broker(self):
        """The broker treats an unanswered question as "wait longer", so giving up
        silently is indistinguishable from a person who has not decided."""
        _, broker, questions = _rig(silent=True, capabilities=FORM, timeout_s=0.05)

        questions.handle(CLARIFY_METHOD, _clarify())
        await asyncio.sleep(0.2)
        await questions.drain()

        assert broker.answers == [("q1", "")]
        assert questions.routes == {"error": 1}

    async def test_an_error_reply_still_answers_the_broker(self):
        _, broker, questions = _rig(
            lambda f: {"error": {"code": -32601, "message": "elicitation/create is not implemented"}},
            capabilities=FORM,
        )

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "")]

    async def test_a_cancelled_round_trip_still_answers_the_broker(self):
        """The tool call is still blocked, and a cancelled question is not a
        reason to leave it that way for the rest of the timeout."""
        _, broker, questions = _rig(silent=True, capabilities=FORM)

        questions.handle(CLARIFY_METHOD, _clarify())
        await asyncio.sleep(0)
        await questions.drain()

        assert broker.answers == [("q1", "")]

    async def test_a_broker_that_already_resolved_is_not_an_error(self):
        """It timed out, or the turn was cancelled and it fail-safed."""
        _, broker, questions = _rig(
            lambda f: {"result": {"action": "accept", "content": {ANSWER_FIELD: "postgres"}}},
            capabilities=FORM,
            accept=False,
        )

        questions.handle(CLARIFY_METHOD, _clarify())
        await _settle(questions)

        assert broker.answers == [("q1", "postgres")]


class TestWhatIsNotTaken:
    @pytest.mark.parametrize(
        "params",
        [
            None,
            "clarify",
            {},
            {"request_id": "q1"},
            {"question": "why?"},
            {"request_id": "", "question": "why?"},
            {"request_id": "q1", "question": ""},
        ],
    )
    def test_a_malformed_notification_is_declined(self, params):
        _, _, questions = _rig()

        assert questions.handle(CLARIFY_METHOD, params) is False

    @pytest.mark.parametrize("method", ["approval.request", "mcp.status", "confirm.request", "event"])
    def test_another_surfaces_notification_is_declined(self, method):
        """The hook is offered every non-event notification. A handler that read
        their params without checking the method would eventually misfire on one
        that happened to carry the same keys."""
        _, _, questions = _rig()

        assert questions.handle(method, _clarify()) is False

    async def test_a_question_before_the_broker_exists_is_declined(self):
        """This object is built before the stack that owns the broker, because the
        hook has to be in place before the first frame can arrive on the sink."""
        from raven.acp.outbound import OutboundRequests
        from raven.acp.questions import AcpQuestions as Q

        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(AcpSession(session_id="acp:s1", session_key="acp:s1", cwd="/w", subscription_id="sub"))
        unbound = Q(outbound=OutboundRequests(emit=lambda f: None), translator=translator, emit=lambda f: None)

        assert unbound.handle(CLARIFY_METHOD, _clarify()) is False

        unbound.set_broker(_Broker())
        assert unbound.handle(CLARIFY_METHOD, _clarify()) is True
        await unbound.drain()

    def test_a_question_from_a_turn_no_session_owns_is_declined(self):
        """A cron turn sharing this process. Not ours to answer, and the broker's
        own default applies."""
        _, _, questions = _rig()

        assert questions.handle(CLARIFY_METHOD, _clarify(conversation_id="cron:nightly")) is False
        assert questions.handle(CLARIFY_METHOD, _clarify(conversation_id="")) is False

    async def test_a_subagents_lane_resolves_to_its_session(self):
        """A direct chat runs on <session>#<agent>/<handle>, and a handle is
        free-form text the model chose -- so the split has to be on the first
        separator only."""
        client, broker, questions = _rig(silent=True, timeout_s=0.05)

        assert questions.handle(CLARIFY_METHOD, _clarify(conversation_id="acp:s1#scout/handle#with hashes")) is True
        await asyncio.sleep(0.2)
        await questions.drain()

        assert client.asked("session/request_permission")["params"]["sessionId"] == "acp:s1"

    async def test_draining_with_nothing_in_flight_is_harmless(self):
        _, _, questions = _rig()

        await questions.drain()
