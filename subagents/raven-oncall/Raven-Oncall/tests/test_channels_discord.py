"""Tests for raven.channels.adapters.discord — group mention gating and
attachment fetch guards. Pure surface; no live gateway / REST."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from raven.channels.adapters.discord.channel import DiscordChannel


def _channel(group_policy="open", allow_from=("*",)):
    cfg = SimpleNamespace(
        group_policy=group_policy,
        token="t",
        gateway_url="wss://x",
        intents=0,
        allow_from=list(allow_from),
    )
    ch = DiscordChannel(cfg)
    ch.intake.publish = AsyncMock()
    ch._start_typing = AsyncMock()  # avoid spawning the network typing loop
    return ch


# ── group mention gating ──────────────────────────────────────────────


def test_open_policy_responds_to_all():
    assert _channel("open")._addressed_in_group({"mentions": []}, "hi") is True


def test_mention_via_mentions_array():
    ch = _channel("mention")
    ch._bot_user_id = "42"
    assert ch._addressed_in_group({"mentions": [{"id": "42"}]}, "hi") is True


def test_mention_via_content_token():
    ch = _channel("mention")
    ch._bot_user_id = "42"
    assert ch._addressed_in_group({"mentions": []}, "hey <@42> there") is True
    assert ch._addressed_in_group({"mentions": []}, "hey <@!42> there") is True


def test_not_mentioned():
    ch = _channel("mention")
    ch._bot_user_id = "42"
    assert ch._addressed_in_group({"mentions": [{"id": "99"}]}, "hi") is False


def test_mention_policy_no_bot_id_denies():
    ch = _channel("mention")
    ch._bot_user_id = None
    assert ch._addressed_in_group({"mentions": []}, "hi") is False


# ── attachment fetch guards ───────────────────────────────────────────


def test_fetch_attachment_no_url():
    res = asyncio.run(_channel()._fetch_attachment({"filename": "x.txt"}))
    assert res.path is None
    assert "x.txt" in res.text


def test_fetch_attachment_too_large():
    ch = _channel()
    ch._http = MagicMock()  # present so the size guard is reached
    res = asyncio.run(ch._fetch_attachment({"url": "u", "filename": "big.zip", "size": 999 * 1024 * 1024}))
    assert res.path is None
    assert "too large" in res.text


def test_fetch_attachment_downloaded(monkeypatch, tmp_path):
    ch = _channel()
    resp = MagicMock()
    resp.content = b"data"
    resp.raise_for_status = MagicMock()
    ch._http = MagicMock()
    ch._http.get = AsyncMock(return_value=resp)
    import raven.channels.adapters.discord.channel as disc

    saved = tmp_path / "saved.bin"
    monkeypatch.setattr(disc, "save_media_bytes", lambda _ch, _data, _name: saved)

    res = asyncio.run(ch._fetch_attachment({"url": "u", "filename": "f.bin", "size": 10}))
    assert res.path == str(saved)
    assert "attachment" in res.text


def test_fetch_attachment_download_error():
    ch = _channel()
    ch._http = MagicMock()
    ch._http.get = AsyncMock(side_effect=RuntimeError("boom"))
    res = asyncio.run(ch._fetch_attachment({"url": "u", "filename": "f.bin", "size": 10}))
    assert res.path is None
    assert "download failed" in res.text


# ── _on_message gating + dispatch (no network) ────────────────────────


def test_on_message_skips_bot_author():
    ch = _channel()
    asyncio.run(ch._on_message({"author": {"bot": True, "id": "1"}, "channel_id": "c"}))
    ch.intake.publish.assert_not_called()


def test_on_message_denies_disallowed_sender_before_side_effects():
    """The inline gate rejects a denied sender BEFORE the side-effecting work —
    no attachment download, no typing — not merely dropping it at the central
    intake. This pins discord's early-reject; it is NOT redundant with the
    central intake deny test because it asserts the side-effects are skipped."""
    ch = _channel(allow_from=["only"])
    ch._fetch_attachment = AsyncMock()
    payload = {
        "author": {"id": "other"},
        "channel_id": "c",
        "content": "hi",
        "attachments": [{"url": "u", "filename": "f.bin"}],
    }
    asyncio.run(ch._on_message(payload))
    ch.intake.publish.assert_not_called()
    ch._fetch_attachment.assert_not_called()  # denied sender → no download
    ch._start_typing.assert_not_awaited()  # denied sender → no typing


def test_on_message_group_not_mentioned_ignored():
    ch = _channel("mention")
    ch._bot_user_id = "42"
    payload = {"author": {"id": "u"}, "channel_id": "c", "content": "hi", "guild_id": "g", "mentions": []}
    asyncio.run(ch._on_message(payload))
    ch.intake.publish.assert_not_called()


def test_on_message_dm_text_dispatches():
    ch = _channel()
    payload = {"author": {"id": "u"}, "channel_id": "c", "content": "hello", "id": "m1"}
    asyncio.run(ch._on_message(payload))
    ch.intake.publish.assert_awaited_once()


# ── _send_file guards + bytes regression ──────────────────────────────


def test_send_file_not_found():
    assert asyncio.run(_channel()._send_file("u", {}, "/nope/missing.bin")) is False


def test_send_file_reads_bytes_for_retry_safety(tmp_path):
    """Regression: _send_file must hand _post_retry re-sendable bytes, not a
    file handle (which would be at EOF on a 429/error retry)."""
    ch = _channel()
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    captured: dict = {}

    async def fake_post_retry(_url, _headers, **kwargs):
        captured.update(kwargs)
        return True

    ch._post_retry = fake_post_retry
    assert asyncio.run(ch._send_file("http://x", {}, str(f))) is True
    assert captured["files"]["files[0]"][1] == b"hello"  # bytes, not a stream


# ── send orchestration (network mocked) ───────────────────────────────


def test_send_text_posts_content():
    ch = _channel()
    ch._http = MagicMock()
    posts: list = []

    async def fake_post_retry(url, _headers, **kw):
        posts.append((url, kw.get("json")))
        return True

    ch._post_retry = fake_post_retry
    asyncio.run(ch.send("c", "hi"))
    url, body = posts[0]
    assert "/channels/c/messages" in url  # chat_id reaches the REST URL
    assert body["content"] == "hi"


def test_send_text_carries_nothing():
    """Carry-nothing parity: a plain text send posts only {content}, no
    message_reference / allowed_mentions (no threading without metadata)."""
    ch = _channel()
    ch._http = MagicMock()
    posts: list = []

    async def fake_post_retry(_url, _headers, **kw):
        posts.append(kw.get("json"))
        return True

    ch._post_retry = fake_post_retry
    asyncio.run(ch.send("c", "hi"))
    assert posts[0] == {"content": "hi"}


def test_send_media_sends_file():
    ch = _channel()
    ch._http = MagicMock()
    files_calls: list = []

    async def fake_send_file(url, _headers, path):
        files_calls.append((url, path))
        return True

    ch._send_file = fake_send_file
    ch._post_retry = AsyncMock(return_value=True)
    asyncio.run(ch.send("c", "", media=["/x/a.bin"]))
    assert files_calls == [("https://discord.com/api/v10/channels/c/messages", "/x/a.bin")]


def test_send_failed_media_emits_notice():
    ch = _channel()
    ch._http = MagicMock()
    posts: list = []

    async def fail_file(*_a, **_k):
        return False

    async def fake_post_retry(_url, _headers, **kw):
        posts.append(kw.get("json"))
        return True

    ch._send_file = fail_file
    ch._post_retry = fake_post_retry
    asyncio.run(ch.send("c", "", media=["/x/a.bin"]))
    assert any("send failed" in (pp or {}).get("content", "") for pp in posts)


# ── _post_retry (429 / success / exhausted) ───────────────────────────


def test_post_retry_success():
    ch = _channel()
    ch._http = MagicMock()
    resp = MagicMock(status_code=200)
    resp.raise_for_status = MagicMock()
    ch._http.post = AsyncMock(return_value=resp)
    assert asyncio.run(ch._post_retry("u", {})) is True


def test_post_retry_honors_429_then_succeeds():
    ch = _channel()
    ch._http = MagicMock()
    resp_429 = MagicMock(status_code=429)
    resp_429.json = MagicMock(return_value={"retry_after": 0.0})  # 0 → no real wait
    resp_ok = MagicMock(status_code=200)
    resp_ok.raise_for_status = MagicMock()
    ch._http.post = AsyncMock(side_effect=[resp_429, resp_ok])
    assert asyncio.run(ch._post_retry("u", {})) is True
    assert ch._http.post.await_count == 2


def test_post_retry_returns_false_after_errors(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # skip the backoff waits
    ch = _channel()
    ch._http = MagicMock()
    ch._http.post = AsyncMock(side_effect=RuntimeError("boom"))
    assert asyncio.run(ch._post_retry("u", {})) is False
    assert ch._http.post.await_count == 3  # 3 attempts then give up


# ── gateway resume / close-code handling (fake gateway frames) ────────


class _FakeWS:
    def __init__(self, frames):
        self.frames = [json.dumps(f) for f in frames]
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def send(self, data):
        self.sent.append(json.loads(data))


def _gateway_ch():
    ch = _channel()
    ch._start_heartbeat = AsyncMock()  # isolate session logic from the beat loop
    return ch


def test_ready_stores_resume_state_and_identifies():
    ch = _gateway_ch()
    ws = _FakeWS(
        [
            {"op": 10, "d": {"heartbeat_interval": 1000}},
            {
                "op": 0,
                "t": "READY",
                "s": 1,
                "d": {"user": {"id": "42"}, "session_id": "sess9", "resume_gateway_url": "wss://resume"},
            },
        ]
    )
    ch._ws = ws
    asyncio.run(ch._gateway_loop())
    assert ws.sent[0]["op"] == 2  # fresh start -> IDENTIFY
    assert ch._session_id == "sess9" and ch._resume_url == "wss://resume"
    assert ch._seq == 1


def test_hello_resumes_when_session_known():
    ch = _gateway_ch()
    ch._session_id, ch._resume_url, ch._seq = "sess9", "wss://resume", 41
    ws = _FakeWS([{"op": 10, "d": {"heartbeat_interval": 1000}}])
    ch._ws = ws
    asyncio.run(ch._gateway_loop())
    assert ws.sent[0]["op"] == 6  # RESUME, not IDENTIFY
    assert ws.sent[0]["d"] == {"token": "t", "session_id": "sess9", "seq": 41}


def test_invalid_session_not_resumable_resets(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # skip the 1-5s anti-storm wait
    ch = _gateway_ch()
    ch._session_id, ch._resume_url, ch._seq = "sess9", "wss://resume", 41
    ch._ws = _FakeWS([{"op": 9, "d": False}])
    asyncio.run(ch._gateway_loop())
    assert ch._session_id is None and ch._resume_url is None


def test_invalid_session_resumable_keeps_session():
    ch = _gateway_ch()
    ch._session_id, ch._resume_url, ch._seq = "sess9", "wss://resume", 41
    ch._ws = _FakeWS([{"op": 9, "d": True}])
    asyncio.run(ch._gateway_loop())
    assert ch._session_id == "sess9"


def test_identify_clears_stale_seq():
    ch = _gateway_ch()
    ch._seq = 99
    ws = _FakeWS([])
    ch._ws = ws
    asyncio.run(ch._identify())
    assert ch._seq is None  # new session: no stale seq in heartbeats
    assert ws.sent[0]["op"] == 2


def _closed_error(code: int):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    return ConnectionClosedError(Close(code, "x"), None)


def _patch_connect(monkeypatch, errors):
    """websockets.connect substitute raising the queued errors on enter."""
    import raven.channels.adapters.discord.channel as disc

    class _Ctx:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise errors.pop(0)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(disc.websockets, "connect", _Ctx)


def test_fatal_close_code_stops_reconnecting(monkeypatch):
    """4004 (auth failed) must stop the channel, not loop re-IDENTIFY forever."""
    ch = _gateway_ch()
    _patch_connect(monkeypatch, [_closed_error(4004)])
    asyncio.run(ch.start())  # returns instead of looping
    assert ch._running is False


def test_new_session_close_code_resets_session(monkeypatch):
    """4009 (session timed out) reconnects but must re-IDENTIFY, not resume."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())  # skip the 5s backoff
    ch = _gateway_ch()
    ch._session_id, ch._resume_url, ch._seq = "sess9", "wss://resume", 41
    _patch_connect(monkeypatch, [_closed_error(4009), asyncio.CancelledError()])
    asyncio.run(ch.start())  # second connect aborts the loop
    assert ch._session_id is None  # next HELLO would IDENTIFY


# ── contract conformance ───────────────────────────────────────────────


def test_discord_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations

    ch = _channel()
    assert isinstance(ch, Channel)  # name/capabilities/start/stop/send
    assert capability_violations(ch) == []  # no login/streaming declared or implemented


def test_discord_spec_import_is_cheap():
    """Importing discord.spec must NOT pull in httpx/websockets (deferred into
    SPEC.factory)."""
    import subprocess
    import sys

    code = (
        "import sys, raven.channels.adapters.discord.spec as s;"
        "assert 'httpx' not in sys.modules and 'websockets' not in sys.modules, "
        "'spec import pulled in a heavy SDK';"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'Discord'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
