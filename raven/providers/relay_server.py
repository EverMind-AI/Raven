"""The host's half of the LLM relay: model calls made here for the Raven agents it launched.

One server per host process, started the first time a Raven agent is launched
with the relay on, listening where only this user can reach it -- a socket in a
fresh ``0700`` directory, or loopback TCP where the system has no Unix sockets
(Windows) -- and answering only requests carrying the token it handed out in the
launch environment.

Each request is one connection: open, the token, one frame in, the answer out,
close. The token comes first, on its own and under a deadline, so a peer
without it -- any local account, on loopback TCP -- is dropped before a frame
is read or buffered. A
stream answers with a frame per delta and a closing frame, and a connection the
agent closes early is how a cancelled turn reaches the call: the host stops it.

Calls are made by a :class:`LiteLLMProvider` built from the agent's own
construction arguments, kept per distinct construction. They are made inside
``usage_record.recorded_by_caller``: the agent records every call it makes, so
the host recording it too would bill it twice.

What the agent asks besides calls -- a context window, a price -- runs a
registered :func:`raven.providers.relay.relayed` function in a worker thread,
because the first answer may import LiteLLM and the host's loop has its own
work to do meanwhile.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import hmac
import json
import secrets
import shutil
import sys
import tempfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from raven.providers import relay_wire, usage_record
from raven.providers.base import LLMProvider
from raven.providers.relay import RELAY_ENV, RELAY_TOKEN_ENV, RelayFaultError, run_relayed

#: Distinct constructions kept built. An agent normally has one or two (its
#: model, a fallback); the bound is for a host that serves many agents.
_PROVIDER_CACHE_SIZE = 32

#: How long a new connection has to present the token before it is dropped.
_ADMIT_TIMEOUT_S = 5.0

ProviderBuilder = Callable[[dict[str, Any]], LLMProvider]


def build_litellm_provider(spec: dict[str, Any]) -> LLMProvider:
    """The provider an agent would have built for itself from ``spec``, its key kept out of the host's environment."""
    from raven.providers.litellm_provider import LiteLLMProvider

    spec = dict(spec)
    generation = relay_wire.decode(spec.pop("generation", None))
    provider = LiteLLMProvider(**spec, export_env=False)
    if generation is not None:
        provider.generation = generation
    return provider


class RelayServer:
    """Serves the relay for this host's agents. Start once; hand :meth:`env` to each launch."""

    def __init__(self, *, build: ProviderBuilder = build_litellm_provider, tcp: bool | None = None) -> None:
        self._build = build
        # Loopback TCP where there is no Unix socket to put in a private directory.
        self._tcp = not hasattr(asyncio, "start_unix_server") if tcp is None else tcp
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.AbstractServer | None = None
        self._address = ""
        self._dir: Path | None = None
        self._providers: OrderedDict[str, LLMProvider] = OrderedDict()
        self._building: dict[str, asyncio.Future[LLMProvider]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        """The loop that answers this server's connections, and the only one that may close it."""

    @property
    def serving(self) -> bool:
        return self._server is not None and self._server.is_serving()

    def env(self) -> dict[str, str]:
        """The launch environment that offers this relay to an agent."""
        return {RELAY_ENV: self._address, RELAY_TOKEN_ENV: self._token}

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self._tcp:
            self._server = await asyncio.start_server(self._serve, host="127.0.0.1", port=0)
            host, port = self._server.sockets[0].getsockname()[:2]
            self._address = f"tcp:{host}:{port}"
        else:
            # mkdtemp makes the directory 0700, which is what keeps another user
            # from reaching the socket at all; the token is the second lock.
            self._dir = Path(tempfile.mkdtemp(prefix="raven-llm-relay-"))
            path = self._dir / "relay.sock"
            self._server = await asyncio.start_unix_server(self._serve, path=str(path))
            self._address = f"unix:{path}"
            atexit.register(shutil.rmtree, self._dir, True)
        logger.debug("llm relay: serving at {}", self._address)

    def abandon(self) -> None:
        """Stop listening without the loop it was started on, which may be closed by now.

        A connection made to a server whose loop no longer runs is accepted by
        the system and then answered by nobody, so its socket has to go.
        """
        server, self._server = self._server, None
        if server is not None:
            # Server.close is synchronous and closes the listening sockets
            # itself; on a closed loop it skips only the reader removal.
            with contextlib.suppress(Exception):
                server.close()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._providers.clear()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._providers.clear()

    async def _provider(self, spec: Any) -> LLMProvider:
        if not isinstance(spec, dict):
            raise RelayFaultError("a call carries no provider construction")
        key = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        found = self._providers.get(key)
        if found is not None:
            self._providers.move_to_end(key)
            return found
        pending = self._building.get(key)
        if pending is None:
            # Built off the loop: the first build of a process imports LiteLLM.
            pending = asyncio.ensure_future(asyncio.to_thread(self._build, spec))
            self._building[key] = pending
        try:
            provider = await asyncio.shield(pending)
        finally:
            self._building.pop(key, None)
        self._providers[key] = provider
        while len(self._providers) > _PROVIDER_CACHE_SIZE:
            self._providers.popitem(last=False)
        return provider

    async def _admit(self, reader: asyncio.StreamReader) -> bool | None:
        """Whether the peer presented this relay's token: ``None`` when it said nothing in time."""
        expected = relay_wire.greeting(self._token)
        try:
            async with asyncio.timeout(_ADMIT_TIMEOUT_S):
                presented = await reader.readexactly(len(expected))
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return None
        return hmac.compare_digest(presented, expected)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            admitted = await self._admit(reader)
            if admitted is None:
                return
            if not admitted:
                await relay_wire.write_frame(writer, {"fault": "this relay does not serve that token"})
                return
            request = await relay_wire.read_frame(reader)
            if request is None:
                return
            op = request.get("op")
            if op == "stream":
                await self._stream(request, reader, writer)
            elif op == "chat":
                await self._chat(request, reader, writer)
            elif op == "call":
                await self._run(request, writer)
            else:
                await relay_wire.write_frame(writer, {"fault": f"unknown relay op {op!r}"})
        except (ConnectionError, relay_wire.FrameError, asyncio.IncompleteReadError):
            pass
        except RelayFaultError as exc:
            with contextlib.suppress(Exception):
                await relay_wire.write_frame(writer, {"fault": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one agent's bad request must not end the server
            logger.warning("llm relay: request failed: {}", exc)
            with contextlib.suppress(Exception):
                await relay_wire.write_frame(writer, {"fault": f"{type(exc).__name__}: {exc}"})
        finally:
            writer.close()

    async def _watched(self, work: Awaitable[None], reader: asyncio.StreamReader) -> BaseException | None:
        """Run ``work`` until it ends or the agent hangs up; what it raised, if anything.

        The agent sends nothing after its request, so any read returning is the
        connection ending -- which is the agent calling the work off.
        """
        task = asyncio.ensure_future(work)
        hangup = asyncio.ensure_future(reader.read(1))
        try:
            await asyncio.wait({task, hangup}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            hangup.cancel()
            with contextlib.suppress(BaseException):
                await hangup
        if task.cancelled():
            return None
        return task.exception()

    async def _failed(self, writer: asyncio.StreamWriter, provider: LLMProvider, exc: BaseException) -> None:
        status = provider._extract_status_code(exc)
        await relay_wire.write_frame(writer, {"error": relay_wire.encode_exception(exc, status=status)})

    async def _stream(
        self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        provider = await self._provider(request.get("spec"))
        args = relay_wire.decode(request.get("args") or {}, sentinel=provider._SENTINEL)

        async def pump() -> None:
            with usage_record.recorded_by_caller():
                async for delta in provider.chat_stream(**args):
                    await relay_wire.write_frame(writer, {"delta": relay_wire.encode(delta)})
            await relay_wire.write_frame(writer, {"done": True})

        failure = await self._watched(pump(), reader)
        if failure is not None and not isinstance(failure, (ConnectionError, relay_wire.FrameError)):
            await self._failed(writer, provider, failure)

    async def _chat(self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        provider = await self._provider(request.get("spec"))
        args = relay_wire.decode(request.get("args") or {}, sentinel=provider._SENTINEL)

        async def answer() -> None:
            with usage_record.recorded_by_caller():
                response = await provider.chat(**args)
            await relay_wire.write_frame(writer, {"result": relay_wire.encode(response)})

        failure = await self._watched(answer(), reader)
        if failure is not None and not isinstance(failure, (ConnectionError, relay_wire.FrameError)):
            await self._failed(writer, provider, failure)

    async def _run(self, request: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        name = request.get("fn")
        args = relay_wire.decode(request.get("args") or [])
        kwargs = relay_wire.decode(request.get("kwargs") or {})
        if not isinstance(name, str) or not isinstance(args, list) or not isinstance(kwargs, dict):
            raise RelayFaultError("a relayed call names no function")
        result = await asyncio.to_thread(run_relayed, name, args, kwargs)
        await relay_wire.write_frame(writer, {"result": relay_wire.encode(result)})


_SERVER: RelayServer | None = None
_STARTING: asyncio.Lock | None = None


async def relay_env() -> dict[str, str]:
    """The environment offering this host's relay to an agent, starting the relay on first use.

    Also starts LiteLLM's import in the background, once: the agents will ask
    for it, and a first answer that waits out the import waits on this host.
    """
    global _SERVER, _STARTING
    loop = asyncio.get_running_loop()
    if _SERVER is not None and _SERVER.loop is not loop:
        if _SERVER.loop is not None and _SERVER.loop.is_running():
            # A loop in another thread serves it, and its socket answers an
            # agent launched from this loop as well as its own.
            if _SERVER.serving:
                return _SERVER.env()
            raise RuntimeError("the relay belongs to an event loop running in another thread")
        # Started under a loop that has ended (a CLI turn is one asyncio.run).
        _SERVER.abandon()
        _SERVER, _STARTING = None, None
    if _SERVER is not None and _SERVER.serving:
        return _SERVER.env()
    if _STARTING is None:
        _STARTING = asyncio.Lock()
    async with _STARTING:
        if _SERVER is None or not _SERVER.serving:
            server = RelayServer()
            await server.start()
            _SERVER = server
            if "litellm" not in sys.modules:
                from raven.providers.litellm_setup import warm_up_in_background

                warm_up_in_background()
    return _SERVER.env()


async def close_relay() -> None:
    """Stop serving. For process shutdown and for tests."""
    global _SERVER, _STARTING
    server, _SERVER, _STARTING = _SERVER, None, None
    if server is not None and server.loop is not asyncio.get_running_loop():
        server.abandon()
    elif server is not None:
        await server.close()
