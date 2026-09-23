"""Tests for ``raven.channels.adapters.whatsapp`` — bridge_token, LID mapping, group_policy."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.channels.adapters.whatsapp.bridge import load_or_create_bridge_token
from raven.channels.adapters.whatsapp.channel import WhatsAppChannel
from tests.conftest import make_channel_config


def _make_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **cfg_overrides,
) -> WhatsAppChannel:
    monkeypatch.setattr(
        "raven.config.paths.get_runtime_subdir",
        lambda name: tmp_path / name,
    )
    cfg = make_channel_config("whatsapp", enabled=True, **cfg_overrides)
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


def test_effective_bridge_token_uses_configured_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When config has a bridge_token, use it verbatim — don't auto-generate."""
    ch = _make_channel(monkeypatch, tmp_path, bridge_token="user-supplied-token")
    assert ch._effective_bridge_token() == "user-supplied-token"
    assert not (tmp_path / "whatsapp-auth" / "bridge-token").exists()


def test_effective_bridge_token_falls_back_to_persistent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty config.bridge_token → auto-generate + persist under whatsapp-auth/."""
    ch = _make_channel(monkeypatch, tmp_path, bridge_token="")
    token = ch._effective_bridge_token()
    assert token
    persisted = tmp_path / "whatsapp-auth" / "bridge-token"
    assert persisted.exists()
    assert persisted.read_text(encoding="utf-8").strip() == token


def test_effective_bridge_token_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_lid_only_resolves_via_cached_phone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_lid_only_uncached_falls_back_to_lid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_group_policy_mention_filters_unmentioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_group_policy_mention_lets_mentioned_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_group_policy_open_passes_unmentioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_dedup_drops_repeated_message_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same message_id processed twice → second is silently dropped."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch.intake.publish = AsyncMock()

    payload = json.dumps(
        {
            "type": "message",
            "pn": "8613800138000@s.whatsapp.net",
            "sender": "12345@lid.whatsapp.net",
            "content": "dup",
            "id": "dup-id",
        }
    )
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


async def test_qr_message_sets_pending_qr_and_pairing_clears_it(tmp_path, monkeypatch):
    """The bridge's qr message is what the web UI renders, and pairing has to
    retract it -- a stale code would keep being served after login."""
    ch = _make_channel(monkeypatch, tmp_path)
    assert ch.pending_qr is None
    assert ch.connected is False

    await ch._handle_bridge_message(json.dumps({"type": "qr", "qr": "2@abc"}))
    assert ch.pending_qr == "2@abc"
    # Still unpaired while the code waits to be scanned.
    assert ch.connected is False

    await ch._handle_bridge_message(json.dumps({"type": "status", "status": "connected"}))
    assert ch.pending_qr is None
    assert ch.connected is True


async def test_qr_message_accepts_the_code_field(tmp_path, monkeypatch):
    """Older bridge builds spell the payload `code` rather than `qr`."""
    ch = _make_channel(monkeypatch, tmp_path)
    await ch._handle_bridge_message(json.dumps({"type": "qr", "code": "2@xyz"}))
    assert ch.pending_qr == "2@xyz"


async def test_invalid_json_is_ignored(tmp_path, monkeypatch):
    ch = _make_channel(monkeypatch, tmp_path)
    ch.intake.publish = AsyncMock()
    await ch._handle_bridge_message("{not json")
    ch.intake.publish.assert_not_called()


async def test_send_emits_ws_payload(tmp_path, monkeypatch):
    ch = _make_channel(monkeypatch, tmp_path)
    ch._bridge_up = True
    sent = {}
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock(side_effect=lambda p: sent.update(payload=p))
    await ch.send("99@lid.whatsapp.net", "hi")
    assert json.loads(sent["payload"]) == {"type": "send", "to": "99@lid.whatsapp.net", "text": "hi"}


# ---------------------------------------------------------------------------
# bridge.ensure_bridge_dir — unit-testable branches
# ---------------------------------------------------------------------------

from raven.channels.adapters.whatsapp import bridge as wb  # noqa: E402


def _bridge_source_tree(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    for name in ("package.json", "package-lock.json", "tsconfig.json"):
        (root / name).write_text("{}", encoding="utf-8")
    (root / "src" / "index.ts").write_text("export const version = 1\n", encoding="utf-8")
    return root


def _installed_build(root: Path, fingerprint: str | None) -> Path:
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "index.js").write_text("//", encoding="utf-8")
    if fingerprint is not None:
        (root / wb._FINGERPRINT_FILE).write_text(fingerprint, encoding="utf-8")
    return root


def _refuse_to_build(*args, **kwargs):
    raise AssertionError("rebuilt a bridge that already matches the packaged source")


def test_ensure_bridge_dir_returns_prebuilt(tmp_path, monkeypatch):
    source = _bridge_source_tree(tmp_path / "source")
    built = _installed_build(tmp_path / "installed", wb.source_fingerprint(source))
    monkeypatch.setattr(wb, "_find_bridge_source", lambda: source)
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: built)
    monkeypatch.setattr(wb.subprocess, "run", _refuse_to_build)
    assert wb.ensure_bridge_dir() == built


@pytest.mark.parametrize("installed_fingerprint", [None, "a build from other sources"], ids=["absent", "different"])
def test_ensure_bridge_dir_rebuilds_a_build_that_does_not_match_the_source(
    tmp_path, monkeypatch, installed_fingerprint
):
    """An upgraded raven ships new bridge sources; the old dist must not stay."""
    source = _bridge_source_tree(tmp_path / "source")
    built = _installed_build(tmp_path / "installed", installed_fingerprint)
    runs: list[list[str]] = []
    monkeypatch.setattr(wb, "_find_bridge_source", lambda: source)
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: built)
    monkeypatch.setattr(wb.shutil, "which", lambda _name: "/usr/bin/npm")
    monkeypatch.setattr(wb.subprocess, "run", lambda cmd, **kwargs: runs.append(list(cmd[1:])))

    assert wb.ensure_bridge_dir() == built
    assert runs == [["install"], ["run", "build"]]
    assert (built / wb._FINGERPRINT_FILE).read_text(encoding="utf-8") == wb.source_fingerprint(source)
    assert (built / "src" / "index.ts").exists()


def test_source_fingerprint_follows_a_changed_src_file(tmp_path):
    source = _bridge_source_tree(tmp_path / "source")
    before = wb.source_fingerprint(source)
    (source / "src" / "index.ts").write_text("export const version = 2\n", encoding="utf-8")
    assert wb.source_fingerprint(source) != before


def test_source_fingerprint_skips_a_manifest_the_source_does_not_carry(tmp_path):
    source = _bridge_source_tree(tmp_path / "source")
    (source / "package-lock.json").unlink()
    assert wb.source_fingerprint(source)


def test_ensure_bridge_dir_rebuilds_when_the_fingerprint_cannot_be_read(tmp_path, monkeypatch):
    """A fingerprint truncated mid-write is as good as an absent one, not a crash."""
    source = _bridge_source_tree(tmp_path / "source")
    built = _installed_build(tmp_path / "installed", None)
    (built / wb._FINGERPRINT_FILE).write_bytes(b"\xff\xfe not utf-8")
    runs: list[list[str]] = []
    monkeypatch.setattr(wb, "_find_bridge_source", lambda: source)
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: built)
    monkeypatch.setattr(wb.shutil, "which", lambda _name: "/usr/bin/npm")
    monkeypatch.setattr(wb.subprocess, "run", lambda cmd, **kwargs: runs.append(list(cmd[1:])))

    assert wb.ensure_bridge_dir() == built
    assert runs == [["install"], ["run", "build"]]


def test_ensure_bridge_dir_keeps_the_working_build_when_a_rebuild_fails(tmp_path, monkeypatch):
    """npm can fail on a machine whose stale build still runs; it must survive."""
    source = _bridge_source_tree(tmp_path / "source")
    built = _installed_build(tmp_path / "installed", "a build from other sources")
    monkeypatch.setattr(wb, "_find_bridge_source", lambda: source)
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: built)
    monkeypatch.setattr(wb.shutil, "which", lambda _name: "/usr/bin/npm")

    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(wb.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        wb.ensure_bridge_dir()
    assert (built / "dist" / "index.js").exists()
    assert (built / wb._FINGERPRINT_FILE).read_text(encoding="utf-8") == "a build from other sources"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["installed", "source"]


def test_ensure_bridge_dir_keeps_a_build_it_cannot_check(tmp_path, monkeypatch):
    """Without the packaged source there is nothing to compare or rebuild from."""
    built = _installed_build(tmp_path / "installed", None)
    monkeypatch.setattr(wb, "_find_bridge_source", lambda: None)
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: built)
    monkeypatch.setattr(wb.subprocess, "run", _refuse_to_build)
    assert wb.ensure_bridge_dir() == built


def test_ensure_bridge_dir_raises_without_the_packaged_source(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_find_bridge_source", lambda: None)
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: tmp_path / "absent")
    with pytest.raises(RuntimeError, match="bridge source not found"):
        wb.ensure_bridge_dir()


def test_ensure_bridge_dir_raises_without_npm(tmp_path, monkeypatch):
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(RuntimeError):
        wb.ensure_bridge_dir()


# ── send: transient vs permanent errors ───────────────────────────────


async def test_send_reraises_transient_for_manager_retry(monkeypatch, tmp_path):
    """A ws drop propagates so manager._send_with_retry can back off."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch._bridge_up = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock(side_effect=ConnectionError("ws closed"))
    with pytest.raises(ConnectionError):
        await ch.send("u1", "hi")


async def test_send_media_surfaced_as_notice(monkeypatch, tmp_path):
    """The bridge send protocol is text-only — dropped attachments become a
    visible notice in the outgoing text instead of vanishing."""
    ch = _make_channel(monkeypatch, tmp_path)
    ch._bridge_up = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock()
    await ch.send("u1", "hi", media=["/m/report.pdf"])
    sent = json.loads(ch._ws.send.await_args.args[0])
    assert sent["text"] == "hi\n[Attachment not sent: report.pdf]"


async def test_send_media_only_still_sends_notice(monkeypatch, tmp_path):
    ch = _make_channel(monkeypatch, tmp_path)
    ch._bridge_up = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock()
    await ch.send("u1", "", media=["/m/a.jpg"])
    sent = json.loads(ch._ws.send.await_args.args[0])
    assert sent["text"] == "[Attachment not sent: a.jpg]"


async def test_send_swallows_permanent_error(monkeypatch, tmp_path):
    ch = _make_channel(monkeypatch, tmp_path)
    ch._bridge_up = True
    ch._ws = MagicMock()
    ch._ws.send = AsyncMock(side_effect=RuntimeError("bad payload"))
    await ch.send("u1", "hi")  # no raise


# ── contract conformance (interactive-login channel) ──────────────────


def test_whatsapp_satisfies_channel_contract(monkeypatch, tmp_path):
    from raven.channels import Channel, SupportsLogin
    from raven.channels.contract import capability_violations

    ch = _make_channel(monkeypatch, tmp_path)
    assert isinstance(ch, Channel)
    assert isinstance(ch, SupportsLogin)  # QR pairing
    assert ch.capabilities.interactive_login is True
    assert capability_violations(ch) == []  # declared interactive_login ↔ implements SupportsLogin


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


def test_the_bridge_reports_a_long_step_through_the_installed_progress(tmp_path, monkeypatch) -> None:
    """The adapter owns no terminal: it announces npm install and tsc through
    ``bridge.progress``, which the CLI login replaces with a spinner."""
    import subprocess
    from contextlib import contextmanager

    from raven.channels.adapters.whatsapp import bridge

    labels: list[str] = []

    @contextmanager
    def _record(label: str):
        labels.append(label)
        yield

    install_dir = tmp_path / "bridge"
    (install_dir / "src").mkdir(parents=True)
    monkeypatch.setattr(bridge, "progress", _record)
    monkeypatch.setattr("raven.config.paths.get_bridge_install_dir", lambda: install_dir)
    monkeypatch.setattr(bridge.shutil, "which", lambda _name: "/usr/bin/npm")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    bridge.ensure_bridge_dir()

    assert labels and any("npm install" in label for label in labels)
    assert any("tsc" in label for label in labels)


def test_the_default_progress_is_a_log_line_not_a_terminal(caplog) -> None:
    from raven.channels.adapters.whatsapp import bridge

    assert bridge.progress is bridge._log_progress
    with bridge.progress("step"):
        pass


# ---------------------------------------------------------------------------
# lifecycle: the adapter owns the bridge process
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never reached")


class _FakeBridge:
    """In-process stand-in for the Node bridge: takes the auth frame, then emits
    whatever frames the test wants the adapter to react to."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.clients: list[Any] = []
        self._server = None

    async def start(self, port: int) -> None:
        import websockets

        self._server = await websockets.serve(self._serve, "127.0.0.1", port)

    async def _serve(self, ws) -> None:
        self.tokens.append(json.loads(await ws.recv())["token"])
        self.clients.append(ws)
        await ws.wait_closed()

    async def emit(self, frame: dict) -> None:
        for ws in self.clients:
            await ws.send(json.dumps(frame))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


class _FakeProcess:
    """Stands in for the spawned bridge process."""

    pid = 4242

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    async def wait(self) -> int | None:
        return self.returncode


async def _stop_adapter(ch, task) -> None:
    """Stop a started adapter and wind down its client task."""
    await ch.stop()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_bridge_endpoint_reads_host_and_port():
    assert wb.bridge_endpoint("ws://localhost:3001") == ("localhost", 3001)
    assert wb.bridge_endpoint("ws://127.0.0.1") == ("127.0.0.1", wb.DEFAULT_BRIDGE_PORT)
    assert wb.is_local_bridge("ws://127.0.0.1:3002") is True
    assert wb.is_local_bridge("ws://bridge.example.net:3001") is False


async def test_start_spawns_the_local_bridge_and_stop_terminates_it(tmp_path, monkeypatch):
    """Nothing listens on the local bridge port, so the adapter runs the bridge
    itself -- without that, enabling WhatsApp from the page can never show a QR."""
    port = _free_port()
    fake, proc = _FakeBridge(), _FakeProcess()
    spawned: dict[str, Any] = {}

    async def _spawn(bridge_dir, token, auth_dir, spawn_port):
        spawned.update(dir=bridge_dir, token=token, auth_dir=auth_dir, port=spawn_port)
        await fake.start(spawn_port)
        return proc

    ch = _make_channel(monkeypatch, tmp_path, bridge_url=f"ws://127.0.0.1:{port}", bridge_token="t0k")
    monkeypatch.setattr(wb, "ensure_bridge_dir", lambda: tmp_path / "bridge")
    monkeypatch.setattr(wb, "spawn_bridge", _spawn)

    task = asyncio.create_task(ch.start())
    try:
        await _wait_for(lambda: bool(fake.tokens))
        assert spawned == {
            "dir": tmp_path / "bridge",
            "token": "t0k",
            "auth_dir": str(tmp_path / "whatsapp-auth"),
            "port": port,
        }
        assert fake.tokens == ["t0k"]
    finally:
        await _stop_adapter(ch, task)
        await fake.stop()

    assert proc.terminated is True


async def test_pairing_is_the_status_frame_not_the_bridge_handshake(tmp_path, monkeypatch):
    """Reaching the bridge is not being signed in: until the bridge says
    connected the row has to stay on "waiting to be signed in"."""
    port = _free_port()
    fake, proc = _FakeBridge(), _FakeProcess()

    async def _spawn(bridge_dir, token, auth_dir, spawn_port):  # noqa: ARG001
        await fake.start(spawn_port)
        return proc

    ch = _make_channel(monkeypatch, tmp_path, bridge_url=f"ws://127.0.0.1:{port}")
    monkeypatch.setattr(wb, "ensure_bridge_dir", lambda: tmp_path / "bridge")
    monkeypatch.setattr(wb, "spawn_bridge", _spawn)

    task = asyncio.create_task(ch.start())
    try:
        await _wait_for(lambda: ch._bridge_up)
        assert ch.connected is False

        await fake.emit({"type": "qr", "qr": "2@abc"})
        await _wait_for(lambda: ch.pending_qr == "2@abc")
        assert ch.connected is False

        await fake.emit({"type": "status", "status": "connected"})
        await _wait_for(lambda: ch.connected)
        assert ch.pending_qr is None
    finally:
        await _stop_adapter(ch, task)
        await fake.stop()


async def test_a_remote_bridge_url_is_never_spawned_locally(tmp_path, monkeypatch):
    """A bridge someone else hosts stays client-only."""

    def _never(*args, **kwargs):
        raise AssertionError("a remote bridge must not be started here")

    ch = _make_channel(monkeypatch, tmp_path, bridge_url="ws://bridge.example.net:3001")
    monkeypatch.setattr(wb, "ensure_bridge_dir", _never)
    monkeypatch.setattr(wb, "spawn_bridge", _never)

    assert await ch._ensure_bridge_process() is True


async def test_start_gives_up_when_the_bridge_cannot_be_built(tmp_path, monkeypatch):
    """Without node the bridge will not come up on any retry, so the adapter
    stops instead of looping every five seconds with the row reading running."""

    def _boom():
        raise RuntimeError("node not found. Please install Node.js >= 20.")

    async def _closed(host, port, timeout=1.0):  # noqa: ARG001
        return False

    ch = _make_channel(monkeypatch, tmp_path)
    monkeypatch.setattr(wb, "port_is_open", _closed)
    monkeypatch.setattr(wb, "ensure_bridge_dir", _boom)

    await asyncio.wait_for(ch.start(), timeout=5)

    assert ch.is_running is False


async def test_stop_during_the_build_terminates_the_bridge_it_spawned(tmp_path, monkeypatch):
    """Gateway shutdown calls stop() without cancelling the channel task, so a
    stop landing while the first build runs finds no process to stop; the child
    spawned right after it must not outlive the gateway holding the session."""
    port = _free_port()
    proc = _FakeProcess()
    building, release = threading.Event(), threading.Event()
    spawned = False

    def _build():
        building.set()
        release.wait(5)
        return tmp_path / "bridge"

    async def _spawn(bridge_dir, token, auth_dir, spawn_port):  # noqa: ARG001
        nonlocal spawned
        spawned = True
        return proc

    async def _port_is_open(host, probe_port, timeout=1.0):  # noqa: ARG001
        return spawned

    ch = _make_channel(monkeypatch, tmp_path, bridge_url=f"ws://127.0.0.1:{port}")
    monkeypatch.setattr(wb, "port_is_open", _port_is_open)
    monkeypatch.setattr(wb, "ensure_bridge_dir", _build)
    monkeypatch.setattr(wb, "spawn_bridge", _spawn)

    task = asyncio.create_task(ch.start())
    try:
        await _wait_for(building.is_set)
        await ch.stop()
        release.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert proc.terminated is True
    assert ch._bridge_proc is None


async def test_a_bridge_that_never_listens_is_not_retried_forever(tmp_path, monkeypatch):
    """A child that spawns but never binds is as dead as one that cannot be
    built: retrying the connect every five seconds leaves the row reading
    running with nothing behind it and no failure anyone can act on."""
    proc = _FakeProcess()

    async def _closed(host, port, timeout=1.0):  # noqa: ARG001
        return False

    async def _spawn(bridge_dir, token, auth_dir, spawn_port):  # noqa: ARG001
        return proc

    ch = _make_channel(monkeypatch, tmp_path, bridge_url=f"ws://127.0.0.1:{_free_port()}")
    monkeypatch.setattr(wb, "port_is_open", _closed)
    monkeypatch.setattr(wb, "ensure_bridge_dir", lambda: tmp_path / "bridge")
    monkeypatch.setattr(wb, "spawn_bridge", _spawn)
    monkeypatch.setattr("raven.channels.adapters.whatsapp.channel._BRIDGE_READY_SECONDS", 0.05)

    await asyncio.wait_for(ch.start(), timeout=2)

    assert ch.is_running is False
    assert proc.terminated is True


async def test_send_raises_a_transient_error_while_the_bridge_is_down(tmp_path, monkeypatch):
    """A reply written while the socket is down must reach the delivery hub as a
    failure it retries and counts, not as a delivered message."""
    from raven.channels.errors import transient_network

    ch = _make_channel(monkeypatch, tmp_path)
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        await ch.send("u1", "hi")
    assert transient_network(excinfo.value)


async def test_login_returns_once_the_bridge_reports_a_paired_session(tmp_path, monkeypatch):
    """`raven channels login whatsapp` runs the same bridge the gateway runs and
    ends when the phone has scanned."""
    port = _free_port()
    fake, proc = _FakeBridge(), _FakeProcess()

    async def _spawn(bridge_dir, token, auth_dir, spawn_port):  # noqa: ARG001
        await fake.start(spawn_port)
        return proc

    ch = _make_channel(monkeypatch, tmp_path, bridge_url=f"ws://127.0.0.1:{port}")
    monkeypatch.setattr(wb, "ensure_bridge_dir", lambda: tmp_path / "bridge")
    monkeypatch.setattr(wb, "spawn_bridge", _spawn)

    login = asyncio.create_task(ch.login())
    try:
        await _wait_for(lambda: bool(fake.clients))
        await fake.emit({"type": "status", "status": "connected"})
        assert await asyncio.wait_for(login, timeout=5) is True
    finally:
        login.cancel()
        with suppress(asyncio.CancelledError):
            await login
        await fake.stop()

    assert proc.terminated is True


# ---------------------------------------------------------------------------
# bridge process helpers and the adapter's branches around them
# ---------------------------------------------------------------------------


async def test_spawn_bridge_runs_node_with_the_bridge_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child gets the shared token, its auth dir and the port from ``bridge_url``
    through the environment; the bridge reads nothing else."""
    seen: dict[str, Any] = {}

    async def _exec(*argv, cwd=None, env=None):
        seen.update(argv=argv, cwd=cwd, env=env)
        return _FakeProcess()

    monkeypatch.setattr(wb.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    monkeypatch.setattr(wb.asyncio, "create_subprocess_exec", _exec)

    proc = await wb.spawn_bridge(tmp_path / "bridge", "t0k", str(tmp_path / "auth"), 3007)

    assert isinstance(proc, _FakeProcess)
    assert seen["argv"] == ("/usr/bin/node", "dist/index.js")
    assert seen["cwd"] == tmp_path / "bridge"
    assert seen["env"]["BRIDGE_TOKEN"] == "t0k"
    assert seen["env"]["AUTH_DIR"] == str(tmp_path / "auth")
    assert seen["env"]["BRIDGE_PORT"] == "3007"


async def test_spawn_bridge_refuses_without_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="node not found"):
        await wb.spawn_bridge(tmp_path / "bridge", "t0k", str(tmp_path / "auth"), 3001)


class _StubbornProcess:
    """A child that ignores SIGTERM and only exits when killed."""

    pid = 4243

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._gone = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._gone.set()

    async def wait(self) -> int | None:
        await self._gone.wait()
        return self.returncode


async def test_terminate_bridge_kills_a_child_that_ignores_the_request() -> None:
    proc = _StubbornProcess()
    await wb.terminate_bridge(proc, timeout=0.05)
    assert proc.terminated is True
    assert proc.killed is True
    assert proc.returncode == -9


async def test_terminate_bridge_leaves_a_finished_child_alone() -> None:
    proc = _FakeProcess()
    proc.returncode = 0
    await wb.terminate_bridge(proc)
    assert proc.terminated is False


async def test_ensure_bridge_process_reuses_a_live_child_or_a_listening_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reasons not to spawn: our own child is still up, or something else
    already serves the port (the CLI login, an operator's bridge)."""

    async def _never(*_a, **_k):
        raise AssertionError("nothing must be spawned here")

    ch = _make_channel(monkeypatch, tmp_path, bridge_url="ws://127.0.0.1:3009")
    monkeypatch.setattr(wb, "spawn_bridge", _never)
    monkeypatch.setattr(wb, "ensure_bridge_dir", _never)

    ch._bridge_proc = _FakeProcess()
    assert await ch._ensure_bridge_process() is True

    ch._bridge_proc = None

    async def _open(host, port, timeout=1.0):
        return True

    monkeypatch.setattr(wb, "port_is_open", _open)
    assert await ch._ensure_bridge_process() is True
    assert ch._bridge_proc is None


async def test_ensure_bridge_process_discards_the_child_when_cancelled_while_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway's shutdown sweep cancels the start task while the bridge is
    still coming up; the child it just spawned must not outlive that."""
    fake = _FakeProcess()

    async def _spawn(bridge_dir, token, auth_dir, port):
        return fake

    async def _closed(host, port, timeout=1.0):
        return False

    async def _cancelled(host, port, timeout):
        raise asyncio.CancelledError

    ch = _make_channel(monkeypatch, tmp_path, bridge_url="ws://127.0.0.1:3010")
    monkeypatch.setattr(wb, "ensure_bridge_dir", lambda: tmp_path / "bridge")
    monkeypatch.setattr(wb, "spawn_bridge", _spawn)
    monkeypatch.setattr(wb, "port_is_open", _closed)
    monkeypatch.setattr(wb, "wait_for_port", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await ch._ensure_bridge_process()
    assert fake.terminated is True
    assert ch._bridge_proc is None


async def test_login_reports_a_client_that_crashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _make_channel(monkeypatch, tmp_path)

    async def _boom() -> None:
        raise RuntimeError("bridge client exploded")

    monkeypatch.setattr(ch, "start", _boom)
    assert await ch.login() is False
    assert ch.is_running is False


async def test_start_reconnects_after_a_dropped_socket_and_stops_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused socket is a wait-and-retry, a cancellation is the end; and a stop
    that lands while the bridge is coming up ends the loop before a connect."""
    import websockets

    from raven.channels.adapters.whatsapp import channel as wc

    ch = _make_channel(monkeypatch, tmp_path, bridge_url="ws://127.0.0.1:3011")
    attempts: list[str] = []

    async def _bridge_ok() -> bool:
        return True

    def _connect(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionRefusedError("nobody there yet")
        raise asyncio.CancelledError

    monkeypatch.setattr(ch, "_ensure_bridge_process", _bridge_ok)
    monkeypatch.setattr(wc, "_RECONNECT_SECONDS", 0)
    monkeypatch.setattr(websockets, "connect", _connect)

    await ch.start()
    assert attempts == ["ws://127.0.0.1:3011", "ws://127.0.0.1:3011"]
    assert ch._bridge_up is False
    assert ch._ws is None

    async def _bridge_ok_but_stopped() -> bool:
        ch._running = False
        return True

    attempts.clear()
    monkeypatch.setattr(ch, "_ensure_bridge_process", _bridge_ok_but_stopped)
    await ch.start()
    assert attempts == [], "stopped while the bridge came up: no connect is attempted"
