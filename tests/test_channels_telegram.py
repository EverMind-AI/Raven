"""Tests for ``raven.channels.adapters.telegram`` — markdown rendering,
media extension resolution, allowlist matching, group addressing, and
inbound metadata. All pure/synchronous surface; no live bot."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.channels.adapters.telegram.channel import (
    TelegramChannel,
    _inbound_ext,
    _markdown_to_html,
    _outbound_kind,
)


def _channel(**cfg):
    config = SimpleNamespace(
        allow_from=cfg.get("allow_from", ["*"]),
        group_policy=cfg.get("group_policy", "open"),
        token="t",
        proxy=None,
        reply_to_message=False,
    )
    return TelegramChannel(config)


# ── markdown → Telegram HTML ──────────────────────────────────────────


@pytest.mark.parametrize(
    "src, expected",
    [
        ("**bold**", "<b>bold</b>"),
        ("__bold__", "<b>bold</b>"),
        ("_italic_", "<i>italic</i>"),
        ("~~gone~~", "<s>gone</s>"),
        ("`code`", "<code>code</code>"),
        ("[t](http://u)", '<a href="http://u">t</a>'),
        ("- item", "• item"),
        ("# Heading", "Heading"),
    ],
)
def test_markdown_inline(src, expected):
    assert expected in _markdown_to_html(src)


def test_markdown_escapes_html_specials():
    out = _markdown_to_html("a < b & c")
    assert "&lt;" in out and "&amp;" in out
    assert "<b" not in out  # the stray '<' must not become a tag


def test_markdown_code_block_preserves_content():
    out = _markdown_to_html("```\nif a < b:\n    x\n```")
    assert "<pre><code>" in out
    assert "&lt;" in out  # content inside the block is escaped, not formatted


def test_markdown_table_renders_as_pre_block():
    table = "| a | b |\n| - | - |\n| 1 | 2 |"
    out = _markdown_to_html(table)
    assert "<pre>" in out


def test_markdown_empty():
    assert _markdown_to_html("") == ""


# ── outbound send (chat_id, content, media) ───────────────────────────


def _running_channel(tmp_path=None):
    ch = _channel()
    ch._app = SimpleNamespace(
        bot=SimpleNamespace(
            send_message=AsyncMock(),
            send_message_draft=AsyncMock(),
            send_photo=AsyncMock(),
            send_voice=AsyncMock(),
            send_audio=AsyncMock(),
            send_document=AsyncMock(),
        )
    )
    return ch


def test_send_text_reaches_sdk_with_chat_id_and_content():
    ch = _running_channel()
    asyncio.run(ch.send("42", "hello world"))
    ch._app.bot.send_message.assert_awaited()
    kwargs = ch._app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert "hello world" in kwargs["text"]
    # carry-nothing: no threading / reply parameters are passed
    assert "reply_parameters" not in kwargs
    assert "message_thread_id" not in kwargs


def test_send_media_reaches_sdk(tmp_path):
    ch = _running_channel()
    photo = tmp_path / "pic.jpg"
    photo.write_bytes(b"x")
    asyncio.run(ch.send("42", "", media=[str(photo)]))
    ch._app.bot.send_photo.assert_awaited()
    kwargs = ch._app.bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert "photo" in kwargs
    assert "reply_parameters" not in kwargs
    assert "message_thread_id" not in kwargs
    ch._app.bot.send_message.assert_not_awaited()   # empty content -> no text send


def test_send_invalid_chat_id_is_dropped():
    ch = _running_channel()
    asyncio.run(ch.send("not-an-int", "hi"))
    ch._app.bot.send_message.assert_not_awaited()


def test_send_when_not_running_is_noop():
    ch = _channel()
    ch._app = None
    asyncio.run(ch.send("42", "hi"))   # must not raise


# ── media extension resolution ────────────────────────────────────────


@pytest.mark.parametrize(
    "path, kind",
    [("a.jpg", "photo"), ("a.png", "photo"), ("a.ogg", "voice"),
     ("a.mp3", "audio"), ("a.pdf", "document"), ("noext", "document")],
)
def test_outbound_kind(path, kind):
    assert _outbound_kind(path) == kind


@pytest.mark.parametrize(
    "kind, mime, filename, ext",
    [
        ("image", "image/png", None, ".png"),
        ("image", None, None, ".jpg"),
        ("voice", None, None, ".ogg"),
        ("audio", None, None, ".mp3"),
        ("file", None, "report.pdf", ".pdf"),
        ("file", None, None, ""),
    ],
)
def test_inbound_ext(kind, mime, filename, ext):
    assert _inbound_ext(kind, mime, filename) == ext


# ── allowlist (id|username form) ──────────────────────────────────────


def test_is_allowed_wildcard():
    assert _channel(allow_from=["*"]).is_allowed("123|alice") is True


def test_is_allowed_empty_denies():
    assert _channel(allow_from=[]).is_allowed("123|alice") is False


def test_is_allowed_matches_numeric_id():
    assert _channel(allow_from=["123"]).is_allowed("123|alice") is True


def test_is_allowed_matches_username():
    assert _channel(allow_from=["alice"]).is_allowed("123|alice") is True


def test_is_allowed_rejects_unknown():
    assert _channel(allow_from=["alice"]).is_allowed("999|bob") is False


def test_is_allowed_rejects_malformed_sender():
    """The id|username guard rejects a sender with no separator, a non-numeric
    id, or an empty username — short-circuiting before the match attempt (note
    'abc|alice' is denied despite 'alice' being allowed)."""
    ch = _channel(allow_from=["alice"])
    assert ch.is_allowed("bob") is False          # no "|" separator
    assert ch.is_allowed("abc|alice") is False    # id part is not numeric
    assert ch.is_allowed("123|") is False         # empty username


# ── group addressing ──────────────────────────────────────────────────


def test_mentions_bot_via_text_fallback():
    ch = _channel()
    ch._bot_username, ch._bot_id = "mybot", 42
    assert ch._mentions_bot("hey @mybot there", None) is True


def test_mentions_bot_via_entity():
    ch = _channel()
    ch._bot_username, ch._bot_id = "mybot", 42
    ent = SimpleNamespace(type="mention", offset=4, length=6)
    assert ch._mentions_bot("hey @mybot", [ent]) is True


def test_mentions_bot_text_mention_by_id():
    ch = _channel()
    ch._bot_username, ch._bot_id = "mybot", 42
    ent = SimpleNamespace(type="text_mention", user=SimpleNamespace(id=42))
    assert ch._mentions_bot("hello", [ent]) is True


def test_mentions_bot_absent():
    ch = _channel()
    ch._bot_username, ch._bot_id = "mybot", 42
    assert ch._mentions_bot("nothing here", None) is False


# ── session key + metadata + sender id ────────────────────────────────


def test_topic_session_key_private_is_none():
    msg = SimpleNamespace(message_thread_id=7, chat=SimpleNamespace(type="private"), chat_id=1)
    assert TelegramChannel._topic_session_key(msg) is None


def test_topic_session_key_forum_topic():
    msg = SimpleNamespace(message_thread_id=7, chat=SimpleNamespace(type="supergroup"), chat_id=99)
    assert TelegramChannel._topic_session_key(msg) == "telegram:99:topic:7"


def test_sender_id_with_and_without_username():
    assert TelegramChannel._sender_id(SimpleNamespace(id=5, username="bob")) == "5|bob"
    assert TelegramChannel._sender_id(SimpleNamespace(id=5, username=None)) == "5"


def test_metadata_shape():
    msg = SimpleNamespace(
        message_id=11,
        message_thread_id=None,
        chat=SimpleNamespace(type="group", is_forum=False),
    )
    user = SimpleNamespace(id=5, username="bob", first_name="Bob")
    meta = TelegramChannel._metadata(msg, user)
    assert meta["message_id"] == 11
    assert meta["is_group"] is True
    assert meta["username"] == "bob"


# ── inbound early gate (reject before side effects) ───────────────────


def test_on_message_disallowed_sender_skips_download_and_typing():
    """Denied sender is rejected before media download / transcription / typing,
    via Telegram's id|username matching — not merely dropped at intake."""
    ch = _channel(allow_from=[])
    ch._remember_thread = MagicMock()
    ch._addressed_to_bot = AsyncMock(return_value=True)
    ch._download = AsyncMock()
    ch._start_typing = MagicMock()
    ch.intake.publish = AsyncMock()
    user = SimpleNamespace(id=5, username="bob")
    update = SimpleNamespace(message=SimpleNamespace(), effective_user=user)
    asyncio.run(ch._on_message(update, None))
    ch._download.assert_not_awaited()        # no media download for a denied sender
    ch._start_typing.assert_not_called()
    ch.intake.publish.assert_not_awaited()


# ── album group buffering (straggler race) ────────────────────────────


def test_flush_group_straggler_schedules_new_flush(monkeypatch):
    """An album item arriving while the flush is publishing must get a fresh
    flush task — not leak. (Old bug: the buffer was popped before the task
    key, so the straggler rebuilt the buffer, saw the stale task key, and the
    rebuilt buffer was never flushed: silent message loss.)"""
    import raven.channels.adapters.telegram.channel as tg
    monkeypatch.setattr(tg, "_ALBUM_WINDOW_S", 0)
    ch = _channel()
    ch._start_typing = MagicMock()
    user = SimpleNamespace(id=5, username="bob")
    published = []

    async def fake_publish(**kw):
        published.append(kw)
        if len(published) == 1:                      # straggler lands mid-publish
            ch._buffer_group("g1", "c1", user, "late", ["/m/late.jpg"], {}, None)

    ch.intake.publish = fake_publish

    async def scenario():
        ch._buffer_group("g1", "c1", user, "first", ["/m/a.jpg"], {}, None)
        await ch._group_tasks["c1:g1"]               # straggler arrives inside this flush
        assert "c1:g1" in ch._group_tasks            # fresh flush was scheduled
        await ch._group_tasks["c1:g1"]               # second flush drains the straggler

    asyncio.run(scenario())
    assert [p["content"] for p in published] == ["first", "late"]
    assert published[1]["media"] == ["/m/late.jpg"]  # nothing lost


# ── stop contract ──────────────────────────────────────────────────────


def test_stop_reaps_helper_tasks():
    """Typing and group-buffer tasks are cancelled AND awaited so none die
    unobserved after stop(); double-stop is a no-op (stop contract #2/#4)."""
    ch = _channel()

    async def scenario():
        ch._typing["c1"] = asyncio.create_task(asyncio.sleep(3600))
        ch._group_tasks["g1"] = asyncio.create_task(asyncio.sleep(3600))
        typing, group = ch._typing["c1"], ch._group_tasks["g1"]
        await asyncio.wait_for(ch.stop(), timeout=2)
        assert typing.cancelled() and group.cancelled()
        assert not ch._typing and not ch._group_tasks
        await asyncio.wait_for(ch.stop(), timeout=2)   # idempotent

    asyncio.run(scenario())


# ── contract conformance (migrated to capability contract) ────────────


def test_telegram_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations
    ch = _channel()
    assert isinstance(ch, Channel)              # name/capabilities/start/stop/send
    assert capability_violations(ch) == []      # no login/streaming declared or implemented


def test_telegram_spec_import_is_cheap():
    """Importing telegram.spec must NOT pull in the python-telegram-bot SDK
    (the heavy import is deferred into SPEC.factory)."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.telegram.spec as s;"
        "assert 'telegram' not in sys.modules, 'spec import pulled in the telegram SDK';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'Telegram'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
