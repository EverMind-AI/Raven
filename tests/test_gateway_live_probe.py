"""The control-plane client, driven against a real control plane.

``live_probe`` is what ``raven gateway status|reload|stop`` calls: one websocket
request per call, the per-boot token as the first frame. Its single network path
carries every one of those commands, so it is exercised here against the server
from :mod:`raven.rpc.control` rather than a stub.
"""

from __future__ import annotations

import pytest

from raven.gateway import live_probe
from raven.rpc.control import ControlPlaneServer, register_control_methods
from raven.rpc.dispatcher import Dispatcher

pytestmark = pytest.mark.asyncio

_STATUS = {
    "pid": 4242,
    "started_at": 1.0,
    "generation": 2,
    "swap_in_flight": False,
    "config_path": "/tmp/config.json",
    "page": {},
}


@pytest.fixture
async def plane(monkeypatch):
    """A control plane on a loopback port, with the probe pointed at it."""
    stopped: list[bool] = []
    swaps: list[bool] = []

    async def _swap(force: bool) -> dict:
        swaps.append(force)
        return {"ok": True, "generation": 3}

    async def _shutdown() -> None:
        stopped.append(True)

    dispatcher = Dispatcher()
    register_control_methods(
        dispatcher,
        channel_manager=None,
        request_swap=_swap,
        status=lambda: _STATUS,
        shutdown=_shutdown,
    )
    server = ControlPlaneServer(0, auth_token="tok")
    server.bind(dispatcher)
    host, port = await server.start()
    monkeypatch.setattr(live_probe, "_endpoint", lambda: (f"ws://{host}:{port}/ws", "tok"))
    live_probe.reset_cache()
    try:
        yield swaps, stopped, (host, port)
    finally:
        await server.stop()
        live_probe.reset_cache()


async def test_status_reads_the_running_generation(plane) -> None:
    assert (await live_probe.status())["generation"] == 2


async def test_reload_relays_the_force_flag_and_the_answer(plane) -> None:
    swaps, _stopped, _addr = plane
    assert (await live_probe.reload(force=True)) == {"ok": True, "generation": 3}
    assert swaps == [True]


async def test_shutdown_reports_that_the_gateway_took_it(plane) -> None:
    _swaps, stopped, _addr = plane
    assert await live_probe.shutdown() is True
    assert stopped == [True]


async def test_a_wrong_token_answers_nothing_rather_than_raising(plane, monkeypatch) -> None:
    _swaps, _stopped, (host, port) = plane
    monkeypatch.setattr(live_probe, "_endpoint", lambda: (f"ws://{host}:{port}/ws", "wrong"))
    assert await live_probe.status() is None


async def test_no_gateway_means_no_answer(monkeypatch) -> None:
    monkeypatch.setattr(live_probe, "_endpoint", lambda: None)
    live_probe.reset_cache()
    assert await live_probe.status() is None
    assert await live_probe.shutdown() is False
