"""Tests for the qq adapter package.

parsing.py — pure route/content resolution from a botpy message.
channel.py — inbound dedup/dispatch and SDK send routing.

Real botpy WebSocket connection / API are live flows left to integration/manual
testing.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from raven.channels.adapters.qq import parsing as qp
from raven.channels.adapters.qq.channel import QQChannel


def _channel():
    ch = QQChannel(SimpleNamespace(app_id="a", secret="s"))
    ch.intake.publish = AsyncMock()
    return ch


def _group_msg(mid="m1", content="hello"):
    return SimpleNamespace(id=mid, content=content, group_openid="g1",
                           author=SimpleNamespace(member_openid="u1"))


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
    assert isinstance(ch, Channel)              # name/capabilities/start/stop/send
    assert capability_violations(ch) == []      # no login/streaming declared or implemented


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
