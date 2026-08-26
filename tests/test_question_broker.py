"""Tests for the QuestionBroker (ask-user round-trip, keyed by conversation_id).

Mirrors the ConfirmBroker tests: pending_req before/after, reply resolves the
future (by either handle), idempotent late/duplicate reply, timeout fail-safe to
default, cancel_all, and overlapping-question replacement.
"""

from __future__ import annotations

import asyncio

from raven.rpc.methods.question import question_respond, register_question_methods
from raven.rpc.question_broker import QuestionBroker, QuestionUndeliverableError, RoutingQuestionBroker

CID = "telegram:123"
PAGE_CID = "tui:abc123"


def _frame_collector() -> tuple[list[dict], object]:
    frames: list[dict] = []

    async def send_frame(frame: dict) -> None:
        frames.append(frame)

    return frames, send_frame


async def _wait_for_frame(frames: list[dict], timeout: float = 1.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while not frames:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("clarify.request frame never emitted")
        await asyncio.sleep(0.005)
    return frames[0]


async def test_question_request_notification_emitted() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="Which?", choices=["a", "b"], default="x"))
    frame = await _wait_for_frame(frames)

    assert "id" not in frame
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "clarify.request"
    params = frame["params"]
    assert params["conversation_id"] == CID
    assert isinstance(params["request_id"], str) and params["request_id"]
    assert params["question"] == "Which?"
    assert params["choices"] == ["a", "b"]

    broker.reply(CID, "a")
    assert await task == "a"


async def test_pending_req_before_and_after() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    assert broker.pending_req(CID) is None

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    frame = await _wait_for_frame(frames)
    rid = frame["params"]["request_id"]

    assert broker.pending_req(CID) == rid

    broker.reply(CID, "answer")
    await task
    assert broker.pending_req(CID) is None


async def test_reply_by_conversation_id_resolves() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)

    assert broker.reply(CID, "yes") is True
    assert await task == "yes"


async def test_reply_by_request_id_resolves() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    frame = await _wait_for_frame(frames)
    rid = frame["params"]["request_id"]

    assert broker.reply(rid, "via-rid") is True
    assert await task == "via-rid"


async def test_reply_idempotent_late_and_duplicate() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)

    assert broker.reply(CID, "first") is True
    assert await task == "first"
    # registry cleaned up — a duplicate / late reply is a no-op
    assert broker.reply(CID, "second") is False


async def test_reply_unknown_key_idempotent() -> None:
    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    assert broker.reply("does-not-exist", "x") is False


async def test_timeout_failsafe_to_default() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    result = await broker.await_question(CID, prompt="?", default="fallback", timeout_s=0.05)
    assert result == "fallback"
    await _wait_for_frame(frames)  # it did emit the request first
    assert broker.pending_req(CID) is None


def _of_method(frames: list[dict], method: str) -> list[dict]:
    return [f for f in frames if f.get("method") == method]


async def _wait_for_method(frames: list[dict], method: str, timeout: float = 1.0) -> dict:
    """The first frame of ``method``. The close notification is sent on its own
    task, so it lands a tick after ``await_question`` has already returned."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not _of_method(frames, method):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no {method} frame within {timeout}s: {[f.get('method') for f in frames]}")
        await asyncio.sleep(0.005)
    return _of_method(frames, method)[0]


async def test_timeout_closes_the_question_on_the_surface() -> None:
    # Without this the surface keeps a sheet nobody can answer: the frontend
    # only clears `clarify` when the user answers, so a backend fail-safe has to
    # say so. Mirrors `approval.closed`.
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    assert await broker.await_question(CID, prompt="?", default="fallback", timeout_s=0.05) == "fallback"
    request = _of_method(frames, "clarify.request")[0]
    closed = await _wait_for_method(frames, "clarify.closed")
    assert closed["params"]["request_id"] == request["params"]["request_id"]
    assert closed["params"]["conversation_id"] == CID


async def test_an_answered_question_is_not_closed() -> None:
    # The surface already dropped its own sheet when it sent the answer, so a
    # close here would be noise -- and would race a later question's request.
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)
    broker.reply(CID, "answer")

    assert await task == "answer"
    assert _of_method(frames, "clarify.closed") == []


async def test_a_cancelled_question_closes_the_surface() -> None:
    # An interrupted turn is the other way a question dies unanswered, and it is
    # why `resetFlowOverlays` used to drop the sheet. It no longer does, so the
    # close has to survive the cancellation that caused it.
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)
    task.cancel()

    assert await task == "d"
    await _wait_for_method(frames, "clarify.closed")


async def test_cancel_all_failsafe() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="bye"))
    await _wait_for_frame(frames)

    broker.cancel_all()
    assert await task == "bye"


async def test_overlapping_question_replaces_stale() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    first = asyncio.create_task(broker.await_question(CID, prompt="q1", default="d1"))
    await _wait_for_frame(frames)

    second = asyncio.create_task(broker.await_question(CID, prompt="q2", default="d2"))
    # The stale first question fail-safes to its own default.
    assert await first == "d1"

    # Let the second emit, then resolve it.
    while len(frames) < 2:
        await asyncio.sleep(0.005)
    broker.reply(CID, "q2-answer")
    assert await second == "q2-answer"


async def test_question_respond_handler_resolves() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)
    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    await _wait_for_frame(frames)

    result = await question_respond({"conversation_id": CID, "answer": "ok"}, question_broker=broker)

    assert result == {"ok": True}
    assert await task == "ok"


async def test_question_respond_handler_unknown_returns_not_ok() -> None:
    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    result = await question_respond({"conversation_id": "nope", "answer": "x"}, question_broker=broker)

    assert result == {"ok": False}


async def test_register_question_methods_adds_respond() -> None:
    from raven.rpc.dispatcher import Dispatcher

    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)
    dispatcher = Dispatcher()
    register_question_methods(dispatcher, question_broker=broker)

    assert "clarify.respond" in dispatcher.methods()


# ---------------------------------------------------------------------------
# RoutingQuestionBroker (the gateway's page + IM channels on one shared loop)
# ---------------------------------------------------------------------------


def _routing_pair() -> tuple[RoutingQuestionBroker, QuestionBroker, list[dict], QuestionBroker, list[dict]]:
    page_frames, page_send = _frame_collector()
    channel_frames, channel_send = _frame_collector()
    page = QuestionBroker(page_send)
    channel = QuestionBroker(channel_send)
    return RoutingQuestionBroker(page=page, channel=channel), page, page_frames, channel, channel_frames


async def test_routing_tui_conversation_reaches_the_page_broker() -> None:
    routed, page, page_frames, _channel, channel_frames = _routing_pair()

    task = asyncio.create_task(routed.await_question(PAGE_CID, prompt="?", default="d"))
    frame = await _wait_for_frame(page_frames)

    assert frame["params"]["conversation_id"] == PAGE_CID
    assert channel_frames == []
    assert page.pending_req(PAGE_CID) is not None

    page.reply(PAGE_CID, "from the page")
    assert await task == "from the page"


async def test_routing_im_conversation_reaches_the_channel_broker() -> None:
    routed, _page, page_frames, channel, channel_frames = _routing_pair()

    task = asyncio.create_task(routed.await_question(CID, prompt="?", default="d"))
    frame = await _wait_for_frame(channel_frames)

    assert frame["params"]["conversation_id"] == CID
    assert page_frames == []
    assert channel.pending_req(CID) is not None

    channel.reply(CID, "from feishu")
    assert await task == "from feishu"


async def test_routing_pending_req_and_reply_consult_both() -> None:
    routed, _page, page_frames, _channel, channel_frames = _routing_pair()

    page_task = asyncio.create_task(routed.await_question(PAGE_CID, prompt="?", default="pd"))
    channel_task = asyncio.create_task(routed.await_question(CID, prompt="?", default="cd"))
    page_frame = await _wait_for_frame(page_frames)
    channel_frame = await _wait_for_frame(channel_frames)

    assert routed.pending_req(PAGE_CID) == page_frame["params"]["request_id"]
    assert routed.pending_req(CID) == channel_frame["params"]["request_id"]

    assert routed.reply(PAGE_CID, "page answer") is True
    assert routed.reply(CID, "channel answer") is True
    assert await page_task == "page answer"
    assert await channel_task == "channel answer"
    assert routed.pending_req(PAGE_CID) is None
    assert routed.pending_req(CID) is None
    assert routed.reply(CID, "late") is False


async def test_routing_cancel_all_failsafes_both() -> None:
    routed, _page, page_frames, _channel, channel_frames = _routing_pair()

    page_task = asyncio.create_task(routed.await_question(PAGE_CID, prompt="?", default="pd"))
    channel_task = asyncio.create_task(routed.await_question(CID, prompt="?", default="cd"))
    await _wait_for_frame(page_frames)
    await _wait_for_frame(channel_frames)

    routed.cancel_all()
    assert await page_task == "pd"
    assert await channel_task == "cd"


async def test_deep_research_clarify_follows_the_same_routing() -> None:
    """The deep-vs-regular clarify keys by the same conversation, so through
    the routing shim a page session's clarify reaches the page broker and an
    IM session's the channel broker."""
    from raven.agent.tools.deep_research import _MODE_DEEP, _ask_search_mode

    routed, page, page_frames, channel, channel_frames = _routing_pair()

    page_task = asyncio.create_task(_ask_search_mode(routed, PAGE_CID))
    frame = await _wait_for_frame(page_frames)
    assert frame["params"]["conversation_id"] == PAGE_CID
    page.reply(PAGE_CID, _MODE_DEEP)
    assert await page_task == "deep"

    channel_task = asyncio.create_task(_ask_search_mode(routed, CID))
    frame = await _wait_for_frame(channel_frames)
    assert frame["params"]["conversation_id"] == CID
    channel.reply(CID, _MODE_DEEP)
    assert await channel_task == "deep"


# ---------------------------------------------------------------------------
# Per-conversation scoping of the notification itself
# (connection.conversation_scoped, what the gateway's page broker is built on)
# ---------------------------------------------------------------------------


async def _hold_connection(sink, conversation_id: str, ready: asyncio.Event, release: asyncio.Event) -> None:
    """Stand in for one live socket: its own connection scope, held open in its
    own task, exactly as a transport holds one while it dispatches frames."""
    from raven.rpc import connection

    token = connection.bind_connection()
    connection.set_frame_sink(sink)
    connection.claim_conversation(conversation_id)
    ready.set()
    try:
        await release.wait()
    finally:
        connection.unbind_connection(token)


async def test_a_question_reaches_only_the_connection_that_owns_the_conversation() -> None:
    """One gateway, two live surfaces: a relayed terminal and a browser tab on
    a session of its own. The question asked inside the terminal's session must
    not open the sheet in the tab, which is not in that conversation.

    The broker itself runs where the engine does -- no connection bound -- which
    is why the ownership has to be recorded rather than read off the context.
    """
    from raven.rpc import connection

    broadcast_frames, broadcast = _frame_collector()
    terminal_frames, terminal_send = _frame_collector()
    tab_frames, tab_send = _frame_collector()

    release = asyncio.Event()
    tab_ready, term_ready = asyncio.Event(), asyncio.Event()
    tab = asyncio.create_task(_hold_connection(tab_send, "tui:tab", tab_ready, release))
    term = asyncio.create_task(_hold_connection(terminal_send, "tui:term", term_ready, release))
    await asyncio.wait_for(tab_ready.wait(), 1)
    await asyncio.wait_for(term_ready.wait(), 1)

    try:
        broker = QuestionBroker(connection.conversation_scoped(broadcast))
        task = asyncio.create_task(broker.await_question("tui:term", prompt="?", default="d"))
        frame = await _wait_for_frame(terminal_frames)
        assert frame["params"]["conversation_id"] == "tui:term"
        assert tab_frames == []
        assert broadcast_frames == []
        broker.reply("tui:term", "typed in the terminal")
        assert await task == "typed in the terminal"
    finally:
        release.set()
        await asyncio.gather(tab, term)


async def test_an_unowned_conversation_still_broadcasts() -> None:
    """A cron or IM turn runs with no connection bound, so nobody claimed its
    conversation. Broadcast stays the fallback: a question that reaches nobody
    stalls the turn until it times out."""
    from raven.rpc import connection

    broadcast_frames, broadcast = _frame_collector()
    broker = QuestionBroker(connection.conversation_scoped(broadcast))

    task = asyncio.create_task(broker.await_question(CID, prompt="?", default="d"))
    frame = await _wait_for_frame(broadcast_frames)
    assert frame["params"]["conversation_id"] == CID
    broker.reply(CID, "answered")
    assert await task == "answered"


async def test_a_claim_dies_with_its_connection() -> None:
    """The owner's socket closing must not strand the question: the claim is
    dropped on unbind, and a send that fails first falls back too."""
    from raven.rpc import connection

    broadcast_frames, broadcast = _frame_collector()
    scoped = connection.conversation_scoped(broadcast)

    async def dead_sink(_frame: dict) -> None:
        raise ConnectionResetError("socket closed")

    token = connection.bind_connection()
    connection.set_frame_sink(dead_sink)
    connection.claim_conversation(PAGE_CID)
    await scoped({"jsonrpc": "2.0", "method": "clarify.request", "params": {"conversation_id": PAGE_CID}})
    assert len(broadcast_frames) == 1

    connection.unbind_connection(token)
    assert connection.frame_sink_for(PAGE_CID) is None
    await scoped({"jsonrpc": "2.0", "method": "clarify.request", "params": {"conversation_id": PAGE_CID}})
    assert len(broadcast_frames) == 2


async def test_undeliverable_question_fails_fast_to_default() -> None:
    """A surface that cannot render the question must not cost the full budget.

    The gateway drops a question whose conversation has no live source. Before,
    the drop was invisible to the broker, which then waited out the whole
    timeout on a question nobody would ever see.
    """

    async def send_frame(frame: dict) -> None:
        raise QuestionUndeliverableError("no live source")

    broker = QuestionBroker(send_frame, timeout_s=30.0)
    started = asyncio.get_running_loop().time()

    answer = await broker.await_question(CID, prompt="?", default="fallback")

    assert answer == "fallback"
    assert asyncio.get_running_loop().time() - started < 1.0
    assert broker.pending_req(CID) is None


async def test_construction_timeout_is_the_default_budget() -> None:
    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame, timeout_s=0.05)

    assert await broker.await_question(CID, prompt="?", default="d") == "d"


async def test_per_call_timeout_overrides_the_construction_default() -> None:
    _frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame, timeout_s=30.0)

    assert await broker.await_question(CID, prompt="?", default="d", timeout_s=0.05) == "d"


async def test_clarify_request_carries_header_and_batch_progress() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)
    batch = [{"question": "Base?", "header": "Base"}, {"question": "Squash?", "header": "Squash"}]

    task = asyncio.create_task(
        broker.await_question(CID, prompt="Squash?", header="Squash", index=1, total=2, batch=batch)
    )
    params = (await _wait_for_frame(frames))["params"]

    assert params["header"] == "Squash"
    assert params["index"] == 1
    assert params["total"] == 2
    assert params["batch"] == batch

    broker.reply(CID, "yes")
    await task


async def test_single_question_still_carries_a_one_item_batch() -> None:
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame)

    task = asyncio.create_task(broker.await_question(CID, prompt="Which?"))
    params = (await _wait_for_frame(frames))["params"]

    assert params["header"] == ""
    assert params["index"] == 0
    assert params["total"] == 1
    assert params["batch"] == [{"question": "Which?", "header": ""}]

    broker.reply(CID, "a")
    await task


async def test_clarify_request_carries_the_recommended_option_and_the_budget() -> None:
    """The surface needs both to do its half: mark the recommended choice, and
    tell the user how long the question will stand."""
    frames, send_frame = _frame_collector()
    broker = QuestionBroker(send_frame, timeout_s=45.0)

    task = asyncio.create_task(broker.await_question(CID, prompt="Ship?", choices=["hold", "ship"], recommended="ship"))
    params = (await _wait_for_frame(frames))["params"]

    assert params["recommended"] == "ship"
    assert params["timeout_s"] == 45.0

    broker.reply(CID, "ship")
    await task
