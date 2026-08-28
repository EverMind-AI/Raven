"""WebSocket JSON-RPC 2.0 server for the gateway's web channel.

Mirrors :class:`raven.rpc.server.RpcServer`, but the transport is a
WebSocket (aiohttp) instead of a TCP-loopback newline-JSON socket: the web
client connects over a WebSocket. Same JSON-RPC 2.0 framing and
the same :class:`~raven.rpc.dispatcher.Dispatcher`; turn events reach clients
because the :class:`~raven.rpc.subscriptions.SubscriptionEmitter` is
constructed with ``send_frame=<this server>.broadcast``.

Single-user by design: bound to loopback and, if ``auth_token`` is set, the
client MUST send that token as its first WS text message before any RPC frame
(mirrors the RpcServer trust gate — loopback is reachable by any local process).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web
from loguru import logger

from raven.web_rpc.files import add_files_routes

if TYPE_CHECKING:
    from raven.agent.tools._deliverables import DeliverableStore
    from raven.rpc.dispatcher import Dispatcher

# Match rpc.server.MAX_FRAME_BYTES (specs §2.5).
MAX_FRAME_BYTES = 1 * 1024 * 1024  # 1 MiB
_AUTH_TIMEOUT_S = 10.0


class WebSocketRpcServer:
    """Serve JSON-RPC 2.0 over WebSocket at ``ws://<host>:<port>/ws``.

    Wire it with :meth:`bind` (a fully-registered ``Dispatcher``) and pass
    :meth:`broadcast` to the ``SubscriptionEmitter`` so notifications flow out.
    Run :meth:`serve_forever` as a gateway coroutine; :meth:`stop` on teardown.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        auth_token: str | None = None,
        deliverables: "DeliverableStore | None" = None,
    ) -> None:
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._deliverables = deliverables
        self._dispatcher: Dispatcher | None = None
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        # Serialize writes so concurrent dispatch tasks / the coalesce loop can't
        # interleave frames on a single ws (aiohttp forbids concurrent sends).
        self._write_lock = asyncio.Lock()

    def bind(self, dispatcher: Dispatcher) -> None:
        """Attach the dispatcher that routes inbound JSON-RPC frames."""
        self._dispatcher = dispatcher

    # ----- write side -------------------------------------------------------

    async def broadcast(self, frame: dict) -> None:
        """``SendFrame`` for the ``SubscriptionEmitter``: push one JSON-RPC
        notification to every connected client. Single-user, but tolerant of
        0..n connections; a failed/closed socket is dropped, never fatal."""
        data = json.dumps(frame, ensure_ascii=False)
        async with self._write_lock:
            for ws in list(self._clients):
                if ws.closed:
                    self._clients.discard(ws)
                    continue
                try:
                    await ws.send_str(data)
                except Exception:
                    logger.exception("web_rpc: broadcast send failed; dropping client")
                    self._clients.discard(ws)

    # ----- connection handling ---------------------------------------------

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES)
        await ws.prepare(request)

        if self._auth_token is not None and not await self._check_auth(ws):
            await ws.close()
            return ws

        self._clients.add(ws)
        logger.info("web_rpc: client connected ({} total)", len(self._clients))
        # One connection, one identity scope (mirrors WsGateway and RpcServer):
        # each dispatch task below snapshots this context, so a surface declared
        # in this socket's system.hello reaches this socket's turn.send and
        # nobody else's.
        from raven.rpc import connection

        conn_token = connection.bind_connection()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # Dispatch concurrently so a slow/streaming call doesn't block
                    # subsequent reads (mirrors RpcServer's per-frame task).
                    asyncio.create_task(self._handle_frame(ws, msg.data))
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING):
                    break
        finally:
            connection.unbind_connection(conn_token)
            self._clients.discard(ws)
            logger.info("web_rpc: client disconnected ({} remain)", len(self._clients))
        return ws

    async def _check_auth(self, ws: web.WebSocketResponse) -> bool:
        """First WS text message must equal the shared secret."""
        try:
            first = await ws.receive(timeout=_AUTH_TIMEOUT_S)
        except Exception:
            logger.error("web_rpc: auth token not received; closing connection")
            return False
        if first.type != WSMsgType.TEXT or first.data.strip() != self._auth_token:
            logger.error("web_rpc: auth token mismatch; closing connection")
            return False
        return True

    async def _handle_frame(self, ws: web.WebSocketResponse, raw: str) -> None:
        if self._dispatcher is None:
            return
        try:
            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                await self._send(
                    ws,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse_error", "data": {"reason": str(exc)}},
                    },
                )
                return
            response = await self._dispatcher.dispatch(frame)
            # JSON-RPC notification (no id) → suppress the response.
            if isinstance(frame, dict) and "id" not in frame:
                return
            await self._send(ws, response)
        except Exception:
            logger.exception("web_rpc: _handle_frame failed")

    async def _send(self, ws: web.WebSocketResponse, frame: dict) -> None:
        data = json.dumps(frame, ensure_ascii=False)
        async with self._write_lock:
            if not ws.closed:
                await ws.send_str(data)

    # ----- lifecycle --------------------------------------------------------

    async def serve_forever(self) -> None:
        """Start the aiohttp WS site and block until cancelled (gateway coroutine)."""
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        add_files_routes(app, self._deliverables)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("web_rpc: WebSocket server on ws://{}:{}/ws", self._host, self._port)
        try:
            await asyncio.Event().wait()  # run until the task is cancelled
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Close every client and tear down the aiohttp runner."""
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("web_rpc: WebSocket server stopped")


__all__ = ["WebSocketRpcServer", "MAX_FRAME_BYTES"]
