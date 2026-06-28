"""Tests for the slack adapter package.

parsing.py — markdown->mrkdwn conversion + sender/respond/dedup decisions.
channel.py — Socket Mode inbound routing and Web-API send.

Real Socket Mode / Web API round-trips are live flows left to integration/manual
testing.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from raven.channels.adapters.slack import parsing as sp
from raven.channels.adapters.slack.channel import SlackChannel


def _cfg(**o):
    dm = SimpleNamespace(
        enabled=o.get("dm_enabled", True),
        policy=o.get("dm_policy", "open"),
        allow_from=o.get("dm_allow", []),
    )
    return SimpleNamespace(
        bot_token="b", app_token="a", mode="socket",
        group_policy=o.get("group_policy", "open"), group_allow_from=o.get("group_allow", []),
        dm=dm, reply_in_thread=o.get("reply_in_thread", False), react_emoji="eyes",
        allow_from=o.get("allow_from", ["*"]),
    )


def _channel(**o):
    ch = SlackChannel(_cfg(**o))
    ch._bot_user_id = o.get("bot_id", "B1")
    ch.intake.publish = AsyncMock()
    ch._web_client = MagicMock()
    ch._web_client.reactions_add = AsyncMock()
    ch._web_client.chat_postMessage = AsyncMock()
    ch._web_client.files_upload_v2 = AsyncMock()
    return ch


def _req(event, req_type="events_api"):
    return SimpleNamespace(type=req_type, envelope_id="env1", payload={"event": event})


# ── parsing: markdown -> mrkdwn ────────────────────────────────────────


def test_to_mrkdwn_empty():
    assert sp.to_mrkdwn("") == ""


def test_to_mrkdwn_bold_and_header():
    out = sp.to_mrkdwn("# Title\n**bold**")
    assert "*Title*" in out
    assert "*bold*" in out and "**" not in out


def test_to_mrkdwn_preserves_code():
    out = sp.to_mrkdwn("see `x=1` and\n```\n**not bold**\n```")
    assert "`x=1`" in out
    assert "**not bold**" in out   # inside fence, untouched


def test_to_mrkdwn_table_flattened():
    md = "| Name | Age |\n| --- | --- |\n| Alice | 30 |"
    out = sp.to_mrkdwn(md)
    assert "Alice" in out and "30" in out
    assert "|" not in out          # table chars gone


def test_strip_bot_mention():
    assert sp.strip_bot_mention("<@B1> hello", "B1") == "hello"
    assert sp.strip_bot_mention("plain", "B1") == "plain"
    assert sp.strip_bot_mention("<@B1> x", None) == "<@B1> x"   # no bot id -> untouched


# ── parsing: permission / respond / dedup ──────────────────────────────


def test_sender_permitted_dm():
    assert sp.sender_permitted(_cfg(dm_enabled=True), "U1", "D1", "im") is True
    assert sp.sender_permitted(_cfg(dm_enabled=False), "U1", "D1", "im") is False
    assert sp.sender_permitted(_cfg(dm_policy="allowlist", dm_allow=["U1"]), "U1", "D1", "im") is True
    assert sp.sender_permitted(_cfg(dm_policy="allowlist", dm_allow=["U9"]), "U1", "D1", "im") is False


def test_sender_permitted_group():
    assert sp.sender_permitted(_cfg(group_policy="open"), "U1", "C1", "channel") is True
    assert sp.sender_permitted(_cfg(group_policy="allowlist", group_allow=["C1"]), "U1", "C1", "channel") is True
    assert sp.sender_permitted(_cfg(group_policy="allowlist", group_allow=["C9"]), "U1", "C1", "channel") is False


def test_should_respond_in_channel():
    assert sp.should_respond_in_channel(_cfg(group_policy="open"), "message", "hi", "C1", "B1") is True
    assert sp.should_respond_in_channel(_cfg(group_policy="mention"), "app_mention", "hi", "C1", "B1") is True
    assert sp.should_respond_in_channel(_cfg(group_policy="mention"), "message", "<@B1> hi", "C1", "B1") is True
    assert sp.should_respond_in_channel(_cfg(group_policy="mention"), "message", "hi", "C1", "B1") is False
    assert sp.should_respond_in_channel(_cfg(group_policy="allowlist", group_allow=["C1"]), "message", "x", "C1", "B1") is True


def test_is_duplicate_mention():
    assert sp.is_duplicate_mention("message", "<@B1> hi", "B1") is True
    assert sp.is_duplicate_mention("app_mention", "<@B1> hi", "B1") is False
    assert sp.is_duplicate_mention("message", "no mention", "B1") is False


# ── channel: _on_socket_request ────────────────────────────────────────


def _run(ch, event, req_type="events_api"):
    client = MagicMock()
    client.send_socket_mode_response = AsyncMock()
    asyncio.run(ch._on_socket_request(client, _req(event, req_type)))
    return client


def test_socket_ignores_non_events_api():
    ch = _channel()
    client = _run(ch, {"type": "message"}, req_type="hello")
    client.send_socket_mode_response.assert_not_awaited()
    ch.intake.publish.assert_not_awaited()


def test_socket_app_mention_dispatches_stripped():
    ch = _channel()
    client = _run(ch, {"type": "app_mention", "user": "U1", "channel": "C1",
                       "text": "<@B1> hello there", "ts": "1.0", "channel_type": "channel"})
    client.send_socket_mode_response.assert_awaited_once()   # ack
    ch.intake.publish.assert_awaited_once()
    kw = ch.intake.publish.await_args.kwargs
    assert kw["content"] == "hello there" and kw["chat_id"] == "C1" and kw["sender_id"] == "U1"


def test_socket_dm_message_dispatches():
    ch = _channel()
    _run(ch, {"type": "message", "user": "U1", "channel": "D1", "text": "hi", "ts": "2.0", "channel_type": "im"})
    ch.intake.publish.assert_awaited_once()


def test_socket_subtype_skipped():
    ch = _channel()
    _run(ch, {"type": "message", "user": "U1", "channel": "C1", "text": "x", "subtype": "bot_message"})
    ch.intake.publish.assert_not_awaited()


def test_socket_self_skipped():
    ch = _channel(bot_id="B1")
    _run(ch, {"type": "message", "user": "B1", "channel": "C1", "text": "x"})
    ch.intake.publish.assert_not_awaited()


def test_socket_duplicate_mention_skipped():
    ch = _channel()
    _run(ch, {"type": "message", "user": "U1", "channel": "C1", "text": "<@B1> hi", "channel_type": "channel"})
    ch.intake.publish.assert_not_awaited()


def test_socket_mention_policy_skips_unmentioned():
    ch = _channel(group_policy="mention")
    _run(ch, {"type": "message", "user": "U1", "channel": "C1", "text": "random", "channel_type": "channel"})
    ch.intake.publish.assert_not_awaited()


def test_socket_dm_disabled_skipped():
    ch = _channel(dm_enabled=False)
    _run(ch, {"type": "message", "user": "U1", "channel": "D1", "text": "hi", "channel_type": "im"})
    ch.intake.publish.assert_not_awaited()


# ── channel: send ──────────────────────────────────────────────────────


def test_send_postmessage_mrkdwn_carries_no_thread():
    ch = _channel()
    asyncio.run(ch.send("C1", "**bold**"))
    call = ch._web_client.chat_postMessage.call_args
    assert call.kwargs["channel"] == "C1"
    assert "*bold*" in call.kwargs["text"]
    assert "thread_ts" not in call.kwargs   # carry-nothing: no threading


def test_send_media_only_skips_text():
    ch = _channel()
    asyncio.run(ch.send("C1", "", media=["/tmp/a.png"]))
    ch._web_client.chat_postMessage.assert_not_called()
    ch._web_client.files_upload_v2.assert_awaited_once()
    upload = ch._web_client.files_upload_v2.await_args
    assert upload.kwargs["channel"] == "C1" and upload.kwargs["file"] == "/tmp/a.png"
    assert "thread_ts" not in upload.kwargs


def test_send_empty_sends_blank():
    ch = _channel()
    asyncio.run(ch.send("C1", ""))
    assert ch._web_client.chat_postMessage.call_args.kwargs["text"] == " "


def test_socket_reply_in_thread_sets_thread_ts():
    ch = _channel(reply_in_thread=True)
    _run(ch, {"type": "app_mention", "user": "U1", "channel": "C1",
              "text": "<@B1> hi", "ts": "5.5", "channel_type": "channel"})
    meta = ch.intake.publish.await_args.kwargs["metadata"]["slack"]
    assert meta["thread_ts"] == "5.5"   # reply_in_thread -> ts becomes thread root
    assert ch.intake.publish.await_args.kwargs["session_key"] == "slack:C1:5.5"


# ── inbound: defensive guards + ack/react side-effects ────────────────


def test_socket_unhandled_event_type_acked_not_dispatched():
    ch = _channel()
    client = _run(ch, {"type": "reaction_added", "user": "U1", "channel": "C1"})
    client.send_socket_mode_response.assert_awaited_once()   # envelope still acked
    ch.intake.publish.assert_not_awaited()


def test_socket_missing_user_skipped():
    ch = _channel()
    _run(ch, {"type": "message", "channel": "C1", "text": "hi", "channel_type": "channel"})
    ch.intake.publish.assert_not_awaited()


def test_socket_missing_channel_skipped():
    ch = _channel()
    _run(ch, {"type": "message", "user": "U1", "text": "hi", "channel_type": "channel"})
    ch.intake.publish.assert_not_awaited()


def test_disallowed_sender_skips_react_and_publish():
    """The allow_from gate runs before the :eyes: react (sender_permitted only
    covers dm/group policy, not allow_from) — a denied sender gets no visible
    acknowledgement and never reaches the bus."""
    ch = _channel(allow_from=["only"])
    _run(ch, {"type": "message", "user": "stranger", "channel": "D1", "text": "hi",
              "ts": "3.3", "channel_type": "im"})
    ch._web_client.reactions_add.assert_not_awaited()
    ch.intake.publish.assert_not_awaited()


def test_socket_reacts_to_triggering_message():
    ch = _channel()
    _run(ch, {"type": "app_mention", "user": "U1", "channel": "C1",
              "text": "<@B1> hi", "ts": "7.7", "channel_type": "channel"})
    ch._web_client.reactions_add.assert_awaited_once()
    kw = ch._web_client.reactions_add.await_args.kwargs
    assert kw["channel"] == "C1" and kw["timestamp"] == "7.7" and kw["name"] == "eyes"


# ── send: defensive branches ──────────────────────────────────────────


def test_send_no_client_is_noop():
    ch = _channel()
    ch._web_client = None
    asyncio.run(ch.send("C1", "hi"))  # must not raise


def test_send_reraises_transient_for_manager_retry():
    """429/5xx/network propagate so manager retry can back off; permanent
    errors stay swallowed (see test_send_swallows_upload_error)."""
    import pytest
    from slack_sdk.errors import SlackApiError
    ch = _channel()
    err = SlackApiError("rate limited", SimpleNamespace(status_code=429))
    ch._web_client.chat_postMessage = AsyncMock(side_effect=err)
    with pytest.raises(SlackApiError):
        asyncio.run(ch.send("C1", "hi"))


def test_send_swallows_upload_error():
    ch = _channel()
    ch._web_client.files_upload_v2 = AsyncMock(side_effect=RuntimeError("boom"))
    asyncio.run(ch.send("C1", "hi", media=["/tmp/a.png"]))
    ch._web_client.chat_postMessage.assert_awaited_once()   # text still sent despite upload failure


# ── stop contract ──────────────────────────────────────────────────────


def test_stop_is_idempotent_and_wakes_keepalive():
    """stop() sets the keepalive event (start returns immediately, not after a
    1s poll tick) and double-stop is a no-op (stop contract #4/#5)."""
    ch = _channel()
    socket_client = MagicMock()
    socket_client.close = AsyncMock()
    ch._socket_client = socket_client
    asyncio.run(ch.stop())
    assert ch._stop_event.is_set()
    socket_client.close.assert_awaited_once()
    asyncio.run(ch.stop())            # second stop: no client left, no raise


# ── contract conformance ───────────────────────────────────────────────


def test_slack_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations
    ch = SlackChannel(_cfg())
    assert isinstance(ch, Channel)              # name/capabilities/start/stop/send
    assert capability_violations(ch) == []      # no login/streaming declared or implemented


def test_slack_spec_import_is_cheap():
    """Importing slack.spec must NOT pull in the slack_sdk SDK (the heavy import
    is deferred into SPEC.factory)."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.slack.spec as s;"
        "assert 'slack_sdk' not in sys.modules, 'spec import pulled in slack_sdk';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'Slack'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
