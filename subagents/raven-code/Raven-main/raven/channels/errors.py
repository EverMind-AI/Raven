"""Outbound send-error classification shared by channel adapters.

``manager._send_with_retry`` backs off only on raised exceptions, so adapters
re-raise TRANSIENT failures (network drop, timeout, 5xx) and keep swallowing
permanent ones (4xx, bad payloads, auth) — retrying those would only repeat
the failure or duplicate side effects.
"""

from __future__ import annotations

import httpx

_transient_bases: list[type[BaseException]] = [TimeoutError, ConnectionError]
try:
    from websockets.exceptions import WebSocketException

    _transient_bases.append(WebSocketException)
except ImportError:
    pass
try:
    import aiohttp

    _transient_bases.append(aiohttp.ClientError)
except ImportError:
    pass
try:
    import requests

    _transient_bases.append(requests.exceptions.ConnectionError)
    _transient_bases.append(requests.exceptions.Timeout)
except ImportError:
    pass

TRANSIENT_NETWORK_ERRORS: tuple[type[BaseException], ...] = tuple(_transient_bases)


def transient_network(err: BaseException) -> bool:
    """True for network-ish failures worth a manager retry (connection drops,
    timeouts, websocket closes). SDK business errors stay outside on purpose."""
    return isinstance(err, TRANSIENT_NETWORK_ERRORS)


def retryable_http(err: Exception) -> bool:
    """httpx flavor: timeouts / transport errors and 5xx responses."""
    if isinstance(err, httpx.TimeoutException | httpx.TransportError):
        return True
    if isinstance(err, httpx.HTTPStatusError):
        return bool(err.response is not None and err.response.status_code >= 500)
    return False
