"""Tests for the feishu adapter package — inbound content extraction
(content.py), outbound format detection/rendering (cards.py), and group
mention gating. Pure surface; no lark SDK / live connection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.channels.adapters.feishu import cards, content
from raven.channels.adapters.feishu.channel import FeishuChannel


def _channel(group_policy="open"):
    cfg = SimpleNamespace(
        app_id="a", app_secret="s", encrypt_key="", verification_token="",
        group_policy=group_policy, react_emoji="THUMBSUP",
    )
    return FeishuChannel(cfg)


# ── content.extract_post ──────────────────────────────────────────────


def test_extract_post_direct():
    payload = {"title": "T", "content": [[{"tag": "text", "text": "hello"}]]}
    text, images = content.extract_post(payload)
    assert "T" in text and "hello" in text
    assert images == []


def test_extract_post_localized_and_image():
    payload = {"zh_cn": {"content": [[
        {"tag": "text", "text": "hi"},
        {"tag": "img", "image_key": "img_k1"},
    ]]}}
    text, images = content.extract_post(payload)
    assert "hi" in text
    assert images == ["img_k1"]


def test_extract_post_wrapped_envelope_and_at():
    payload = {"post": {"zh_cn": {"content": [[{"tag": "at", "user_name": "bob"}]]}}}
    text, _ = content.extract_post(payload)
    assert "@bob" in text


def test_extract_post_empty():
    assert content.extract_post({}) == ("", [])


# ── content.extract_share_card / interactive ──────────────────────────


def test_extract_share_chat():
    assert content.extract_share_card({"chat_id": "oc_x"}, "share_chat") == "[shared chat: oc_x]"


def test_extract_system_message():
    assert content.extract_share_card({}, "system") == "[system message]"


def test_extract_interactive_pulls_markdown():
    card = {"elements": [[{"tag": "markdown", "content": "card body"}]]}
    assert "card body" in content.extract_share_card(card, "interactive")


def test_extract_element_link():
    assert content.extract_element({"tag": "a", "href": "http://u", "text": "label"}) == [
        "link: http://u",
        "label",
    ]


# ── cards.detect_format ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, fmt",
    [
        ("hello there", "text"),
        ("**bold**", "interactive"),
        ("```code```", "interactive"),
        ("| a | b |\n| - | - |\n| 1 | 2 |", "interactive"),
        ("- item one\n- item two", "interactive"),
        ("see [docs](http://u)", "post"),
        ("x" * 250, "post"),
        ("y" * 2100, "interactive"),
    ],
)
def test_detect_format(text, fmt):
    assert cards.detect_format(text) == fmt


# ── cards rendering ───────────────────────────────────────────────────


def test_post_payload_renders_link():
    import json
    payload = json.loads(cards.post_payload("see [docs](http://u) now"))
    tags = [el["tag"] for para in payload["zh_cn"]["content"] for el in para]
    assert "a" in tags and "text" in tags


def test_parse_table():
    table = cards.parse_table("| a | b |\n| - | - |\n| 1 | 2 |")
    assert table["tag"] == "table"
    assert len(table["columns"]) == 2
    assert table["rows"][0] == {"c0": "1", "c1": "2"}


def test_parse_table_rejects_non_table():
    assert cards.parse_table("not | a table") is None


def test_card_payloads_split_multiple_tables():
    two = "| a |\n| - |\n| 1 |\n\ntext\n\n| b |\n| - |\n| 2 |"
    payloads = cards.card_payloads(two)
    assert len(payloads) == 2  # one table per card (Feishu API 11310)


# ── group mention gating ──────────────────────────────────────────────


def test_mentioned_via_at_all():
    ch = _channel(group_policy="mention")
    msg = SimpleNamespace(content="hi @_all", mentions=None)
    assert ch._is_bot_mentioned(msg) is True


def test_mentioned_via_bot_open_id():
    ch = _channel(group_policy="mention")
    mention = SimpleNamespace(id=SimpleNamespace(user_id=None, open_id="ou_bot1"))
    msg = SimpleNamespace(content="hey", mentions=[mention])
    assert ch._is_bot_mentioned(msg) is True


def test_not_mentioned():
    ch = _channel(group_policy="mention")
    user_mention = SimpleNamespace(id=SimpleNamespace(user_id="u_1", open_id="ou_user"))
    msg = SimpleNamespace(content="hey", mentions=[user_mention])
    assert ch._is_bot_mentioned(msg) is False


def test_open_policy_addresses_all():
    ch = _channel(group_policy="open")
    msg = SimpleNamespace(content="hey", mentions=None)
    assert ch._addressed_to_bot(msg) is True


# ── two-tier transcription (native Feishu STT first, Groq fallback) ───


def test_transcribe_prefers_native_feishu_stt(monkeypatch):
    ch = _channel()
    ch._lark_stt_sync = lambda _path: "native text"
    groq = AsyncMock(return_value="groq text")
    monkeypatch.setattr("raven.channels.adapters.feishu.channel.transcribe_audio", groq)
    assert asyncio.run(ch._transcribe("/tmp/a.opus")) == "native text"
    groq.assert_not_called()


def test_transcribe_falls_back_to_groq(monkeypatch):
    ch = _channel()
    ch._lark_stt_sync = lambda _path: None
    groq = AsyncMock(return_value="groq text")
    monkeypatch.setattr("raven.channels.adapters.feishu.channel.transcribe_audio", groq)
    assert asyncio.run(ch._transcribe("/tmp/a.opus")) == "groq text"
    groq.assert_awaited_once()


def test_transcribe_skips_native_once_disabled(monkeypatch):
    ch = _channel()
    ch._native_stt_disabled = True
    calls = []
    ch._lark_stt_sync = lambda _path: calls.append(1) or "unused"
    monkeypatch.setattr(
        "raven.channels.adapters.feishu.channel.transcribe_audio",
        AsyncMock(return_value="groq text"),
    )
    assert asyncio.run(ch._transcribe("/tmp/a.opus")) == "groq text"
    assert calls == []  # native STT not attempted when disabled


# ── inbound early gate (reject before side effects) ───────────────────


def test_on_message_disallowed_sender_skips_react_and_download():
    """Denied sender is rejected before _react (network) and _extract (media
    download), not merely dropped at the central intake."""
    ch = _channel()
    ch.config.allow_from = []          # deny all
    ch._react = AsyncMock()
    ch._extract = AsyncMock()
    ch.intake.publish = AsyncMock()
    msg = SimpleNamespace(message_id="m1", chat_type="p2p", message_type="text", content="{}")
    sender = SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="other"))
    data = SimpleNamespace(event=SimpleNamespace(message=msg, sender=sender))
    asyncio.run(ch._on_message(data))
    ch._react.assert_not_awaited()
    ch._extract.assert_not_awaited()
    ch.intake.publish.assert_not_awaited()


# ── send: transient vs permanent errors ───────────────────────────────


def test_send_text_reaches_sdk_with_chat_id_and_content():
    """send(chat_id, content) drives the lark message.create call with that
    content and chat_id; no media upload, no threading/reply params."""
    ch = _channel()
    ch._client = MagicMock()
    ch._client.im.v1.message.create.return_value = SimpleNamespace(success=lambda: True)
    ch._upload_image_sync = MagicMock()
    ch._upload_file_sync = MagicMock()
    asyncio.run(ch.send("oc_1", "hello world"))
    ch._upload_image_sync.assert_not_called()
    ch._upload_file_sync.assert_not_called()
    request = ch._client.im.v1.message.create.call_args.args[0]
    body = request.request_body
    assert body.receive_id == "oc_1"
    assert "hello world" in body.content


def test_send_media_uploads_and_posts_file_key():
    """send(chat_id, "", media=[path]) uploads the media and posts its key;
    empty content sends no text message."""
    ch = _channel()
    ch._client = MagicMock()
    ch._client.im.v1.message.create.return_value = SimpleNamespace(success=lambda: True)
    ch._upload_file_sync = MagicMock(return_value="file_k1")
    ch._send_text = AsyncMock()
    import os
    monkey = os.path.isfile
    os.path.isfile = lambda _p: True
    try:
        asyncio.run(ch.send("ou_user", "", media=["/tmp/doc.pdf"]))
    finally:
        os.path.isfile = monkey
    ch._upload_file_sync.assert_called_once_with("/tmp/doc.pdf")
    ch._send_text.assert_not_awaited()
    request = ch._client.im.v1.message.create.call_args.args[0]
    assert "file_k1" in request.request_body.content


def test_send_reraises_transient_for_manager_retry():
    """requests-level network errors from the lark executor propagate so the
    manager retry can back off; lark business errors stay swallowed."""
    import pytest
    import requests
    ch = _channel()
    ch._client = object()
    ch._send_text = AsyncMock(side_effect=requests.exceptions.ConnectionError("down"))
    with pytest.raises(requests.exceptions.ConnectionError):
        asyncio.run(ch.send("oc_1", "hi"))


def test_send_swallows_permanent_error():
    ch = _channel()
    ch._client = object()
    ch._send_text = AsyncMock(side_effect=RuntimeError("lark errcode"))
    asyncio.run(ch.send("oc_1", "hi"))  # no raise


# ── stop contract ──────────────────────────────────────────────────────


def test_stop_blocks_zombie_inbound(monkeypatch):
    """lark's ws client has no stop(); after stop() the socket may keep
    delivering — the sync bridge must drop those, or a restarted instance
    double-publishes (stop contract #1/#3)."""
    ch = _channel()
    ch._loop = MagicMock()
    ch._loop.is_running.return_value = True
    calls = []
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda *a, **k: calls.append(a))
    ch._running = True
    ch._on_message_sync(MagicMock())
    assert len(calls) == 1
    ch._running = False
    ch._on_message_sync(MagicMock())
    assert len(calls) == 1   # zombie delivery dropped


# ── contract conformance ───────────────────────────────────────────────


def test_feishu_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations
    ch = _channel()
    assert isinstance(ch, Channel)              # name/capabilities/start/stop/send
    assert capability_violations(ch) == []      # no login/streaming declared or implemented


def test_feishu_spec_import_is_cheap():
    """Importing feishu.spec must NOT pull in lark_oapi (deferred into
    SPEC.factory)."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.feishu.spec as s;"
        "assert 'lark_oapi' not in sys.modules, 'spec import pulled in lark_oapi';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'Feishu'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
