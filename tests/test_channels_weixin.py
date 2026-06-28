"""Tests for the weixin adapter package — AES media crypto round-trip,
iLink protocol helpers, and inbound item parsing. Pure surface; no live
iLink connection / WeChat account required."""

import asyncio
import base64
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.channels.adapters.weixin import crypto
from raven.channels.adapters.weixin import protocol as p
from raven.channels.adapters.weixin.channel import WeixinChannel
from raven.config.schema import WeixinConfig


def _channel():
    ch = WeixinChannel(WeixinConfig())
    ch.config.allow_from = ["*"]
    ch.intake.set_submit(AsyncMock())
    return ch

_KEY16 = b"0123456789abcdef"
_KEY_RAW_B64 = base64.b64encode(_KEY16).decode()           # base64 of 16 raw bytes
_KEY_HEX_B64 = base64.b64encode(_KEY16.hex().encode()).decode()  # base64 of 32 hex chars


# ── crypto ────────────────────────────────────────────────────────────


def test_parse_aes_key_raw_and_hex_flavours():
    assert crypto.parse_aes_key(_KEY_RAW_B64) == _KEY16
    assert crypto.parse_aes_key(_KEY_HEX_B64) == _KEY16


def test_encrypt_decrypt_roundtrip_raw_key():
    enc = crypto.encrypt(b"hello world", _KEY_RAW_B64)
    assert enc != b"hello world"
    assert crypto.decrypt(enc, _KEY_RAW_B64) == b"hello world"


def test_encrypt_decrypt_roundtrip_hex_key():
    enc = crypto.encrypt(b"some longer payload here", _KEY_HEX_B64)
    assert crypto.decrypt(enc, _KEY_HEX_B64) == b"some longer payload here"


def test_unpad_pkcs7_valid_and_invalid():
    assert crypto.unpad_pkcs7(b"abcdefghijk" + bytes([5]) * 5) == b"abcdefghijk"
    assert crypto.unpad_pkcs7(b"abc") == b"abc"  # not block-aligned -> returned as-is


def test_parse_aes_key_rejects_bad_length():
    import pytest
    with pytest.raises(ValueError):
        crypto.parse_aes_key(base64.b64encode(b"short").decode())


def test_encrypt_raises_on_bad_key():
    """Upload-side encryption must fail loudly, never upload plaintext while
    advertising an AES key (silently corrupted media for the receiver)."""
    import pytest
    with pytest.raises(ValueError):
        crypto.encrypt(b"data", base64.b64encode(b"short").decode())


def test_encrypt_raises_without_backend(monkeypatch):
    import pytest
    monkeypatch.setattr(crypto, "_run_ecb", lambda *a, **k: None)
    key = base64.b64encode(b"0123456789abcdef").decode()
    with pytest.raises(RuntimeError):
        crypto.encrypt(b"data", key)


def test_decrypt_keeps_lenient_fallback(monkeypatch):
    """Download side intentionally stays lenient: raw bytes are still the best
    available result when the backend is missing."""
    monkeypatch.setattr(crypto, "_run_ecb", lambda *a, **k: None)
    key = base64.b64encode(b"0123456789abcdef").decode()
    assert crypto.decrypt(b"data", key) == b"data"


# ── protocol ──────────────────────────────────────────────────────────


def test_build_client_version():
    assert p.build_client_version("2.1.1") == (2 << 16) | (1 << 8) | 1
    assert p.build_client_version("3") == (3 << 16)


def test_ext_for_type():
    assert p.ext_for_type("image") == ".jpg"
    assert p.ext_for_type("voice") == ".silk"
    assert p.ext_for_type("file") == ""
    assert p.ext_for_type("unknown") == ""


def test_has_downloadable_media_locator():
    assert p.has_downloadable_media_locator({"full_url": "http://x"}) is True
    assert p.has_downloadable_media_locator({"encrypt_query_param": "q"}) is True
    assert p.has_downloadable_media_locator({}) is False
    assert p.has_downloadable_media_locator(None) is False


def test_build_headers():
    h = p.build_headers("tok", "rt")
    assert h["Authorization"] == "Bearer tok"
    assert h["SKRouteTag"] == "rt"
    assert "X-WECHAT-UIN" in h
    assert "Authorization" not in p.build_headers("")  # no token -> no bearer


# ── inbound item parsing (pure) ───────────────────────────────────────


def test_render_text_plain():
    assert WeixinChannel._render_text_item({"text_item": {"text": "hi"}}) == ["hi"]


def test_render_text_quoted_media_is_just_reply():
    item = {"text_item": {"text": "reply"}, "ref_msg": {"message_item": {"type": p.ITEM_IMAGE}}}
    assert WeixinChannel._render_text_item(item) == ["reply"]


def test_render_text_quoted_text_includes_quote():
    item = {
        "text_item": {"text": "reply"},
        "ref_msg": {"title": "T", "message_item": {"type": p.ITEM_TEXT, "text_item": {"text": "orig"}}},
    }
    out = WeixinChannel._render_text_item(item)[0]
    assert "引用" in out and "reply" in out and "orig" in out


def test_typed_item():
    assert WeixinChannel._typed_item({"image_item": {"k": 1}}, p.ITEM_IMAGE) == {"k": 1}


def test_first_quoted_media():
    items = [{"type": p.ITEM_TEXT, "ref_msg": {"message_item": {"type": p.ITEM_FILE, "file_item": {"file_name": "a"}}}}]
    kind, typed = WeixinChannel._first_quoted_media(items)
    assert kind == p.ITEM_FILE
    assert typed == {"file_name": "a"}


def test_first_quoted_media_none():
    assert WeixinChannel._first_quoted_media([{"type": p.ITEM_TEXT}]) is None


# ── session pause ─────────────────────────────────────────────────────


def test_session_pause_blocks_then_clears():
    ch = _channel()
    ch._session_pause_until = time.time() + 120
    assert ch._session_remaining_s() > 0
    with pytest.raises(RuntimeError):
        ch._assert_session_active()
    ch._session_pause_until = 0.0
    assert ch._session_remaining_s() == 0
    ch._assert_session_active()  # no raise once cleared


# ── outbound message envelope ─────────────────────────────────────────


def test_bot_msg_shape():
    ch = _channel()
    m = ch._bot_msg("u1", "ctok", [{"type": p.ITEM_TEXT, "text_item": {"text": "hi"}}])
    assert m["to_user_id"] == "u1"
    assert m["from_user_id"] == ""
    assert m["message_type"] == p.MESSAGE_TYPE_BOT
    assert m["message_state"] == p.MESSAGE_STATE_FINISH
    assert m["client_id"].startswith("raven-")
    assert m["item_list"][0]["text_item"]["text"] == "hi"
    assert m["context_token"] == "ctok"


def test_bot_msg_omits_empty_fields():
    m = _channel()._bot_msg("u1", "")
    assert "item_list" not in m
    assert "context_token" not in m


# ── outbound send (new contract: chat_id, content, media) ─────────────


def _send_ch():
    ch = _channel()
    ch._client = object()
    ch._token = "tok"
    ch._context_tokens = {"u1": "ctok"}
    ch._typing.start = AsyncMock()
    ch._typing.stop = AsyncMock()
    ch._stop_typing = AsyncMock()
    ch._send_text = AsyncMock()
    ch._send_one_media = AsyncMock()
    return ch


def test_send_text_reaches_send_text_with_chat_id_and_content():
    ch = _send_ch()
    asyncio.run(ch.send("u1", "hello there"))
    ch._send_text.assert_awaited_once_with("u1", "hello there", "ctok")
    ch._send_one_media.assert_not_called()


def test_send_media_reaches_send_one_media():
    ch = _send_ch()
    asyncio.run(ch.send("u1", "", media=["/media/x.jpg"]))
    ch._send_one_media.assert_awaited_once_with("u1", "/media/x.jpg", "ctok")
    ch._send_text.assert_not_called()  # empty content -> no text part


def test_send_carry_nothing_always_clears_remote_typing():
    # No metadata/_progress anymore: every send stops typing up-front and clears
    # the remote indicator in the finally (clear_remote=True unconditionally).
    ch = _send_ch()
    asyncio.run(ch.send("u1", "hi"))
    ch._stop_typing.assert_awaited_once_with("u1", clear_remote=True)
    ch._typing.stop.assert_awaited_once_with("u1", clear_remote=True)


def test_send_raises_when_context_token_missing():
    ch = _send_ch()
    ch._context_tokens = {}
    with pytest.raises(RuntimeError):
        asyncio.run(ch.send("u1", "hi"))


# ── _process_message gating + dedup (text path, no network) ───────────


def test_process_skips_bot_message():
    ch = _channel()
    asyncio.run(ch._process_message({"message_type": p.MESSAGE_TYPE_BOT, "from_user_id": "u1"}))
    ch.intake._submit.assert_not_called()


def test_process_denies_disallowed_sender():
    ch = _channel()
    ch.config.allow_from = ["only"]
    msg = {"message_type": p.MESSAGE_TYPE_USER, "from_user_id": "other", "message_id": "m1",
           "item_list": [{"type": p.ITEM_TEXT, "text_item": {"text": "hi"}}]}
    asyncio.run(ch._process_message(msg))
    ch.intake._submit.assert_not_called()


def test_process_dedup_text_message():
    ch = _channel()
    ch._save_state = lambda: None  # avoid disk write on context_token cache
    msg = {"message_type": p.MESSAGE_TYPE_USER, "from_user_id": "u1", "message_id": "m1",
           "context_token": "c1",
           "item_list": [{"type": p.ITEM_TEXT, "text_item": {"text": "hi"}}]}
    asyncio.run(ch._process_message(msg))
    asyncio.run(ch._process_message(msg))  # same message_id -> deduped
    assert ch.intake._submit.await_count == 1


# ── state persistence round-trip ──────────────────────────────────────


def test_state_save_load_roundtrip(tmp_path):
    ch = _channel()
    ch._dir = lambda: tmp_path
    ch._token = "tok"
    ch._updates_buf = "buf"
    ch._context_tokens = {"u": "c"}
    ch._save_state()

    ch2 = _channel()
    ch2._dir = lambda: tmp_path
    assert ch2._load_state() is True
    assert ch2._token == "tok"
    assert ch2._updates_buf == "buf"
    assert ch2._context_tokens == {"u": "c"}


def test_load_state_missing_file():
    ch = _channel()
    ch._dir = lambda: __import__("pathlib").Path("/nonexistent/raven-test-dir")
    assert ch._load_state() is False


def test_authenticate_with_config_token_still_loads_state(tmp_path):
    """A configured token must not skip _load_state — the get_updates cursor
    and per-chat context_tokens have to survive a restart (the old bug lost
    them, making send() raise until each chat spoke again)."""
    seed = _channel()
    seed._dir = lambda: tmp_path
    seed._token = "persisted"
    seed._updates_buf = "buf9"
    seed._context_tokens = {"u": "c"}
    seed._save_state()

    ch = _channel()
    ch.config.token = "cfg-token"
    ch._dir = lambda: tmp_path
    assert asyncio.run(ch._authenticate()) is True
    assert ch._token == "cfg-token"            # configured token wins
    assert ch._updates_buf == "buf9"           # persisted cursor survives
    assert ch._context_tokens == {"u": "c"}    # reply context survives


def test_authenticate_falls_back_to_qr(monkeypatch):
    ch = _channel()
    ch.config.token = ""
    ch._dir = lambda: __import__("pathlib").Path("/nonexistent/raven-test-dir")
    ch._qr_login = AsyncMock(return_value=True)
    assert asyncio.run(ch._authenticate()) is True
    ch._qr_login.assert_awaited_once()


# ── media item rendering (download mocked) ────────────────────────────


def test_render_media_voice_uses_server_transcription():
    ch = _channel()
    parts, media = [], []
    asyncio.run(ch._render_media_item({"text": "transcribed words"}, p.ITEM_VOICE, parts, media))
    assert parts == ["[voice] transcribed words"]
    assert media == []  # no download when server already transcribed


def test_render_media_image_downloaded():
    ch = _channel()

    async def fake_download(_typed, _media_type, _filename=None):
        return "/media/x.jpg"

    ch._download_media = fake_download
    parts, media = [], []
    asyncio.run(ch._render_media_item({"media": {"full_url": "u"}}, p.ITEM_IMAGE, parts, media))
    assert media == ["/media/x.jpg"]
    assert any("source: /media/x.jpg" in part for part in parts)


def test_render_media_image_download_failed():
    ch = _channel()

    async def fake_download(*_a, **_k):
        return None

    ch._download_media = fake_download
    parts, media = [], []
    asyncio.run(ch._render_media_item({"media": {}}, p.ITEM_IMAGE, parts, media))
    assert media == []
    assert parts == ["[image]"]


def test_download_media_no_locator_returns_none():
    # No full_url and no encrypt_query_param -> bail before any network call.
    assert asyncio.run(_channel()._download_media({"media": {}}, "image")) is None


# ── typing tickets + keepalive (characterization baseline) ────────────
# Pins the previously untested typing subsystem ahead of its extraction:
# ticket TTL reuse, success bookkeeping, exponential backoff with stale-ticket
# serving, keepalive lifecycle, and the clear_remote CANCEL.


def _typing_ch():
    ch = _channel()
    ch._client = object()
    ch._token = "tok"
    ch._post = AsyncMock(return_value={"ret": 0, "typing_ticket": "tk1"})
    return ch


def test_typing_ticket_fetch_then_ttl_reuse():
    ch = _typing_ch()

    async def scenario():
        first = await ch._typing.ticket_for("u1", "ctx")
        second = await ch._typing.ticket_for("u1")   # within TTL -> cached
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second == "tk1"
    ch._post.assert_awaited_once()                   # no second fetch
    entry = ch._typing._tickets["u1"]
    assert entry["ever_succeeded"] is True
    assert entry["retry_delay_s"] == p.CONFIG_CACHE_INITIAL_RETRY_S


def test_typing_ticket_failure_backoff_doubles_and_serves_stale():
    ch = _typing_ch()
    asyncio.run(ch._typing.ticket_for("u1"))         # success -> tk1 cached
    ch._typing._tickets["u1"]["next_fetch_at"] = 0    # expire
    ch._post = AsyncMock(return_value={"ret": 1})    # refresh fails
    assert asyncio.run(ch._typing.ticket_for("u1")) == "tk1"   # stale served
    assert ch._typing._tickets["u1"]["retry_delay_s"] == p.CONFIG_CACHE_INITIAL_RETRY_S * 2
    ch._typing._tickets["u1"]["next_fetch_at"] = 0
    asyncio.run(ch._typing.ticket_for("u1"))
    assert ch._typing._tickets["u1"]["retry_delay_s"] == p.CONFIG_CACHE_INITIAL_RETRY_S * 4


def test_typing_ticket_first_failure_records_empty_entry():
    ch = _typing_ch()
    ch._post = AsyncMock(return_value={"ret": 1})
    assert asyncio.run(ch._typing.ticket_for("u1")) == ""
    entry = ch._typing._tickets["u1"]
    assert entry["ticket"] == "" and entry["ever_succeeded"] is False


def test_start_typing_keepalive_stops_cleanly(monkeypatch):
    monkeypatch.setattr(p, "TYPING_KEEPALIVE_INTERVAL_S", 0)
    ch = _typing_ch()
    sent = []

    async def fake_post(endpoint, body=None, **kw):
        if endpoint.endswith("getconfig"):
            return {"ret": 0, "typing_ticket": "tk1"}
        sent.append(body["status"])
        return {"ret": 0}

    ch._post = fake_post

    async def scenario():
        await ch._start_typing("u1")
        await asyncio.sleep(0.05)                    # keepalive ticks
        await ch._stop_typing("u1", clear_remote=True)
        count = len(sent)
        await asyncio.sleep(0.05)
        assert len(sent) == count                    # nothing after stop

    asyncio.run(scenario())
    assert sent[0] == p.TYPING_STATUS_TYPING
    assert sent[-1] == p.TYPING_STATUS_CANCEL        # clear_remote sends CANCEL
    assert not ch._typing._tasks


def test_stop_typing_without_clear_remote_sends_no_cancel():
    ch = _typing_ch()
    sent = []

    async def fake_post(endpoint, body=None, **kw):
        if endpoint.endswith("getconfig"):
            return {"ret": 0, "typing_ticket": "tk1"}
        sent.append(body["status"])
        return {"ret": 0}

    ch._post = fake_post

    async def scenario():
        await ch._start_typing("u1")
        await ch._stop_typing("u1", clear_remote=False)

    asyncio.run(scenario())
    assert p.TYPING_STATUS_CANCEL not in sent


# ── contract conformance (interactive-login channel) ──────────────────


def test_weixin_satisfies_channel_contract():
    from raven.channels import Channel, SupportsLogin
    from raven.channels.contract import capability_violations
    ch = _channel()
    assert isinstance(ch, Channel)
    assert isinstance(ch, SupportsLogin)             # QR pairing
    assert ch.capabilities.interactive_login is True
    assert capability_violations(ch) == []           # declared interactive_login ↔ implements SupportsLogin


def test_weixin_spec_declares_interactive_login_and_is_cheap():
    """spec.py declares interactive_login (CLI login routing reads it) and its
    import must NOT pull in httpx (deferred into SPEC.factory)."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.weixin.spec as s;"
        "assert 'httpx' not in sys.modules, 'spec import pulled in httpx';"
        "assert s.SPEC.capabilities.interactive_login is True;"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'WeChat'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_download_media_non_image_requires_key():
    # Locator present but no AES key, non-image type -> bail before network.
    assert asyncio.run(_channel()._download_media({"media": {"full_url": "u"}}, "voice")) is None
