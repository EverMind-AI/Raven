"""Socket.IO transport for the Mochat channel — a dumb pipe.

Owns the socket.io client lifecycle (build, connect, request, close) and
registers the channel-provided event handlers on the client. Every decision —
what connect/disconnect means, event routing, subscribe payloads, the polling
fallback — lives in those handlers on the channel side; the transport never
calls back into the channel beyond invoking them.

Ordering contract (pinned by tests): the client is held — ``request()`` works —
BEFORE ``client.connect()`` is awaited. socket.io fires the "connect" event
during the handshake, and the channel's connect handler subscribes via
``request()`` at that very moment; assigning the client only after connect()
returned would make every handshake-time subscribe fail with "socket not
connected". On a failed connect the client reference is cleared again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

try:
    import socketio

    SOCKETIO_AVAILABLE = True
except ImportError:
    socketio = None
    SOCKETIO_AVAILABLE = False

try:
    import msgpack  # noqa: F401

    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False

EventHandler = Callable[..., Awaitable[None]]


class SocketTransport:
    """Dumb socket.io pipe: connect / request / close + handler registration."""

    def __init__(self, config: Any, handlers: dict[str, EventHandler]):
        self._config = config
        self._handlers = handlers
        self._client: Any = None

    def _make_client(self) -> Any:
        serializer = "default"
        if not self._config.socket_disable_msgpack:
            if MSGPACK_AVAILABLE:
                serializer = "msgpack"
            else:
                logger.warning("msgpack not installed but socket_disable_msgpack=false; using JSON")
        return socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=self._config.max_retry_attempts or None,
            reconnection_delay=max(0.1, self._config.socket_reconnect_delay_ms / 1000.0),
            reconnection_delay_max=max(0.1, self._config.socket_max_reconnect_delay_ms / 1000.0),
            logger=False,
            engineio_logger=False,
            serializer=serializer,
        )

    async def connect(self) -> bool:
        if not SOCKETIO_AVAILABLE:
            logger.warning("python-socketio not installed, Mochat using polling fallback")
            return False

        client = self._make_client()
        # Handlers must be coroutine functions so the AsyncClient awaits them;
        # plain lambdas returning a coroutine would be left un-awaited.
        for event_name, handler in self._handlers.items():
            client.on(event_name, handler)

        url = (self._config.socket_url or self._config.base_url).strip().rstrip("/")
        path = (self._config.socket_path or "/socket.io").strip().lstrip("/")
        self._client = client  # before connect() — see the ordering contract above
        try:
            await client.connect(
                url,
                transports=["websocket"],
                socketio_path=path,
                auth={"token": self._config.claw_token},
                wait_timeout=max(1.0, self._config.socket_connect_timeout_ms / 1000.0),
            )
            return True
        except Exception as e:
            logger.error("Failed to connect Mochat websocket: {}", e)
            try:
                await client.disconnect()
            except Exception:
                pass
            self._client = None
            return False

    async def request(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._client:
            return {"result": False, "message": "socket not connected"}
        try:
            raw = await self._client.call(event_name, payload, timeout=10)
        except Exception as e:
            return {"result": False, "message": str(e)}
        return raw if isinstance(raw, dict) else {"result": True, "data": raw}

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
