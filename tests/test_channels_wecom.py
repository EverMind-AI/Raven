"""Tests for raven.channels.adapters.wecom — frame/body parsing, per-type
content extraction, inbound dedup, and outbound text/media sends. Pure
surface; no live SDK (media upload/send is mocked at the WSClient boundary)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("wecom_aibot_sdk")

from raven.channels.adapters.wecom.channel import WecomChannel


def _channel(welcome_message="", allow_from=("*",)):
    cfg = SimpleNamespace(bot_id="b", secret="s", welcome_message=welcome_message, allow_from=list(allow_from))
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
    body = {
        "mixed": {
            "item": [
                {"type": "text", "text": {"content": "hi there"}},
                {"type": "image"},
            ]
        }
    }
    out = asyncio.run(ch._extract(body, "mixed"))
    assert "hi there" in out
    assert "[image]" in out


# ── media extract (download mocked; save_media_bytes stubbed off disk) ─


def test_extract_image_downloads_and_labels(monkeypatch):
    import raven.channels.adapters.wecom.channel as wecom_mod

    saved = Path("/m/abcd_pic.jpg")
    monkeypatch.setattr(wecom_mod, "save_media_bytes", lambda channel, data, name: saved)
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.download_file = AsyncMock(return_value=(b"\x89PNG", "pic.jpg"))
    out = asyncio.run(ch._extract({"image": {"url": "u", "aeskey": "k"}}, "image"))
    assert "[image: pic.jpg]" in out
    # str(saved) so the expected separators match whatever platform renders the path.
    assert f"[Image: source: {saved}]" in out


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
    frame = SimpleNamespace(
        body={
            "msgid": "m1",
            "from": {"userid": "u1"},
            "chattype": "single",
            "text": {"content": "hi"},
        }
    )
    asyncio.run(ch._process(frame, "text"))
    asyncio.run(ch._process(frame, "text"))  # same msgid -> deduped
    assert ch.intake.publish.await_count == 1


def test_frames_are_lru_capped(monkeypatch):
    import raven.channels.adapters.wecom.channel as wecom_mod

    monkeypatch.setattr(wecom_mod, "_FRAMES_CAP", 2)
    ch = _channel()
    for i in range(3):
        frame = SimpleNamespace(
            body={
                "msgid": f"m{i}",
                "from": {"userid": f"u{i}"},
                "chattype": "single",
                "chatid": f"c{i}",
                "text": {"content": "hi"},
            }
        )
        asyncio.run(ch._process(frame, "text"))
    assert len(ch._frames) == 2  # capped
    assert "c0" not in ch._frames  # oldest evicted
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


def test_send_media_replies_with_cached_frame(tmp_path):
    """A cached frame routes the file through the in-band reply (upload +
    media_id reply), mirroring how the text reply is routed."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "here you go", media=[str(f)]))
    ch._client.reply_stream.assert_awaited_once()  # text first
    ch._client.reply_media.assert_awaited_once()
    args, _ = ch._client.reply_media.await_args
    assert args[0] is ch._frames["c1"]
    assert args[1] == str(f)


def test_send_media_without_frame_uses_proactive_send(tmp_path):
    """No cached frame: the SDK's chatid-based proactive send still delivers."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    ch = _channel()
    ch._client = AsyncMock()
    asyncio.run(ch.send("c1", "", media=[str(f)]))
    ch._client.send_media_message.assert_awaited_once_with("c1", str(f))
    ch._client.reply_media.assert_not_awaited()


def test_send_media_resolves_file_url(tmp_path):
    """A Path.as_uri() ref (the form raven itself emits) must resolve on both
    POSIX and Windows: url2pathname handles the Windows drive prefix."""
    f = tmp_path / "data.csv"
    f.write_text("x")
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=[f.as_uri()]))
    assert ch._client.reply_media.await_args.args[1] == str(f)


def test_send_media_blank_refs_skipped():
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=["", "   "]))
    ch._client.reply_media.assert_not_awaited()
    ch._client.reply_stream.assert_not_awaited()  # no failure notice for blanks


def test_send_media_missing_file_reports_failure():
    """A missing file is logged and surfaced to the user, not dropped."""
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "done", media=["/nope/report.pdf"]))
    ch._client.reply_media.assert_not_awaited()
    assert ch._client.reply_stream.await_count == 2  # content, then failure notice
    assert "[Attachment send failed: report.pdf]" in ch._client.reply_stream.await_args.args[2]


def test_send_media_too_large_reports_failure(monkeypatch, tmp_path):
    import raven.channels.adapters.wecom.channel as wecom_mod

    monkeypatch.setattr(wecom_mod, "_MAX_MEDIA_BYTES", 4)
    f = tmp_path / "big.bin"
    f.write_bytes(b"12345678")
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=[str(f)]))
    ch._client.reply_media.assert_not_awaited()  # rejected before upload
    assert "[Attachment send failed: big.bin]" in ch._client.reply_stream.await_args.args[2]


def test_send_media_upload_error_reports_failure(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.reply_media = AsyncMock(side_effect=RuntimeError("upload failed"))
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "hi", media=[str(f)]))  # no raise: failure is reported
    assert "[Attachment send failed: report.pdf]" in ch._client.reply_stream.await_args.args[2]


def test_send_media_failure_notice_only_when_text_sendable():
    """No frame means no text channel either: the failure stays in the log."""
    ch = _channel()
    ch._client = AsyncMock()
    asyncio.run(ch.send("c1", "", media=["/nope/report.pdf"]))
    ch._client.send_media_message.assert_not_awaited()
    ch._client.reply_stream.assert_not_awaited()


def test_send_media_remote_file_uri_reports_failure(tmp_path):
    """A file:// URI naming another host is refused even when its path happens
    to exist locally: the host is what is being refused, not a missing file."""
    real = tmp_path / "x.pdf"
    real.write_bytes(b"%PDF")
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=[f"file://server{real}"]))
    ch._client.reply_media.assert_not_awaited()
    assert "[Attachment send failed: x.pdf]" in ch._client.reply_stream.await_args.args[2]


def test_send_media_unparsable_file_uri_reports_failure():
    """A malformed URI is refused, and its notice still names something."""
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=["file://[bad/x.pdf"]))
    ch._client.reply_media.assert_not_awaited()
    assert "[Attachment send failed:" in ch._client.reply_stream.await_args.args[2]


def test_send_media_failure_notice_names_file_uri_by_basename(tmp_path):
    """The notice reads the file's name, not the percent-encoded URI tail."""
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF")
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.reply_media = AsyncMock(side_effect=RuntimeError("nope"))
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=[target.as_uri()]))
    assert "[Attachment send failed: report.pdf]" in ch._client.reply_stream.await_args.args[2]


def test_send_media_failure_notice_decodes_file_uri_basename(tmp_path):
    """A space in the name distinguishes a decoded basename from the raw URI
    tail: ``my report 1.pdf``, never ``my%20report%201.pdf``."""
    target = tmp_path / "my report 1.pdf"
    target.write_bytes(b"%PDF")
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.reply_media = AsyncMock(side_effect=RuntimeError("nope"))
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=[target.as_uri()]))
    notice = ch._client.reply_stream.await_args.args[2]
    assert "[Attachment send failed: my report 1.pdf]" in notice
    assert "my%20report%201.pdf" not in notice


def test_resolve_media_path_treats_a_stat_failure_as_missing(monkeypatch, tmp_path):
    """A file removed between the existence check and the size read is reported
    as missing, not left to escape ``send()`` as an OSError the hub retries."""
    f = tmp_path / "vanishing.pdf"
    f.write_bytes(b"%PDF")
    real_is_file = Path.is_file
    real_stat = Path.stat

    def is_file(self):
        return True if self == f else real_is_file(self)

    def stat(self, *args, **kwargs):
        if self == f:
            raise FileNotFoundError(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "stat", stat)
    assert WecomChannel._resolve_media_path(str(f)) is None


def test_send_media_transient_failure_reraises(tmp_path):
    """A transient network error is re-raised for the hub to retry, like the
    text path, instead of becoming a permanent failure notice."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF")
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.reply_media = AsyncMock(side_effect=ConnectionResetError("ws dropped"))
    ch._frames["c1"] = SimpleNamespace(body={})
    with pytest.raises(ConnectionResetError):
        asyncio.run(ch.send("c1", "hi", media=[str(f)]))


def test_send_two_attachments_both_sent(tmp_path):
    """Every attachment in the list is uploaded, not only the first."""
    first = tmp_path / "one.pdf"
    first.write_bytes(b"1")
    second = tmp_path / "two.pdf"
    second.write_bytes(b"2")
    ch = _channel()
    ch._client = AsyncMock()
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "two files", media=[str(first), str(second)]))
    assert ch._client.reply_media.await_count == 2
    assert [call.args[1] for call in ch._client.reply_media.await_args_list] == [str(first), str(second)]


def test_send_media_continues_after_one_upload_failure(tmp_path):
    """One attachment's failure does not abort the rest of the list."""
    first = tmp_path / "one.pdf"
    first.write_bytes(b"1")
    second = tmp_path / "two.pdf"
    second.write_bytes(b"2")
    ch = _channel()
    ch._client = AsyncMock()
    ch._client.reply_media = AsyncMock(side_effect=[RuntimeError("boom"), None])
    ch._frames["c1"] = SimpleNamespace(body={})
    asyncio.run(ch.send("c1", "", media=[str(first), str(second)]))
    assert ch._client.reply_media.await_count == 2
    assert [call.args[1] for call in ch._client.reply_media.await_args_list] == [str(first), str(second)]
    assert "[Attachment send failed: one.pdf]" in ch._client.reply_stream.await_args.args[2]


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
    frame = SimpleNamespace(
        body={
            "msgid": "m1",
            "from": {"userid": "u1"},
            "chattype": "single",
            "image": {"url": "u", "aeskey": "k"},
        }
    )
    asyncio.run(ch._process(frame, "image"))
    ch._extract.assert_not_awaited()  # no media download for a denied sender
    ch.intake.publish.assert_not_awaited()


# ── contract conformance ───────────────────────────────────────────────


def test_wecom_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations

    ch = _channel()
    assert isinstance(ch, Channel)  # name/capabilities/start/stop/send
    assert capability_violations(ch) == []  # no login/streaming declared or implemented


def test_wecom_declares_file_attachments():
    from raven.channels.adapters.wecom.spec import SPEC

    assert _channel().capabilities.file_attachments is True
    assert SPEC.capabilities.file_attachments is True


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
