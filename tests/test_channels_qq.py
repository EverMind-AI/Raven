"""Tests for the qq adapter package.

parsing.py — pure route/content resolution from a botpy message.
channel.py — inbound dedup/dispatch and SDK send routing.

Real botpy WebSocket connection / API are live flows left to integration/manual
testing.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

pytest.importorskip("botpy")

from raven.channels.adapters.qq import channel as qq_channel
from raven.channels.adapters.qq import parsing as qp
from raven.channels.adapters.qq.channel import QQChannel, _Fetched


def _channel():
    # allow_from mirrors a configured channel: the intake gate is deny-by-default,
    # and _on_message consults it before downloading anything.
    ch = QQChannel(SimpleNamespace(app_id="a", secret="s", allow_from=["*"]))
    ch.intake.publish = AsyncMock()
    return ch


def _group_msg(mid="m1", content="hello"):
    return SimpleNamespace(id=mid, content=content, group_openid="g1", author=SimpleNamespace(member_openid="u1"))


# ── parsing ────────────────────────────────────────────────────────────


def test_clean_content():
    assert qp.clean_content(SimpleNamespace(content="  hi  ")) == "hi"
    assert qp.clean_content(SimpleNamespace(content="")) == ""
    assert qp.clean_content(SimpleNamespace(content=None)) == ""


def test_resolve_route_group():
    assert qp.resolve_route(_group_msg(), is_group=True) == ("g1", "u1", "group")


def test_resolve_route_c2c_by_id():
    data = SimpleNamespace(author=SimpleNamespace(id="u2"))
    assert qp.resolve_route(data, is_group=False) == ("u2", "u2", "c2c")


def test_resolve_route_c2c_user_openid_fallback():
    data = SimpleNamespace(author=SimpleNamespace(user_openid="u3"))
    assert qp.resolve_route(data, is_group=False) == ("u3", "u3", "c2c")


def test_resolve_route_c2c_unknown():
    data = SimpleNamespace(author=SimpleNamespace())
    assert qp.resolve_route(data, is_group=False) == ("unknown", "unknown", "c2c")


def test_resolve_route_guild_dm():
    """A botpy DirectMessage carries guild_id (the DM session id) — replies
    must route through post_dms, not the C2C endpoint."""
    data = SimpleNamespace(guild_id="gld9", author=SimpleNamespace(id="u7"))
    assert qp.resolve_route(data, is_group=False) == ("gld9", "u7", "guild_dm")


# ── channel: inbound ───────────────────────────────────────────────────


def test_on_message_group_dispatch():
    ch = _channel()
    asyncio.run(ch._on_message(_group_msg(), is_group=True))
    kw = ch.intake.publish.await_args.kwargs
    assert (kw["sender_id"], kw["chat_id"], kw["content"]) == ("u1", "g1", "hello")
    assert kw["metadata"] == {"message_id": "m1"}
    assert ch._chat_type_cache["g1"] == "group"


def test_on_message_c2c_dispatch():
    ch = _channel()
    data = SimpleNamespace(id="m2", content="yo", author=SimpleNamespace(id="u2"))
    asyncio.run(ch._on_message(data, is_group=False))
    kw = ch.intake.publish.await_args.kwargs
    assert (kw["sender_id"], kw["chat_id"], kw["content"]) == ("u2", "u2", "yo")
    assert ch._chat_type_cache["u2"] == "c2c"


def test_on_message_dedup():
    ch = _channel()
    asyncio.run(ch._on_message(_group_msg(mid="dup"), is_group=True))
    asyncio.run(ch._on_message(_group_msg(mid="dup"), is_group=True))
    assert ch.intake.publish.await_count == 1


def test_on_message_empty_content_skipped():
    ch = _channel()
    asyncio.run(ch._on_message(_group_msg(content="   "), is_group=True))
    ch.intake.publish.assert_not_awaited()


def _att(url="//example.com/a.png", ctype="image/png", name="a.png", size=1):
    return SimpleNamespace(url=url, content_type=ctype, filename=name, id="a1", size=size)


def test_on_message_image_only_reaches_the_agent():
    """A message carrying just an image used to be dropped for having no text,
    so the agent never learned one had been sent."""
    ch = _channel()
    ch._download_attachment = AsyncMock(return_value=_Fetched("/media/a.png"))
    msg = _group_msg(content="")
    msg.attachments = [_att()]

    asyncio.run(ch._on_message(msg, is_group=True))

    kwargs = ch.intake.publish.await_args.kwargs
    assert kwargs["media"] == ["/media/a.png"]
    assert "[image: /media/a.png]" in kwargs["content"]


def test_on_message_keeps_text_alongside_an_image():
    ch = _channel()
    ch._download_attachment = AsyncMock(return_value=_Fetched("/media/a.png"))
    msg = _group_msg(content="look at this")
    msg.attachments = [_att()]

    asyncio.run(ch._on_message(msg, is_group=True))

    kwargs = ch.intake.publish.await_args.kwargs
    assert kwargs["content"] == "look at this\n[image: /media/a.png]"
    assert kwargs["media"] == ["/media/a.png"]


def test_on_message_reports_a_failed_download_instead_of_dropping_it():
    """An expired url must still tell the agent something arrived."""
    ch = _channel()
    ch._download_attachment = AsyncMock(return_value=_Fetched(None, "download failed"))
    msg = _group_msg(content="")
    msg.attachments = [_att()]

    asyncio.run(ch._on_message(msg, is_group=True))

    kwargs = ch.intake.publish.await_args.kwargs
    assert kwargs["media"] is None
    assert "download failed" in kwargs["content"]


def test_on_message_does_not_download_for_a_denied_sender():
    """The allow-list is checked before the fetch, so a blocked sender cannot
    make the bot pull bytes it will immediately discard."""
    ch = _channel()
    ch.is_allowed = lambda sender_id: False
    ch._download_attachment = AsyncMock(return_value=_Fetched("/media/a.png"))
    msg = _group_msg(content="hi")
    msg.attachments = [_att()]

    asyncio.run(ch._on_message(msg, is_group=True))

    ch._download_attachment.assert_not_awaited()
    ch.intake.publish.assert_not_awaited()


class _Resp:
    """Enough of an httpx response for the download path."""

    def __init__(self, content=b"bytes", error=None):
        self.content = content
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error


class _Http:
    """Stands in for the client `start()` opens and `stop()` closes."""

    def __init__(self, resp=None, error=None):
        self.resp = resp or _Resp()
        self.error = error
        self.urls: list[str] = []

    async def get(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.resp


def _with_media(monkeypatch, saver=None):
    monkeypatch.setattr(
        "raven.channels.adapters.qq.channel.save_media_bytes",
        saver or (lambda channel, data, name: f"/media/{name}"),
    )


def test_download_attachment_adds_the_missing_scheme(monkeypatch):
    """QQ hands back scheme-relative urls, which httpx rejects outright."""
    _with_media(monkeypatch)
    ch = _channel()
    ch._http = _Http()

    out = asyncio.run(ch._download_attachment(_att(url="//gchat.qpic.cn/x.png")))

    assert ch._http.urls == ["https://gchat.qpic.cn/x.png"]
    assert out.path == "/media/a.png"


def test_download_attachment_reuses_one_client(monkeypatch):
    """A client per attachment is a TLS handshake per image; the house pattern
    holds one on the channel."""
    _with_media(monkeypatch)
    ch = _channel()
    ch._http = _Http()

    asyncio.run(ch._download_attachment(_att()))
    asyncio.run(ch._download_attachment(_att()))

    assert len(ch._http.urls) == 2


def test_download_attachment_gives_up_on_a_non_2xx(monkeypatch):
    _with_media(monkeypatch)
    ch = _channel()
    ch._http = _Http(resp=_Resp(error=httpx.HTTPError("404")))

    assert asyncio.run(ch._download_attachment(_att())).path is None


def test_download_attachment_without_a_url_is_skipped(monkeypatch):
    _with_media(monkeypatch)
    ch = _channel()
    ch._http = _Http()

    assert asyncio.run(ch._download_attachment(_att(url=None))).path is None
    assert ch._http.urls == []


def test_a_write_failure_degrades_like_a_fetch_failure(monkeypatch):
    """An OSError from the media dir used to escape this helper and be caught by
    `_on_message`, dropping the whole message, text included."""

    def _boom(channel, data, name):
        raise OSError("No space left on device")

    _with_media(monkeypatch, _boom)
    ch = _channel()
    ch._http = _Http()

    assert asyncio.run(ch._download_attachment(_att())).path is None


def test_an_oversized_attachment_is_refused_before_the_fetch(monkeypatch):
    """`resp.content` buffers the whole body, so the size has to be refused
    rather than measured."""
    _with_media(monkeypatch)
    ch = _channel()
    ch._http = _Http()

    out = asyncio.run(ch._download_attachment(_att(size=21 * 1024 * 1024)))

    assert out.path is None
    assert out.reason == "too large", "a refusal is not a failure; saying so sends the model chasing a network fault"
    assert ch._http.urls == []


def test_download_attachment_without_a_client_is_a_noop(monkeypatch):
    """`stop()` closes the client; a late event must not resurrect one."""
    _with_media(monkeypatch)
    ch = _channel()
    ch._http = None

    assert asyncio.run(ch._download_attachment(_att())).path is None


def test_a_denied_sender_is_still_logged():
    """Returning early skips the rejection Intake.publish used to log, and that
    line is how an operator finds out why the bot is ignoring someone."""
    ch = _channel()
    ch.is_allowed = lambda sender_id: False
    seen: list[str] = []
    with patch.object(qq_channel.logger, "warning", lambda msg, *a: seen.append(msg)):
        asyncio.run(ch._on_message(_group_msg(content="hi"), is_group=True))

    assert any("Access denied for sender" in m for m in seen)


def test_an_oversized_attachment_is_not_reported_as_a_failure():
    """It was refused, not broken. "download failed" sends the model looking for
    a network fault and tells the operator to check a url that is fine."""
    ch = _channel()
    ch._download_attachment = AsyncMock(return_value=_Fetched(None, "too large"))
    msg = _group_msg(content="")
    msg.attachments = [_att(name="clip.mp4")]

    asyncio.run(ch._on_message(msg, is_group=True))

    content = ch.intake.publish.await_args.kwargs["content"]
    assert "clip.mp4 - too large" in content
    assert "download failed" not in content


def test_a_failed_download_keeps_the_filename():
    """`[image: download failed]` says something broke but not what."""
    ch = _channel()
    ch._download_attachment = AsyncMock(return_value=_Fetched(None, "download failed"))
    msg = _group_msg(content="")
    msg.attachments = [_att(name="photo.png")]

    asyncio.run(ch._on_message(msg, is_group=True))

    assert "photo.png - download failed" in ch.intake.publish.await_args.kwargs["content"]


# ── channel: outbound ──────────────────────────────────────────────────


def _client():
    client = MagicMock()
    client.api.post_group_message = AsyncMock()
    client.api.post_c2c_message = AsyncMock()
    return client


def test_send_group_routes_to_group_api():
    ch = _channel()
    ch._client = _client()
    ch._chat_type_cache["g1"] = "group"
    asyncio.run(ch.send("g1", "reply"))
    ch._client.api.post_group_message.assert_awaited_once()
    ch._client.api.post_c2c_message.assert_not_called()
    kw = ch._client.api.post_group_message.await_args.kwargs
    assert kw["group_openid"] == "g1" and kw["markdown"] == {"content": "reply"}
    assert kw["msg_id"] is None


def test_send_c2c_default_route():
    ch = _channel()
    ch._client = _client()
    asyncio.run(ch.send("u2", "hi"))
    ch._client.api.post_c2c_message.assert_awaited_once()
    kw = ch._client.api.post_c2c_message.await_args.kwargs
    assert kw["openid"] == "u2"
    assert kw["msg_id"] is None


def test_send_increments_msg_seq():
    ch = _channel()
    ch._client = _client()
    before = ch._msg_seq
    asyncio.run(ch.send("u2", "a"))
    asyncio.run(ch.send("u2", "b"))
    assert ch._client.api.post_c2c_message.await_args_list[0].kwargs["msg_seq"] == before + 1
    assert ch._client.api.post_c2c_message.await_args_list[1].kwargs["msg_seq"] == before + 2


def test_send_guild_dm_routes_to_post_dms():
    ch = _channel()
    ch._client = _client()
    ch._client.api.post_dms = AsyncMock()
    dm = SimpleNamespace(id="m3", content="hi bot", guild_id="gld9", author=SimpleNamespace(id="u7"))
    asyncio.run(ch._on_message(dm, is_group=False))
    assert ch._chat_type_cache["gld9"] == "guild_dm"

    asyncio.run(ch.send("gld9", "reply"))
    ch._client.api.post_dms.assert_awaited_once_with(guild_id="gld9", content="reply", msg_id=None)
    ch._client.api.post_c2c_message.assert_not_called()
    ch._client.api.post_group_message.assert_not_called()


def test_send_media_routes_through_c2c():
    ch = _channel()
    ch._client = _client()
    asyncio.run(ch.send("u2", "", media=["/tmp/pic.png"]))
    ch._client.api.post_c2c_message.assert_awaited_once()
    kw = ch._client.api.post_c2c_message.await_args.kwargs
    assert kw["openid"] == "u2" and kw["markdown"] == {"content": ""}


def test_send_no_client_is_noop():
    ch = _channel()
    ch._client = None
    asyncio.run(ch.send("u2", "x"))  # must not raise


def test_send_reraises_transient_for_manager_retry():
    """5xx / network errors propagate so manager._send_with_retry can back off;
    other errors stay swallowed (see test_send_swallows_api_error)."""
    import pytest
    from botpy.errors import ServerError

    ch = _channel()
    ch._client = _client()
    ch._client.api.post_c2c_message = AsyncMock(side_effect=ServerError("502"))
    with pytest.raises(ServerError):
        asyncio.run(ch.send("u2", "x"))


def test_send_swallows_api_error():
    ch = _channel()
    ch._client = _client()
    ch._client.api.post_c2c_message = AsyncMock(side_effect=RuntimeError("boom"))
    asyncio.run(ch.send("u2", "x"))  # must not raise


# ── contract conformance ───────────────────────────────────────────────


def test_qq_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations

    ch = QQChannel(SimpleNamespace(app_id="a", secret="s"))
    assert isinstance(ch, Channel)  # name/capabilities/start/stop/send
    assert capability_violations(ch) == []  # no login/streaming declared or implemented


def test_qq_spec_import_is_cheap():
    """Importing qq.spec must NOT pull in the botpy SDK (the heavy import is
    deferred into SPEC.factory)."""
    import subprocess
    import sys

    code = (
        "import sys, raven.channels.adapters.qq.spec as s;"
        "assert 'botpy' not in sys.modules, 'spec import pulled in the botpy SDK';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'QQ'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
