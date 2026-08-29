"""The gateway control plane: charter, seat, token gate, and the six methods.

The plane is the daemon's window for other raven processes (see
raven/rpc/control.py). These tests pin the charter (a seventh method is a new
client contract nobody declared), the seat (the server lives on the surface
side and reaches nothing inner at import time), the token gate behaviourally,
and each method against fakes plus both real QR adapters.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from raven.rpc.control import CHARTER, CONTROL_METHOD_MODELS, ControlPlaneServer, register_control_methods
from raven.rpc.dispatcher import Dispatcher
from tests.conftest import make_channel_config


async def _dispatch(d: Dispatcher, method: str, params: dict, rid: int = 1) -> dict:
    return await d.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def test_the_control_plane_registers_exactly_the_charter() -> None:
    d = Dispatcher()
    register_control_methods(d, channel_manager=None)
    assert set(d.methods()) == set(CHARTER)
    assert set(CONTROL_METHOD_MODELS) == set(CHARTER)


def test_the_server_module_reaches_nothing_inner_at_import_time() -> None:
    """raven/rpc/control.py sits on the surface side; the daemon package is the
    client. A module-level import of raven.gateway here would invert that."""
    src = Path(__file__).resolve().parents[1] / "raven" / "rpc" / "control.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("raven."):
            assert not node.module.startswith("raven.gateway"), node.module


async def test_host_commands_answer_unavailable_without_their_closures() -> None:
    """A plane built without the gateway's closures keeps the charter whole:
    the methods exist and refuse, rather than not existing."""
    d = Dispatcher()
    register_control_methods(d, channel_manager=None)
    assert (await _dispatch(d, "gateway.reload", {}))["result"] == {"ok": False, "reason": "unavailable"}
    assert (await _dispatch(d, "gateway.shutdown", {}))["result"] == {"ok": False}
    assert (await _dispatch(d, "gateway.status", {}))["result"]["generation"] == 0


async def test_reload_relays_the_coordinator_reply_and_the_force_flag() -> None:
    seen: list[bool] = []

    async def _swap(force: bool) -> dict:
        seen.append(force)
        return {"ok": True, "generation": 2, "swap": "pending", "grace_s": 5.0}

    d = Dispatcher()
    register_control_methods(d, channel_manager=None, request_swap=_swap)
    r = (await _dispatch(d, "gateway.reload", {"force": True}))["result"]
    assert r == {"ok": True, "generation": 2, "swap": "pending", "grace_s": 5.0}
    assert seen == [True]


async def test_the_token_gate_closes_a_wrong_first_frame_and_answers_the_right_one() -> None:
    aiohttp = pytest.importorskip("aiohttp")
    from raven.rpc.transports.ws import pick_port

    port = await pick_port(18901)
    d = Dispatcher()
    register_control_methods(d, channel_manager=None, status=lambda: {
        "pid": 1, "started_at": 0.0, "generation": 3, "swap_in_flight": False, "config_path": "", "page": {},
    })
    server = ControlPlaneServer(port, auth_token="s3cret")
    server.bind(d)
    host, bound = await server.start()
    assert (host, bound) == ("127.0.0.1", port)
    url = f"ws://{host}:{bound}/ws"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                await ws.send_str("wrong-token")
                msg = await ws.receive(timeout=5)
                assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
            async with session.ws_connect(url) as ws:
                await ws.send_str("s3cret")
                await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "gateway.status", "params": {}}))
                msg = await ws.receive(timeout=5)
                assert msg.type is aiohttp.WSMsgType.TEXT
                frame = json.loads(msg.data)
                assert frame["id"] == 7 and frame["result"]["generation"] == 3
    finally:
        await server.stop()


async def test_channels_start_switches_the_adapter_and_reports_the_outcome() -> None:
    """gateway.channels.start relays ChannelManager's outcome word verbatim, and
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
    register_control_methods(d, channel_manager=mgr)

    assert (await _dispatch(d, "gateway.channels.start", {"name": "telegram"}))["result"] == {"outcome": "started"}
    assert (await _dispatch(d, "gateway.channels.start", {"name": "telegram", "enabled": False}))["result"] == {
        "outcome": "stopped"
    }
    assert mgr.calls == [("start", "telegram"), ("stop", "telegram")]

    d2 = Dispatcher()
    register_control_methods(d2, channel_manager=None)
    assert (await _dispatch(d2, "gateway.channels.start", {"name": "telegram"}))["result"] == {"outcome": "no_manager"}


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
    register_control_methods(d, channel_manager=_Mgr())

    out = (await _dispatch(d, "gateway.channels.live", {}))["result"]["channels"]
    assert out["weixin"] == {"running": True, "connected": True, "qr_login": True}
    assert out["telegram"] == {"running": True, "connected": None, "qr_login": False}
    assert out["discord"] == {"running": False, "connected": None, "qr_login": False}


async def test_channels_qr_renders_pending_login_code() -> None:
    """gateway.channels.qr renders a channel's pending login QR to a PNG data URI,
    and reports connected from the adapter's own login flag."""

    class _Ch:
        pending_qr: str | None = "https://example.com/login?token=abc"
        is_running = True
        connected = False

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch() if name == "weixin" else None

    d = Dispatcher()
    register_control_methods(d, channel_manager=_Mgr())

    resp = await _dispatch(d, "gateway.channels.qr", {"name": "weixin"})
    r = resp["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["qr_text"] is None  # rasterised, so no raw-payload fallback
    assert r["connected"] is False  # a QR is pending -> not paired yet
    assert r["running"] is True

    _Ch.pending_qr = None
    _Ch.connected = True  # paired
    resp = await _dispatch(d, "gateway.channels.qr", {"name": "weixin"})
    assert resp["result"] == {"qr": None, "qr_text": None, "connected": True, "running": True, "rebind": None}

    resp = await _dispatch(d, "gateway.channels.qr", {"name": "nope"})
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
    register_control_methods(d, channel_manager=_Mgr())

    resp = await _dispatch(d, "gateway.channels.qr", {"name": "whatsapp"})
    assert resp["result"]["connected"] is False
    assert resp["result"]["running"] is True


async def test_channels_qr_falls_back_to_raw_payload_without_qrcode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qrcode ships only with the QR-login extras, so a gateway without it must
    hand the client the raw payload instead of raising ModuleNotFoundError."""
    from raven.rpc import control as mc

    monkeypatch.setattr(mc, "_render_qr_png", lambda text: None)

    class _Ch:
        pending_qr = "https://example.com/login?token=abc"
        is_running = True
        connected = False

    class _Mgr:
        def get_channel(self, name: str):
            return _Ch()

    d = Dispatcher()
    register_control_methods(d, channel_manager=_Mgr())

    r = (await _dispatch(d, "gateway.channels.qr", {"name": "weixin"}))["result"]
    assert r["qr"] is None
    assert r["qr_text"] == "https://example.com/login?token=abc"


# ---------------------------------------------------------------------------
# gateway.channels.qr against the real adapters. The RPC reaches into the channel
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
    register_control_methods(d, channel_manager=_Mgr())

    # Nothing pending yet, and the task being up is not being paired.
    r = (await _dispatch(d, "gateway.channels.qr", {"name": "whatsapp"}))["result"]
    # `rebind` is None for an adapter that does not offer the flow -- whatsapp
    # pairs by QR but has no rebind of its own yet.
    assert r == {"qr": None, "qr_text": None, "connected": False, "running": True, "rebind": None}

    await ch._handle_bridge_message(_json.dumps({"type": "qr", "qr": "2@abc"}))
    r = (await _dispatch(d, "gateway.channels.qr", {"name": "whatsapp"}))["result"]
    assert r["qr"].startswith("data:image/png;base64,")
    assert r["connected"] is False

    await ch._handle_bridge_message(_json.dumps({"type": "status", "status": "connected"}))
    r = (await _dispatch(d, "gateway.channels.qr", {"name": "whatsapp"}))["result"]
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
    register_control_methods(d, channel_manager=_Mgr())

    r = (await _dispatch(d, "gateway.channels.qr", {"name": "weixin"}))["result"]
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
    r = (await _dispatch(d, "gateway.channels.qr", {"name": "weixin"}))["result"]
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
    register_control_methods(d, channel_manager=_OnlyChannel(ch))
    r = (await _dispatch(d, "gateway.channels.qr", {"name": "weixin"}))["result"]
    assert r["rebind"]["phase"] == "waiting"
    assert r["rebind"]["code_age_s"] == 4.0
