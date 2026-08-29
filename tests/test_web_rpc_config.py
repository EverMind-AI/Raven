"""The gateway control plane's channel methods (raven.channels.qr/start/live).

The forty-odd config-admin methods that used to share this file's subject died
with ui-webui (C7 ruling); their tests went with them. What remains is the
probe's vocabulary: the QR poll (against fakes and both real QR adapters, so an
adapter-side rename fails loudly), the start/stop switch, and the rebind state
riding the QR poll.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.web_rpc.methods_config import register_config_methods
from tests.conftest import make_channel_config


async def _dispatch(d: Dispatcher, method: str, params: dict, rid: int = 1) -> dict:
    return await d.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


async def test_channels_start_switches_the_adapter_and_reports_the_outcome() -> None:
    """raven.channels.start relays ChannelManager's outcome word verbatim, and
    answers no_manager (a state, not an error) when there is nothing live."""

    class _Mgr:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def start_one(self, name: str) -> str:
            self.calls.append(("start", name))
            return "started"

        async def stop_one(self, name: str) -> str:
            self.calls.append(("stop", name))
            return "stopped"

    mgr = _Mgr()
    d = Dispatcher()
    register_config_methods(d, channel_manager=mgr)

    assert (await _dispatch(d, "raven.channels.start", {"name": "telegram"}))["result"] == {"outcome": "started"}
    assert (await _dispatch(d, "raven.channels.start", {"name": "telegram", "enabled": False}))["result"] == {
        "outcome": "stopped"
    }
    assert mgr.calls == [("start", "telegram"), ("stop", "telegram")]

    d2 = Dispatcher()
    register_config_methods(d2, channel_manager=None)
    assert (await _dispatch(d2, "raven.channels.start", {"name": "telegram"}))["result"] == {"outcome": "no_manager"}


async def test_channels_live_reports_three_valued_connectedness() -> None:
    """Only the QR adapters report a pairing; everyone else answers null, never
    false -- a running Telegram bot is not "not connected"."""

    class _Qr:
        pending_qr = None
        is_running = True
        connected = True

    class _Plain:
        is_running = True

    class _Mgr:
        def get_channel(self, name: str):
            return {"weixin": _Qr(), "telegram": _Plain()}.get(name)

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    out = (await _dispatch(d, "raven.channels.live", {}))["result"]["channels"]
    assert out["weixin"] == {"running": True, "connected": True, "qr_login": True}
    assert out["telegram"] == {"running": True, "connected": None, "qr_login": False}
    assert out["discord"] == {"running": False, "connected": None, "qr_login": False}


async def test_channels_qr_renders_pending_login_code() -> None:
    """raven.channels.qr renders a channel's pending login QR to a PNG data URI,
    and reports connected from the adapter's own login flag."""

    class _Ch:
        pending_qr: str | None = "https://example.com/login?token=abc"
        is_running = True
        connected = False

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch() if name == "weixin" else None

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    resp = await _dispatch(d, "raven.channels.qr", {"name": "weixin"})
    r = resp["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["qr_text"] is None  # rasterised, so no raw-payload fallback
    assert r["connected"] is False  # a QR is pending -> not paired yet
    assert r["running"] is True

    _Ch.pending_qr = None
    _Ch.connected = True  # paired
    resp = await _dispatch(d, "raven.channels.qr", {"name": "weixin"})
    assert resp["result"] == {"qr": None, "qr_text": None, "connected": True, "running": True, "rebind": None}

    resp = await _dispatch(d, "raven.channels.qr", {"name": "nope"})
    assert resp["result"] == {"qr": None, "qr_text": None, "connected": False, "running": False, "rebind": None}


async def test_channels_qr_is_not_connected_before_the_first_qr_arrives() -> None:
    """Both QR adapters flip _running before fetching the first QR. Reporting
    connected off "running and no QR pending" would call that window paired and
    stop the UI polling, so it has to come from the adapter's login flag."""

    class _Ch:
        pending_qr = None  # not fetched yet
        is_running = True  # start() already set this
        connected = False  # but nobody has scanned anything

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch()

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    resp = await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"})
    assert resp["result"]["connected"] is False
    assert resp["result"]["running"] is True


async def test_channels_qr_falls_back_to_raw_payload_without_qrcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qrcode ships only with the QR-login extras, so a gateway without it must
    hand the client the raw payload instead of raising ModuleNotFoundError."""
    from raven.web_rpc import methods_config as mc

    monkeypatch.setattr(mc, "_render_qr_png", lambda text: None)

    class _Ch:
        pending_qr = "https://example.com/login?token=abc"
        is_running = True
        connected = False

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch()

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    r = (await _dispatch(d, "raven.channels.qr", {"name": "weixin"}))["result"]
    assert r["qr"] is None
    assert r["qr_text"] == "https://example.com/login?token=abc"


# ---------------------------------------------------------------------------
# raven.channels.qr against the real adapters. The RPC reaches into the channel
# by attribute name (pending_qr / is_running / connected), and a fake channel
# would keep every assertion green through a rename on the adapter side. These
# drive the adapters' own code paths so the contract fails loudly instead.
# ---------------------------------------------------------------------------


async def test_channels_qr_reads_a_real_whatsapp_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    from raven.channels.adapters.whatsapp.channel import WhatsAppChannel

    monkeypatch.setattr("raven.config.paths.get_runtime_subdir", lambda name: tmp_path / name)
    ch = WhatsAppChannel(make_channel_config("whatsapp", enabled=True))
    ch._running = True

    class _Mgr:
        def get_channel(self, name: str):
            return ch

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    # Nothing pending yet, and the task being up is not being paired.
    r = (await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"}))["result"]
    # `rebind` is None for an adapter that does not offer the flow -- whatsapp
    # pairs by QR but has no rebind of its own yet.
    assert r == {"qr": None, "qr_text": None, "connected": False, "running": True, "rebind": None}

    await ch._handle_bridge_message(_json.dumps({"type": "qr", "qr": "2@abc"}))
    r = (await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"}))["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["connected"] is False

    await ch._handle_bridge_message(_json.dumps({"type": "status", "status": "connected"}))
    r = (await _dispatch(d, "raven.channels.qr", {"name": "whatsapp"}))["result"]
    assert r == {"qr": None, "qr_text": None, "connected": True, "running": True, "rebind": None}


async def test_channels_qr_reads_a_real_weixin_adapter() -> None:
    from unittest.mock import AsyncMock

    from raven.channels.adapters.weixin.channel import WeixinChannel


    ch = WeixinChannel(make_channel_config("weixin"))
    ch._running = True
    ch._save_state = lambda: None
    ch._print_qr = lambda url: None

    class _Mgr:
        def get_channel(self, name: str):
            return ch

    d = Dispatcher()
    register_config_methods(d, channel_manager=_Mgr())

    r = (await _dispatch(d, "raven.channels.qr", {"name": "weixin"}))["result"]
    assert {k: v for k, v in r.items() if k != "rebind"} == {
        "qr": None,
        "qr_text": None,
        "connected": False,
        "running": True,
    }
    # A rebindable adapter reports the flow's state on the same poll, so the
    # dialog needs one request per tick rather than two that can drift apart.
    assert r["rebind"]["phase"] == "idle"
    assert r["rebind"]["code_age_s"] is None

    # Park the login flow on "waiting to be scanned" and read the RPC mid-flight.
    ch._fetch_qr = AsyncMock(return_value=("qid", "https://scan/1"))
    ch._get = AsyncMock(return_value={"status": "waiting"})
    login = asyncio.create_task(ch._qr_login())
    for _ in range(50):
        await asyncio.sleep(0)
        if ch.pending_qr:
            break
    r = (await _dispatch(d, "raven.channels.qr", {"name": "weixin"}))["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["connected"] is False

    ch._running = False  # unwind the poll loop
    login.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await login


class _RebindableChannel:
    """A QR channel as far as the rebind methods read it."""

    def __init__(self, *, started=True, reason="") -> None:
        self.calls: list[str] = []
        self._started = started
        self._reason = reason
        self.pending_qr = None
        self.connected = True
        self.is_running = True

    def rebind_state(self) -> dict:
        return {"phase": "waiting", "refreshes": 1, "max_refreshes": 3, "code_age_s": 4.0, "detail": ""}

    async def begin_rebind(self) -> dict:
        self.calls.append("begin")
        return {"started": self._started, "reason": self._reason, **self.rebind_state()}

    def cancel_rebind(self) -> dict:
        self.calls.append("cancel")
        return {**self.rebind_state(), "phase": "cancelled"}


class _OnlyChannel:
    def __init__(self, ch) -> None:
        self._ch = ch

    def get_channel(self, name: str):
        return self._ch if name == "weixin" else None


async def test_the_qr_poll_carries_the_rebind_phase() -> None:
    # One poll, two facts: the page already asks for the code once a second, and
    # a second call for the phase would drift out of step with it.
    ch = _RebindableChannel()
    ch.pending_qr = "https://scan/1"
    d = Dispatcher()
    register_config_methods(d, channel_manager=_OnlyChannel(ch))
    r = (await _dispatch(d, "raven.channels.qr", {"name": "weixin"}))["result"]
    assert r["rebind"]["phase"] == "waiting"
    assert r["rebind"]["code_age_s"] == 4.0
