"""Real PTY output replay and keyboard input over the authenticated RPC gateway."""

import asyncio
import json
import os
import sys

import aiohttp
import pytest
from aiohttp import web

from raven.agent.registry.identity import IdentityRegistry
from raven.cli import _terminal_rpc
from raven.rpc.dispatcher import Dispatcher
from raven.rpc.subscriptions import SubscriptionEmitter
from raven.rpc.terminal_services import TerminalServices
from raven.rpc.terminal_stream import decode_frame
from raven.rpc.transports.ws import WsGateway, build_app

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX PTY host")


async def test_cli_and_stream_share_one_real_host(tmp_path, monkeypatch):
    gateway = WsGateway()
    gateway.dispatcher = Dispatcher()
    emitter = SubscriptionEmitter(gateway.broadcast)
    identities = IdentityRegistry(
        tmp_path / "identities.json", config_rows=lambda: [{"name": "Raven", "kind": "builtin"}]
    )
    services = TerminalServices(emitter, gateway.broadcast, identities=identities)
    services.register(gateway.dispatcher)
    app = build_app(gateway, None)
    services.attach(app, 0)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    metadata = tmp_path / "serve.json"
    metadata.write_text(json.dumps({"port": port, "token": gateway.session_token}))
    monkeypatch.setattr(_terminal_rpc, "state_path", lambda: metadata)
    try:
        created = await _terminal_rpc.request(
            "terminal.create",
            {
                "worktree_id": f"repo::{tmp_path}",
                "title": "rpc-smoke",
                "command": [
                    sys.executable,
                    "-u",
                    "-c",
                    "import sys; print('READY'); [print('ECHO:' + line.strip()) for line in sys.stdin]",
                ],
            },
        )
        assert created["ok"], created
        record = created["result"]["terminal"]
        handle = record["handle"]
        assert record["connected"] and not record["visible"]
        async with asyncio.timeout(5):
            while b"READY" not in services.host.raw_snapshot(handle):
                await asyncio.sleep(0.01)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"http://127.0.0.1:{port}/rpc", headers={"X-Raven-Token": gateway.session_token}
            ) as ws:
                await ws.send_json(
                    {"jsonrpc": "2.0", "id": 1, "method": "terminal.subscribe", "params": {"handle": handle}}
                )
                replay = bytearray()
                async with asyncio.timeout(5):
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            header, data = decode_frame(message.data)
                            assert header["replay"] is True
                            replay.extend(data)
                        elif message.json().get("id") == 1:
                            assert "result" in message.json()
                            break
                assert b"READY" in replay
                listed = await _terminal_rpc.request("terminal.list", {"include_visual_layouts": True})
                assert listed["ok"] and listed["_meta"] == created["_meta"]
                assert listed["result"]["terminals"][0]["visible"] is True
                typed = await _terminal_rpc.request("terminal.input", {"handle": handle, "data": "hello\n"})
                assert typed["ok"], typed
                live = bytearray()
                seq = 0
                async with asyncio.timeout(5):
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.BINARY:
                            continue
                        header, data = decode_frame(message.data)
                        assert not header.get("replay")
                        assert header["seq"] == seq + len(data)
                        seq = header["seq"]
                        live.extend(data)
                        if b"ECHO:hello" in live:
                            break
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "terminal.subscribe",
                        "params": {"handle": handle, "ack": seq},
                    }
                )
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "terminal.subscribe",
                        "params": {"handle": handle, "enabled": False},
                    }
                )
                async with asyncio.timeout(5):
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT and message.json().get("id") == 3:
                            assert message.json()["result"]["subscription"]["enabled"] is False
                            break
                assert handle not in services.stream.outputs
                shown = await _terminal_rpc.request("terminal.show", {"handle": handle})
                assert shown["result"]["terminal"]["connected"] is True
                assert shown["result"]["terminal"]["visible"] is False
        closed = await _terminal_rpc.request("terminal.close", {"handle": handle})
        assert closed["ok"] and closed["result"]["close"]["closed"]
    finally:
        await services.shutdown()
        await runner.cleanup()
