"""The adapter's one connection to the Mirasim host already running on this machine.

Mirasim's desktop and phone apps are clients of a *host*: one long-running
server that runs the agents, keeps the sessions and speaks JSON frames over a
WebSocket at ``/ws``. This adapter is one more client of it -- it never starts a
host of its own, because two hosts over one ``~/.mirasim`` contend for the same
sessions and the same machine identity. Where the host listens, in the order
:func:`locate_hosts` names the places to try:

- ``RAVEN_MIRASIM_HOST``: ``unix:<path>`` for a socket, ``<port>`` or
  ``127.0.0.1:<port>`` for a loopback port, when the host is somewhere else;
- ``~/.mirasim-remote/run/server.sock``: the host Mirasim runs on a machine it
  reaches over SSH (POSIX only). The same user is trusted there, no token;
- every loopback port a host wrote its access token for, as
  ``run/local-<port>.token`` under Mirasim's state directory (``MIRASIM_HOME``,
  else ``~/.mirasim``) -- the files ``mirasim ui-cli --port`` reads. The
  installed desktop app's port, 4970, comes first, then 4939, where
  ``mirasim serve`` listens unless told otherwise, then the rest in ascending
  order. A host writes the file for the port it bound -- ``mirasim serve``
  moves on to the next port when its own is taken -- and removes it when it
  stops, but one that is killed leaves it behind. So a file names a place to
  try, not a host: the first place that answers is the host, and every connect
  looks again, which finds a host that came back on another port.

Two frames are never sent, because the answers carry secrets: ``ready`` makes
the host push its whole settings tree, IM bot tokens included, and
``getConfig`` answers with the configuration. No frame is logged whole --
only the reason a refusal gives, which names what failed. The token travels in
the query of the upgrade request, which is where the host reads it: its own
clients send it there, and a host from 0.0.571 on answers a token sent in a
header alone with 401 (measured against 0.0.571, 0.0.576 and 0.0.585). It goes
in the ``x-mirasim-token`` header as well, which the hosts this adapter was
written against (0.0.537 to 0.0.558) read. So the one URL that carries it is
never logged and never quoted in an error: a refused upgrade is worded from
its status, and the token is taken out of whatever else is said. ``/api/health``
is the one path a host serves without a token, so it is asked without one: the
port a killed host left its file for may be another program's by now, and a
place whose health names another service is passed over before the upgrade. A
place found from a token file has to name a Mirasim host there at all, because
the upgrade is the request that carries that file's token; a place named in
``RAVEN_MIRASIM_HOST``, or Mirasim's SSH socket (no token), is still tried when
its health cannot say.

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
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from loguru import logger

HOST_ENV = "RAVEN_MIRASIM_HOST"
DESKTOP_PORT = 4970
SERVE_PORT = 4939
"""Where ``mirasim serve`` listens unless ``--port`` or ``MIRASIM_SERVER_PORT`` names another port."""
TOKEN_FILE = re.compile(r"local-([1-9][0-9]{0,4})\.token")
"""The name a host gives the file it writes its token in, under ``run/``, after the port it bound."""
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
    "No Mirasim host is running on this machine: start the Mirasim app or `mirasim serve` (or let the app "
    f"reach this machine), or name where the host listens in {HOST_ENV}."
)


class HostUnavailableError(Exception):
    """The host could not be reached, or the connection to it dropped."""


class _UnreachableError(HostUnavailableError):
    """One place did not answer; ``reason`` says why, in words that carry nothing of its token."""

    def __init__(self, address: HostAddress, reason: str) -> None:
        super().__init__(f"cannot reach the Mirasim host at {address.describe()}: {reason}")
        self.address = address
        self.reason = reason


class _NotMirasimError(Exception):
    """What answers at a place is not a Mirasim host: its ``/api/health`` names another program, or, at a
    place found by scanning, does not name one at all."""


@dataclass(frozen=True)
class HostAddress:
    """Where a host listens: a unix socket, or a port on the loopback with the token it asks for."""

    socket: str | None = None
    port: int | None = None
    token: str | None = field(default=None, repr=False)
    host: str = "127.0.0.1"
    """The loopback name the port is reached on, as the address named it: ``localhost`` and ``[::1]`` stay so."""

    discovered: bool = field(default=False, compare=False)
    """Whether scanning ``run/local-<port>.token`` found this place, which must then name itself in
    ``/api/health`` before the token is sent to it: the file only says where a host once was."""

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


def token_ports(env: Mapping[str, str] | None = None) -> list[int]:
    """The loopback ports a host wrote its token for under ``run/``, in the order they are tried.

    The desktop app's port first, then ``mirasim serve``'s, then the rest in ascending order. A blank file
    holds no token (:func:`port_token`), so its port is not one to try.
    """
    try:
        names = os.listdir(mirasim_home(env) / "run")
    except OSError:
        return []
    ports = {int(found.group(1)) for name in names if (found := TOKEN_FILE.fullmatch(name))}
    ordered = sorted(
        (port for port in ports if port < 65536), key=lambda port: (port != DESKTOP_PORT, port != SERVE_PORT, port)
    )
    return [port for port in ordered if port_token(port, env) is not None]


def locate_hosts(env: Mapping[str, str] | None = None) -> list[HostAddress]:
    """The places the running host may listen, in the order they are tried (see the module docstring).

    A host named in ``RAVEN_MIRASIM_HOST`` is the only place, and so is the socket of the host Mirasim
    runs here over SSH while that socket is on disk; otherwise each port :func:`token_ports` names, with
    its token. Whether a place answers is for :meth:`HostConnection.connect` to find out.
    """
    env = env if env is not None else os.environ
    if named := env.get(HOST_ENV, "").strip():
        return [parse_address(named, env)]
    socket = remote_socket()
    if UNIX_SOCKETS:
        with contextlib.suppress(OSError):
            if socket.is_socket():
                return [HostAddress(socket=str(socket))]
    return [HostAddress(port=port, token=port_token(port, env), discovered=True) for port in token_ports(env)]


def locate_host(env: Mapping[str, str] | None = None) -> HostAddress | None:
    """The first place :func:`locate_hosts` names, or ``None`` when it names none."""
    return next(iter(locate_hosts(env)), None)


Located = HostAddress | Sequence[HostAddress] | None
"""What a ``locate`` callable answers: one place, several to try in order, or none."""

Predicate = Callable[[dict[str, Any]], bool]


class HostConnection:
    """One WebSocket to the host, shared by every session of the adapter."""

    def __init__(
        self,
        *,
        on_frame: Callable[[dict[str, Any]], None],
        locate: Callable[[], Located] = locate_hosts,
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
        """Connect unless connected; :exc:`HostUnavailableError` when there is no host to reach.

        The places ``locate`` names are tried in order, and the first that answers is the host. Where none
        does, a lone place's own refusal is raised, and with several the error names each and why.
        """
        if self.connected:
            return
        async with self._connect_lock:
            if self.connected:
                return
            if self._closed:
                raise HostUnavailableError("the connection to the Mirasim host was closed")
            try:
                located = self._locate()
            except ValueError as exc:
                raise HostUnavailableError(str(exc)) from exc
            places = [located] if isinstance(located, HostAddress) else list(located or ())
            if not places:
                raise HostUnavailableError(NO_HOST)
            refusals: list[_UnreachableError] = []
            for address in places:
                try:
                    session, ws, version = await self._open(address)
                except _UnreachableError as exc:
                    refusals.append(exc)
                    continue
                break
            else:
                if len(refusals) == 1:
                    raise refusals[0]
                raise HostUnavailableError(_none_answered(refusals))
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

    async def _open(
        self, address: HostAddress
    ) -> tuple[aiohttp.ClientSession, aiohttp.ClientWebSocketResponse, str | None]:
        """The session, WebSocket and version of the host at ``address``; :exc:`_UnreachableError` when it does not answer."""
        if address.socket and not UNIX_SOCKETS:
            raise _UnreachableError(
                address,
                f"asyncio opens no unix socket on this platform, so name the Mirasim app's port in {HOST_ENV} instead",
            )
        connector: aiohttp.BaseConnector = (
            aiohttp.UnixConnector(path=address.socket) if address.socket else aiohttp.TCPConnector()
        )
        session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT_S)
        )
        headers = {"x-mirasim-token": address.token} if address.token else {}
        failed: BaseException | None = None
        try:
            version = await _health(session, address)
            # The session's timeout bounds the connection, not the upgrade's answer: a host that
            # takes the request and never upgrades would hold the connect lock, and every send, forever.
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                ws = await session.ws_connect(
                    upgrade_url(address),
                    headers=headers,
                    max_msg_size=MAX_FRAME_BYTES,
                )
        except BaseException as exc:
            await session.close()
            if not isinstance(exc, (aiohttp.ClientError, OSError, TimeoutError, _NotMirasimError)):
                raise
            failed = exc
        if failed is not None:
            # Raised outside the handler so nothing is chained by default: a refused
            # upgrade's own exception holds the request, URL and token included
            # (`request_info`), and a formatted traceback would print it. Any other
            # failure names no URL and stays the cause.
            unreachable = _UnreachableError(address, _reason(failed, address))
            raise unreachable from (None if isinstance(failed, aiohttp.ClientResponseError) else failed)
        return session, ws, version

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


def upgrade_url(address: HostAddress) -> str:
    """The URL the WebSocket upgrade is requested at: ``/ws``, with the token in its query when there is one.

    The one place the token is put in a URL (see the module docstring); nothing
    logs or quotes what this returns.
    """
    base = f"{address.base_url}/ws"
    return f"{base}?token={quote(address.token, safe='')}" if address.token else base


def _reason(exc: BaseException, address: HostAddress) -> str:
    """Why the host could not be reached, in words that carry nothing of the token.

    A refused upgrade is a ``ClientResponseError`` whose ``str()`` quotes the
    request's URL, token and all, so it is worded from its status instead (and
    not chained either, see :meth:`HostConnection.connect`) -- and
    a host that turns the token away is told apart from one that is not there,
    in words the roster does not read as the agent's own sign-in being wrong
    (``looks_like_auth``): the token is Raven's way in to the host, not the
    agent's. A connection nothing took -- the port's host is gone, or the socket
    file is all that is left of it -- is said in so many words, which matters
    most when a killed host's token files are listed one after another. Whatever
    another failure says has the token taken out as well.
    """
    if isinstance(exc, aiohttp.ClientConnectorError) and isinstance(
        exc.os_error, (ConnectionRefusedError, FileNotFoundError)
    ):
        return "nothing listens there"
    if isinstance(exc, aiohttp.ClientResponseError):
        if exc.status in (401, 403):
            where = f"Mirasim's run/local-{address.port}.token" if address.port else "its token file"
            return (
                f"it turned this connection away: the token Raven read from {where} is not the one it takes. "
                "Start Mirasim again, which writes that file afresh, then try again"
            )
        said = f"its upgrade answered HTTP {exc.status}: {exc.message}"
    else:
        said = str(exc) or ("it did not answer in time" if isinstance(exc, TimeoutError) else type(exc).__name__)
    return said.replace(address.token, "<token>") if address.token else said


async def _health(session: aiohttp.ClientSession, address: HostAddress) -> str | None:
    """The host's version from ``/api/health``, or ``None`` when an explicitly selected host did not say.

    Asked without the token, which the path does not take (see the module docstring). A place found by
    scanning must name a Mirasim host here, because the upgrade is the request that carries its token:
    another program on that port, and a host that cannot say, both raise :exc:`_NotMirasimError`. A place
    named outright is trusted as before, and only a name for another program raises.
    """
    body: Any = None
    try:
        async with session.get(
            f"{address.base_url}/api/health", timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S)
        ) as response:
            body = await response.json(content_type=None) if response.status == 200 else None
    except (aiohttp.ClientConnectionError, OSError, TimeoutError):
        raise
    except (aiohttp.ClientError, ValueError):
        pass
    name = body.get("name") if isinstance(body, dict) else None
    if isinstance(name, str):
        if name != "mirasim":
            raise _NotMirasimError(f"it is not a Mirasim host: its /api/health names {name[:80]!r}")
    elif address.discovered:
        raise _NotMirasimError("it did not identify itself as a Mirasim host")
    version = body.get("version") if isinstance(body, dict) else None
    return version.strip() if isinstance(version, str) and version.strip() else None


def _none_answered(refusals: Sequence[_UnreachableError]) -> str:
    """Why no place answered: each place and its reason, in the order they were tried."""
    places = "; ".join(f"{refusal.address.describe()} ({refusal.reason})" for refusal in refusals)
    return (
        f"cannot reach a Mirasim host at any of the {len(refusals)} places it may listen: {places}; start the "
        f"Mirasim app or `mirasim serve` (or let the app reach this machine), or name where the host listens in "
        f"{HOST_ENV}"
    )


__all__ = [
    "DESKTOP_PORT",
    "HOST_ENV",
    "NO_HOST",
    "SERVE_PORT",
    "HostAddress",
    "HostConnection",
    "HostUnavailableError",
    "Located",
    "locate_host",
    "locate_hosts",
    "mirasim_home",
    "parse_address",
    "port_token",
    "remote_socket",
    "token_ports",
    "upgrade_url",
]
