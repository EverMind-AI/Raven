"""Tests for ``raven.channels.adapters.dingtalk`` — inbound media + handler routing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dingtalk_stream import AckMessage

from raven.channels.adapters.dingtalk import parsing as p
from raven.channels.adapters.dingtalk.api import MAX_REDIRECTS
from raven.channels.adapters.dingtalk.channel import (
    DingTalkCallbackHandler,
    DingTalkChannel,
)
from raven.config.schema import DingTalkConfig


def _make_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DingTalkChannel:
    """Build a DingTalkChannel with the shared media dir redirected under tmp_path."""
    monkeypatch.setattr(
        "raven.channels.media.get_media_dir",
        lambda _channel: tmp_path,
    )
    cfg = DingTalkConfig(enabled=True, client_id="ak", client_secret="sk")
    return DingTalkChannel(cfg)


# ---------------------------------------------------------------------------
# DingTalkAPI.download_file — transport mechanics
# ---------------------------------------------------------------------------


def _api_with_token(ch, token="t1"):
    """Wire the channel's API client with a stubbed token + mock http."""
    ch._api.access_token = AsyncMock(return_value=token)
    ch._api._http = MagicMock()
    return ch._api


async def test_api_download_happy_returns_bytes(tmp_path, monkeypatch):
    """post URL → get bytes → returns the file content."""
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"downloadUrl": "https://dt.example/file"},
        )
    )
    api._http.get = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            content=b"\x89PNG" + b"\x00" * 100,
        )
    )

    data = await api.download_file("dc123")
    assert data is not None and data.startswith(b"\x89PNG")


async def test_api_download_token_missing(tmp_path, monkeypatch):
    """No access token → return None without touching HTTP."""
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch, token=None)
    api._http.post = AsyncMock()
    api._http.get = AsyncMock()

    assert await api.download_file("dc123") is None
    api._http.post.assert_not_called()
    api._http.get.assert_not_called()


async def test_api_download_oversize_aborts(tmp_path, monkeypatch):
    """Body > 20MB → return None."""
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"downloadUrl": "https://dt.example/file"},
        )
    )
    api._http.get = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            content=b"X" * (21 * 1024 * 1024),
        )
    )

    assert await api.download_file("dc123") is None


async def test_api_download_url_missing(tmp_path, monkeypatch):
    """API returns 200 but no downloadUrl → bail before the GET."""
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(return_value=SimpleNamespace(status_code=200, text="", json=lambda: {}))
    api._http.get = AsyncMock()

    assert await api.download_file("dc123") is None
    api._http.get.assert_not_called()


# ---------------------------------------------------------------------------
# _download_dingtalk_file — channel-side persistence
# ---------------------------------------------------------------------------


async def test_download_saves_via_shared_sink(tmp_path, monkeypatch):
    """Bytes persisted through save_media_bytes: <media>/<hash>_<sender>_<name>,
    keeping the sender in the name (no per-sender subdir)."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api.download_file = AsyncMock(return_value=b"\x89PNG" + b"\x00" * 8)

    fp = await ch._download_dingtalk_file("dc123", "image.jpg", "alice")
    assert fp is not None
    saved = Path(fp)
    assert saved.parent == tmp_path
    assert saved.name.endswith("_alice_image.jpg")  # <hash>_alice_image.jpg
    assert saved.read_bytes().startswith(b"\x89PNG")


async def test_download_sanitizes_path_traversal(tmp_path, monkeypatch):
    """A malicious filename must not escape the media dir (path traversal)."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api.download_file = AsyncMock(return_value=b"data")

    fp = await ch._download_dingtalk_file("dc123", "../../../../etc/evil.sh", "alice")
    saved = Path(fp)
    assert saved.parent == tmp_path  # stayed inside the media dir
    assert ".." not in saved.name
    assert saved.name.endswith("evil.sh")  # basename only


async def test_download_none_when_fetch_fails(tmp_path, monkeypatch):
    """API returns None → channel writes nothing and returns None."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api.download_file = AsyncMock(return_value=None)
    assert await ch._download_dingtalk_file("dc123", "x.bin", "alice") is None
    assert not any(tmp_path.iterdir())  # nothing written


# ---------------------------------------------------------------------------
# stop contract
# ---------------------------------------------------------------------------


async def test_stop_awaits_tasks_before_closing_api(tmp_path, monkeypatch):
    """In-flight downloads are cancelled AND awaited before the http client
    they use is closed; double-stop is a no-op (stop contract #2/#4)."""
    ch = _make_channel(tmp_path, monkeypatch)
    task = asyncio.create_task(asyncio.sleep(3600))
    ch._background_tasks.add(task)

    def assert_task_done():
        assert task.done()  # tasks reaped before the client goes away

    ch._api.close = AsyncMock(side_effect=assert_task_done)
    await asyncio.wait_for(ch.stop(), timeout=2)
    assert task.cancelled()
    await asyncio.wait_for(ch.stop(), timeout=2)  # idempotent


# ---------------------------------------------------------------------------
# DingTalkCallbackHandler.process — branch routing
# ---------------------------------------------------------------------------


def _fake_callback_message(data: dict) -> SimpleNamespace:
    return SimpleNamespace(data=data)


def _patch_chatbot_msg(monkeypatch: pytest.MonkeyPatch, fake_msg) -> None:
    fake_cls = MagicMock()
    fake_cls.from_dict.return_value = fake_msg
    monkeypatch.setattr("raven.channels.adapters.dingtalk.channel.ChatbotMessage", fake_cls)


async def test_process_picture_branch_attaches_image_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """picture message with download_code → content has [Image] + Received files footer."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._download_dingtalk_file = AsyncMock(return_value="/fake/path/image.jpg")
    ch._on_message = AsyncMock()

    fake_msg = SimpleNamespace(
        text=None,
        extensions={},
        message_type="picture",
        image_content=SimpleNamespace(download_code="dc-pic"),
        sender_staff_id="alice",
        sender_id="alice",
        sender_nick="Alice",
    )
    _patch_chatbot_msg(monkeypatch, fake_msg)

    handler = DingTalkCallbackHandler(ch)
    await handler.process(_fake_callback_message({}))

    while ch._background_tasks:
        task = next(iter(ch._background_tasks))
        await task

    ch._on_message.assert_awaited_once()
    forwarded_content = ch._on_message.await_args.args[0]
    assert "[Image]" in forwarded_content
    assert "Received files:" in forwarded_content
    assert "/fake/path/image.jpg" in forwarded_content
    forwarded_file_paths = ch._on_message.await_args.args[5]
    assert forwarded_file_paths == ["/fake/path/image.jpg"]


async def test_process_text_only_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """text message → no media handling, content unchanged (regression guard)."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._download_dingtalk_file = AsyncMock()
    ch._on_message = AsyncMock()

    fake_msg = SimpleNamespace(
        text=SimpleNamespace(content="hello bot"),
        extensions={},
        message_type="text",
        image_content=None,
        rich_text_content=None,
        sender_staff_id="alice",
        sender_id="alice",
        sender_nick="Alice",
    )
    _patch_chatbot_msg(monkeypatch, fake_msg)

    handler = DingTalkCallbackHandler(ch)
    await handler.process(_fake_callback_message({}))

    while ch._background_tasks:
        task = next(iter(ch._background_tasks))
        await task

    ch._download_dingtalk_file.assert_not_called()
    ch._on_message.assert_awaited_once()
    forwarded_content = ch._on_message.await_args.args[0]
    assert forwarded_content == "hello bot"
    assert "Received files:" not in forwarded_content
    forwarded_file_paths = ch._on_message.await_args.args[5]
    assert not forwarded_file_paths


async def test_process_disallowed_sender_skips_download_and_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Denied sender is rejected in process() before file download + dispatch —
    not merely dropped at the central intake."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch.config.allow_from = []  # deny all
    ch._download_dingtalk_file = AsyncMock(return_value="/fake/x.jpg")
    ch._on_message = AsyncMock()

    fake_msg = SimpleNamespace(
        text=None,
        extensions={},
        message_type="picture",
        image_content=SimpleNamespace(download_code="dc-pic"),
        sender_staff_id="alice",
        sender_id="alice",
        sender_nick="Alice",
    )
    _patch_chatbot_msg(monkeypatch, fake_msg)

    handler = DingTalkCallbackHandler(ch)
    await handler.process(_fake_callback_message({}))

    while ch._background_tasks:
        task = next(iter(ch._background_tasks))
        await task

    ch._download_dingtalk_file.assert_not_called()  # no download for a denied sender
    ch._on_message.assert_not_called()


async def test_process_file_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """file message → downloads + content has [File] + footer with original filename path."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._download_dingtalk_file = AsyncMock(return_value="/fake/path/report.pdf")
    ch._on_message = AsyncMock()

    fake_msg = SimpleNamespace(
        text=None,
        extensions={},
        message_type="file",
        sender_staff_id="bob",
        sender_id="bob",
        sender_nick="Bob",
    )
    _patch_chatbot_msg(monkeypatch, fake_msg)

    data = {
        "content": {"downloadCode": "dc-file", "fileName": "report.pdf"},
    }
    handler = DingTalkCallbackHandler(ch)
    await handler.process(_fake_callback_message(data))

    while ch._background_tasks:
        task = next(iter(ch._background_tasks))
        await task

    ch._download_dingtalk_file.assert_awaited_once_with("dc-file", "report.pdf", "bob")
    forwarded_content = ch._on_message.await_args.args[0]
    assert "[File]" in forwarded_content
    assert "/fake/path/report.pdf" in forwarded_content
    forwarded_file_paths = ch._on_message.await_args.args[5]
    assert forwarded_file_paths == ["/fake/path/report.pdf"]


async def test_process_empty_message_acked_but_not_dispatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A message that parses to no text and no media is acked OK but never
    forwarded to the bus (guard for the empty/unsupported branch)."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._download_dingtalk_file = AsyncMock()
    ch._on_message = AsyncMock()

    fake_msg = SimpleNamespace(
        text=SimpleNamespace(content=""),
        extensions={},
        message_type="text",
        image_content=None,
        rich_text_content=None,
        sender_staff_id="alice",
        sender_id="alice",
        sender_nick="Alice",
    )
    _patch_chatbot_msg(monkeypatch, fake_msg)

    handler = DingTalkCallbackHandler(ch)
    status, _ = await handler.process(_fake_callback_message({}))

    assert status == AckMessage.STATUS_OK
    ch._on_message.assert_not_called()


# ---------------------------------------------------------------------------
# DingTalkAPI.fetch_remote — outbound SSRF
# ---------------------------------------------------------------------------


async def test_outbound_blocks_private_ip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer 1 blocks http://10.x at the very first hop, no HTTP call made."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api._http = MagicMock()
    ch._api._http.get = AsyncMock()

    data, ct = await ch._api.fetch_remote("http://10.0.0.1/img.jpg")
    assert data is None
    assert ct is None
    ch._api._http.get.assert_not_called()


async def test_outbound_allows_public_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Public URL with public DNS resolution → bytes returned."""

    def fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    ch = _make_channel(tmp_path, monkeypatch)
    ch._api._http = MagicMock()
    ch._api._http.get = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            headers={"content-type": "image/png"},
            content=b"\x89PNG" + b"\x00" * 100,
        )
    )

    data, ct = await ch._api.fetch_remote("https://example.com/img.png")
    assert data is not None
    assert data.startswith(b"\x89PNG")
    assert ct == "image/png"


async def test_outbound_redirect_revalidates_each_hop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hop 1 = public OK; Hop 2 = redirect to 10.0.0.1 → Layer 1 blocks at hop 2."""

    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    ch = _make_channel(tmp_path, monkeypatch)
    ch._api._http = MagicMock()
    ch._api._http.get = AsyncMock(
        return_value=SimpleNamespace(
            status_code=302,
            headers={"location": "http://10.0.0.1/internal"},
            content=b"",
        )
    )

    data, _ = await ch._api.fetch_remote("https://shortener.example/abc")
    assert data is None
    ch._api._http.get.assert_awaited_once()


async def test_on_message_threads_file_paths_to_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_on_message passes file_paths as media= to intake.publish (multimodal hand-off)."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch.intake.publish = AsyncMock()

    await ch._on_message(
        "[Image]\n\nReceived files:\n- /tmp/a.jpg",
        sender_id="alice",
        sender_name="Alice",
        conversation_type="1",
        conversation_id="alice",
        file_paths=["/tmp/a.jpg"],
    )

    ch.intake.publish.assert_awaited_once()
    kw = ch.intake.publish.await_args.kwargs
    assert kw["media"] == ["/tmp/a.jpg"]
    assert kw["chat_id"] == "alice"


async def test_on_message_empty_file_paths_passes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty / missing file_paths → media=None (regression guard)."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch.intake.publish = AsyncMock()

    await ch._on_message(
        "hello",
        sender_id="alice",
        sender_name="Alice",
    )

    kw = ch.intake.publish.await_args.kwargs
    assert kw["media"] is None


async def test_outbound_too_many_redirects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Chained redirects exceed MAX_REDIRECTS → bail."""

    def fake_getaddrinfo(*_args, **_kwargs):
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    ch = _make_channel(tmp_path, monkeypatch)
    ch._api._http = MagicMock()
    ch._api._http.get = AsyncMock(
        return_value=SimpleNamespace(
            status_code=302,
            headers={"location": "https://example.com/next"},
            content=b"",
        )
    )

    data, _ = await ch._api.fetch_remote("https://example.com/start")
    assert data is None
    assert ch._api._http.get.await_count == MAX_REDIRECTS + 1


# ---------------------------------------------------------------------------
# parsing.py — pure helpers (no I/O)
# ---------------------------------------------------------------------------


def _msg(**attrs):
    attrs.setdefault("text", None)
    attrs.setdefault("extensions", {})
    attrs.setdefault("message_type", "text")
    attrs.setdefault("sender_staff_id", "staff1")
    attrs.setdefault("sender_id", "user1")
    attrs.setdefault("sender_nick", "Alice")
    return SimpleNamespace(**attrs)


def test_is_http_url():
    assert p.is_http_url("https://x/y.png") is True
    assert p.is_http_url("http://x") is True
    assert p.is_http_url("/local/path") is False
    assert p.is_http_url("file:///x") is False


def test_guess_upload_type():
    assert p.guess_upload_type("a.PNG") == "image"
    assert p.guess_upload_type("a.mp3") == "voice"
    assert p.guess_upload_type("a.mp4") == "video"
    assert p.guess_upload_type("a.txt") == "file"
    assert p.guess_upload_type("https://h/p/a.jpg?x=1") == "image"


def test_guess_filename():
    assert p.guess_filename("https://h/path/doc.pdf", "file") == "doc.pdf"
    assert p.guess_filename("https://h/", "image") == "image.jpg"
    assert p.guess_filename("https://h/", "voice") == "audio.amr"
    assert p.guess_filename("https://h/", "file") == "file.bin"


def test_parse_inbound_text():
    parsed = p.parse_inbound(_msg(text=SimpleNamespace(content="  hi  ")), {"conversationType": "1"})
    assert parsed.text == "hi"
    assert parsed.media == []
    assert (parsed.sender_id, parsed.sender_uid, parsed.sender_name) == ("staff1", "staff1", "Alice")
    assert parsed.conversation_type == "1"


def test_parse_inbound_recognition_fallback():
    parsed = p.parse_inbound(_msg(extensions={"content": {"recognition": "  voice words "}}), {})
    assert parsed.text == "voice words"


def test_parse_inbound_raw_text_fallback():
    parsed = p.parse_inbound(_msg(), {"text": {"content": "raw body"}})
    assert parsed.text == "raw body"


def test_parse_inbound_picture():
    parsed = p.parse_inbound(_msg(message_type="picture", image_content=SimpleNamespace(download_code="dc1")), {})
    assert parsed.media == [p.MediaRequest("dc1", "image.jpg", "[Image]")]


def test_parse_inbound_file():
    parsed = p.parse_inbound(
        _msg(message_type="file"),
        {"content": {"downloadCode": "dc2", "fileName": "report.pdf"}},
    )
    assert parsed.media == [p.MediaRequest("dc2", "report.pdf", "[File]")]


def test_parse_inbound_file_top_level_fallback():
    parsed = p.parse_inbound(_msg(message_type="file"), {"downloadCode": "dc3"})
    assert parsed.media == [p.MediaRequest("dc3", "file", "[File]")]


def test_parse_inbound_rich_text():
    rich = SimpleNamespace(
        rich_text_list=[
            {"type": "text", "text": "hello"},
            {"downloadCode": "dc4", "fileName": "a.png"},
            {"type": "text", "text": "world"},
            "not-a-dict",
        ]
    )
    parsed = p.parse_inbound(_msg(message_type="richText", rich_text_content=rich), {})
    assert parsed.text == "hello world"
    assert parsed.media == [p.MediaRequest("dc4", "a.png", "[File]")]


def test_parse_inbound_sender_fallbacks():
    parsed = p.parse_inbound(_msg(sender_staff_id=None), {})
    assert parsed.sender_id == "user1" and parsed.sender_uid == "user1"
    parsed2 = p.parse_inbound(_msg(sender_staff_id=None, sender_id=None, sender_nick=None), {})
    assert parsed2.sender_id is None and parsed2.sender_uid == "unknown"
    assert parsed2.sender_name == "Unknown"


def test_resolve_chat_id():
    assert p.resolve_chat_id("2", "conv1", "s1") == "group:conv1"
    assert p.resolve_chat_id("1", None, "s1") == "s1"
    assert p.resolve_chat_id("2", None, "s1") == "s1"  # group flag but no conv id


def test_append_files_footer():
    assert p.append_files_footer("hi", ["/a", "/b"]) == "hi\n\nReceived files:\n- /a\n- /b"
    assert p.append_files_footer("hi", []) == "hi"


# ---------------------------------------------------------------------------
# DingTalkAPI.access_token / send / upload_media — transport logic
# ---------------------------------------------------------------------------


def _resp(status=200, payload=None, text="", json_ct=True):
    return SimpleNamespace(
        status_code=status,
        text=text,
        headers={"content-type": "application/json"} if json_ct else {},
        json=lambda: payload if payload is not None else {},
    )


async def test_api_access_token_caches(tmp_path, monkeypatch):
    """First call fetches + caches; second call reuses without a second POST."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api._http = MagicMock()
    ch._api._http.post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"accessToken": "tok", "expireIn": 7200},
        )
    )
    assert await ch._api.access_token() == "tok"
    assert await ch._api.access_token() == "tok"
    ch._api._http.post.assert_awaited_once()


async def test_api_send_private_payload(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(return_value=_resp(payload={"errcode": 0}))

    assert await api.send("u1", "sampleMarkdown", {"text": "hi"}) is True
    call = api._http.post.call_args
    assert call.args[0].endswith("oToMessages/batchSend")
    assert call.kwargs["json"]["userIds"] == ["u1"]
    assert call.kwargs["json"]["msgKey"] == "sampleMarkdown"
    assert call.kwargs["headers"]["x-acs-dingtalk-access-token"] == "t1"


async def test_api_send_group_payload(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(return_value=_resp(payload={"errcode": 0}))

    assert await api.send("group:g1", "sampleMarkdown", {"text": "hi"}) is True
    call = api._http.post.call_args
    assert call.args[0].endswith("groupMessages/send")
    assert call.kwargs["json"]["openConversationId"] == "g1"
    assert "userIds" not in call.kwargs["json"]


async def test_api_send_errcode_fails(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(return_value=_resp(payload={"errcode": 400013}))
    assert await api.send("u1", "sampleMarkdown", {"text": "x"}) is False


async def test_api_upload_media_parses_id(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(return_value=_resp(payload={"errcode": 0, "media_id": "m1"}))
    assert await api.upload_media("image", "a.png", b"x", "image/png") == "m1"


async def test_api_upload_media_nested_id(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(return_value=_resp(payload={"errcode": 0, "result": {"mediaId": "m2"}}))
    assert await api.upload_media("file", "a.bin", b"x", "application/octet-stream") == "m2"


async def test_api_upload_media_errcode_none(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    api = _api_with_token(ch)
    api._http.post = AsyncMock(return_value=_resp(payload={"errcode": 40004}))
    assert await api.upload_media("image", "a.png", b"x", "image/png") is None


# ---------------------------------------------------------------------------
# channel outbound orchestration
# ---------------------------------------------------------------------------


async def test_read_media_bytes_local_file(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello")
    data, name, _ct = await ch._read_media_bytes(str(f))
    assert data == b"hello" and name == "doc.txt"


async def test_read_media_bytes_missing(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    assert await ch._read_media_bytes(str(tmp_path / "nope.txt")) == (None, None, None)


async def test_send_media_ref_http_image_shortcut(tmp_path, monkeypatch):
    """A public image URL is sent by reference — no read/upload."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api.send = AsyncMock(return_value=True)
    ch._read_media_bytes = AsyncMock()

    assert await ch._send_media_ref("c1", "https://h/a.jpg") is True
    ch._read_media_bytes.assert_not_called()
    call = ch._api.send.call_args
    assert call.args[1] == "sampleImageMsg"
    assert call.args[2] == {"photoURL": "https://h/a.jpg"}


async def test_send_media_ref_uploads_file(tmp_path, monkeypatch):
    """A local file is read, uploaded, then sent as sampleFile."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._read_media_bytes = AsyncMock(return_value=(b"x", "report.pdf", "application/pdf"))
    ch._api.upload_media = AsyncMock(return_value="mid1")
    ch._api.send = AsyncMock(return_value=True)

    assert await ch._send_media_ref("c1", "/tmp/report.pdf") is True
    ch._api.upload_media.assert_awaited_once()
    call = ch._api.send.call_args
    assert call.args[1] == "sampleFile"
    assert call.args[2] == {"mediaId": "mid1", "fileName": "report.pdf", "fileType": "pdf"}


async def test_send_skips_without_token(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api.access_token = AsyncMock(return_value=None)
    ch._reply_markdown = AsyncMock()
    ch._send_media_ref = AsyncMock()

    await ch.send("c1", "hi")
    ch._reply_markdown.assert_not_called()
    ch._send_media_ref.assert_not_called()


async def test_send_dispatches_text_then_media(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api.access_token = AsyncMock(return_value="t1")
    ch._reply_markdown = AsyncMock(return_value=True)
    ch._send_media_ref = AsyncMock(return_value=True)

    await ch.send("c1", "hi", media=["/tmp/a.jpg"])
    ch._reply_markdown.assert_awaited_once_with("c1", "hi")
    ch._send_media_ref.assert_awaited_once_with("c1", "/tmp/a.jpg")


async def test_send_media_failure_replies_with_marker(tmp_path, monkeypatch):
    """When a media ref fails to send, the user gets a visible failure marker
    instead of the attachment dropping silently."""
    ch = _make_channel(tmp_path, monkeypatch)
    ch._api.access_token = AsyncMock(return_value="t1")
    ch._reply_markdown = AsyncMock(return_value=True)
    ch._send_media_ref = AsyncMock(return_value=False)

    await ch.send("c1", "", media=["/tmp/report.pdf"])
    ch._send_media_ref.assert_awaited_once_with("c1", "/tmp/report.pdf")
    ch._reply_markdown.assert_awaited_once()
    assert "Attachment send failed" in ch._reply_markdown.await_args.args[1]


# ── contract conformance ───────────────────────────────────────────────


def test_dingtalk_satisfies_channel_contract() -> None:
    from raven.channels import Channel
    from raven.channels.contract import capability_violations

    ch = DingTalkChannel(DingTalkConfig(enabled=True, client_id="ak", client_secret="sk"))
    assert isinstance(ch, Channel)  # name/capabilities/start/stop/send
    assert capability_violations(ch) == []  # no login/streaming declared or implemented


def test_dingtalk_spec_import_is_cheap() -> None:
    """Importing dingtalk.spec must NOT pull in dingtalk_stream (the heavy import
    is deferred into SPEC.factory)."""
    import subprocess
    import sys

    code = (
        "import sys, raven.channels.adapters.dingtalk.spec as s;"
        "assert 'dingtalk_stream' not in sys.modules, 'spec import pulled in dingtalk_stream';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'DingTalk'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
