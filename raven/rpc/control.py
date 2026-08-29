"""The gateway control plane: the daemon's window for other raven processes.

A channel adapter is a live object owned by the ``raven gateway`` process, and
so is the running generation. Facts about them (is this channel actually
paired? what is the login QR? which generation is serving?) and commands on
them (start this adapter now, rebuild from config, stop) can only be answered
inside that process, so every other surface -- the terminal, the served page,
the CLI -- reaches them through this plane. The client side is
:mod:`raven.gateway.live_probe`, which discovers the endpoint through the
gateway lock payload; this module is the server side.

Charter (the whole vocabulary, pinned by ``tests/test_rpc_control.py``):

- runtime facts: ``gateway.channels.live``, ``gateway.channels.qr``,
  ``gateway.status``;
- host commands: ``gateway.channels.start``, ``gateway.reload``,
  ``gateway.shutdown``.

Never on this plane: turn or chat streams (those are the terminal dialect's,
over the page or TUI transports) and config writes (those go through the
``raven.config.update_*`` writers; ``gateway.reload`` is how a write takes
effect).

Trust: loopback only, a per-boot token sent as the first WebSocket text frame,
published beside the gateway lock with mode 0600. On POSIX that is the same
uid that could already signal the process; on Windows the payload's
protection is the profile directory's ACL. The internal contract is the
Pydantic models below plus the tests -- no OpenRPC document, no codegen: the
only client is another raven process.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from raven.rpc.errors import RpcError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

# Match rpc.server.MAX_FRAME_BYTES (specs 2.5).
MAX_FRAME_BYTES = 1 * 1024 * 1024
_AUTH_TIMEOUT_S = 10.0

CHARTER: frozenset[str] = frozenset(
    {
        "gateway.channels.live",
        "gateway.channels.qr",
        "gateway.channels.start",
        "gateway.reload",
        "gateway.status",
        "gateway.shutdown",
    }
)


class _Strict(BaseModel):
    """Base class for control-plane models: no extra fields, no lax coercion.

    Strict on purpose -- the models are the contract, so a string "false"
    for a boolean is a -32602, not a forced reload.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class RebindState(_Strict):
    """How a QR channel's rebind is going, carried on the QR poll the client
    already makes.

    ``code_age_s`` is an age rather than a deadline because the two sides do not
    share a clock; the client compares it against ``max_refreshes`` behaviour it
    can see, not against a timestamp it has to trust.
    """

    phase: str = Field(
        ...,
        description="idle | waiting | scanned | confirmed | failed | cancelled.",
    )
    refreshes: int = Field(..., description="Codes reissued so far, against max_refreshes.")
    max_refreshes: int
    code_age_s: float | None = Field(
        ..., description="Seconds since the current code was issued; null when none is up."
    )
    detail: str = Field(..., description="Why it failed, when it did: expired | no_token | error.")



class ChannelLive(_Strict):
    running: bool
    connected: bool | None = Field(
        ..., description="Three-valued: null means the channel does not report a pairing at all."
    )
    qr_login: bool



class ChannelsQrParams(_Strict):
    name: str | None = None



class ChannelsQrResult(_Strict):
    qr: str | None = Field(..., description="Login QR as a PNG data URI, or null when none is pending.")
    qr_text: str | None = Field(..., description="Raw scan payload when the gateway cannot rasterise a PNG.")
    connected: bool
    running: bool
    rebind: RebindState | None = Field(None, description="Null for a channel that does not offer rebinding.")



class ChannelsStartParams(_Strict):
    name: str | None = None
    enabled: bool | None = Field(None, description="False stops the adapter; anything else starts it.")



class ChannelsStartResult(_Strict):
    outcome: str = Field(
        ...,
        description=(
            "started | already | stopped | absent | disabled | deny_all | missing_dep | unknown | no_manager."
        ),
    )



class ChannelsLiveParams(_Strict):
    pass



class ChannelsLiveResult(_Strict):
    channels: dict[str, ChannelLive]



class ReloadParams(_Strict):
    force: bool = Field(False, description="Swap even while sub-agents or questions are in flight.")


class ReloadResult(_Strict):
    ok: bool
    reason: str | None = Field(
        None,
        description="swap_in_flight | too_soon | busy | build_failed when ok is false.",
    )
    generation: int | None = Field(None, description="The generation that will serve after the swap.")
    swap: str | None = Field(None, description="'pending': the running generation stops within ~1s.")
    grace_s: float | None = Field(None, description="Seconds in-flight turns get before they are cancelled.")
    error: str | None = None
    subagents: int | None = None
    questions: int | None = None


class StatusParams(_Strict):
    pass


class StatusResult(_Strict):
    pid: int
    started_at: float
    generation: int
    swap_in_flight: bool
    config_path: str
    page: dict[str, Any] = Field(default_factory=dict, description="{mounted, url} of the served page.")


class ShutdownParams(_Strict):
    pass


class ShutdownResult(_Strict):
    ok: bool


class InvalidControlParamsError(RpcError):
    """Params that do not fit the method's declared model (JSON-RPC -32602)."""

    CODE = -32602
    MESSAGE = "invalid_params"


def _params(model: type[BaseModel], params: dict) -> BaseModel:
    try:
        return model.model_validate(params or {})
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        raise InvalidControlParamsError(str(first.get("msg", "invalid params")), data={"errors": exc.errors()}) from exc


CONTROL_METHOD_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "gateway.channels.qr": (ChannelsQrParams, ChannelsQrResult),
    "gateway.channels.start": (ChannelsStartParams, ChannelsStartResult),
    "gateway.channels.live": (ChannelsLiveParams, ChannelsLiveResult),
    "gateway.reload": (ReloadParams, ReloadResult),
    "gateway.status": (StatusParams, StatusResult),
    "gateway.shutdown": (ShutdownParams, ShutdownResult),
}


def _render_qr_png(text: str) -> str | None:
    """A scan payload as a PNG data URI, so a client shows the login QR with no
    QR library of its own.

    ``qrcode`` only ships with the QR-login channel extras, so an install that
    enabled such a channel some other way still has to degrade rather than 500:
    None tells the caller to fall back to handing the raw payload to the client.
    """
    import base64
    import io

    try:
        import qrcode
    except ImportError:
        return None

    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def register_control_methods(
    dispatcher: "Dispatcher",
    *,
    channel_manager: Any,
    request_swap: Callable[[bool], Awaitable[dict[str, Any]]] | None = None,
    status: Callable[[], dict[str, Any]] | None = None,
    shutdown: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Register exactly the charter on ``dispatcher``.

    The host-command callables are the gateway's own closures (they need the
    swap coordinator and the serving loop); a plane built without them answers
    those methods with ``ok: false`` rather than not existing, so the charter
    is the same set on every gateway.
    """
    from raven.config.update_channels import channel_names

    async def _channels_qr(params: dict) -> dict:
        """Login QR for a QR-login channel (weixin/whatsapp) as a PNG data URI,
        plus whether the account is actually paired. Empty when there is no live
        manager, no such channel, or nothing pending (e.g. already paired).

        ``connected`` reads the adapter's own login state, never "the task is
        running and no QR is pending yet" -- both adapters flip ``_running``
        before the first QR is fetched, so inferring it would report an unpaired
        channel as connected during that window and stop the UI from polling.
        ``qr_text`` carries the raw payload for the client to render when the
        gateway has no ``qrcode`` to rasterise with.
        """
        name = _params(ChannelsQrParams, params).name or ""
        ch = channel_manager.get_channel(name) if channel_manager is not None else None
        qr = getattr(ch, "pending_qr", None) if ch is not None else None
        running = ch is not None and bool(getattr(ch, "is_running", False))
        png = _render_qr_png(qr) if qr else None
        # The rebind snapshot rides this poll rather than needing its own: the
        # client is already asking once a second for the code, and phase + code
        # age are what turn "a picture" into "scan it, it expires, we reissued".
        rebind = ch.rebind_state() if ch is not None and hasattr(ch, "rebind_state") else None
        return {
            "qr": png,
            "qr_text": qr if qr and png is None else None,
            "connected": running and bool(getattr(ch, "connected", False)),
            "running": running,
            "rebind": rebind,
        }

    async def _channels_start(params: dict) -> dict:
        """Start (or stop) one channel's adapter now, without a gateway restart.

        The switch is written by whichever process the reader is talking to --
        the page's ``raven serve``, the TUI, the CLI -- and the adapter only
        exists here. So enabling a channel used to mean nothing until the next
        launch: no adapter, no QR to scan, no messages. ``outcome`` is a word
        from ChannelManager, every value of which is a state the caller draws.
        """
        args = _params(ChannelsStartParams, params)
        name = args.name or ""
        want_on = args.enabled
        if channel_manager is None:
            return {"outcome": "no_manager"}
        if want_on is False:
            return {"outcome": await channel_manager.stop_one(name)}
        return {"outcome": await channel_manager.start_one(name)}

    async def _channels_live(params: dict) -> dict:
        """Every channel's runtime state in one round trip, for a client drawing
        a list of them.

        ``connected`` is deliberately three-valued. Only the QR adapters
        (weixin, whatsapp) know whether an account is paired; the rest expose
        ``is_running`` and nothing more. Reporting ``false`` for those would say
        "not connected" about a Telegram bot that is happily serving messages --
        the same lie as reading the config flag, wearing a different hat. Null
        means the channel does not report a pairing, and the caller must not
        render it as a negative.
        """
        out: dict[str, Any] = {}
        for name in channel_names():
            ch = channel_manager.get_channel(name) if channel_manager is not None else None
            if ch is None:
                out[name] = {"running": False, "connected": None, "qr_login": False}
                continue
            reports_pairing = hasattr(type(ch), "connected")
            out[name] = {
                "running": bool(getattr(ch, "is_running", False)),
                "connected": bool(getattr(ch, "connected", False)) if reports_pairing else None,
                "qr_login": hasattr(ch, "pending_qr"),
            }
        return {"channels": out}

    async def _reload(params: dict) -> dict:
        """Rebuild the runtime from config and swap it in.

        The reply describes what actually happens: the candidate is built
        before the reply (a config that does not assemble answers
        build_failed and leaves the serving generation alone); once built,
        the serving generation stops within about a second, in-flight turns
        get ``grace_s`` and are then cancelled, sub-agents and pending
        questions are cancelled, and a mounted page reconnects. ``busy`` is
        the refusal for a gateway with work in flight; ``force`` overrides it.
        """
        args = _params(ReloadParams, params)
        if request_swap is None:
            return {"ok": False, "reason": "unavailable"}
        return await request_swap(args.force)

    async def _status(params: dict) -> dict:
        if status is None:
            return {"pid": 0, "started_at": 0.0, "generation": 0, "swap_in_flight": False, "config_path": ""}
        return status()

    async def _shutdown(params: dict) -> dict:
        """Graceful stop: the same teardown chain Ctrl-C runs, reachable
        without signal rights (and on platforms without SIGTERM semantics)."""
        if shutdown is None:
            return {"ok": False}
        await shutdown()
        return {"ok": True}

    dispatcher.register("gateway.channels.qr", _channels_qr)
    dispatcher.register("gateway.channels.start", _channels_start)
    dispatcher.register("gateway.channels.live", _channels_live)
    dispatcher.register("gateway.reload", _reload)
    dispatcher.register("gateway.status", _status)
    dispatcher.register("gateway.shutdown", _shutdown)


class ControlPlaneServer:
    """JSON-RPC 2.0 over WebSocket at ``ws://127.0.0.1:<port>/ws``, loopback only.

    Same framing and the same :class:`~raven.rpc.dispatcher.Dispatcher` as the
    terminal transports; the token is the first text frame. Process-lifetime:
    the gateway binds it once with a dispatcher registered once -- nothing on
    this plane depends on the generation, so nothing here rebinds at a swap.
    :meth:`start` binds and records the actual port; the endpoint is published
    only after that, so the lock never advertises a port that failed to bind.
    """

    def __init__(self, port: int, *, auth_token: str, host: str = "127.0.0.1") -> None:
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._dispatcher: Dispatcher | None = None
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._bound: tuple[str, int] | None = None
        # Serialize writes: aiohttp forbids concurrent sends on one ws.
        self._write_lock = asyncio.Lock()

    def bind(self, dispatcher: "Dispatcher") -> None:
        self._dispatcher = dispatcher

    @property
    def bound(self) -> tuple[str, int] | None:
        """``(host, port)`` actually listening, once :meth:`start` returned."""
        return self._bound

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        # No browser is a legitimate client of this plane: an Origin header
        # means a page is trying to reach it, and the answer is no before the
        # upgrade, whatever token it might go on to send.
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(reason="control plane accepts no browser origin")
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES)
        await ws.prepare(request)
        if not await self._check_auth(ws):
            await ws.close()
            return ws
        self._clients.add(ws)
        pending: set[asyncio.Task] = set()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    task = asyncio.create_task(self._handle_frame(ws, msg.data))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING):
                    break
        finally:
            for task in pending:
                task.cancel()
            self._clients.discard(ws)
        return ws

    async def _check_auth(self, ws: web.WebSocketResponse) -> bool:
        """The first text frame must equal the per-boot token."""
        try:
            first = await ws.receive(timeout=_AUTH_TIMEOUT_S)
        except Exception:
            logger.error("control plane: token not received; closing connection")
            return False
        if first.type != WSMsgType.TEXT or not secrets.compare_digest(first.data.strip(), self._auth_token):
            logger.error("control plane: token mismatch; closing connection")
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
            if isinstance(frame, dict) and "id" not in frame:
                return
            await self._send(ws, response)
        except Exception:
            logger.exception("control plane: frame handling failed")

    async def _send(self, ws: web.WebSocketResponse, frame: dict) -> None:
        data = json.dumps(frame, ensure_ascii=False)
        async with self._write_lock:
            if not ws.closed:
                await ws.send_str(data)

    async def start(self) -> tuple[str, int]:
        """Bind the site; returns the ``(host, port)`` actually listening."""
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        # Read the port back from the listening socket so port 0 works and
        # the published endpoint is the one actually bound.
        port = self._port
        for sock in getattr(getattr(site, "_server", None), "sockets", None) or ():
            try:
                port = int(sock.getsockname()[1])
                break
            except Exception:
                continue
        self._bound = (self._host, port)
        logger.info("control plane: listening on ws://{}:{}/ws", self._host, port)
        return self._bound

    async def serve(self) -> None:
        """Hold the site open until cancelled (a gateway coroutine)."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("control plane: stopped")


__all__ = ["CHARTER", "CONTROL_METHOD_MODELS", "ControlPlaneServer", "register_control_methods"]
