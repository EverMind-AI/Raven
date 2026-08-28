"""Ask the running gateway what a channel is actually doing.

A channel adapter is a live object owned by the ``raven gateway`` process, so
no other process can read it. Every other surface therefore reported the config
file's ``enabled`` flag as if it were a connection: written true, drawn
"connected", whether or not a single message could pass.

This is the thin client that closes that gap. It finds the gateway the same way
``doctor`` does -- the lock payload, which now carries the endpoint and a
per-boot token -- and asks it. Deliberately small: one request per call, no
subscriptions, no reconnect ladder. Every failure answers ``None``, which the
caller renders as "unknown", never as "disconnected": a probe that cannot reach
the gateway knows less than nothing about the channels, and saying "not
connected" would be the same lie in a new place.

The answer is cached for a beat because the status poll is on a timer and the
underlying facts move on the order of seconds, not milliseconds.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

_CACHE_TTL_S = 3.0
_CONNECT_TIMEOUT_S = 1.5
_CALL_TIMEOUT_S = 2.5

_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _endpoint() -> tuple[str, str] | None:
    """``(ws_url, token)`` for the live gateway, or None when none is running."""
    try:
        from raven.cli._gateway_lock import read_status

        info = read_status(time.time())
    except Exception:
        return None
    if info is None or not info.web_url:
        return None
    return info.web_url, info.web_token


async def _ask(url: str, token: str, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        import aiohttp
    except ImportError:
        return None
    timeout = aiohttp.ClientTimeout(total=_CONNECT_TIMEOUT_S + _CALL_TIMEOUT_S)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, session.ws_connect(url) as ws:
            if token:
                # The token is the first text frame, before any RPC -- the same
                # trust gate the TUI's transport uses.
                await ws.send_str(token)
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
            async with asyncio.timeout(_CALL_TIMEOUT_S):
                async for msg in ws:
                    if msg.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    frame = json.loads(msg.data)
                    if isinstance(frame, dict) and frame.get("id") == 1:
                        return frame.get("result") if isinstance(frame.get("result"), dict) else None
    except Exception:
        return None
    return None


async def channel_liveness() -> dict[str, dict[str, Any]] | None:
    """``{name: {running, connected, qr_login}}``, or None if nobody answered.

    ``connected`` is null for a channel that does not report a pairing; see
    ``raven.channels.live`` on the gateway for why that is not ``false``.
    """
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL_S:
        return _cache[1]
    endpoint = _endpoint()
    if endpoint is None:
        return None
    result = await _ask(endpoint[0], endpoint[1], "raven.channels.live", {})
    channels = (result or {}).get("channels")
    if not isinstance(channels, dict):
        return None
    _cache = (time.monotonic(), channels)
    return channels


async def channel_qr(name: str) -> dict[str, Any] | None:
    """The pending login QR for one channel, uncached -- it is short-lived and
    only asked for while a dialog is open."""
    endpoint = _endpoint()
    if endpoint is None:
        return None
    return await _ask(endpoint[0], endpoint[1], "raven.channels.qr", {"name": name})


async def channel_start(name: str, *, enabled: bool = True) -> str | None:
    """Ask the gateway to start (or stop) one channel's adapter now.

    Answers the gateway's outcome word, or None when no gateway answered --
    which is not a failure to report as one: with nothing running there is no
    adapter to start, and the config write the caller just made is what the
    next launch reads.
    """
    endpoint = _endpoint()
    if endpoint is None:
        return None
    result = await _ask(endpoint[0], endpoint[1], "raven.channels.start", {"name": name, "enabled": enabled})
    outcome = (result or {}).get("outcome")
    return str(outcome) if outcome else None


def reset_cache() -> None:
    """Drop the cached liveness. For tests, and for a caller that just changed
    a channel and wants the next read to be fresh."""
    global _cache
    _cache = None


__all__ = ["channel_liveness", "channel_qr", "channel_start", "reset_cache"]
