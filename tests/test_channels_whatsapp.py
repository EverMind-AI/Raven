"""Tests for ``raven.channels.adapters.whatsapp`` — bridge_token, LID mapping, group_policy."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.channels.adapters.whatsapp.bridge import load_or_create_bridge_token
from raven.channels.adapters.whatsapp.channel import WhatsAppChannel
from raven.config.schema import WhatsAppConfig


def _make_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **cfg_overrides,
) -> WhatsAppChannel:
    monkeypatch.setattr(
        "raven.config.paths.get_runtime_subdir",
        lambda name: tmp_path / name,
    )
    cfg = WhatsAppConfig(enabled=True, **cfg_overrides)
    return WhatsAppChannel(cfg)


# ---------------------------------------------------------------------------
# bridge_token persistence
# ---------------------------------------------------------------------------


def test_load_or_create_bridge_token_creates_then_reads(tmp_path: Path) -> None:
    token_file = tmp_path / "bridge-token"
    t1 = load_or_create_bridge_token(token_file)
    assert token_file.exists()
    assert len(t1) >= 32

    t2 = load_or_create_bridge_token(token_file)
    assert t1 == t2, "second call must read the same persisted token"


def test_effective_bridge_token_uses_configured_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When config has a bridge_token, use it verbatim — don't auto-generate."""
    ch = _make_channel(monkeypatch, tmp_path, bridge_token="user-supplied-token")
    assert ch._effective_bridge_token() == "user-supplied-token"
    assert not (tmp_path / "whatsapp-auth" / "bridge-token").exists()


def test_effective_bridge_token_falls_back_to_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty config.bridge_token → auto-generate + persist under whatsapp-auth/."""
    ch = _make_channel(monkeypatch, tmp_path, bridge_token="")
    token = ch._effective_bridge_token()
    assert token
    persisted = tmp_path / "whatsapp-auth" / "bridge-token"
    assert persisted.exists()
    assert persisted.read_text(encoding="utf-8").strip() == token


def test_effective_bridge_token_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once resolved, second call returns the cached value (no file re-read)."""
    ch = _make_channel(monkeypatch, tmp_path, bridge_token="")
    t1 = ch._effective_bridge_token()
    (tmp_path / "whatsapp-auth" / "bridge-token").write_text("DIFFERENT", encoding="utf-8")
    t2 = ch._effective_bridge_token()
    assert t1 == t2


# ---------------------------------------------------------------------------
# LID-to-phone mapping
# ---------------------------------------------------------------------------


async def test_lid_to_phone_mapping_populated_when_both_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First message carries both phone + lid → channel caches lid → phone."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch.intake.publish = AsyncMock()

    payload = {
        "type": "message",
        "pn": "8613800138000@s.whatsapp.net",
        "sender": "12345@lid.whatsapp.net",
        "content": "hi",
        "id": "m1",
    }
    await ch._handle_bridge_message(json.dumps(payload))

    assert ch._lid_to_phone == {"12345": "8613800138000"}
    kw = ch.intake.publish.await_args.kwargs
    assert kw["sender_id"] == "8613800138000"


async def test_lid_only_resolves_via_cached_phone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subsequent LID-only message resolves to the cached phone."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch.intake.publish = AsyncMock()
    ch._lid_to_phone["12345"] = "8613800138000"

    payload = {
        "type": "message",
        "pn": "",
        "sender": "12345@lid.whatsapp.net",
        "content": "hello again",
        "id": "m2",
    }
    await ch._handle_bridge_message(json.dumps(payload))

    kw = ch.intake.publish.await_args.kwargs
    assert kw["sender_id"] == "8613800138000"


async def test_lid_only_uncached_falls_back_to_lid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LID-only message with no cache → sender_id is the lid itself (best effort)."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch.intake.publish = AsyncMock()

    payload = {
        "type": "message",
        "pn": "",
        "sender": "99999@lid.whatsapp.net",
        "content": "stranger",
        "id": "m3",
    }
    await ch._handle_bridge_message(json.dumps(payload))

    kw = ch.intake.publish.await_args.kwargs
    assert kw["sender_id"] == "99999"


# ---------------------------------------------------------------------------
# group_policy
# ---------------------------------------------------------------------------


async def test_group_policy_mention_filters_unmentioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """group_policy=mention + isGroup=True + wasMentioned=False → message dropped."""
    ch = _make_channel(monkeypatch, tmp_path, group_policy="mention")
    ch.intake.publish = AsyncMock()

    payload = {
        "type": "message",
        "pn": "8613800138000@s.whatsapp.net",
        "sender": "g1@g.us",
        "content": "random group chatter",
        "id": "m4",
        "isGroup": True,
        "wasMentioned": False,
    }
    await ch._handle_bridge_message(json.dumps(payload))

    ch.intake.publish.assert_not_awaited()


async def test_group_policy_mention_lets_mentioned_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """group_policy=mention + wasMentioned=True → forwarded."""
    ch = _make_channel(monkeypatch, tmp_path, group_policy="mention")
    ch.intake.publish = AsyncMock()

    payload = {
        "type": "message",
        "pn": "8613800138000@s.whatsapp.net",
        "sender": "g1@g.us",
        "content": "@bot hi",
        "id": "m5",
        "isGroup": True,
        "wasMentioned": True,
    }
    await ch._handle_bridge_message(json.dumps(payload))

    ch.intake.publish.assert_awaited_once()


async def test_group_policy_open_passes_unmentioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """group_policy=open (default) → group messages forwarded regardless of mention."""
    ch = _make_channel(monkeypatch, tmp_path, group_policy="open")
    ch.intake.publish = AsyncMock()

    payload = {
        "type": "message",
        "pn": "8613800138000@s.whatsapp.net",
        "sender": "g1@g.us",
        "content": "casual group chat",
        "id": "m6",
        "isGroup": True,
        "wasMentioned": False,
    }
    await ch._handle_bridge_message(json.dumps(payload))

    ch.intake.publish.assert_awaited_once()


async def test_dedup_drops_repeated_message_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same message_id processed twice → second is silently dropped."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch.intake.publish = AsyncMock()

    payload = json.dumps({
        "type": "message",
        "pn": "8613800138000@s.whatsapp.net",
        "sender": "12345@lid.whatsapp.net",
        "content": "dup",
        "id": "dup-id",
    })
    await ch._handle_bridge_message(payload)
    await ch._handle_bridge_message(payload)

    assert ch.intake.publish.await_count == 1


# ---------------------------------------------------------------------------
# parsing.py — pure helpers
# ---------------------------------------------------------------------------

from raven.channels.adapters.whatsapp import parsing as wp  # noqa: E402


def test_classify_sender_phone_only():
    assert wp.classify_sender("861@s.whatsapp.net", "", {}) == ("861", "", "861")


def test_classify_sender_lid_only_uncached():
    assert wp.classify_sender("", "99@lid.whatsapp.net", {}) == ("", "99", "99")


def test_classify_sender_lid_cached():
    assert wp.classify_sender("", "99@lid.whatsapp.net", {"99": "861"}) == ("", "99", "861")


def test_classify_sender_both_present():
    assert wp.classify_sender("861@s.whatsapp.net", "99@lid.whatsapp.net", {}) == ("861", "99", "861")


def test_classify_sender_bare_value_is_phone():
    assert wp.classify_sender("861", "", {}) == ("861", "", "861")


def test_classify_sender_all_empty():
    assert wp.classify_sender("", "", {}) == ("", "", "")


def test_should_skip_group():
    assert wp.should_skip_group(True, "mention", False) is True
    assert wp.should_skip_group(True, "mention", True) is False
    assert wp.should_skip_group(True, "open", False) is False
    assert wp.should_skip_group(False, "mention", False) is False


def test_build_inbound_content_voice():
    out = wp.build_inbound_content("[Voice Message]", [])
    assert "Transcription not available" in out


def test_build_inbound_content_media_tags():
    out = wp.build_inbound_content("hello", ["/p/a.jpg", "/p/doc.pdf"])
    assert out.startswith("hello")
    assert "[image: /p/a.jpg]" in out and "[file: /p/doc.pdf]" in out


def test_build_inbound_content_media_only():
    assert wp.build_inbound_content("", ["/p/a.png"]) == "[image: /p/a.png]"


# ---------------------------------------------------------------------------
# channel: status / error / invalid-json / send
# ---------------------------------------------------------------------------


async def test_status_updates_connected(tmp_path, monkeypatch):
    ch = _make_channel(monkeypatch, tmp_path)
    await ch._handle_bridge_message(json.dumps({"type": "status", "status": "connected"}))
    assert ch._connected is True
    await ch._handle_bridge_message(json.dumps({"type": "status", "status": "disconnected"}))
    assert ch._connected is False


async def test_invalid_json_is_ignored(tmp_path, monkeypatch):
    ch = _make_channel(monkeypatch, tmp_path)
    ch.intake.publish = AsyncMock()
    await ch._handle_bridge_message("{not json")
    ch.intake.publish.assert_not_called()


async def test_send_emits_ws_payload(tmp_path, monkeypatch):
    ch = _make_channel(monkeypatch, tmp_path)
    ch._connected = True
    sent = {}
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock(side_effect=lambda p: sent.update(payload=p))
    await ch.send("99@lid.whatsapp.net", "hi")
    assert json.loads(sent["payload"]) == {"type": "send", "to": "99@lid.whatsapp.net", "text": "hi"}


# ---------------------------------------------------------------------------
# bridge.ensure_bridge_dir — unit-testable branches
# ---------------------------------------------------------------------------

from raven.channels.adapters.whatsapp import bridge as wb  # noqa: E402


def test_ensure_bridge_dir_returns_prebuilt(tmp_path, monkeypatch):
    built = tmp_path / "installed"
    (built / "dist").mkdir(parents=True)
    (built / "dist" / "index.js").write_text("//", encoding="utf-8")
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: built)
    assert wb.ensure_bridge_dir() == built


def test_ensure_bridge_dir_raises_without_npm(tmp_path, monkeypatch):
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(RuntimeError):
        wb.ensure_bridge_dir()


# ── send: transient vs permanent errors ───────────────────────────────


async def test_send_reraises_transient_for_manager_retry(monkeypatch, tmp_path):
    """A ws drop propagates so manager._send_with_retry can back off."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch._connected = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock(side_effect=ConnectionError("ws closed"))
    with pytest.raises(ConnectionError):
        await ch.send("u1", "hi")


async def test_send_media_surfaced_as_notice(monkeypatch, tmp_path):
    """The bridge send protocol is text-only — dropped attachments become a
    visible notice in the outgoing text instead of vanishing."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch._connected = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock()
    await ch.send("u1", "hi", media=["/m/report.pdf"])
    sent = json.loads(ch._ws.send.await_args.args[0])
    assert sent["text"] == "hi\n[Attachment not sent: report.pdf]"


async def test_send_media_only_still_sends_notice(monkeypatch, tmp_path):
    ch = _make_channel(monkeypatch, tmp_path)
    ch._connected = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock()
    await ch.send("u1", "", media=["/m/a.jpg"])
    sent = json.loads(ch._ws.send.await_args.args[0])
    assert sent["text"] == "[Attachment not sent: a.jpg]"


async def test_send_swallows_permanent_error(monkeypatch, tmp_path):
    ch = _make_channel(monkeypatch, tmp_path)
    ch._connected = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock(side_effect=RuntimeError("bad payload"))
    await ch.send("u1", "hi")  # no raise


# ── contract conformance (interactive-login channel) ──────────────────


def test_whatsapp_satisfies_channel_contract(monkeypatch, tmp_path):
    from raven.channels import Channel, SupportsLogin
    from raven.channels.contract import capability_violations
    ch = _make_channel(monkeypatch, tmp_path)
    assert isinstance(ch, Channel)
    assert isinstance(ch, SupportsLogin)             # QR pairing
    assert ch.capabilities.interactive_login is True
    assert capability_violations(ch) == []           # declared interactive_login ↔ implements SupportsLogin


def test_whatsapp_spec_declares_interactive_login_and_is_cheap():
    """spec.py must declare interactive_login (CLI login routing reads it) and
    importing it must NOT import the channel implementation."""
    import subprocess
    import sys
    code = (
        "import sys, raven.channels.adapters.whatsapp.spec as s;"
        "assert 'raven.channels.adapters.whatsapp.channel' not in sys.modules, "
        "'spec import pulled in the channel implementation';"
        "assert s.SPEC.capabilities.interactive_login is True;"
        "assert callable(s.SPEC.factory) and s.SPEC.display_name == 'WhatsApp'"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
