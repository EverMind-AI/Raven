"""`ask_user` over Raven-X's extension: a notification out, a request back.

The failure this replaces is the same one `elicitation/create` was wired for and
it arrives by a different door: the asking agent blocks its tool call for ten
minutes before falling back to the question's default, so a question raven does
not answer is a stall, not a dropped frame. Every test here therefore checks the
answer went back -- an assertion that the user was asked proves only half of it,
and the half that leaves a turn hanging is the other one.

The route is armed by the client's declaration rather than by the frame, so the
first test is that raven declares it. Everything after that assumes it did.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


def _frame(question: str = "which market?", *, request_id: str = "q-1", session: str = "s1", choices=None) -> dict:
    return {
        "sessionId": session,
        "update": {
            "sessionUpdate": "ask_user_request",
            "requestId": request_id,
            "question": question,
            "choices": choices if choices is not None else ["EU", "US"],
        },
    }


class _Recorder:
    """The send half, captured instead of put on a wire."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, params: dict) -> None:
        self.sent.append(params)


class _Answers:
    def __init__(self, answer: str | None = "EU") -> None:
        self.answer = answer
        self.asked: list[tuple[str, list | None, str]] = []

    async def ask(self, prompt, choices, conversation_id):
        self.asked.append((prompt, choices, conversation_id))
        return self.answer


class _RecordingAsker:
    """The `Asker` half of the round trip, without a broker."""

    def __init__(self, answer: str = "") -> None:
        self.asked: list[tuple[str, list[str] | None]] = []
        self._answer = answer

    async def ask(self, prompt, choices, conversation_id):
        self.asked.append((prompt, choices))
        return self._answer


class _StubAutofill:
    """Whatever the resolver would have decided, without a model.

    Keyed by field name; `""` is the single-question route's key.
    """

    def __init__(self, decisions: dict[str, tuple[str, str]]) -> None:
        self._decisions = decisions

    async def resolve(self, questions, *, agent, instance):
        from raven.agent.acp import autofill

        out = []
        for question in questions:
            status, payload = self._decisions.get(question.key, ("defer", ""))
            if status == "answer":
                out.append(autofill.Resolution(status="answer", answer=payload))
            elif status == "partial":
                out.append(autofill.Resolution(status="partial", known=payload))
            else:
                out.append(autofill.Resolution(status="defer"))
        return out


async def _ask_with(asker, autofill_obj, question: str = "Which branch?") -> str:
    """One question, through a real `AskUserResponder`, with the turn bound as a turn binds it.

    Bound before the `AskUserResponder` is constructed because that is where both the
    asker and the autofill are read; bound after, the responder would have neither.
    """
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    sent: list[dict] = []

    async def respond(params: dict) -> None:
        sent.append(params)

    start_ask_turn(asker, autofill_obj, conversation_id="tui:c1")
    frame = {
        "sessionId": "s1",
        "update": {
            "sessionUpdate": "ask_user_request",
            "requestId": "q-1",
            "question": question,
            "choices": [],
        },
    }
    responder = AskUserResponder("raven-code", "a1b2", respond)
    responder.dispatch(frame)
    for _ in range(20):
        await asyncio.sleep(0)
    return sent[0]["answer"] if sent else ""


async def _settle() -> None:
    """Let the tasks `dispatch` created run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_the_client_declares_the_extension() -> None:
    """Without this the agent never sends a question at all.

    Both sides opt in: Raven-X arms its broker only when the client declared
    `_meta.raven.askUser`, and falls back to ending the turn on its questions
    otherwise. The key is what turns every other test here into live behaviour.
    """
    from raven.agent.acp.protocol import CLIENT_CAPABILITIES, initialize_params

    assert CLIENT_CAPABILITIES["_meta"]["raven"]["askUser"] is True
    assert initialize_params()["clientCapabilities"]["_meta"]["raven"]["askUser"] is True


async def test_a_question_reaches_the_user_and_the_answer_goes_back() -> None:
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    tool, sent = _Answers("EU"), _Recorder()
    start_ask_turn(tool, conversation_id="tui:c1")
    AskUserResponder("research", "t1", sent).dispatch(_frame())
    await _settle()

    assert tool.asked == [("research(t1): which market?", ["EU", "US"], "tui:c1")]
    assert sent.sent == [{"answer": "EU", "requestId": "q-1", "sessionId": "s1"}]


async def test_a_question_with_no_choices_is_put_as_free_text() -> None:
    """`None`, not `[]`: the contract's own way of saying "type an answer"."""
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    tool, sent = _Answers("2024"), _Recorder()
    start_ask_turn(tool, conversation_id="tui:c1")
    AskUserResponder("research", "t1", sent).dispatch(_frame("which year?", choices=[]))
    await _settle()

    assert tool.asked[0][1] is None
    assert sent.sent[0]["answer"] == "2024"


async def test_a_turn_with_no_reachable_user_answers_empty_at_once() -> None:
    """A CRON turn binds no asker. Answering is the whole point of this case.

    Silence would cost the agent its full fail-safe wait for a question nobody
    was ever going to see; an empty answer is what its tool already renders as
    "the user did not answer; proceed with best judgment".
    """
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    sent = _Recorder()
    start_ask_turn(None, conversation_id="tui:c1")
    AskUserResponder("research", "t1", sent).dispatch(_frame())
    await _settle()

    assert sent.sent == [{"answer": "", "requestId": "q-1", "sessionId": "s1"}]


async def test_a_run_that_ended_answers_empty_without_asking() -> None:
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    tool, sent = _Answers("EU"), _Recorder()
    start_ask_turn(tool, conversation_id="tui:c1")
    responder = AskUserResponder("research", "t1", sent)
    responder.cancel()
    responder.dispatch(_frame())
    await _settle()

    assert tool.asked == []
    assert sent.sent == [{"answer": "", "requestId": "q-1", "sessionId": "s1"}]


async def test_a_question_already_put_is_cancelled_and_still_answered() -> None:
    """The window `cancel` exists for: a sheet up, and the run behind it gone."""
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    sent = _Recorder()

    class Parks:
        async def ask(self, prompt, choices, conversation_id):
            await asyncio.sleep(3600)

    start_ask_turn(Parks(), conversation_id="tui:c1")
    responder = AskUserResponder("research", "t1", sent)
    responder.dispatch(_frame())
    await asyncio.sleep(0)
    responder.cancel()
    await _settle()

    assert sent.sent == [{"answer": "", "requestId": "q-1", "sessionId": "s1"}]


async def test_an_asker_that_raises_still_answers() -> None:
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    sent = _Recorder()

    class Breaks:
        async def ask(self, prompt, choices, conversation_id):
            raise RuntimeError("the sheet is gone")

    start_ask_turn(Breaks(), conversation_id="tui:c1")
    AskUserResponder("research", "t1", sent).dispatch(_frame())
    await _settle()

    assert sent.sent == [{"answer": "", "requestId": "q-1", "sessionId": "s1"}]


async def test_a_structurally_unavailable_round_trip_reads_as_no_answer() -> None:
    """`ask_direct` returns `None` for "no broker, no conversation"."""
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    tool, sent = _Answers(None), _Recorder()
    start_ask_turn(tool, conversation_id="tui:c1")
    AskUserResponder("research", "t1", sent).dispatch(_frame())
    await _settle()

    assert sent.sent[0]["answer"] == ""


async def test_a_frame_with_no_handle_is_dropped_rather_than_guessed() -> None:
    """Nothing to answer into: `requestId` and `sessionId` both absent."""
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    tool, sent = _Answers("EU"), _Recorder()
    start_ask_turn(tool, conversation_id="tui:c1")
    frame = {"update": {"sessionUpdate": "ask_user_request", "question": "which?"}}
    AskUserResponder("research", "t1", sent).dispatch(frame)
    await _settle()

    assert tool.asked == [] and sent.sent == []


async def test_a_session_scoped_answer_carries_no_request_id() -> None:
    """The agent's own handler accepts either key, so only what is known is sent."""
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    tool, sent = _Answers("EU"), _Recorder()
    start_ask_turn(tool, conversation_id="tui:c1")
    frame = _frame()
    frame["update"].pop("requestId")
    AskUserResponder("research", "t1", sent).dispatch(frame)
    await _settle()

    assert sent.sent == [{"answer": "EU", "sessionId": "s1"}]


async def test_two_questions_on_one_conversation_do_not_overlap() -> None:
    """The broker allows one pending question per conversation.

    An overlapping one is fail-safed to its default there, which reads as a skip
    nobody ever saw -- so the lock is what keeps the second question a question.
    """
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    order: list[str] = []
    sent = _Recorder()

    class Slow:
        async def ask(self, prompt, choices, conversation_id):
            order.append(f"start:{prompt}")
            await asyncio.sleep(0.02)
            order.append(f"end:{prompt}")
            return "ok"

    start_ask_turn(Slow(), conversation_id="tui:c1")
    AskUserResponder("a", "h1", sent).dispatch(_frame("first", request_id="q-1"))
    AskUserResponder("b", "h2", sent).dispatch(_frame("second", request_id="q-2", session="s2"))
    for _ in range(60):
        await asyncio.sleep(0.005)
        if len(sent.sent) == 2:
            break

    assert order == ["start:a(h1): first", "end:a(h1): first", "start:b(h2): second", "end:b(h2): second"]
    assert {s["requestId"] for s in sent.sent} == {"q-1", "q-2"}


async def test_a_question_kept_waiting_for_its_conversation_answers_empty(monkeypatch) -> None:
    """Giving up beats waiting: the asking side is counting down the same budget.

    An answer that lands after the agent has already fail-safed is worse than a
    fast empty one, which at least gets that turn moving again.
    """
    from raven.agent.acp import ask_user as ask_user_module
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    sent = _Recorder()

    class Parked:
        async def ask(self, prompt, choices, conversation_id):
            await asyncio.sleep(0.2)
            return "late"

    monkeypatch.setattr(ask_user_module, "LOCK_WAIT_SECONDS", 0.01)
    start_ask_turn(Parked(), conversation_id="tui:c1")
    holder = AskUserResponder("a", "h1", sent)
    holder.dispatch(_frame("first", request_id="q-1"))
    # One turn of the loop is all the holder needs to take the lock; without it
    # the queued question could win the race.
    await asyncio.sleep(0)
    AskUserResponder("b", "h2", sent).dispatch(_frame("second", request_id="q-2", session="s2"))
    for _ in range(100):
        await asyncio.sleep(0.005)
        if len(sent.sent) == 2:
            break

    by_request = {s["requestId"]: s["answer"] for s in sent.sent}
    assert by_request == {"q-1": "late", "q-2": ""}


async def test_an_elicitation_form_and_a_question_share_the_lock() -> None:
    """Both routes end at one broker, so they must not overlap each other either."""
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    order: list[str] = []
    sent = _Recorder()

    class Slow:
        async def ask(self, prompt, choices, conversation_id, **_):
            order.append(f"start:{prompt.split(':')[0]}")
            await asyncio.sleep(0.02)
            order.append(f"end:{prompt.split(':')[0]}")
            return "redis"

    start_ask_turn(Slow(), conversation_id="tui:c1")
    form = {
        "sessionId": "s1",
        "mode": "form",
        "message": "which backend?",
        "requestedSchema": {"type": "object", "properties": {"backend": {"type": "string"}}},
    }
    holder = asyncio.create_task(Elicitor("form", "h1").elicit(form))
    await asyncio.sleep(0)
    AskUserResponder("ask", "h2", sent).dispatch(_frame("second", request_id="q-2", session="s2"))
    assert await holder == {"action": "accept", "content": {"backend": "redis"}}
    for _ in range(60):
        await asyncio.sleep(0.005)
        if sent.sent:
            break

    assert order == ["start:form(h1)", "end:form(h1)", "start:ask(h2)", "end:ask(h2)"]


async def test_the_dispatcher_routes_the_frame_as_well_as_answering_it() -> None:
    """The run's collector reads the same stream.

    Intercepting would leave a turn whose record does not say a question was
    asked, which is the one thing a reader of that record most needs to see.
    """
    from raven.agent.acp.ask_user import AskUserResponder, notification_dispatcher
    from raven.agent.acp.asker import start_ask_turn

    routed: list[tuple[str, dict]] = []
    sent = _Recorder()

    async def route(method, params):
        routed.append((method, params))

    class Responders:
        def __init__(self, responder):
            self._responder = responder

        def dispatch(self, params):
            self._responder.dispatch(params)
            return True

    start_ask_turn(_Answers("EU"), conversation_id="tui:c1")
    handle = notification_dispatcher("a", route, responders=Responders(AskUserResponder("a", "h1", sent)))
    await handle("session/update", _frame())
    await _settle()

    assert [m for m, _ in routed] == ["session/update"]
    assert sent.sent


async def test_the_dispatcher_does_not_wait_on_the_user() -> None:
    """Notifications are dispatched inline on the read loop.

    Awaiting a human there stalls every other session of a pooled connection --
    the defect the request path was already fixed for, arriving by the door that
    was not.
    """
    from raven.agent.acp.ask_user import AskUserResponder, notification_dispatcher
    from raven.agent.acp.asker import start_ask_turn

    class Parks:
        async def ask(self, prompt, choices, conversation_id):
            await asyncio.sleep(3600)

    class Responders:
        def __init__(self, responder):
            self._responder = responder

        def dispatch(self, params):
            self._responder.dispatch(params)
            return True

    async def route(method, params):
        return None

    start_ask_turn(Parks(), conversation_id="tui:c1")
    responder = AskUserResponder("a", "h1", _Recorder())
    handle = notification_dispatcher("a", route, responders=Responders(responder))
    async with asyncio.timeout(1):
        await handle("session/update", _frame())
    responder.cancel()


async def test_an_ordinary_update_is_not_taken_for_a_question() -> None:
    from raven.agent.acp.ask_user import is_ask_user_update, notification_dispatcher

    taken: list[dict] = []

    class Responders:
        def dispatch(self, params):
            taken.append(params)
            return True

    routed: list[str] = []

    async def route(method, params):
        routed.append(method)

    assert not is_ask_user_update("session/update", {"update": {"sessionUpdate": "agent_message_chunk"}})
    assert not is_ask_user_update("session/cancel", _frame())
    handle = notification_dispatcher("a", route, responders=Responders())
    await handle("session/update", {"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk"}})

    assert taken == [] and routed == ["session/update"]


async def test_a_question_for_a_finished_session_is_routed_and_not_answered() -> None:
    """Nobody owns it, and raven cannot answer for a run that is gone.

    Not an error either: a question can arrive for a session whose turn has just
    settled, and the agent has its own timeout for exactly that.
    """
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.pool import _SessionResponders

    responders = _SessionResponders("a")
    assert responders.dispatch(_frame()) is False

    responder = AskUserResponder("a", "h1", _Recorder())
    responders.attach("s1", responder)
    assert responders.current("s1") is responder
    responders.detach("s1", AskUserResponder("a", "h2", _Recorder()))
    assert responders.current("s1") is responder, "a later run must not tear down this one's routing"
    responders.detach("s1", responder)
    assert responders.dispatch(_frame()) is False


async def test_a_responder_that_raises_does_not_kill_the_read_loop() -> None:
    from raven.agent.acp.ask_user import notification_dispatcher

    routed: list[str] = []

    async def route(method, params):
        routed.append(method)

    class Breaks:
        def dispatch(self, params):
            raise RuntimeError("registry is gone")

    handle = notification_dispatcher("a", route, responders=Breaks())
    await handle("session/update", _frame())

    assert routed == ["session/update"]


async def test_an_answered_question_never_reaches_the_asker() -> None:
    asker = _RecordingAsker()
    answer = await _ask_with(asker, _StubAutofill({"": ("answer", "feat/x")}), "Which branch?")
    assert answer == "feat/x"
    assert asker.asked == []


async def test_a_deferred_question_reaches_the_asker_unchanged() -> None:
    asker = _RecordingAsker("feat/y")
    answer = await _ask_with(asker, _StubAutofill({"": ("defer", "")}), "Which branch?")
    assert answer == "feat/y"
    assert asker.asked[0][0] == "raven-code(a1b2): Which branch?"


async def test_a_partial_question_reaches_the_asker_with_its_note() -> None:
    asker = _RecordingAsker("feat/y")
    await _ask_with(asker, _StubAutofill({"": ("partial", "you said feat/x earlier")}), "Which branch?")
    assert "you said feat/x earlier" in asker.asked[0][0]


async def test_an_answered_question_never_takes_the_conversation_lock() -> None:
    from raven.agent.acp.asker import question_lock

    lock = question_lock("tui:c1")
    await lock.acquire()
    try:
        answer = await asyncio.wait_for(
            _ask_with(_RecordingAsker(), _StubAutofill({"": ("answer", "feat/x")}), "Which branch?"),
            timeout=1.0,
        )
    finally:
        lock.release()
    assert answer == "feat/x"


async def test_an_answered_question_does_not_answer_for_a_run_that_ended() -> None:
    """An answered question still has to notice its run is gone.

    `resolve` awaits a model call, and `cancel` can land during it. The lock path
    re-checks the flag after its own wait for exactly this; the answered path
    skips the lock, so it needs a check of its own.

    The swallow below is what leaves the flag as the only trace: `cancel` marks
    the run and cancels the waiting task, and an absorbed cancellation -- what
    `QuestionBroker` does to the one on the asking side -- takes the other half.
    """
    import contextlib

    from raven.agent.acp import autofill
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    held: dict = {}

    class _CancellingAutofill:
        async def resolve(self, questions, *, agent, instance):
            held["responder"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
            return [autofill.Resolution(status="answer", answer="feat/x") for _ in questions]

    asker, sent = _RecordingAsker("asked the user"), _Recorder()
    start_ask_turn(asker, _CancellingAutofill(), conversation_id="tui:c1")
    held["responder"] = AskUserResponder("raven-code", "a1b2", sent)
    held["responder"].dispatch(_frame("Which branch?", choices=[]))
    await _settle()

    assert sent.sent[0]["answer"] == ""
    assert asker.asked == []


async def test_the_autofill_is_read_at_construction_not_when_the_question_lands() -> None:
    """The autofill must be read at construction, in the run's own context.

    A question arrives on the connection's read loop, whose ContextVars are a
    copy of whichever turn first opened the connection, and the pool keeps that
    connection for the life of the process. The rebinding below stands for that
    drift: a responder reading at question time would see the newest turn's
    autofill -- here, none -- and put a question raven could have answered.
    """
    from raven.agent.acp.ask_user import AskUserResponder
    from raven.agent.acp.asker import start_ask_turn

    asker, sent = _RecordingAsker("asked the user"), _Recorder()
    start_ask_turn(asker, _StubAutofill({"": ("answer", "feat/x")}), conversation_id="tui:c1")
    responder = AskUserResponder("raven-code", "a1b2", sent)
    start_ask_turn(asker, None, conversation_id="tui:c1")
    responder.dispatch(_frame("Which branch?", choices=[]))
    await _settle()

    assert sent.sent[0]["answer"] == "feat/x"
    assert asker.asked == []
