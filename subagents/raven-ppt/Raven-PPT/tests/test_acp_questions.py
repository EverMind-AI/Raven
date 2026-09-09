"""Asking the user, over a protocol that has no method for it.

The three routes are covered by what the client declared rather than by calling
them directly, because the declaration is the whole of the choice. The last test
drives the real ``ask_user`` tool through a real broker: everything above it can
pass while the tool still answers "not configured", which is the state this
module exists to end.
"""

import asyncio
import contextlib
import json
import re

import pytest

from raven.acp import protocol
from raven.acp.capabilities import ClientCapabilities
from raven.acp.outbound import OutboundRequests
from raven.acp.questions import ANSWER_FIELD, CLARIFY_METHOD, AcpQuestions

SESSION = "acp:s-1"

# A choice the model wrote with a credential in it. Both routes publish the
# choices, and both frames are ones the client keeps in a transcript.
SECRET = "sk-proj-abcdefghijklmnop"
CREDENTIAL_CHOICE = f"api_key={SECRET}"
REDACTED_CHOICE = "api_key=[redacted]"


class Wire:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def sent(self, method: str) -> list[dict]:
        return [f for f in self.frames if f.get("method") == method]


class Sessions:
    """Just the lookup ``AcpQuestions`` makes of the session table."""

    def __init__(self, *ids: str) -> None:
        self._ids = set(ids)

    def get(self, session_id):
        return object() if session_id in self._ids else None


def build(*, elicitation: bool, sessions: Sessions | None = None):
    wire = Wire()
    outbound = OutboundRequests(wire)
    questions = AcpQuestions(
        outbound=outbound,
        sessions=sessions if sessions is not None else Sessions(SESSION),
        emit=wire,
        timeout_s=2.0,
    )
    declared = {"elicitation": {"form": {}}} if elicitation else {}
    questions.set_client(ClientCapabilities.from_params({"clientCapabilities": declared}))
    return wire, outbound, questions


class Broker:
    """Records what the round trip hands back to the waiting tool call."""

    def __init__(self) -> None:
        self.answers: list[tuple[str, str]] = []

    def reply(self, request_id: str, answer: str) -> bool:
        self.answers.append((request_id, answer))
        return True


def ask(questions, broker, *, question="which theme", choices=None, session=SESSION):
    return questions.handle(
        broker,
        CLARIFY_METHOD,
        {
            "request_id": "q-1",
            "conversation_id": session,
            "question": question,
            "choices": choices or [],
        },
    )


async def settle(questions):
    """Let the round trip's task run to completion."""
    await asyncio.wait_for(questions.drain(), timeout=5.0)


@pytest.mark.asyncio
async def test_a_client_with_a_form_is_asked_through_elicitation():
    wire, outbound, questions = build(elicitation=True)
    broker = Broker()

    assert ask(questions, broker, choices=["teal", "amber"]) is True
    await asyncio.sleep(0)

    sent = wire.sent("elicitation/create")[0]
    assert sent["params"]["sessionId"] == SESSION
    assert sent["params"]["mode"] == "form"
    field = sent["params"]["requestedSchema"]["properties"][ANSWER_FIELD]
    # The enum is what makes this route answerable without guessing.
    assert field["enum"] == ["teal", "amber"]

    outbound.resolve({"id": sent["id"], "result": {"action": "accept", "content": {ANSWER_FIELD: "amber"}}})
    await settle(questions)

    assert broker.answers == [("q-1", "amber")]
    assert questions.routes == {"elicitation": 1}


@pytest.mark.asyncio
async def test_a_free_text_question_is_answerable_only_through_elicitation():
    wire, outbound, questions = build(elicitation=True)
    broker = Broker()

    ask(questions, broker, question="what should the cover say")
    await asyncio.sleep(0)
    sent = wire.sent("elicitation/create")[0]
    assert "enum" not in sent["params"]["requestedSchema"]["properties"][ANSWER_FIELD]

    outbound.resolve({"id": sent["id"], "result": {"action": "accept", "content": {ANSWER_FIELD: "a title"}}})
    await settle(questions)

    assert broker.answers == [("q-1", "a title")]


@pytest.mark.asyncio
async def test_an_answer_outside_the_offered_choices_is_refused_rather_than_passed_on():
    wire, outbound, questions = build(elicitation=True)
    broker = Broker()

    ask(questions, broker, choices=["teal", "amber"])
    await asyncio.sleep(0)
    outbound.resolve(
        {
            "id": wire.sent("elicitation/create")[0]["id"],
            "result": {"action": "accept", "content": {ANSWER_FIELD: "puce"}},
        }
    )
    await settle(questions)

    # An answer nobody offered cannot be acted on, so the tool takes its default.
    assert broker.answers == [("q-1", "")]


@pytest.mark.asyncio
async def test_a_credential_in_a_choice_is_kept_out_of_the_form_and_still_answerable():
    """Both halves, because leaking nothing and answering nothing is a regression.

    A schema enum is the answer token as well as the label, so redacting it moves
    what the client will send back. The tool still gets the choice it authored.
    """
    wire, outbound, questions = build(elicitation=True)
    broker = Broker()

    ask(questions, broker, choices=[CREDENTIAL_CHOICE, "use the default"])
    await asyncio.sleep(0)
    sent = wire.sent("elicitation/create")[0]
    assert SECRET not in json.dumps(sent)
    shown = sent["params"]["requestedSchema"]["properties"][ANSWER_FIELD]["enum"]
    assert shown == [REDACTED_CHOICE, "use the default"]

    outbound.resolve({"id": sent["id"], "result": {"action": "accept", "content": {ANSWER_FIELD: shown[0]}}})
    await settle(questions)

    assert broker.answers == [("q-1", CREDENTIAL_CHOICE)]


@pytest.mark.asyncio
async def test_two_choices_that_redact_alike_are_offered_once_rather_than_ambiguously():
    """A duplicate enum value is a button nobody can tell from its neighbour, and
    an answer no lookup can resolve to one of the two rather than the other."""
    wire, outbound, questions = build(elicitation=True)
    broker = Broker()

    ask(questions, broker, choices=[CREDENTIAL_CHOICE, "api_key=sk-proj-zyxwvutsrqponmlk"])
    await asyncio.sleep(0)
    sent = wire.sent("elicitation/create")[0]
    assert sent["params"]["requestedSchema"]["properties"][ANSWER_FIELD]["enum"] == [REDACTED_CHOICE]

    outbound.resolve({"id": sent["id"], "result": {"action": "accept", "content": {ANSWER_FIELD: REDACTED_CHOICE}}})
    await settle(questions)

    assert broker.answers == [("q-1", CREDENTIAL_CHOICE)]


@pytest.mark.asyncio
async def test_a_dismissed_form_is_not_an_error_and_takes_the_default():
    wire, outbound, questions = build(elicitation=True)
    broker = Broker()

    ask(questions, broker, choices=["teal"])
    await asyncio.sleep(0)
    outbound.resolve({"id": wire.sent("elicitation/create")[0]["id"], "result": {"action": "decline"}})
    await settle(questions)

    assert broker.answers == [("q-1", "")]


@pytest.mark.asyncio
async def test_a_client_without_a_form_is_asked_through_a_synthesised_permission():
    wire, outbound, questions = build(elicitation=False)
    broker = Broker()

    ask(questions, broker, choices=["teal", "amber"])
    await asyncio.sleep(0)

    sent = wire.sent("session/request_permission")[0]
    params = sent["params"]
    # Marked rather than disguised: the tool call is required by the schema and
    # this question is not a tool doing anything.
    assert params["_meta"]["raven.synthesisedToolCall"] is True
    assert params["toolCall"]["kind"] == "other"
    assert [o["name"] for o in params["options"]] == ["teal", "amber"]

    chosen = params["options"][1]["optionId"]
    outbound.resolve({"id": sent["id"], "result": {"outcome": {"outcome": "selected", "optionId": chosen}}})
    await settle(questions)

    assert broker.answers == [("q-1", "amber")]
    assert questions.routes == {"permission": 1}


@pytest.mark.asyncio
async def test_an_option_id_the_client_invented_selects_nothing():
    wire, outbound, questions = build(elicitation=False)
    broker = Broker()

    ask(questions, broker, choices=["teal"])
    await asyncio.sleep(0)
    outbound.resolve(
        {
            "id": wire.sent("session/request_permission")[0]["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "made-up"}},
        }
    )
    await settle(questions)

    assert broker.answers == [("q-1", "")]


@pytest.mark.asyncio
async def test_a_credential_in_a_choice_is_kept_out_of_the_permission_and_still_answerable():
    """The same two halves on the route whose option carries an id.

    Matching already keys on the minted id, so redacting the displayed name costs
    the round trip nothing -- the client sends back an id, never the label.
    """
    wire, outbound, questions = build(elicitation=False)
    broker = Broker()

    ask(questions, broker, choices=[CREDENTIAL_CHOICE, "use the default"])
    await asyncio.sleep(0)
    sent = wire.sent("session/request_permission")[0]
    assert SECRET not in json.dumps(sent)
    options = sent["params"]["options"]
    assert [o["name"] for o in options] == [REDACTED_CHOICE, "use the default"]

    outbound.resolve(
        {"id": sent["id"], "result": {"outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}}}
    )
    await settle(questions)

    assert broker.answers == [("q-1", CREDENTIAL_CHOICE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("elicitation", "method", "field"),
    [(True, "elicitation/create", ("message",)), (False, "session/request_permission", ("toolCall", "title"))],
)
async def test_the_question_text_is_scrubbed_without_a_call_at_the_field(elicitation, method, field):
    """Neither of these fields is wrapped where it is written any more.

    That they still arrive redacted is the choke point doing the work, which is the
    difference between this surface being safe and the fields somebody remembered.
    """
    wire, _outbound, questions = build(elicitation=elicitation)

    ask(questions, broker=Broker(), question=f"is {CREDENTIAL_CHOICE} still valid", choices=["yes", "no"])
    await asyncio.sleep(0)

    published = wire.sent(method)[0]["params"]
    for key in field:
        published = published[key]
    assert published == f"is {REDACTED_CHOICE} still valid"


@pytest.mark.asyncio
async def test_the_ids_this_surface_minted_survive_the_scan_intact():
    """Caught on a live connection, not here: the synthesised ``ask-<hex>`` id went
    out as ``a[redacted]``, because the vendor patterns match ``sk-`` inside a word
    and a hex id is 32 chars of coin flips. An id is not prose and must not be
    rewritten; a client correlating the permission with the tool call needs it whole.
    """
    wire, _outbound, questions = build(elicitation=False)

    ask(questions, broker=Broker(), choices=["teal", "amber"])
    await asyncio.sleep(0)
    params = wire.sent("session/request_permission")[0]["params"]

    assert params["sessionId"] == SESSION
    assert re.fullmatch(r"ask-[0-9a-f]{32}", params["toolCall"]["toolCallId"])
    assert all(re.fullmatch(r"choice-\d+-[0-9a-f]{32}", o["optionId"]) for o in params["options"])


@pytest.mark.asyncio
async def test_a_field_nobody_here_named_is_scrubbed_too():
    """The shape rather than the field list.

    Redacting per field protects whatever the last edit had in mind, which is how
    the choices came to sit unscrubbed two lines under a scrubbed title. A payload
    grown a field later has to be covered without anyone rewrapping it.
    """
    wire, _outbound, questions = build(elicitation=True)

    task = asyncio.create_task(
        questions._publish("elicitation/create", {"addedLater": {"nested": [CREDENTIAL_CHOICE]}})
    )
    await asyncio.sleep(0)
    sent = wire.sent("elicitation/create")[0]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert SECRET not in json.dumps(sent)
    assert sent["params"]["addedLater"]["nested"] == [REDACTED_CHOICE]


@pytest.mark.asyncio
async def test_a_free_text_question_with_no_form_is_shown_rather_than_swallowed():
    wire, _outbound, questions = build(elicitation=False)
    broker = Broker()

    ask(questions, broker, question="what should the cover say")
    await settle(questions)

    # Nothing on this route can carry typed text, so the person sees the question
    # and can answer it in their next prompt.
    said = [f for f in wire.frames if f.get("method") == "session/update"]
    assert said and said[0]["params"]["update"]["content"]["text"] == "what should the cover say"
    assert broker.answers == [("q-1", "")]
    assert questions.routes == {"shown-only": 1}


@pytest.mark.asyncio
async def test_cancelling_the_session_releases_the_tool_call_and_retracts_the_request():
    wire, _outbound, questions = build(elicitation=True)
    broker = Broker()

    ask(questions, broker, choices=["teal"])
    await asyncio.sleep(0)
    assert questions.cancel(SESSION) == 1
    await settle(questions)

    assert broker.answers == [("q-1", "")]
    assert wire.sent(protocol.CANCEL_REQUEST_METHOD)
    assert questions.routes == {"cancelled": 1}


@pytest.mark.asyncio
async def test_a_question_from_a_session_this_connection_does_not_have_is_declined():
    _wire, _outbound, questions = build(elicitation=True, sessions=Sessions("acp:other"))
    broker = Broker()

    assert ask(questions, broker) is False
    assert broker.answers == []


@pytest.mark.asyncio
async def test_a_notification_that_is_not_a_question_is_left_alone():
    _wire, _outbound, questions = build(elicitation=True)
    broker = Broker()

    assert questions.handle(broker, "mcp.status", {"request_id": "q-1"}) is False
    assert questions.handle(broker, CLARIFY_METHOD, {"request_id": "q-1"}) is False


@pytest.mark.asyncio
async def test_the_ask_user_tool_gets_a_real_answer_over_acp():
    """The whole point, end to end: the tool, its broker, and the wire.

    Everything above can pass while ``ask_user`` still answers "not configured",
    because the tool is what holds the turn and it is bound separately.
    """
    from raven.agent.tools.ask_user import AskUserTool

    wire, outbound, questions = build(elicitation=True)
    broker = questions.new_broker()
    tool = AskUserTool()
    tool.set_broker(broker)
    tool.set_context(SESSION)

    call = asyncio.create_task(tool.execute(questions=[{"question": "which theme", "options": ["teal", "amber"]}]))
    for _ in range(20):
        await asyncio.sleep(0)
        if wire.sent("elicitation/create"):
            break
    sent = wire.sent("elicitation/create")[0]
    outbound.resolve({"id": sent["id"], "result": {"action": "accept", "content": {ANSWER_FIELD: "teal"}}})

    result = await asyncio.wait_for(call, timeout=5.0)
    assert "teal" in result.model_text
    assert "not configured" not in result.model_text


@pytest.mark.asyncio
async def test_the_ask_gives_up_before_the_broker_does():
    """Ordering, not duration. Nothing in this fork retracts an expired question,
    so if this side outlived the broker the tool would take its default while the
    client still held a form nobody could answer into."""
    import inspect

    from raven.acp.questions import HUMAN_ANSWER_TIMEOUT_S
    from raven.tui_rpc.question_broker import QuestionBroker

    broker_budget = inspect.signature(QuestionBroker.await_question).parameters["timeout_s"].default
    assert HUMAN_ANSWER_TIMEOUT_S < broker_budget, (
        f"the ask waits {HUMAN_ANSWER_TIMEOUT_S}s and the broker only {broker_budget}s"
    )


@pytest.mark.asyncio
async def test_a_client_that_never_answers_releases_the_tool_and_retracts_the_form():
    wire = Wire()
    outbound = OutboundRequests(wire)
    questions = AcpQuestions(outbound=outbound, sessions=Sessions(SESSION), emit=wire, timeout_s=0.01)
    questions.set_client(ClientCapabilities.from_params({"clientCapabilities": {"elicitation": {"form": {}}}}))
    broker = Broker()

    ask(questions, broker, choices=["teal"])
    await settle(questions)

    assert broker.answers == [("q-1", "")]
    assert wire.sent(protocol.CANCEL_REQUEST_METHOD)
    assert questions.routes == {"error": 1}
