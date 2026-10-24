"""Page Stop cancels the linked turn, not a pending page correction."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from raven.channels.intake import Intake
from raven.gateway.linked_chat import Binding, LinkedChat
from raven.gateway.outlet import ChannelOutletAdapter
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import turn
from raven.rpc.question_broker import QuestionBroker
from raven.rpc.spine import build_rpc_spine
from raven.session.manager import SessionManager
from raven.spine.delivery import DeliveryHub
from tests.test_gateway_linked_chat import KEY, OWNER, Emitter, Loop, dm


@pytest.fixture
async def linked_page(tmp_path, monkeypatch):
    monkeypatch.setattr(turn, "_active_turns", {})
    monkeypatch.setattr(turn, "_pending_injects", {})
    monkeypatch.setattr(turn, "_scheduler", None)
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create(KEY)
    session.add_message("user", "existing history")
    sessions.save(session)
    loop = Loop(sessions)
    telegram = SimpleNamespace(name="telegram", capabilities=SimpleNamespace(file_attachments=True), send=AsyncMock())
    hub = DeliveryHub()
    hub.register(ChannelOutletAdapter(telegram))
    link = LinkedChat(Binding(OWNER, OWNER, KEY), loop, hub, {})
    emitter = Emitter()
    scheduler, _, turn_ids, close = build_rpc_spine(
        loop,
        emitter,
        linked_chat=link,
        on_turn_end=turn.clear_active,
        on_turn_start=turn.promote_pending_inject,
        on_turn_bind=turn.bind_active,
    )
    dispatcher = Dispatcher()
    turn.register_turn_methods(dispatcher, emitter=emitter, scheduler=scheduler, turn_ids=turn_ids)
    broker = QuestionBroker(AsyncMock(), timeout_s=2)
    subagents = SimpleNamespace(cancel_by_session=AsyncMock(return_value=0))
    tree = ast.parse((Path(__file__).resolve().parents[1] / "raven/cli/gateway_commands.py").read_text())
    dispatch = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_inbound_dispatch")
    namespace = {
        "linked_chat": link,
        "page_mount": SimpleNamespace(scheduler=scheduler),
        "question_broker": broker,
        "agent": SimpleNamespace(subagents=subagents),
    }
    exec(compile(ast.Module(body=[dispatch], type_ignores=[]), "native_gateway_dispatch", "exec"), namespace)
    intake = Intake("telegram", SimpleNamespace(allow_from=[OWNER]), allow_check=lambda s: s.split("|")[0] == OWNER)
    intake.set_submit(namespace["_inbound_dispatch"])
    rig = SimpleNamespace(
        loop=loop,
        telegram=telegram,
        hub=hub,
        link=link,
        emitter=emitter,
        scheduler=scheduler,
        turn_ids=turn_ids,
        dispatcher=dispatcher,
        intake=intake,
        subagents=subagents,
    )
    try:
        yield rig
    finally:
        loop.release.set()
        await close()
        await hub.aclose()
        broker.cancel_all()


async def rpc(rig, method, **params):
    response = await asyncio.wait_for(
        rig.dispatcher.dispatch({"jsonrpc": "2.0", "id": "test-request", "method": method, "params": params}), 2
    )
    assert "error" not in response, response
    return response["result"]


async def inbound(rig, text):
    source = dm().source
    await rig.intake.publish(source.sender_id, source.chat_id, text, metadata=source.extras)


async def running(rig):
    req = await asyncio.wait_for(rig.loop.entered.get(), 2)
    turn_id, handle = rig.scheduler.running_turn(KEY)
    assert turn_id == req.turn_id
    return req, handle


def page_stop_params(rig):
    # stages.ts binds only turn.started; runtime.ts omits a null openedTurn.
    opened = None
    for cid, event in rig.emitter.events:
        if cid != KEY:
            continue
        if event["type"] == "message.start":
            opened = None
        elif event["type"] == "turn.started":
            opened = event["payload"]["turn_id"]
    return {"session_key": KEY, **({"turn_id": opened} if opened else {})}


def cancelled_ids(rig):
    return [
        event["payload"].get("turn_id")
        for cid, event in rig.emitter.events
        if cid == KEY and event["type"] == "error" and event["payload"].get("reason") == "cancelled_by_client"
    ]


def assert_cleared(rig):
    assert not rig.scheduler.has_inflight(KEY)
    assert KEY not in rig.turn_ids
    assert KEY not in turn._active_turns
    assert KEY not in turn._pending_injects
    assert rig.link.active == {}
    assert rig.link.sources == {}


async def test_page_turn_still_cancels_without_an_id(linked_page):
    r = linked_page
    sent = await rpc(r, "turn.send", session_key=KEY, content="page work")
    req, handle = await running(r)
    params = page_stop_params(r)
    assert params == {"session_key": KEY}
    assert await rpc(r, "turn.cancel", **params) == {"cancelled": True}
    assert await handle.result() is None
    assert req.turn_id == sent["turn_id"]
    assert cancelled_ids(r) == [req.turn_id]
    assert_cleared(r)


@pytest.mark.parametrize("pending_page", [False, True])
async def test_page_stop_reaches_linked_turn_and_preserves_pending_inject(linked_page, pending_page):
    r = linked_page
    await inbound(r, "linked work")
    host, handle = await running(r)
    openings = [
        event for cid, event in r.emitter.events if cid == KEY and event["type"] in ("message.start", "turn.started")
    ]
    assert openings == [{"type": "message.start", "payload": {"turn_id": host.turn_id, "content": "linked work"}}]
    if pending_page:
        sent = await rpc(r, "turn.send", session_key=KEY, content="page correction", busy="inject")
        assert sent["accepted"]
        assert sent["turn_id"] in turn._pending_injects[KEY]
        assert len(r.scheduler._lanes[KEY]._inject_mailbox) == 1
    params = page_stop_params(r)
    assert await rpc(r, "turn.cancel", **params) == {"cancelled": True}
    assert KEY not in r.link.active, "Page Stop left the Telegram turn running"
    assert await asyncio.wait_for(handle.result(), 2) is None
    assert cancelled_ids(r) == [host.turn_id]
    if pending_page:
        correction, correction_handle = await running(r)
        assert correction.turn_id == sent["turn_id"]
        assert correction.source.channel == "tui"
        assert correction.text == "page correction"
        assert r.turn_ids[KEY] == correction.turn_id
        assert turn.is_turn_active(KEY)
        assert KEY not in turn._pending_injects
        r.loop.release.set()
        assert await asyncio.wait_for(correction_handle.result(), 2) is not None
        assert [req.text for req, _ in r.loop.calls] == ["linked work", "page correction"]
        assert [m["content"] for m in r.loop.sessions.peek(KEY).messages if m["role"] == "user"] == [
            "existing history",
            "linked work",
            "page correction",
        ]
    else:
        assert r.loop.running == 0
    await r.hub.wait_idle("telegram")
    r.telegram.send.assert_not_called()
    assert cancelled_ids(r) == [host.turn_id]
    assert_cleared(r)


async def test_named_linked_cancel_emits_exactly_one_frame(linked_page):
    r = linked_page
    await inbound(r, "linked work")
    host, handle = await running(r)
    assert await rpc(r, "turn.cancel", session_key=KEY, turn_id=host.turn_id) == {"cancelled": True}
    assert await handle.result() is None
    assert r.loop.running == 0
    assert cancelled_ids(r) == [host.turn_id]
    assert_cleared(r)


async def test_stale_named_cancel_leaves_linked_turn_and_pending_inject_alone(linked_page):
    r = linked_page
    await inbound(r, "linked work")
    host, handle = await running(r)
    sent = await rpc(r, "turn.send", session_key=KEY, content="page correction", busy="inject")
    assert await rpc(r, "turn.cancel", session_key=KEY, turn_id="finished-turn") == {"cancelled": False}
    assert r.scheduler.running_turn(KEY)[0] == host.turn_id
    assert sent["turn_id"] in turn._pending_injects[KEY]
    assert cancelled_ids(r) == []
    correction = turn._pending_injects[KEY][sent["turn_id"]][0]
    r.loop.release.set()
    assert await asyncio.wait_for(handle.result(), 2) is not None
    assert await asyncio.wait_for(correction.result(), 2) is not None
    assert [req.text for req, _ in r.loop.calls] == ["linked work", "page correction"]
    await r.hub.wait_idle("telegram")
    r.telegram.send.assert_awaited_once_with(OWNER, "reply:linked work")
    assert_cleared(r)


@pytest.mark.parametrize("surface", ["page", "telegram"])
async def test_dm_stop_still_reports_the_active_turn_and_discards_pending_page_input(linked_page, surface):
    r = linked_page
    if surface == "page":
        await rpc(r, "turn.send", session_key=KEY, content="page work")
    else:
        await inbound(r, "linked work")
    host, handle = await running(r)
    sent = await rpc(r, "turn.send", session_key=KEY, content="page correction", busy="inject")
    correction = turn._pending_injects[KEY][sent["turn_id"]][0]
    await inbound(r, "/stop")
    assert await handle.result() is None
    assert await correction.result() is None
    assert cancelled_ids(r) == [host.turn_id]
    assert len(r.loop.calls) == 1
    assert r.loop.sessions.peek(KEY).messages[0]["content"] == "existing history"
    r.subagents.cancel_by_session.assert_awaited_once_with(KEY, reason="the user sent /stop")
    await r.hub.wait_idle("telegram")
    r.telegram.send.assert_awaited_once_with(OWNER, "Stopped 2 task(s).")
    assert_cleared(r)
