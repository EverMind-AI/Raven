"""Tests for raven.channels.adapters.wecom — frame/body parsing, per-type
content extraction, and inbound dedup. Pure surface; no live SDK."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from raven.channels.adapters.wecom.channel import WecomChannel


def _channel(welcome_message="", allow_from=("*",)):
    cfg = SimpleNamespace(bot_id="b", secret="s", welcome_message=welcome_message,
                          allow_from=list(allow_from))
    ch = WecomChannel(cfg)
    ch.intake.publish = AsyncMock()
    return ch


# ── body extraction ───────────────────────────────────────────────────


def test_body_from_frame_attr():
    assert WecomChannel._body(SimpleNamespace(body={"x": 1})) == {"x": 1}


def test_body_from_dict():
    assert WecomChannel._body({"body": {"y": 2}}) == {"y": 2}
    assert WecomChannel._body({"y": 2}) == {"y": 2}  # no 'body' key -> frame itself


def test_body_other_type():
    assert WecomChannel._body(123) == {}


# ── per-type content extraction (text/voice/mixed are SDK-free) ───────


def test_extract_text():
    ch = _channel()
    assert asyncio.run(ch._extract({"text": {"content": "hello"}}, "text")) == "hello"


def test_extract_voice_uses_platform_transcription():
    ch = _channel()
    body = {"voice": {"content": "transcribed words"}}
    assert asyncio.run(ch._extract(body, "voice")) == "[voice] transcribed words"


def test_extract_voice_without_content():
    ch = _channel()
    assert asyncio.run(ch._extract({"voice": {}}, "voice")) == "[voice]"


def test_extract_mixed():
    ch = _channel()
    body = {"mixed": {"item": [
        {"type": "text", "text": {"content": "hi there"}},
        {"type": "image"},
    ]}}
    out = asyncio.run(ch._extract(body, "mixed"))
    assert "hi there" in out
    assert "[image]" in out


# ── media extract (download mocked; save_media_bytes stubbed off disk) ─


def test_extract_image_downloads_and_labels(monkeypatch):
    import raven.channels.adapters.wecom.channel as wecom_mod
    monkeypatch.setattr(wecom_mod, "save_media_bytes", lambda channel, data, name: Path("/m/abcd_pic.jpg"))
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.download_file = AsyncMock(return_value=(b"\x89PNG", "pic.jpg"))
    out = asyncio.run(ch._extract({"image": {"url": "u", "aeskey": "k"}}, "image"))
    assert "[image: pic.jpg]" in out
    assert "[Image: source: /m/abcd_pic.jpg]" in out


def test_extract_file_uses_provided_name_over_server_name(monkeypatch):
    import raven.channels.adapters.wecom.channel as wecom_mod
    monkeypatch.setattr(wecom_mod, "save_media_bytes", lambda channel, data, name: Path("/m/h_doc.pdf"))
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.download_file = AsyncMock(return_value=(b"%PDF", "server.bin"))
    out = asyncio.run(ch._extract({"file": {"url": "u", "aeskey": "k", "name": "doc.pdf"}}, "file"))
    assert "[file: doc.pdf]" in out  # display uses the provided name, not the server fname


def test_extract_image_missing_keys_marks_failed():
    ch = _channel()
    assert asyncio.run(ch._extract({"image": {}}, "image")) == "[image: image: download failed]"


def test_extract_image_marks_failed_when_download_returns_no_data():
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.download_file = AsyncMock(return_value=(None, "pic.jpg"))
    out = asyncio.run(ch._extract({"image": {"url": "u", "aeskey": "k"}}, "image"))
    assert out == "[image: image: download failed]"


# ── dedup ─────────────────────────────────────────────────────────────


def test_process_dedup_skips_repeated_msgid():
    ch = _channel()
    frame = SimpleNamespace(body={
        "msgid": "m1", "from": {"userid": "u1"},
        "chattype": "single", "text": {"content": "hi"},
    })
    asyncio.run(ch._process(frame, "text"))
    asyncio.run(ch._process(frame, "text"))  # same msgid -> deduped
    assert ch.intake.publish.await_count == 1


def test_frames_are_lru_capped(monkeypatch):
    import raven.channels.adapters.wecom.channel as wecom_mod
    monkeypatch.setattr(wecom_mod, "_FRAMES_CAP", 2)
    ch = _channel()
    for i in range(3):
        frame = SimpleNamespace(body={
            "msgid": f"m{i}", "from": {"userid": f"u{i}"},
            "chattype": "single", "chatid": f"c{i}", "text": {"content": "hi"},
        })
        asyncio.run(ch._process(frame, "text"))
    assert len(ch._frames) == 2       # capped
    assert "c0" not in ch._frames     # oldest evicted
    assert "c2" in ch._frames


# ── outbound (send) ────────────────────────────────────────────────────


def test_send_noop_without_client():
    ch = _channel()
    ch._client = None
    asyncio.run(ch.send("c1", "hi"))  # no raise


def test_send_skips_empty_content():
    ch = _channel()
    ch._client = AsyncMock()
    asyncio.run(ch.send("c1", "   "))
    ch._client.reply_stream.assert_not_awaited()


def test_send_skips_when_no_frame():
    ch = _channel()
    ch._client = AsyncMock()
    asyncio.run(ch.send("c1", "hi"))
    ch._client.reply_stream.assert_not_awaited()


def test_send_media_surfaced_as_notice():
    """reply_stream is text-only — dropped attachments become a visible
    notice instead of vanishing."""
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "hi", media=["/m/report.pdf"]))
    sent_text = ch._client.reply_stream.await_args.args[2]
    assert "hi" in sent_text and "[Attachment not sent: report.pdf]" in sent_text


def test_send_replies_with_cached_frame():
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "hi"))
    ch._client.reply_stream.assert_awaited_once()
    args, kwargs = ch._client.reply_stream.await_args
    assert args[0] is ch._frames["c1"]
    assert args[1].startswith("stream_")
    assert args[2] == "hi"
    assert kwargs["finish"] is True


def test_send_reraises_transient_for_manager_retry():
    """A ws drop/timeout propagates (the inbound frame is still cached, so the
    manager retry can succeed); business errors stay swallowed."""
    import pytest
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.reply_stream = AsyncMock(side_effect=TimeoutError("ws ack timeout"))
    ch._frames["c1"] = SimpleNamespace(body={})
    with pytest.raises(TimeoutError):
        asyncio.run(ch.send("c1", "hi"))


def test_send_swallows_reply_error():
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.reply_stream = AsyncMock(side_effect=RuntimeError("boom"))
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "hi"))  # no raise


# ── enter_chat welcome ─────────────────────────────────────────────────


def test_on_enter_chat_sends_welcome_when_configured():
    ch = _channel(welcome_message="hello there")
    ch._client = AsyncMock()
    frame = SimpleNamespace(body={"chatid": "c1"})
    asyncio.run(ch._on_enter_chat(frame))
    ch._client.reply_welcome.assert_awaited_once()
    args, _ = ch._client.reply_welcome.await_args
    assert args[0] is frame
    assert args[1] == {"msgtype": "text", "text": {"content": "hello there"}}


def test_on_enter_chat_noop_without_welcome():
    ch = _channel(welcome_message="")
    ch._client = AsyncMock()
    asyncio.run(ch._on_enter_chat(SimpleNamespace(body={"chatid": "c1"})))
    ch._client.reply_welcome.assert_not_awaited()


# ── inbound early gate (reject before side effects) ───────────────────


def test_process_disallowed_sender_skips_download_and_publish():
    """Denied sender is rejected before _extract (which downloads media) and
    before publishing — not merely dropped at the central intake."""
    ch = _channel(allow_from=[])
    ch._extract = AsyncMock()
    frame = SimpleNamespace(body={
        "msgid": "m1", "from": {"userid": "u1"},
        "chattype": "single", "image": {"url": "u", "aeskey": "k"},
    })
    asyncio.run(ch._process(frame, "image"))
    ch._extract.assert_not_awaited()              # no media download for a denied sender
    ch.intake.publish.assert_not_awaited()


# ── contract conformance ───────────────────────────────────────────────


def test_wecom_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations
    ch = _channel()
    assert isinstance(ch, Channel)              # name/capabilities/start/stop/send
    assert capability_violations(ch) == []      # no login/streaming declared or implemented


def test_wecom_spec_import_is_cheap():
    """Importing wecom.spec must NOT pull in wecom_aibot_sdk (the heavy import is
    deferred into SPEC.factory)."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.wecom.spec as s;"
        "assert 'wecom_aibot_sdk' not in sys.modules, 'spec import pulled in wecom_aibot_sdk';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'WeCom'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
