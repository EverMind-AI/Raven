"""Tests for the matrix adapter package — pure content helpers (content.py):
markdown->HTML, payload builders, event-field extraction, and room/mention
decisions. The nio sync / E2EE / upload / download flows are live and left to
integration/manual testing."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from raven.channels.adapters.matrix import content
from raven.channels.adapters.matrix.channel import MatrixChannel


def _event(cnt=None, **attrs):
    attrs.setdefault("source", {"content": cnt or {}})
    return SimpleNamespace(**attrs)


def _channel(**cfg):
    config = SimpleNamespace(
        allow_from=cfg.get("allow_from", ["*"]),
        user_id="@bot:x",
        group_policy=cfg.get("group_policy", "open"),
        group_allow_from=cfg.get("group_allow_from", []),
        allow_room_mentions=cfg.get("allow_room_mentions", False),
        e2ee_enabled=cfg.get("e2ee_enabled", False),
    )
    return MatrixChannel(config)


# ── markdown -> HTML ──────────────────────────────────────────────────


def test_render_markdown_html_formats():
    html = content.render_markdown_html("**bold**")
    assert html and "<strong>bold</strong>" in html


def test_render_markdown_html_plain_returns_none():
    assert content.render_markdown_html("just text") is None


def test_render_markdown_html_sanitizes_disallowed():
    html = content.render_markdown_html("[x](javascript:alert(1))")
    assert html is None or "javascript:" not in html


# ── payload builders ──────────────────────────────────────────────────


def test_attr_filter():
    assert content._attr_filter("a", "href", "https://x") == "https://x"
    assert content._attr_filter("a", "href", "javascript:alert(1)") is None
    assert content._attr_filter("img", "src", "mxc://h/a") == "mxc://h/a"
    assert content._attr_filter("img", "src", "http://x/a.png") is None
    assert content._attr_filter("code", "class", "language-python") == "language-python"
    assert content._attr_filter("code", "class", "language-_evil") is None
    assert content._attr_filter("p", "title", "anything") == "anything"


def test_build_text_content_plain_vs_formatted():
    plain = content.build_text_content("hi")
    assert plain == {"msgtype": "m.text", "body": "hi", "m.mentions": {}}
    rich = content.build_text_content("**hi**")
    assert rich["format"] == content.HTML_FORMAT
    assert "formatted_body" in rich


def test_build_attachment_content_kind_and_url():
    plain = content.build_attachment_content(filename="p.png", mime="image/png", size_bytes=10, mxc_url="mxc://h/a")
    assert plain["msgtype"] == "m.image"
    assert plain["url"] == "mxc://h/a" and "file" not in plain
    assert (
        content.build_attachment_content(
            filename="f.bin", mime="application/octet-stream", size_bytes=1, mxc_url="mxc://h/b"
        )["msgtype"]
        == "m.file"
    )


def test_build_attachment_content_encrypted():
    enc = content.build_attachment_content(
        filename="a.ogg",
        mime="audio/ogg",
        size_bytes=5,
        mxc_url="mxc://h/c",
        encryption_info={"key": {"k": "x"}},
    )
    assert enc["msgtype"] == "m.audio"
    assert enc["file"]["url"] == "mxc://h/c" and "url" not in enc


def test_build_thread_relates_to():
    rel = content.build_thread_relates_to({"thread_root_event_id": "$r", "thread_reply_to_event_id": "$y"})
    assert rel == {
        "rel_type": "m.thread",
        "event_id": "$r",
        "m.in_reply_to": {"event_id": "$y"},
        "is_falling_back": True,
    }
    assert content.build_thread_relates_to(None) is None
    assert content.build_thread_relates_to({"thread_root_event_id": ""}) is None


# ── event extraction ──────────────────────────────────────────────────


def test_event_content_and_thread():
    evt = _event(
        {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}},
        event_id="$evt",
    )
    assert content.event_content(evt)["m.relates_to"]["event_id"] == "$root"
    assert content.thread_root_id(evt) == "$root"
    assert content.thread_metadata(evt) == {"thread_root_event_id": "$root", "thread_reply_to_event_id": "$evt"}


def test_thread_root_id_non_thread():
    evt = _event({"m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}})
    assert content.thread_root_id(evt) is None
    assert content.thread_metadata(evt) is None


def test_attachment_kind_mime_size():
    evt = _event({"msgtype": "m.audio", "info": {"size": 42, "mimetype": "audio/ogg"}})
    assert content.attachment_kind(evt) == "audio"
    assert content.declared_size_bytes(evt) == 42
    assert content.media_mime(evt) == "audio/ogg"
    assert content.attachment_kind(_event({"msgtype": "m.sticker"})) == "file"
    assert content.declared_size_bytes(_event({})) is None


def test_media_mime_falls_back_to_event_attr():
    assert content.media_mime(_event({}, mimetype="image/gif")) == "image/gif"
    assert content.media_mime(_event({})) is None


def test_is_encrypted_media():
    enc = _event(key={"k": "x"}, hashes={"sha256": "h"}, iv="iv")
    assert content.is_encrypted_media(enc) is True
    assert content.is_encrypted_media(_event(url="mxc://h/a")) is False


def test_media_filename():
    assert content.media_filename(_event(body="photo.png"), "image") == "photo.png"
    assert content.media_filename(_event(body="  "), "image") == "image"
    assert content.media_filename(_event(body=None), "file") == content.DEFAULT_ATTACH_NAME


def test_attachment_path_uses_event_prefix_and_keeps_suffix():
    evt = _event(event_id="$abc:server")
    p = content.attachment_path(Path("/tmp/m"), evt, "image", "pic.png", "image/png")
    assert p.parent == Path("/tmp/m")
    assert p.name.endswith("_pic.png")
    assert p.name.startswith("abc")


def test_attachment_path_guesses_suffix_from_mime():
    evt = _event(event_id="$e")
    p = content.attachment_path(Path("/tmp/m"), evt, "image", "noext", "image/png")
    assert p.suffix == ".png"


# ── room / mention decisions ──────────────────────────────────────────


def test_is_direct_room():
    assert content.is_direct_room(SimpleNamespace(member_count=2)) is True
    assert content.is_direct_room(SimpleNamespace(member_count=5)) is False
    assert content.is_direct_room(SimpleNamespace(member_count=None)) is False


def test_is_bot_mentioned():
    by_id = _event({"m.mentions": {"user_ids": ["@bot:x"]}})
    assert content.is_bot_mentioned(by_id, "@bot:x", False) is True
    assert content.is_bot_mentioned(by_id, "@other:x", False) is False
    room_ping = _event({"m.mentions": {"room": True}})
    assert content.is_bot_mentioned(room_ping, "@bot:x", True) is True
    assert content.is_bot_mentioned(room_ping, "@bot:x", False) is False
    assert content.is_bot_mentioned(_event({}), "@bot:x", True) is False


# ── outbound media paths ──────────────────────────────────────────────


def test_collect_media_candidates_dedup_and_order():
    out = content.collect_media_candidates(["/tmp/a", "/tmp/a", "  ", "", "/tmp/b"])
    assert [p.name for p in out] == ["a", "b"]


# ── channel decision logic ────────────────────────────────────────────


def _group(room_id="!r"):
    return SimpleNamespace(room_id=room_id, member_count=5, display_name="Room")


def test_should_process_sender_and_policies():
    evt = SimpleNamespace(sender="@u:x", source={"content": {}})

    denied = _channel(allow_from=[])
    assert denied._should_process(_group(), evt) is False

    # direct room short-circuits policy
    direct = SimpleNamespace(room_id="!d", member_count=2, display_name="DM")
    assert _channel()._should_process(direct, evt) is True

    assert _channel(group_policy="open")._should_process(_group(), evt) is True

    allowlist = _channel(group_policy="allowlist", group_allow_from=["!r"])
    assert allowlist._should_process(_group("!r"), evt) is True
    assert allowlist._should_process(_group("!other"), evt) is False


def test_should_process_mention_policy():
    ch = _channel(group_policy="mention")
    mentioned = SimpleNamespace(sender="@u:x", source={"content": {"m.mentions": {"user_ids": ["@bot:x"]}}})
    plain = SimpleNamespace(sender="@u:x", source={"content": {}})
    assert ch._should_process(_group(), mentioned) is True
    assert ch._should_process(_group(), plain) is False


def test_base_metadata():
    ch = _channel()
    evt = SimpleNamespace(
        event_id="$e",
        source={"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}},
    )
    meta = ch._base_metadata(_group(), evt)
    assert meta["room"] == "Room"
    assert meta["event_id"] == "$e"
    assert meta["thread_root_event_id"] == "$root"
    assert meta["thread_reply_to_event_id"] == "$e"


# ── outbound send (new spine signature) ───────────────────────────────


@pytest.mark.asyncio
async def test_send_text_reaches_room():
    ch = _channel()
    ch.client = AsyncMock()
    ch._stop_typing = AsyncMock()

    await ch.send("!room", "hello world")

    ch.client.room_send.assert_awaited_once()
    kwargs = ch.client.room_send.await_args.kwargs
    assert kwargs["room_id"] == "!room"
    payload = kwargs["content"]
    assert payload["body"] == "hello world"
    assert "m.relates_to" not in payload


@pytest.mark.asyncio
async def test_send_media_uploads_and_carries_nothing():
    ch = _channel()
    ch.client = AsyncMock()
    ch._stop_typing = AsyncMock()
    ch._media_limit_bytes = AsyncMock(return_value=1000)
    ch._upload_attachment = AsyncMock(return_value=None)

    await ch.send("!room", "", media=["/tmp/a.png"])

    ch._upload_attachment.assert_awaited_once()
    room_id, path, _limit, relates_to = ch._upload_attachment.await_args.args
    assert room_id == "!room"
    assert path.name == "a.png"
    assert relates_to is None
    ch.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_noop_without_client():
    ch = _channel()
    ch.client = None
    await ch.send("!room", "ignored")


# ── contract conformance ───────────────────────────────────────────────


def test_matrix_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations

    ch = _channel()
    assert isinstance(ch, Channel)  # name/capabilities/start/stop/send
    assert capability_violations(ch) == []  # no login/streaming declared or implemented


def test_matrix_spec_import_is_cheap():
    """Importing matrix.spec must NOT pull in matrix-nio (deferred into
    SPEC.factory)."""
    import subprocess
    import sys

    code = (
        "import sys, raven.channels.adapters.matrix.spec as s;"
        "assert 'nio' not in sys.modules, 'spec import pulled in matrix-nio';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'Matrix'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
