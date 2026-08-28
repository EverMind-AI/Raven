"""Unix socket endpoints that put a host-held MCP connection within a sub-agent's reach.

One endpoint per (node, server). A downstream connection gets its own upstream
transport, and frames are relayed at the JSON-RPC message level -- which is what
makes the relay identical for stdio, sse and streamable HTTP.

No ``ClientSession`` is built here on purpose. The sub-agent runs ``initialize``
itself, so capabilities are negotiated between the sub-agent and the real server:
a session on this side would make raven the peer, and every server-initiated
request (sampling, elicitation, progress) would arrive at a client that never
declared it could answer.

Two things are nevertheless not transparent, and both are policy the host owes
whoever configured it: a stdio upstream is spawned under the host's sandbox
executor (:func:`_upstream_executor`), and the host's disabled-tool set is
applied to the frames that cross (:class:`_DenyPolicy`).
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import ValidationError

from raven.mcp.bridge import iter_raw_frames
from raven.mcp.client import open_mcp_transport, resolve_transport
from raven.mcp.naming import spellings
from raven.sandbox import SandboxInitError

_SOCKET_SUBDIR = "raven-mcp"
_HASH_LEN = 12

_counter = itertools.count()

ExecutorProvider = Callable[[], Awaitable[Any]]
"""An async callable resolving to the sandbox executor, started.

Same shape ``MCPConnectionManager`` takes, and for the same reason: it is
awaited only when an upstream is actually dialled, so an endpoint nobody
connects to never spins the sandbox up.
"""


def socket_dir() -> Path:
    d = Path(tempfile.gettempdir()) / _SOCKET_SUBDIR
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def bridge_command(*, path: str | None = None) -> list[str] | None:
    """The argv a sub-agent is handed, or ``None`` when no raven can be found.

    This build's own console script first, and resolving the name is the
    fallback rather than the rule: ``mcp bridge`` is a facility of the raven
    that holds the socket, so a lookup on somebody else's PATH can hand the
    child a *different* build. An older one answers the relay with "No such
    command 'mcp'" and every bridged server for that dispatch dies as a closed
    connection -- silently, because a sub-agent that cannot reach its servers
    still answers the turn.

    The PATH lookup stays for the case this cannot serve: a raven installed
    where the adapter runs but not beside this interpreter.
    """
    own = Path(sys.executable).with_name("raven")
    if own.is_file() and os.access(own, os.X_OK):
        return [str(own), "mcp", "bridge"]
    exe = shutil.which("raven", path=path)
    return [exe, "mcp", "bridge"] if exe else None


class _DenyPolicy:
    """The host's tool off-switch, applied to one bridged connection.

    The relay is the only place it can happen. Nothing terminates MCP between
    the sub-agent and the real server, so a withheld tool is not withheld by
    anything the way the host's own registry withholds it -- the child would
    find it in ``tools/list`` and call it.

    The set is spelled in registered names (``mcp_<server>_<tool>``) while the
    wire carries the server's own names, and the mapping between them is not a
    concatenation: sanitising rewrites characters, a length cap truncates, and a
    collision appends a hash to whichever of two pairs committed second. So the
    comparison runs the other way -- :func:`~raven.mcp.naming.spellings`
    generates every name one pair could ever have been registered under, from
    the pair alone, and an entry matching any of them names this tool. Same test
    ``ToolRegistry.resolve_configured`` makes, for the same reason.

    It can over-deny in one shape, and that is the safe direction for an off
    switch: two servers can produce the same historical spelling (``a.b``/``a/b``
    both clean to ``a_b``), so an entry written for one of them denies the other
    here as well.
    """

    def __init__(self, server: str, disabled: frozenset[str]) -> None:
        self._server = server
        self._disabled = disabled
        self._verdicts: dict[str, bool] = {}
        self._listing: set[Any] = set()
        """Ids of this connection's in-flight ``tools/list`` requests, so the
        result that comes back can be recognised without inspecting every
        response the upstream sends."""

    def denies(self, tool: str) -> bool:
        """Whether the host withheld this server's ``tool``."""
        verdict = self._verdicts.get(tool)
        if verdict is None:
            verdict = bool(spellings(self._server, tool) & self._disabled)
            self._verdicts[tool] = verdict
        return verdict

    def refuse(self, message: Any) -> bytes | None:
        """The error frame this request is owed, or ``None`` to forward it as-is."""
        from mcp.types import INVALID_PARAMS, ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCRequest

        root = message.root
        if not self._disabled or not isinstance(root, JSONRPCRequest):
            return None
        if root.method == "tools/list":
            self._listing.add(root.id)
            return None
        if root.method != "tools/call":
            return None
        name = (root.params or {}).get("name")
        if not isinstance(name, str) or not self.denies(name):
            return None
        logger.warning("mcp endpoint: refused disabled tool {!r} on server {!r}", name, self._server)
        reply = JSONRPCMessage(
            JSONRPCError(
                jsonrpc="2.0",
                id=root.id,
                error=ErrorData(code=INVALID_PARAMS, message=f"tool {name!r} is disabled by the host"),
            )
        )
        return reply.model_dump_json(by_alias=True, exclude_none=True).encode() + b"\n"

    def redact(self, message: Any) -> None:
        """Drop every disabled tool from a ``tools/list`` result, in place."""
        from mcp.types import JSONRPCError, JSONRPCResponse

        if not self._listing:
            return
        root = message.root
        if isinstance(root, JSONRPCError) and root.id in self._listing:
            self._listing.discard(root.id)
            return
        if not isinstance(root, JSONRPCResponse) or root.id not in self._listing:
            return
        self._listing.discard(root.id)
        tools = root.result.get("tools")
        if not isinstance(tools, list):
            return
        root.result["tools"] = [
            entry
            for entry in tools
            if not (isinstance(entry, dict) and isinstance(entry.get("name"), str) and self.denies(entry["name"]))
        ]


@dataclass(frozen=True)
class _Upstream:
    """Everything one downstream connection needs to reach its own upstream."""

    server: str
    cfg: Any = field(repr=False)
    transport: str
    http_auth: httpx.Auth | None = None
    resolve_executor: ExecutorProvider | None = None
    disabled_tools: frozenset[str] = frozenset()


async def _upstream_executor(up: _Upstream) -> Any:
    """The sandbox executor this upstream has to be spawned under.

    Threaded through rather than left unset: for a stdio server it is the host
    that spawns ``cfg.command``, so the confinement a configured sandbox
    promises for MCP processes has to hold whether the tool is called by the
    main loop or by a bridged sub-agent.

    The socket is unaffected by where the server ends up. The host is the client
    on this hop, and under a microVM executor it reaches the server over the
    streams ``start_process`` hands back -- the sub-agent's socket stays on the
    host side of that, touched only by this process.
    """
    executor = await up.resolve_executor() if up.resolve_executor is not None else None
    if (
        up.transport == "stdio"
        and executor is not None
        and executor.is_sandboxed
        and not executor.supports_process_spawning
    ):
        # The same hard failure ``connect_mcp_server`` makes, for the same
        # reason: falling through to a host spawn would drop the confinement
        # silently, and a bridged dispatch is the last place to discover that.
        raise SandboxInitError(
            f"MCP server '{up.server}' uses stdio transport, but the active sandbox "
            f"({type(executor).__name__}) does not support process spawning, so it cannot be "
            "bridged to a sub-agent either. Either switch to an HTTP/SSE MCP server or set "
            "sandbox.backend='none'."
        )
    return executor


async def _relay(up: _Upstream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    policy = _DenyPolicy(up.server, up.disabled_tools)
    # One writer, two producers: ``up_to_down`` forwards results and
    # ``down_to_up`` answers a refused call itself. Concurrent ``drain`` on one
    # StreamWriter trips an assertion in the flow-control mixin.
    downstream = asyncio.Lock()

    async def send_down(payload: bytes) -> None:
        async with downstream:
            writer.write(payload)
            await writer.drain()

    try:
        executor = await _upstream_executor(up)
        async with open_mcp_transport(up.cfg, up.transport, executor, http_auth=up.http_auth) as (up_read, up_write):

            async def down_to_up() -> None:
                async for frame in iter_raw_frames(reader):
                    try:
                        message = JSONRPCMessage.model_validate_json(frame)
                    except ValidationError as exc:
                        # One malformed frame must not take the connection with
                        # it: the SDK's own stdio reader wraps a decode failure
                        # as a stream item for the layer above to skip.
                        logger.warning("mcp endpoint: dropped a malformed frame: {}", exc)
                        continue
                    if (refusal := policy.refuse(message)) is not None:
                        await send_down(refusal)
                        continue
                    await up_write.send(SessionMessage(message))

            async def up_to_down() -> None:
                # The close belongs here rather than after teardown: an upstream
                # that dies mid-request leaves the sub-agent waiting on a reply
                # that will never come, and only an EOF on its side turns that
                # into a prompt error instead of its own timeout.
                try:
                    async for item in up_read:
                        if isinstance(item, Exception):
                            logger.debug("mcp endpoint: upstream stream error: {}", item)
                            continue
                        policy.redact(item.message)
                        payload = item.message.model_dump_json(by_alias=True, exclude_none=True)
                        await send_down(payload.encode() + b"\n")
                finally:
                    writer.close()

            pumps = (asyncio.create_task(down_to_up()), asyncio.create_task(up_to_down()))
            try:
                done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and (exc := task.exception()) is not None:
                        logger.warning("mcp endpoint: relay ended on {}: {}", type(exc).__name__, exc)
            finally:
                # Owned here, whichever way this scope is left -- including a
                # cancellation delivered straight into ``asyncio.wait``, which
                # cancels none of its futures. Leaving them running left the
                # downstream writer open until the upstream happened to die, and
                # ``close`` then paid its whole timeout waiting for a detach.
                for task in pumps:
                    task.cancel()
                writer.close()
                await asyncio.gather(*pumps, return_exceptions=True)
    except (Exception, BaseExceptionGroup) as exc:
        logger.error("mcp endpoint: upstream failed: {}", _causes(exc))
    finally:
        # Again, because cancellation can also land before the transport is
        # open: the downstream would then never be closed at all, and that is
        # the shape that made a reap sit out the full ``_CLOSE_TIMEOUT_S``.
        writer.close()


def _causes(exc: BaseException) -> str:
    """``exc`` named with its leaves, because a group's own message has none.

    An upstream that dies inside a TaskGroup surfaces as "unhandled errors in a
    TaskGroup (1 sub-exception)", which says only that something failed. The one
    line an operator gets for a bridged server that never came up has to name
    what actually went wrong.
    """
    leaves: list[str] = []

    def walk(e: BaseException) -> None:
        inner = getattr(e, "exceptions", None)
        if inner:
            for sub in inner:
                walk(sub)
            return
        leaves.append(f"{type(e).__name__}: {e}")

    walk(exc)
    return "; ".join(leaves) or f"{type(exc).__name__}: {exc}"


# Teardown bound. Cancelling the relays is what lets wait_closed return, so
# reaching this means a relay ignored its cancellation -- a wedged endpoint must
# not hold the dispatch that is trying to reap it.
_CLOSE_TIMEOUT_S = 10.0


@dataclass
class _Endpoint:
    listener: asyncio.AbstractServer
    path: Path
    relays: set[asyncio.Task]
    """In-flight relays for this endpoint. Tracked because ``close`` has to
    cancel them: ``Server.wait_closed`` returns only once every accepted
    connection has detached, so closing the listener alone blocks for as long as
    a sub-agent keeps its bridge open -- which is exactly the moment a node ends.
    """


async def _settle(endpoint: _Endpoint, relays: list[asyncio.Task]) -> None:
    """Wait out one endpoint's teardown: its relays first, then its listener.

    The relays are awaited rather than left to finish detached, because each one
    owns an upstream transport and a stdio upstream is a process this host has
    to reap. ``wait_closed`` after them costs nothing: every relay closes its
    downstream writer before it starts on its upstream.
    """
    if relays:
        await asyncio.gather(*relays, return_exceptions=True)
    await endpoint.listener.wait_closed()


class McpEndpoints:
    """Every endpoint one host has open, indexed by node so a node can be reaped alone."""

    def __init__(
        self,
        *,
        executor_provider: ExecutorProvider | None = None,
        disabled_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._by_node: dict[str, list[_Endpoint]] = {}
        self._executor_provider = executor_provider
        self._executor_lock = asyncio.Lock()
        self._executor_memo: list[Any] = []
        self._disabled_tools = frozenset(disabled_tools)
        # Unique per instance, and an instance is per dispatch. Nobody derives
        # these paths -- the creator writes each one into the stanza it hands the
        # sub-agent -- so determinism buys nothing and costs correctness: node ids
        # are unique only within one run, so two concurrent runs of the same
        # playbook would otherwise bind, unlink and reap each other's sockets.
        self._salt = f"{os.getpid()}-{next(_counter)}"

    def path_for(self, node_id: str, server: str) -> Path:
        """This instance's path for one (node, server) endpoint.

        A hash rather than the names themselves: AF_UNIX caps the whole path at
        104 bytes, and a playbook name plus a node id plus a server name
        concatenate past that -- where the failure surfaces as a bind error, far
        from its cause.
        """
        key = f"{self._salt}\0{node_id}\0{server}".encode()
        return socket_dir() / f"{hashlib.sha256(key).hexdigest()[:_HASH_LEN]}.sock"

    async def _resolve_executor(self) -> Any:
        """The host's MCP executor, started once and shared by every relay.

        Memoised for the same reason the manager memoises it: the loop's
        ``_start_executor`` was written for one caller at a time, and an
        endpoint set can dial several upstreams at once.
        """
        async with self._executor_lock:
            provider = self._executor_provider
            if provider is not None and not self._executor_memo:
                self._executor_memo.append(await provider())
            return self._executor_memo[0] if self._executor_memo else None

    async def open(self, node_id: str, server: str, cfg, http_auth: httpx.Auth | None = None) -> Path:
        # No None guard: resolve_transport returns None only for a config naming
        # neither a command nor a url, and resolve_grant already recorded that
        # one as invalid_transport rather than granting it.
        transport = resolve_transport(cfg)
        upstream = _Upstream(
            server=server,
            cfg=cfg,
            transport=transport,
            http_auth=http_auth,
            resolve_executor=self._resolve_executor if self._executor_provider is not None else None,
            disabled_tools=self._disabled_tools,
        )
        path = self.path_for(node_id, server)
        path.unlink(missing_ok=True)
        relays: set[asyncio.Task] = set()

        def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.create_task(_relay(upstream, reader, writer))
            relays.add(task)
            task.add_done_callback(relays.discard)

        listener = await asyncio.start_unix_server(_accept, path=str(path))
        path.chmod(0o600)
        self._by_node.setdefault(node_id, []).append(_Endpoint(listener, path, relays))
        return path

    async def close(self, node_id: str) -> None:
        for endpoint in self._by_node.pop(node_id, []):
            endpoint.listener.close()
            relays = list(endpoint.relays)
            for task in relays:
                task.cancel()
            try:
                await asyncio.wait_for(_settle(endpoint, relays), timeout=_CLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("mcp endpoint: {} did not settle in {}s", endpoint.path.name, _CLOSE_TIMEOUT_S)
            endpoint.path.unlink(missing_ok=True)

    async def aclose(self) -> None:
        for node_id in list(self._by_node):
            await self.close(node_id)
