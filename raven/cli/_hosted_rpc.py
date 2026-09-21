"""One RPC call into the raven that is serving the page, from a terminal.

A stint's rounds run inside whichever process holds the driver. A CLI verb
that opens a round -- ``stints resume``, ``stints extend`` -- used to build a
driver of its own and hold the terminal while the round ran, which is right on
a machine with no raven running and wrong on one with a page up: the round then
runs in a shell process nobody is watching, reports to nobody, and dies with
the shell (measured 2026-09-20: a model, told to resume, ran the verb through
its exec tool, the tool's timeout killed it, and the round with it).

So the verb asks first whether a page is being served, and if so sends the
call there. The page's raven has the driver, the conversation the stint was
started in, and the announcer that conversation reads.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

_HEALTH_TIMEOUT_S = 2.0
_CALL_TIMEOUT_S = 120.0


def hosted_page() -> tuple[int, str] | None:
    """(port, token) of a live raven serving the page, whichever binary it is.

    Three facts have to line up, as they do for `raven web`: serve.json names a
    pid and a port, the pid is alive, and the port's ``/health`` answers as
    ``raven-serve``. A stale file left by a killed serve reads as None.
    """
    from raven.cli.serve_commands import _pid_alive, _state_path

    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        pid, port, token = int(data["pid"]), int(data["port"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid <= 0 or port <= 0 or not token or not _pid_alive(pid):
        return None
    if not asyncio.run(_healthy(port)):
        return None
    return port, token


async def _healthy(port: int) -> bool:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                return resp.status == 200 and (await resp.json()).get("service") == "raven-serve"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False


def call(port: int, token: str, method: str, params: dict[str, Any], *, timeout_s: float = _CALL_TIMEOUT_S) -> Any:
    """Send one JSON-RPC request over the page's ``/rpc`` socket and return its result.

    Raises ``RuntimeError`` with the server's own message when the call is
    refused, so a CLI verb can print exactly what the page would have shown.
    """
    return asyncio.run(_call(port, token, method, params, timeout_s))


async def _call(port: int, token: str, method: str, params: dict[str, Any], timeout_s: float) -> Any:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(f"http://127.0.0.1:{port}/rpc", headers={"X-Raven-Token": token}) as ws:
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                frame = json.loads(message.data)
                # Notifications and other sockets' traffic carry no id, or not ours.
                if frame.get("id") != 1:
                    continue
                if "error" in frame:
                    error = frame["error"] or {}
                    raise RuntimeError(str(error.get("message") or error))
                return frame.get("result")
    raise RuntimeError(f"the page's raven closed the socket before answering {method}")
