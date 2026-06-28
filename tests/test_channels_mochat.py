"""Tests for the mochat adapter package — pure parsing/decision helpers
(parsing.py) and the channel's pure methods (dedup, id-list, group-id).
Socket.IO / HTTP / polling flows are left to integration/manual testing."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.channels.adapters.mochat import parsing as mp
from raven.channels.adapters.mochat.api import MochatAPI
from raven.channels.adapters.mochat.channel import MochatChannel


def _channel():
    return MochatChannel(SimpleNamespace())


# ── parsing: content / target ─────────────────────────────────────────


def test_normalize_content():
    assert mp.normalize_content("  hi  ") == "hi"
    assert mp.normalize_content(None) == ""
    assert mp.normalize_content({"a": 1}) == '{"a": 1}'


def test_resolve_target_prefixes():
    assert mp.resolve_target("panel:p1") == mp.MochatTarget("p1", True)
    assert mp.resolve_target("group:g1").is_panel is True
    assert mp.resolve_target("mochat:session_x") == mp.MochatTarget("session_x", False)
    assert mp.resolve_target("session_abc").is_panel is False
    assert mp.resolve_target("plainid").is_panel is True  # non-session -> panel
    assert mp.resolve_target("   ").id == ""


# ── parsing: mentions ─────────────────────────────────────────────────


def test_extract_mention_ids():
    assert mp.extract_mention_ids(["a", " b ", ""]) == ["a", "b"]
    assert mp.extract_mention_ids([{"id": "x"}, {"userId": "y"}, {"_id": "z"}]) == ["x", "y", "z"]
    assert mp.extract_mention_ids("notalist") == []


def test_resolve_was_mentioned():
    assert mp.resolve_was_mentioned({"meta": {"mentioned": True}}, "u1") is True
    assert mp.resolve_was_mentioned({"meta": {"mentions": ["u1"]}}, "u1") is True
    assert mp.resolve_was_mentioned({"content": "hey <@u1>"}, "u1") is True
    assert mp.resolve_was_mentioned({"content": "hey @u1"}, "u1") is True
    assert mp.resolve_was_mentioned({"content": "hi"}, "u1") is False
    assert mp.resolve_was_mentioned({"content": "hi"}, "") is False


def test_resolve_require_mention():
    cfg = SimpleNamespace(
        groups={"g1": SimpleNamespace(require_mention=True)},
        mention=SimpleNamespace(require_in_groups=False),
    )
    assert mp.resolve_require_mention(cfg, "s1", "g1") is True   # per-group
    assert mp.resolve_require_mention(cfg, "s1", "other") is False  # global fallback


# ── parsing: buffering / timestamp ────────────────────────────────────


def test_build_buffered_body():
    e1 = mp.MochatBufferedEntry(raw_body="one", author="a", sender_name="Alice", group_id="g")
    e2 = mp.MochatBufferedEntry(raw_body="two", author="b", sender_name="Bob", group_id="g")
    assert mp.build_buffered_body([e1], False) == "one"
    grouped = mp.build_buffered_body([e1, e2], True)
    assert "Alice: one" in grouped and "Bob: two" in grouped
    assert mp.build_buffered_body([e1, e2], False) == "one\ntwo"


def test_parse_timestamp():
    assert mp.parse_timestamp("2026-06-04T00:00:00Z") is not None
    assert mp.parse_timestamp("not-a-date") is None
    assert mp.parse_timestamp(12345) is None


def test_safe_dict_and_str_field():
    assert mp.safe_dict({"a": 1}) == {"a": 1}
    assert mp.safe_dict("x") == {}
    assert mp.str_field({"a": "", "b": " v "}, "a", "b") == "v"
    assert mp.str_field({}, "a") == ""


# ── channel pure methods ──────────────────────────────────────────────


def test_dedup_remembers_per_key():
    from raven.channels.adapters.mochat.pipeline import Dedup
    d = Dedup()
    assert d.seen("k", "m1") is False  # first time
    assert d.seen("k", "m1") is True   # duplicate
    assert d.seen("k", "m2") is False  # different id
    assert d.seen("k2", "m1") is False  # same id, different target key


def test_dedup_fifo_cap_evicts_oldest():
    from raven.channels.adapters.mochat.pipeline import Dedup
    d = Dedup(cap=2)
    d.seen("k", "m1")
    d.seen("k", "m2")
    d.seen("k", "m3")  # m1 evicted
    assert d.seen("k", "m1") is False   # forgotten -> accepted again
    assert d.seen("k", "m3") is True    # still remembered




def test_normalize_id_list():
    assert MochatChannel._normalize_id_list(["a", "*", " b "]) == (["a", "b"], True)
    assert MochatChannel._normalize_id_list(["x"]) == (["x"], False)


def test_make_synthetic_event():
    evt = mp.make_synthetic_event(
        "m1", "a1", "hi", {"k": 1}, "g1", "c1",
        timestamp="2026-01-01T00:00:00Z", author_info={"nick": "x"},
    )
    assert evt["type"] == "message.add"
    assert evt["timestamp"] == "2026-01-01T00:00:00Z"
    p = evt["payload"]
    assert (p["messageId"], p["author"], p["content"]) == ("m1", "a1", "hi")
    assert (p["groupId"], p["converseId"]) == ("g1", "c1")
    assert p["meta"] == {"k": 1}
    assert p["authorInfo"] == {"nick": "x"}

    evt2 = mp.make_synthetic_event("m", "a", "c", None, "g", "c2")
    assert "authorInfo" not in evt2["payload"]  # omitted when not provided
    assert evt2["timestamp"]  # auto-generated when none passed
    assert evt2["payload"]["meta"] == {}  # safe_dict(None) -> {}


def test_ack_items():
    assert MochatChannel._ack_items([{"a": 1}, "skip"]) == [{"a": 1}]
    assert MochatChannel._ack_items({"sessions": [{"s": 1}]}) == [{"s": 1}]
    assert MochatChannel._ack_items({"sessionId": "s"}) == [{"sessionId": "s"}]
    assert MochatChannel._ack_items("nope") == []
    assert MochatChannel._ack_items({}) == []


def test_cursor_store_mark_keeps_max(tmp_path):
    from raven.channels.adapters.mochat.cursors import CursorStore
    store = CursorStore(tmp_path)
    store._save_task = MagicMock()
    store._save_task.done = lambda: False  # skip scheduling the debounced save
    store.mark("s1", 5)
    assert store.get("s1") == 5
    store.mark("s1", 3)   # lower -> ignored
    store.mark("s1", -1)  # negative -> ignored
    assert store.get("s1") == 5
    store.mark("s1", 7)   # higher -> updates
    assert store.get("s1") == 7
    assert "s1" in store and "s2" not in store


def test_seed_targets_from_config():
    ch = MochatChannel(SimpleNamespace(sessions=["session_a", "*"], panels=["p1"]))
    ch._seed_targets_from_config()
    assert "session_a" in ch._session_set and ch._auto_discover_sessions is True  # "*" -> auto
    assert "p1" in ch._panel_set and ch._auto_discover_panels is False
    assert "session_a" in ch._cold_sessions  # no saved cursor yet


def test_cursor_store_load_filters_invalid(tmp_path):
    from raven.channels.adapters.mochat.cursors import CursorStore
    store = CursorStore(tmp_path)
    (tmp_path / "session_cursors.json").write_text(
        json.dumps({"cursors": {"s1": 5, "neg": -1, "s2": 3}})
    )
    asyncio.run(store.load())
    assert store.snapshot() == {"s1": 5, "s2": 3}  # negative cursor filtered out


def test_cursor_store_save_load_roundtrip_and_close(tmp_path):
    """close() cancels the pending debounced save and persists immediately;
    a fresh store loads the same map back (restart resume)."""
    from raven.channels.adapters.mochat.cursors import CursorStore

    async def scenario():
        store = CursorStore(tmp_path, debounce_s=60)   # debounce far away
        store.mark("s1", 5)                            # schedules a save task
        pending = store._save_task
        await store.close()                            # cancel + immediate save
        await asyncio.sleep(0)                         # let the cancellation land
        assert pending.cancelled()

        fresh = CursorStore(tmp_path)
        await fresh.load()
        assert fresh.snapshot() == {"s1": 5}

    asyncio.run(scenario())


def test_cursor_store_debounced_save_fires(tmp_path):
    from raven.channels.adapters.mochat.cursors import CursorStore

    async def scenario():
        store = CursorStore(tmp_path, debounce_s=0)
        store.mark("s1", 9)
        await store._save_task                         # debounce elapses -> saved
        data = json.loads((tmp_path / "session_cursors.json").read_text())
        assert data["cursors"] == {"s1": 9} and data["schemaVersion"] == 1

    asyncio.run(scenario())


# ── MochatAPI: HTTP transport ─────────────────────────────────────────


def _api():
    return MochatAPI(SimpleNamespace(base_url="https://m.x/", claw_token="tok"))


def _resp(status=200, payload=None, text="", success=True):
    return SimpleNamespace(
        is_success=success, status_code=status, text=text,
        json=lambda: payload if payload is not None else {},
    )


def test_api_post_unwraps_envelope():
    api = _api()
    api._http = MagicMock()
    api._http.post = AsyncMock(return_value=_resp(payload={"code": 200, "data": {"x": 1}}))
    assert asyncio.run(api.post("/p", {})) == {"x": 1}
    call = api._http.post.call_args
    assert call.args[0] == "https://m.x/p"
    assert call.kwargs["headers"]["X-Claw-Token"] == "tok"


def test_api_post_raises_on_error_code():
    api = _api()
    api._http = MagicMock()
    api._http.post = AsyncMock(return_value=_resp(payload={"code": 500, "message": "bad"}))
    with pytest.raises(RuntimeError):
        asyncio.run(api.post("/p", {}))


def test_api_post_raises_on_http_failure():
    api = _api()
    api._http = MagicMock()
    api._http.post = AsyncMock(return_value=_resp(status=503, success=False, text="down"))
    with pytest.raises(RuntimeError):
        asyncio.run(api.post("/p", {}))


def test_api_post_passthrough_without_envelope():
    api = _api()
    api._http = MagicMock()
    api._http.post = AsyncMock(return_value=_resp(payload={"foo": 1}))
    assert asyncio.run(api.post("/p", {})) == {"foo": 1}


def test_api_post_requires_open():
    with pytest.raises(RuntimeError):
        asyncio.run(_api().post("/p", {}))


def test_api_endpoint_payloads():
    api = _api()
    api.post = AsyncMock(return_value={})

    asyncio.run(api.send_session("s1", "hi", "r1"))
    assert api.post.call_args.args == ("/api/claw/sessions/send", {"sessionId": "s1", "content": "hi", "replyTo": "r1"})

    asyncio.run(api.send_panel("p1", "yo", None, "g1"))
    assert api.post.call_args.args == ("/api/claw/groups/panels/send", {"panelId": "p1", "content": "yo", "groupId": "g1"})

    asyncio.run(api.watch_session("s2", 5, 1000, 50))
    assert api.post.call_args.args == ("/api/claw/sessions/watch", {"sessionId": "s2", "cursor": 5, "timeoutMs": 1000, "limit": 50})

    asyncio.run(api.panel_messages("p2", 30))
    assert api.post.call_args.args == ("/api/claw/groups/panels/messages", {"panelId": "p2", "limit": 30})

    asyncio.run(api.list_sessions())
    assert api.post.call_args.args == ("/api/claw/sessions/list", {})

    asyncio.run(api.get_groups())
    assert api.post.call_args.args == ("/api/claw/groups/get", {})


# ── characterization: inbound pipeline (pins behavior across the rewrite) ──


def _ch(**over):
    cfg = SimpleNamespace(
        allow_from=["*"], agent_user_id="bot",
        reply_delay_mode="instant", reply_delay_ms=0,
        groups={}, mention=SimpleNamespace(require_in_groups=False),
        base_url="http://m", claw_token="t", watch_limit=50,
        sessions=[], panels=[],
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    ch = MochatChannel(cfg)
    ch.intake.publish = AsyncMock()
    return ch


def _event(author="alice", message_id="m1", content="hello", group_id="", seq=None, meta=None):
    payload = {"author": author, "messageId": message_id, "content": content,
               "groupId": group_id, "authorInfo": {"nickname": "Alice"}}
    if meta is not None:
        payload["meta"] = meta
    evt = {"type": "message.add", "timestamp": "2026-01-01T00:00:00Z", "payload": payload}
    if seq is not None:
        evt["seq"] = seq
    return evt


def test_char_process_event_dispatches():
    ch = _ch()
    asyncio.run(ch._process_inbound_event("sess1", _event(), "session"))
    ch.intake.publish.assert_awaited_once()
    kw = ch.intake.publish.await_args.kwargs
    assert kw["sender_id"] == "alice"
    assert kw["chat_id"] == "sess1"
    assert kw["content"] == "hello"
    assert kw["metadata"]["message_id"] == "m1"
    assert kw["metadata"]["sender_name"] == "Alice"
    assert kw["metadata"]["target_kind"] == "session"


def test_char_process_event_skips_agent_self():
    ch = _ch(agent_user_id="bot")
    asyncio.run(ch._process_inbound_event("sess1", _event(author="bot"), "session"))
    ch.intake.publish.assert_not_called()


def test_char_process_event_skips_disallowed():
    ch = _ch(allow_from=["only_this"])
    asyncio.run(ch._process_inbound_event("sess1", _event(author="alice"), "session"))
    ch.intake.publish.assert_not_called()


def test_char_process_event_dedup():
    ch = _ch()
    asyncio.run(ch._process_inbound_event("sess1", _event(message_id="dup"), "session"))
    asyncio.run(ch._process_inbound_event("sess1", _event(message_id="dup"), "session"))
    assert ch.intake.publish.await_count == 1


def test_char_process_event_empty_content_placeholder():
    ch = _ch()
    asyncio.run(ch._process_inbound_event("sess1", _event(content=""), "session"))
    assert ch.intake.publish.await_args.kwargs["content"] == "[empty message]"


def test_char_require_mention_skips_unmentioned():
    ch = _ch(groups={"g1": SimpleNamespace(require_mention=True)})
    asyncio.run(ch._process_inbound_event("g1", _event(group_id="g1", content="hi nobody"), "panel"))
    ch.intake.publish.assert_not_called()


def test_char_require_mention_allows_mentioned():
    ch = _ch(groups={"g1": SimpleNamespace(require_mention=True)})
    evt = _event(group_id="g1", content="hey <@bot>", meta={"mentions": ["bot"]})
    asyncio.run(ch._process_inbound_event("g1", evt, "panel"))
    ch.intake.publish.assert_awaited_once()
    assert ch.intake.publish.await_args.kwargs["metadata"]["was_mentioned"] is True


def test_char_watch_payload_advances_cursor_and_dispatches():
    ch = _ch()
    payload = {"sessionId": "s1", "cursor": 5, "events": [_event(seq=6)]}
    asyncio.run(ch._handle_watch_payload(payload, "session"))
    ch.intake.publish.assert_awaited_once()
    assert ch._cursors.get("s1") == 6


def test_char_watch_payload_cold_session_drains_without_dispatch():
    ch = _ch()
    ch._cold_sessions.add("s1")
    payload = {"sessionId": "s1", "cursor": 9, "events": [_event(seq=10)]}
    asyncio.run(ch._handle_watch_payload(payload, "session"))
    ch.intake.publish.assert_not_called()
    assert "s1" not in ch._cold_sessions
    assert ch._cursors.get("s1") == 9   # cursor still advanced from payload


def test_char_watch_payload_ignores_non_dict_and_no_session():
    ch = _ch()
    asyncio.run(ch._handle_watch_payload("nope", "session"))
    asyncio.run(ch._handle_watch_payload({"events": []}, "session"))
    ch.intake.publish.assert_not_called()


def test_char_dispatch_entries_buffered_body_group():
    ch = _ch()
    e1 = mp.MochatBufferedEntry(raw_body="one", author="a", sender_name="A", group_id="g")
    e2 = mp.MochatBufferedEntry(raw_body="two", author="b", sender_name="B", group_id="g")
    asyncio.run(ch._dispatch_entries("g1", "panel", [e1, e2], was_mentioned=True))
    kw = ch.intake.publish.await_args.kwargs
    assert "A: one" in kw["content"] and "B: two" in kw["content"]
    assert kw["metadata"]["buffered_count"] == 2
    assert kw["metadata"]["is_group"] is True


# ── parsing: build_entry / mention_gate ───────────────────────────────


def test_build_entry():
    payload = {"author": "alice", "messageId": "m1", "content": "  hi  ", "groupId": "g",
               "authorInfo": {"nickname": "Alice", "agentId": "ag1"}}
    e = mp.build_entry(payload, "2026-06-04T00:00:00Z")
    assert (e.author, e.raw_body, e.message_id, e.group_id) == ("alice", "hi", "m1", "g")
    assert e.sender_name == "Alice" and e.sender_username == "ag1"
    assert e.timestamp is not None
    # empty content -> placeholder
    assert mp.build_entry({"author": "a", "content": ""}, None).raw_body == "[empty message]"


def test_mention_gate():
    cfg = SimpleNamespace(groups={"g1": SimpleNamespace(require_mention=True)},
                          mention=SimpleNamespace(require_in_groups=False),
                          reply_delay_mode="instant")
    # panel + group requiring mention
    assert mp.mention_gate(cfg, "panel", "g1", "g1") == (True, False)
    # session is never gated/delayed
    assert mp.mention_gate(cfg, "session", "s1", "") == (False, False)
    # non-mention delay mode -> use_delay True
    cfg.reply_delay_mode = "non-mention"
    assert mp.mention_gate(cfg, "panel", "other", "g2") == (False, True)


def test_char_inbox_append_refreshes_on_cache_miss():
    """notify:chat.inbox.append for an unknown converseId refreshes the session
    directory once, then dispatches to the resolved session (regression guard)."""
    ch = _ch()
    ch._refresh_sessions_directory = AsyncMock(
        side_effect=lambda _sn: ch._session_by_converse.update({"cv1": "sessX"})
    )
    payload = {"type": "message", "payload": {
        "converseId": "cv1", "messageId": "m1", "messageAuthor": "alice", "messagePlainContent": "hi"}}
    asyncio.run(ch._on_inbox_append(payload))
    ch._refresh_sessions_directory.assert_awaited_once()
    ch.intake.publish.assert_awaited_once()
    assert ch.intake.publish.await_args.kwargs["chat_id"] == "sessX"


def test_char_inbox_append_skips_group_detail():
    ch = _ch()
    asyncio.run(ch._on_inbox_append({"type": "message", "payload": {"groupId": "g", "converseId": "c"}}))
    ch.intake.publish.assert_not_called()


# ── characterization: DelayBuffer timer path (rewrite baseline) ───────
# Outcome invariant: per-ENTRY exactly-once (assert entry sets, not flush
# counts). Contract invariants: dispatch runs outside the state lock (re-entry
# safe) and a timer-fired flush never cancels itself.


def _entry(mid="m1", body="hi"):
    return mp.MochatBufferedEntry(raw_body=body, author="alice", message_id=mid)


def _delay_ch():
    ch = _ch(reply_delay_mode="non-mention", reply_delay_ms=0)
    ch._dispatch_entries = AsyncMock()
    return ch


def test_delay_timer_flushes_all_entries_once():
    """N enqueues on one key -> the (replaced) timer fires once with all
    entries; the timer-fired flush completes (it must not cancel itself)."""
    ch = _delay_ch()
    e1, e2 = _entry("m1"), _entry("m2")

    async def scenario():
        await ch._delays.enqueue("session:s1", "s1", "session", e1)
        await ch._delays.enqueue("session:s1", "s1", "session", e2)
        await ch._delays._states["session:s1"].timer

    asyncio.run(scenario())
    ch._dispatch_entries.assert_awaited_once()
    args = ch._dispatch_entries.await_args.args
    assert args[2] == [e1, e2]                 # all entries, exactly once
    assert ch._delays._states["session:s1"].entries == []


def test_delay_mention_flush_includes_trigger_and_cancels_timer():
    """A mention flush drains buffered + trigger entries immediately and
    cancels the pending timer so nothing is dispatched twice."""
    ch = _delay_ch()
    e1, e2 = _entry("m1"), _entry("m2 mention")

    async def scenario():
        await ch._delays.enqueue("session:s1", "s1", "session", e1)
        timer = ch._delays._states["session:s1"].timer
        await ch._delays.flush_now("session:s1", "s1", "session", e2)
        try:
            await timer                         # cancelled -> must not fire
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    ch._dispatch_entries.assert_awaited_once()
    args = ch._dispatch_entries.await_args
    assert args.args[2] == [e1, e2]            # buffered + trigger, exactly once
    assert args.args[3] is True                 # reason "mention" -> was_mentioned


def test_delay_cancel_all_drops_pending():
    """cancel_all (stop path) cancels pending timers; buffered entries are not
    dispatched and no timer task leaks."""
    ch = _delay_ch()

    async def scenario():
        await ch._delays.enqueue("session:s1", "s1", "session", _entry())
        timer = ch._delays._states["session:s1"].timer
        await ch._delays.cancel_all()
        try:
            await timer
        except asyncio.CancelledError:
            pass
        assert timer.done()

    asyncio.run(scenario())
    ch._dispatch_entries.assert_not_awaited()


def test_delay_dispatch_runs_outside_state_lock():
    """Contract: the flush callback executes outside the state lock — a
    callback that re-enters enqueue on the same key must not deadlock."""
    ch = _delay_ch()
    seen: list[list] = []

    async def reentrant_dispatch(target_id, target_kind, entries, was_mentioned):
        seen.append(entries)
        if len(seen) == 1:                      # re-enter once from the callback
            await ch._delays.enqueue("session:s1", "s1", "session", _entry("m2"))

    ch._dispatch_entries = reentrant_dispatch

    async def scenario():
        await ch._delays.enqueue("session:s1", "s1", "session", _entry("m1"))
        await asyncio.wait_for(ch._delays._states["session:s1"].timer, timeout=2)
        await asyncio.wait_for(ch._delays._states["session:s1"].timer, timeout=2)

    asyncio.run(scenario())                     # completing at all proves no deadlock
    assert [e.message_id for batch in seen for e in batch] == ["m1", "m2"]


# ── characterization: send() (rewrite baseline) ───────────────────────


def _send_ch(**over):
    ch = _ch(**over)
    ch._api.send_session = AsyncMock()
    ch._api.send_panel = AsyncMock()
    return ch


def test_send_session_route():
    """A session_-prefixed id routes to send_session (anything else is a
    panel per resolve_target); carry-nothing -> no reply_to/group_id."""
    ch = _send_ch()
    asyncio.run(ch.send("session_s1", "hello"))
    ch._api.send_session.assert_awaited_once_with("session_s1", "hello")
    ch._api.send_panel.assert_not_called()


def test_send_panel_route():
    ch = _send_ch()
    asyncio.run(ch.send("p1", "hi"))
    ch._api.send_panel.assert_awaited_once_with("p1", "hi")
    ch._api.send_session.assert_not_called()


def test_send_joins_content_and_media():
    ch = _send_ch()
    asyncio.run(ch.send("session_s1", "text", media=["/m/a.jpg", "  "]))
    assert ch._api.send_session.await_args.args[1] == "text\n/m/a.jpg"


def test_send_media_only():
    ch = _send_ch()
    asyncio.run(ch.send("session_s1", "", media=["/m/a.jpg"]))
    ch._api.send_session.assert_awaited_once_with("session_s1", "/m/a.jpg")


def test_send_skips_without_token():
    ch = _send_ch(claw_token="")
    asyncio.run(ch.send("session_s1", "hi"))
    ch._api.send_session.assert_not_called()
    ch._api.send_panel.assert_not_called()


def test_send_skips_empty_content():
    ch = _send_ch()
    asyncio.run(ch.send("session_s1", "   ", media=[]))
    ch._api.send_session.assert_not_called()


def test_send_reraises_transient_for_manager_retry():
    """httpx timeouts/transport errors propagate so manager retry can back
    off; business errors stay swallowed (see test_send_swallows_api_error)."""
    import httpx
    import pytest
    ch = _send_ch()
    ch._api.send_session = AsyncMock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(ch.send("session_s1", "hi"))


def test_send_swallows_api_error():
    ch = _send_ch()
    ch._api.send_session = AsyncMock(side_effect=RuntimeError("boom"))
    asyncio.run(ch.send("session_s1", "hi"))     # logged, not raised


# ── subscribe orchestration (transport.request mocked) ────────────────


def test_subscribe_sessions_payload_and_cold_marking():
    ch = _ch()
    ch._cursors.mark = lambda *a: None
    ch._cursors._cursors["s1"] = 7                  # s1 warm, s2 cold
    ch._transport.request = AsyncMock(return_value={"result": True})
    assert asyncio.run(ch._subscribe_sessions(["s1", "s2"])) is True
    args = ch._transport.request.await_args.args
    assert args[0] == "com.claw.im.subscribeSessions"
    assert args[1]["sessionIds"] == ["s1", "s2"]
    assert args[1]["cursors"] == {"s1": 7}          # snapshot of known cursors
    assert "s2" in ch._cold_sessions and "s1" not in ch._cold_sessions


def test_subscribe_sessions_failure_returns_false():
    ch = _ch()
    ch._transport.request = AsyncMock(return_value={"result": False, "message": "nope"})
    assert asyncio.run(ch._subscribe_sessions(["s1"])) is False


def test_subscribe_sessions_replays_ack_items():
    ch = _ch()
    ch._transport.request = AsyncMock(return_value={
        "result": True, "data": [{"sessionId": "s1", "events": []}],
    })
    ch._handle_watch_payload = AsyncMock()
    asyncio.run(ch._subscribe_sessions(["s1"]))
    ch._handle_watch_payload.assert_awaited_once_with({"sessionId": "s1", "events": []}, "session")


def test_subscribe_sessions_empty_is_noop():
    ch = _ch()
    ch._transport.request = AsyncMock()
    assert asyncio.run(ch._subscribe_sessions([])) is True
    ch._transport.request.assert_not_called()


def test_subscribe_panels_payload_and_failure():
    ch = _ch()
    ch._transport.request = AsyncMock(return_value={"result": True})
    assert asyncio.run(ch._subscribe_panels(["p1"])) is True
    assert ch._transport.request.await_args.args == (
        "com.claw.im.subscribePanels", {"panelIds": ["p1"]},
    )
    ch._transport.request = AsyncMock(return_value={"result": False})
    assert asyncio.run(ch._subscribe_panels(["p1"])) is False


# ── SocketTransport (dumb pipe; fake socket.io client) ────────────────


def _transport_cfg(**over):
    cfg = SimpleNamespace(
        socket_disable_msgpack=True, max_retry_attempts=3,
        socket_reconnect_delay_ms=1000, socket_max_reconnect_delay_ms=5000,
        socket_url="", base_url="http://m.example/", socket_path="",
        claw_token="t", socket_connect_timeout_ms=2000,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


class _FakeSocketClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.handlers: dict = {}
        self.connect_args: dict = {}
        self.disconnected = False
        self.call = AsyncMock(return_value={"result": True})

    def on(self, event, handler):
        self.handlers[event] = handler

    async def connect(self, url, **kwargs):
        self.connect_args = {"url": url, **kwargs}

    async def disconnect(self):
        self.disconnected = True


def _patched_transport(monkeypatch, cfg=None, handlers=None, setup=None):
    from raven.channels.adapters.mochat import transport as tr
    fake_holder = {}

    def factory(**kwargs):
        client = _FakeSocketClient(**kwargs)
        if setup:
            setup(client)
        fake_holder["client"] = client
        return client

    monkeypatch.setattr(tr, "socketio", SimpleNamespace(AsyncClient=factory))
    monkeypatch.setattr(tr, "SOCKETIO_AVAILABLE", True)
    t = tr.SocketTransport(cfg or _transport_cfg(), handlers or {})
    return t, fake_holder


def test_transport_request_three_branches(monkeypatch):
    t, holder = _patched_transport(monkeypatch)
    # not connected -> synthesized failure ack
    assert asyncio.run(t.request("ev", {})) == {"result": False, "message": "socket not connected"}

    asyncio.run(t.connect())
    client = holder["client"]
    # dict passthrough
    client.call = AsyncMock(return_value={"result": True, "data": 1})
    assert asyncio.run(t.request("ev", {})) == {"result": True, "data": 1}
    # non-dict wrapped
    client.call = AsyncMock(return_value=[1, 2])
    assert asyncio.run(t.request("ev", {})) == {"result": True, "data": [1, 2]}
    # exception -> failure ack with message
    client.call = AsyncMock(side_effect=RuntimeError("boom"))
    assert asyncio.run(t.request("ev", {})) == {"result": False, "message": "boom"}


def test_transport_client_held_before_connect(monkeypatch):
    """Ordering contract: handlers fired DURING the handshake must already be
    able to use request() — the client is held before connect() is awaited."""
    seen = {}
    t = None

    def setup(client):
        async def handshake_connect(url, **kwargs):
            seen["ack"] = await t.request("subscribe", {})   # fired mid-handshake
        client.connect = handshake_connect
        client.call = AsyncMock(return_value={"result": True})

    t, _ = _patched_transport(monkeypatch, setup=setup)
    assert asyncio.run(t.connect()) is True
    assert seen["ack"] == {"result": True}                # NOT "socket not connected"


def test_transport_connect_failure_clears_client(monkeypatch):
    def setup(client):
        async def boom(url, **kwargs):
            raise RuntimeError("refused")
        client.connect = boom

    t, holder = _patched_transport(monkeypatch, setup=setup)
    assert asyncio.run(t.connect()) is False
    assert t._client is None                              # cleared on failure
    assert holder["client"].disconnected is True          # best-effort cleanup


def test_transport_url_path_normalization_and_handlers(monkeypatch):
    async def h(*_a):
        pass

    cfg = _transport_cfg(socket_url="  http://ws.example//  ", socket_path=" /sock/ ")
    t, holder = _patched_transport(monkeypatch, cfg=cfg, handlers={"connect": h, "claw.session.events": h})
    assert asyncio.run(t.connect()) is True
    client = holder["client"]
    assert client.connect_args["url"] == "http://ws.example"          # stripped + rstripped
    assert client.connect_args["socketio_path"] == "sock/"            # lstripped
    assert client.connect_args["auth"] == {"token": "t"}
    assert set(client.handlers) == {"connect", "claw.session.events"}  # table registered


def test_transport_serializer_selection(monkeypatch):
    from raven.channels.adapters.mochat import transport as tr
    # msgpack enabled + available -> msgpack
    t, holder = _patched_transport(monkeypatch, cfg=_transport_cfg(socket_disable_msgpack=False))
    monkeypatch.setattr(tr, "MSGPACK_AVAILABLE", True)
    asyncio.run(t.connect())
    assert holder["client"].kwargs["serializer"] == "msgpack"
    # msgpack enabled but missing -> default (degraded)
    monkeypatch.setattr(tr, "MSGPACK_AVAILABLE", False)
    t2, holder2 = _patched_transport(monkeypatch, cfg=_transport_cfg(socket_disable_msgpack=False))
    asyncio.run(t2.connect())
    assert holder2["client"].kwargs["serializer"] == "default"
    # disabled -> default regardless
    t3, holder3 = _patched_transport(monkeypatch, cfg=_transport_cfg(socket_disable_msgpack=True))
    asyncio.run(t3.connect())
    assert holder3["client"].kwargs["serializer"] == "default"


def test_transport_close_disconnects_and_clears(monkeypatch):
    t, holder = _patched_transport(monkeypatch)
    asyncio.run(t.connect())
    asyncio.run(t.close())
    assert holder["client"].disconnected is True
    assert t._client is None
    asyncio.run(t.close())                                # idempotent, no raise


def test_transport_unavailable_socketio_returns_false(monkeypatch):
    from raven.channels.adapters.mochat import transport as tr
    monkeypatch.setattr(tr, "SOCKETIO_AVAILABLE", False)
    t = tr.SocketTransport(_transport_cfg(), {})
    assert asyncio.run(t.connect()) is False


# ── contract conformance ───────────────────────────────────────────────


def test_mochat_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations
    ch = _ch()
    assert isinstance(ch, Channel)              # name/capabilities/start/stop/send
    assert capability_violations(ch) == []      # no login/streaming declared or implemented


def test_mochat_spec_import_is_cheap():
    """Importing mochat.spec must NOT import the channel implementation — the
    API/socket client import is deferred into SPEC.factory."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.mochat.spec as s;"
        "assert 'raven.channels.adapters.mochat.channel' not in sys.modules, "
        "'spec import pulled in the channel implementation';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'Mochat'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
