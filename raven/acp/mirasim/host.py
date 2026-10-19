"""The adapter's one connection to the Mirasim host already running on this machine.

Mirasim's desktop and phone apps are clients of a *host*: one long-running
server that runs the agents, keeps the sessions and speaks JSON frames over a
WebSocket at ``/ws``. This adapter is one more client of it -- it never starts a
host of its own, because two hosts over one ``~/.mirasim`` contend for the same
sessions and the same machine identity. Where the host listens, in the order
:func:`locate_host` looks:

- ``RAVEN_MIRASIM_HOST``: ``unix:<path>`` for a socket, ``<port>`` or
  ``127.0.0.1:<port>`` for a loopback port, when the host is somewhere else;
- ``~/.mirasim-remote/run/server.sock``: the host Mirasim runs on a machine it
  reaches over SSH (POSIX only). The same user is trusted there, no token;
- ``127.0.0.1:4970``: the installed desktop app's server, whose access token is
  ``local-4970.token`` under Mirasim's state directory (``MIRASIM_HOME``,
  else ``~/.mirasim``) -- the file ``mirasim ui-cli --port`` reads.

Two frames are never sent, because the answers carry secrets: ``ready`` makes
the host push its whole settings tree, IM bot tokens included, and
``getConfig`` answers with the configuration. No frame is logged whole --
only the reason a refusal gives, which names what failed -- and the token
travels in a header, never in a URL.

One connection serves every session of the adapter. A frame comes back to its
request by what the host echoes, which differs per frame: ``clientRef`` on
``accepted`` / ``error`` / ``sessionDeleteResult``, ``reqId`` on ``sessions`` and
``history``, only ``agent`` on ``catalog`` and ``commands``, nothing on
``clis`` -- so :meth:`HostConnection.request` takes a predicate rather than a
key. Every frame, answered or not, also goes to ``on_frame``. A connection that
drops is made again in the background while ``wanted`` says something depends
on it; a turn the host is running goes on regardless, and the sessions are
subscribed to again once it is back (``on_connect``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

HOST_ENV = "RAVEN_MIRASIM_HOST"
DESKTOP_PORT = 4970
UNIX_SOCKETS = os.name != "nt"
"""Whether asyncio can open a unix socket here: on Windows neither of its event loops can."""

CONNECT_TIMEOUT_S = 10.0
HEALTH_TIMEOUT_S = 5.0
STREAM_CADENCE_MS = 150
"""How long the host may merge a session's patches before sending them.

Measured on 0.0.465: a 98-second Claude Code turn sent 4,015 frames uncoalesced,
text two to four characters at a time; at 150 ms text arrives six to twenty-four
characters at a time and a short turn takes a dozen frames."""
MAX_FRAME_BYTES = 64 * 1024 * 1024
RECONNECT_PAUSES_S = (0.2, 0.5, 1.0, 2.0, 5.0)

NO_HOST = (
    "No Mirasim host is running on this machine: start the Mirasim app (or let it reach this machine), "
    f"or name where the host listens in {HOST_ENV}."
)


class HostUnavailableError(Exception):
    """The host could not be reached, or the connection to it dropped."""


@dataclass(frozen=True)
class HostAddress:
    """Where a host listens: a unix socket, or a port on the loopback with the token it asks for."""

    socket: str | None = None
    port: int | None = None
    token: str | None = field(default=None, repr=False)
    host: str = "127.0.0.1"
    """The loopback name the port is reached on, as the address named it: ``localhost`` and ``[::1]`` stay so."""

    @property
    def base_url(self) -> str:
        # Over a unix socket the host part is never resolved; it names the request.
        return "http://localhost" if self.socket else f"http://{self.host}:{self.port}"

    def describe(self) -> str:
        return f"unix socket {self.socket}" if self.socket else f"{self.host}:{self.port}"


def mirasim_home(env: Mapping[str, str] | None = None) -> Path:
    """Mirasim's state directory: ``MIRASIM_HOME``, else ``~/.mirasim``."""
    named = (env if env is not None else os.environ).get("MIRASIM_HOME", "").strip()
    return Path(named).expanduser() if named else Path.home() / ".mirasim"


def remote_socket() -> Path:
    """Where the host Mirasim runs on a machine it reaches over SSH listens; fixed under the home."""
    return Path.home() / ".mirasim-remote" / "run" / "server.sock"


def port_token(port: int, env: Mapping[str, str] | None = None) -> str | None:
    """The access token the host on ``port`` wrote for its local clients, or ``None``."""
    try:
        token = (mirasim_home(env) / "run" / f"local-{port}.token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def parse_address(value: str, env: Mapping[str, str] | None = None) -> HostAddress:
    """``unix:<path>``, ``<port>`` or ``<loopback host>:<port>``; a :exc:`ValueError` for anything else."""
    value = value.strip()
    if value.startswith("unix:"):
        path = value.removeprefix("unix:")
        if not path:
            raise ValueError(f"{HOST_ENV}={value!r} names no socket")
        return HostAddress(socket=path)
    host, _, port_text = value.rpartition(":")
    if host not in ("", "127.0.0.1", "localhost", "[::1]"):
        raise ValueError(f"{HOST_ENV}={value!r}: the host must listen on this machine's loopback")
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError(f"{HOST_ENV}={value!r} names no port")
    port = int(port_text)
    return HostAddress(port=port, token=port_token(port, env), host=host or "127.0.0.1")


def locate_host(env: Mapping[str, str] | None = None) -> HostAddress | None:
    """Where the running host listens, or ``None`` when there is none to find (see the module docstring)."""
    env = env if env is not None else os.environ
    if named := env.get(HOST_ENV, "").strip():
        return parse_address(named, env)
    socket = remote_socket()
    if UNIX_SOCKETS:
        with contextlib.suppress(OSError):
            if socket.is_socket():
                return HostAddress(socket=str(socket))
    token = port_token(DESKTOP_PORT, env)
    return HostAddress(port=DESKTOP_PORT, token=token) if token is not None else None


Predicate = Callable[[dict[str, Any]], bool]


class HostConnection:
    """One WebSocket to the host, shared by every session of the adapter."""

    def __init__(
        self,
        *,
        on_frame: Callable[[dict[str, Any]], None],
        locate: Callable[[], HostAddress | None] = locate_host,
        on_connect: Callable[[], Awaitable[None]] | None = None,
        on_lost: Callable[[], None] | None = None,
        wanted: Callable[[], bool] = lambda: False,
    ) -> None:
        self._locate = locate
        self._on_frame = on_frame
        self._on_connect = on_connect
        self._on_lost = on_lost
        self._wanted = wanted
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._reconnect: asyncio.Task[None] | None = None
        self._closing: set[asyncio.Future[None]] = set()
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._listeners: list[tuple[Predicate, asyncio.Queue[dict[str, Any] | BaseException]]] = []
        self._closed = False
        self.address: HostAddress | None = None
        self.version: str | None = None
        """The host's version, as its ``/api/health`` said on the last connect."""
        self.lost_at: float | None = None
        """``time.monotonic()`` when the connection last dropped; ``None`` while it is up or was never made."""
        self.epoch = 0
        """How many connections have been made: a frame sent under an older one may have been lost with it."""

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """Connect unless connected; :exc:`HostUnavailableError` when there is no host to reach."""
        if self.connected:
            return
        async with self._connect_lock:
            if self.connected:
                return
            if self._closed:
                raise HostUnavailableError("the connection to the Mirasim host was closed")
            try:
                address = self._locate()
            except ValueError as exc:
                raise HostUnavailableError(str(exc)) from exc
            if address is None:
                raise HostUnavailableError(NO_HOST)
            if address.socket and not UNIX_SOCKETS:
                raise HostUnavailableError(
                    f"cannot reach the Mirasim host at {address.describe()}: asyncio opens no unix socket on this "
                    f"platform, so name the Mirasim app's port in {HOST_ENV} instead"
                )
            connector: aiohttp.BaseConnector = (
                aiohttp.UnixConnector(path=address.socket) if address.socket else aiohttp.TCPConnector()
            )
            session = aiohttp.ClientSession(
                connector=connector, timeout=aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT_S)
            )
            headers = {"x-mirasim-token": address.token} if address.token else {}
            try:
                version = await _health(session, address, headers)
                # The session's timeout bounds the connection, not the upgrade's answer: a host that
                # takes the request and never upgrades would hold this lock, and every send, forever.
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
                    ws = await session.ws_connect(
                        f"{address.base_url}/ws",
                        headers=headers,
                        max_msg_size=MAX_FRAME_BYTES,
                    )
            except BaseException as exc:
                await session.close()
                if not isinstance(exc, (aiohttp.ClientError, OSError, TimeoutError)):
                    raise
                said = str(exc) or (
                    "it did not answer in time" if isinstance(exc, TimeoutError) else type(exc).__name__
                )
                raise HostUnavailableError(f"cannot reach the Mirasim host at {address.describe()}: {said}") from exc
            self._session, self._ws, self.address, self.version = session, ws, address, version
            self.lost_at = None
            self.epoch += 1
            self._reader = asyncio.create_task(self._read(ws, session))
            await self._send(
                {"type": "setStreamCadence", "coalesceMs": STREAM_CADENCE_MS, "statusMs": STREAM_CADENCE_MS}
            )
            logger.info("Mirasim adapter: connected to the Mirasim host {} at {}", version or "?", address.describe())
        if self._on_connect is not None:
            await self._on_connect()

    async def send(self, frame: dict[str, Any]) -> None:
        """Send one frame, connecting first if need be."""
        await self.connect()
        await self._send(frame)

    async def request(self, frame: dict[str, Any], reply: Predicate, *, timeout: float) -> dict[str, Any]:
        """Send ``frame`` and answer the first frame ``reply`` accepts; :exc:`TimeoutError` past ``timeout``."""
        with self.listen(reply) as queue:
            await self.send(frame)
            got = await asyncio.wait_for(queue.get(), timeout)
        if isinstance(got, BaseException):
            raise got
        return got

    @contextlib.contextmanager
    def listen(self, accept: Predicate) -> Iterator[asyncio.Queue[dict[str, Any] | BaseException]]:
        """The frames ``accept`` takes, queued while the block runs; a dropped connection queues the error."""
        queue: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        entry = (accept, queue)
        self._listeners.append(entry)
        try:
            yield queue
        finally:
            self._listeners.remove(entry)

    async def close(self) -> None:
        self._closed = True
        if self._reconnect is not None:
            self._reconnect.cancel()
        ws, session, reader = self._ws, self._session, self._reader
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if session is not None:
            await session.close()
        await asyncio.gather(*self._closing, return_exceptions=True)

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise HostUnavailableError("the connection to the Mirasim host is closed")
        try:
            async with self._send_lock:
                await ws.send_str(json.dumps(frame, ensure_ascii=False))
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as exc:
            raise HostUnavailableError(f"the Mirasim host could not be sent a frame: {exc}") from exc

    async def _read(self, ws: aiohttp.ClientWebSocketResponse, session: aiohttp.ClientSession) -> None:
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(message.data)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    self._deliver(frame)
        finally:
            if self._ws is ws:
                self._lost(session)

    def _deliver(self, frame: dict[str, Any]) -> None:
        for accept, queue in list(self._listeners):
            try:
                taken = accept(frame)
            except Exception:  # noqa: BLE001 - one bad predicate must not stop the others
                taken = False
            if taken:
                queue.put_nowait(frame)
        try:
            self._on_frame(frame)
        except Exception:  # noqa: BLE001 - a frame that breaks its handler must not end the connection
            logger.opt(exception=True).warning("Mirasim adapter: handling a {!r} frame failed", frame.get("type"))

    def _lost(self, session: aiohttp.ClientSession) -> None:
        self._ws = None
        self.lost_at = time.monotonic()
        lost = HostUnavailableError("the connection to the Mirasim host dropped")
        for _, queue in list(self._listeners):
            queue.put_nowait(lost)
        closing = asyncio.ensure_future(session.close())
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)
        if self._closed:
            return
        logger.warning("Mirasim adapter: the connection to the Mirasim host dropped")
        if self._on_lost is not None:
            self._on_lost()
        if self._reconnect is None or self._reconnect.done():
            self._reconnect = asyncio.create_task(self._come_back())

    async def _come_back(self) -> None:
        """Connect again, pausing longer each time, while anything still depends on the connection."""
        pauses = iter(RECONNECT_PAUSES_S)
        while not self._closed and not self.connected and self._wanted():
            await asyncio.sleep(next(pauses, RECONNECT_PAUSES_S[-1]))
            try:
                await self.connect()
            except HostUnavailableError as exc:
                logger.debug("Mirasim adapter: the Mirasim host is still unreachable: {}", exc)


async def _health(session: aiohttp.ClientSession, address: HostAddress, headers: dict[str, str]) -> str | None:
    """The host's version from ``/api/health``, or ``None``: a host that cannot say is still one to talk to."""
    try:
        async with session.get(
            f"{address.base_url}/api/health", headers=headers, timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S)
        ) as response:
            body = await response.json(content_type=None) if response.status == 200 else None
    except (aiohttp.ClientConnectionError, OSError, TimeoutError):
        raise
    except (aiohttp.ClientError, ValueError):
        return None
    version = body.get("version") if isinstance(body, dict) else None
    return version.strip() if isinstance(version, str) and version.strip() else None


__all__ = [
    "DESKTOP_PORT",
    "HOST_ENV",
    "NO_HOST",
    "HostAddress",
    "HostConnection",
    "HostUnavailableError",
    "locate_host",
    "mirasim_home",
    "parse_address",
    "port_token",
    "remote_socket",
]
