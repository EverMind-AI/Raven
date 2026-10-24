"""Shared-lane stops and questions retain their originating turn's authority."""

import asyncio
from types import SimpleNamespace

import pytest

from raven.acp_client.asker import current_ask
from raven.agent.subagent.manager import SubagentManager
from raven.agent.tools.ask_user import AskUserTool
from raven.cli.gateway_commands import _deliver_question_to_channel
from raven.gateway.linked_chat import Binding, LinkedChat
from raven.gateway.submit_router import route_submit
from raven.rpc.question_broker import QuestionBroker, RoutingQuestionBroker
from raven.rpc.spine import build_rpc_spine
from raven.rpc.subscriptions import QUEUE_CAPACITY, SubscriptionEmitter
from raven.session.manager import SessionManager
from raven.spine import ChatType, Origin, Source, StreamDelta, Text, TurnRequest
from tests.test_gateway_linked_chat import KEY, OWNER, Emitter, Hub, Loop, dm


def ui(text):
    return TurnRequest(
        Origin.USER,
        Source("tui", KEY.removeprefix("tui:"), "user", ChatType.DM),
        text,
        conversation=KEY,
        turn_id=text,
    )


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def no_children(*args, **kwargs):
    return 0


def rig(root, emitter=None):
    sessions = SessionManager(root)
    sessions.save(sessions.get_or_create(KEY))
    loop = Loop(sessions)
    hub = Hub()
    link = LinkedChat(Binding(OWNER, OWNER, KEY), loop, hub, {})
    scheduler, _, turn_ids, close = build_rpc_spine(loop, emitter or Emitter(), linked_chat=link)
    broker = QuestionBroker(lambda frame: asyncio.sleep(0), timeout_s=2)
    return SimpleNamespace(
        loop=loop, hub=hub, link=link, scheduler=scheduler, turn_ids=turn_ids, close=close, broker=broker
    )


@pytest.mark.parametrize("unstarted", [False, True])
async def test_stop_cancels_native_children_without_starting_a_result_turn(tmp_path, unstarted):
    r = rig(tmp_path)
    manager = SubagentManager(SimpleNamespace(get_default_model=lambda: "synthetic-model"), tmp_path, agents=[])
    manager.set_submit(route_submit(page=r.scheduler.submit, channel=lambda req: pytest.fail("wrong lane")))
    origin = {"channel": "telegram", "chat_id": OWNER, "session_key": KEY}
    started = asyncio.Event()

    async def child():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await manager._announce_cancelled(
                "child", "synthetic child", "synthetic work", origin, manager._cancel_reasons.pop("child")
            )
            raise

    task = asyncio.create_task(asyncio.Event().wait() if unstarted else child())
    manager._running_tasks["child"] = task
    manager._session_tasks[KEY] = {"child"}
    if unstarted:
        manager._unstarted["child"] = ("synthetic work", "synthetic child", origin, None)
    else:
        await started.wait()
        await r.link.dispatch(dm("host"), r.scheduler, r.broker, manager)
        await r.loop.entered.get()
    try:
        await r.link.dispatch(dm("/stop"), r.scheduler, r.broker, manager)
        await asyncio.sleep(0)
        assert task.cancelled()
        assert not r.scheduler.has_running()
        assert not any(req.origin is Origin.SUBAGENT for req, _ in r.loop.calls)
        assert "child" not in manager._unstarted
        assert any(isinstance(e, Text) and e.content.startswith("Stopped ") for e in r.hub.events)
        r.loop.release.set()
        await r.scheduler.submit(ui("new owner request")).result()
        assert r.loop.calls[-1][0].text == "new owner request"
    finally:
        r.loop.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await r.close()


async def test_stop_reaches_running_work_before_ui_backpressure(tmp_path):
    blocked, overflow, network_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send_frame(frame):
        event = frame.get("params", {}).get("event", {})
        (overflow if event.get("payload", {}).get("code") == -32016 else blocked).set()
        await network_release.wait()

    emitter = SubscriptionEmitter(send_frame)
    subscription = await emitter.register(KEY)
    await emitter.emit(KEY, {"type": "message.start", "payload": {"turn_id": "holding"}})
    await asyncio.wait_for(blocked.wait(), 2)
    for _ in range(QUEUE_CAPACITY):
        await emitter.emit(KEY, {"type": "notice", "payload": {"detail": "synthetic backlog"}})
    r = rig(tmp_path, emitter)
    mutate, cancelled = asyncio.Event(), asyncio.Event()
    effects = []

    async def action(req):
        try:
            await r.scheduler._sink(StreamDelta("before-stop", source=req.source, conversation_id=KEY))
            await mutate.wait()
            effects.append("business write")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    r.loop.action = action
    running = r.scheduler.submit(ui("holding"))
    await asyncio.wait_for(overflow.wait(), 2)
    queued = r.scheduler.submit(ui("queued-ui"))
    r.turn_ids[KEY] = "queued-ui"
    stopper = asyncio.create_task(
        r.link.dispatch(dm("/stop"), r.scheduler, r.broker, SimpleNamespace(cancel_by_session=no_children))
    )
    try:
        await wait_until(lambda: not r.scheduler._lanes[KEY]._pending)
        assert r.scheduler._lanes[KEY]._run_task.cancelling() > 0
        mutate.set()
        await asyncio.wait_for(cancelled.wait(), 2)
        assert effects == []
    finally:
        network_release.set()
        await asyncio.wait_for(stopper, 2)
        await queued.result()
        await running.result()
        r.loop.release.set()
        await r.close()
        await emitter.unregister(subscription)


@pytest.mark.parametrize("next_surface", ["tui", "telegram"])
async def test_expired_dm_asker_cannot_question_a_later_turn(tmp_path, next_surface):
    r = rig(tmp_path)
    frames = []

    async def question(frame):
        frames.append(frame)

    page_broker = QuestionBroker(question, timeout_s=2)
    channel_broker = QuestionBroker(question, timeout_s=2)
    r.loop.tools["ask_user"] = AskUserTool(
        RoutingQuestionBroker(page_broker, channel_broker, channel_route=r.link.question_route)
    )
    captured = []

    async def action(req):
        captured.append(current_ask())

    r.loop.action = action
    children = SimpleNamespace(cancel_by_session=no_children)
    try:
        await r.link.dispatch(dm("first"), r.scheduler, channel_broker, children)
        await r.loop.entered.get()
        asker, cid = captured[0]
        pending = asyncio.create_task(asker.ask("current DM question", None, cid))
        await wait_until(lambda: channel_broker.pending_req(KEY))
        assert page_broker.pending_req(KEY) is None
        await r.link.dispatch(dm("DM answer"), r.scheduler, channel_broker, children)
        assert await pending == "DM answer"
        r.loop.release.set()
        await wait_until(lambda: not r.scheduler.has_running())
        r.loop.release.clear()
        if next_surface == "tui":
            r.scheduler.submit(ui("second"))
        else:
            await r.link.dispatch(dm("second"), r.scheduler, channel_broker, children)
        await r.loop.entered.get()
        before = list(frames)
        assert await asyncio.wait_for(asker.ask("expired DM question", None, cid), 0.5) == ""
        assert frames == before
        assert page_broker.pending_req(KEY) is None
        assert channel_broker.pending_req(KEY) is None
    finally:
        r.loop.release.set()
        await r.close()


async def test_captured_page_asker_does_not_follow_an_unrelated_dm(tmp_path):
    r = rig(tmp_path)
    frames = []

    async def page(frame):
        frames.append(frame)

    async def channel(frame):
        await _deliver_question_to_channel(frame, sources=r.link.sources, hub=r.hub)

    page_broker, channel_broker = QuestionBroker(page, timeout_s=2), QuestionBroker(channel, timeout_s=2)
    routed = RoutingQuestionBroker(page_broker, channel_broker, channel_route=r.link.question_route)
    r.loop.tools["ask_user"] = AskUserTool(routed)
    captured = []

    async def action(req):
        captured.append(current_ask())

    r.loop.action = action
    r.loop.release.set()
    pending = None
    try:
        await r.scheduler.submit(ui("page-launch")).result()
        asker, cid = captured[0]
        pending = asyncio.create_task(asker.ask("page control", None, cid))
        await wait_until(lambda: page_broker.pending_req(KEY))
        assert channel_broker.pending_req(KEY) is None
        page_broker.reply(KEY, "page answer")
        assert await pending == "page answer"
        r.loop.release.clear()
        await r.link.dispatch(
            dm("unrelated DM"), r.scheduler, channel_broker, SimpleNamespace(cancel_by_session=no_children)
        )
        await wait_until(lambda: r.link.channel_active(KEY) and KEY in r.link.sources)
        pending = asyncio.create_task(asker.ask("page background question", None, cid))
        await wait_until(lambda: channel_broker.pending_req(KEY) or page_broker.pending_req(KEY))
        assert page_broker.pending_req(KEY) is not None
        assert channel_broker.pending_req(KEY) is None
        assert not any(isinstance(e, Text) and "page background question" in e.content for e in r.hub.events)
        page_broker.reply(KEY, "still the page")
        assert await pending == "still the page"
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        r.loop.release.set()
        await r.close()
