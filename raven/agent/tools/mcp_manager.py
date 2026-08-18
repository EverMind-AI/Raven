"""Per-server MCP connection lifecycle.

Replaces the shared-stack, one-shot connect in ``connect_mcp_servers`` for
the live agent loop: each server owns a private ``AsyncExitStack`` so it can
be attached, detached, and reconnected independently while the loop runs
(plugin install/uninstall, re-auth, config edits — no restart).

Ordering constraint worth flagging: ``disconnect`` unregisters the server's
tools *before* closing its stack, so the agent never sees a tool whose
session is already gone. In-flight calls on a closing session are covered by
``MCPToolWrapper``'s existing timeout/exception handling.

Locking model: the manager lock guards only the connection map and the
begin/commit edges of a connect. The transport handshake itself runs
*outside* the lock — an OAuth connect legitimately blocks for minutes while
the user is in the browser, and the 5s ``reload.mcp`` poll must keep getting
answers meanwhile. Each connect attempt carries an ``epoch`` token; commit
verifies the record still wants this attempt (same epoch, still
``connecting``) and rolls the attempt back otherwise, so a concurrent
disconnect/config-change during the handshake wins cleanly.

State transitions (``sync``/``connect`` drive them):

    disconnected -> connecting -> connected
                        |-> auth_required   (OAuth needed / token expired /
                        |                    parked at the browser step)
                        `-> error           (kept until config changes or an
                                             explicit connect() retries it —
                                             the 5s reload poll must not turn
                                             a dead server into a retry storm)

``auth_required`` is reached two ways. A connect that *failed* with an auth
error rests there until an explicit ``connect()``. A background connect that
*parked* at the browser-authorization step is moved there immediately — while
its attempt keeps running: ``sync`` stops awaiting it, the state tells every
poller who the wait is on, and if the user completes the authorization the
still-live attempt commits and the server flips to ``connected`` on its own.
The commit/abort checks therefore accept ``auth_required`` alongside
``connecting`` when the epoch matches.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from raven.agent.tools.mcp import connect_mcp_server, resolve_transport
from raven.agent.tools.mcp_oauth import OAUTH_FLOW_TIMEOUT
from raven.agent.tools.registry import ToolRegistry
from raven.sandbox import SandboxInitError

MCPState = str  # "disconnected" | "connecting" | "connected" | "auth_required" | "error"

_ERROR_MAX = 300

# Handshake progress bound. Generous: a cold stdio server may download its
# package on first run. A connect parked at the browser-authorization step
# does not count against it (see _handshake_watchdog).
_HANDSHAKE_TIMEOUT = 90.0
# How long a server may stay exempt from that bound because it is parked at the
# browser-authorization step. Derived, not chosen: it has to outlast the OAuth
# flow's own timeout so a real flow always resolves first and only a leaked one
# hits this. Written as a literal it silently inverted when the flow timeout was
# raised, and the watchdog started cancelling authorizations the page had just
# promised the reader another eight minutes for.
_AUTH_PARK_GRACE = 120.0
_AUTH_PARK_MAX = OAUTH_FLOW_TIMEOUT + _AUTH_PARK_GRACE


def _log_detached_connect(task: "asyncio.Task") -> None:
    """Report a connect attempt that finished after sync stopped awaiting it.

    Success needs no line here -- the commit already logs it and broadcasts
    ``mcp.status``. A failure would otherwise vanish: no caller holds this
    task any more.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("MCP: a connect left running behind an authorization failed: {}", exc)


@dataclass
class MCPConnection:
    """Live record for one configured server."""

    name: str
    config: Any
    stack: AsyncExitStack | None = None
    tool_names: set[str] = field(default_factory=set)
    state: MCPState = "disconnected"
    error: str | None = None
    epoch: object | None = None
    """Identity of the in-flight connect attempt; commit checks it so a
    disconnect/reconfigure that raced the handshake invalidates the result."""
    auth_parked: asyncio.Event | None = None
    """Set when the current attempt reaches the browser-authorization step,
    so a caller that must not wait on a person can stop awaiting it."""


def _cfg_fingerprint(cfg: Any) -> Any:
    """Comparable view of a server config, for change detection in sync()."""
    dump = getattr(cfg, "model_dump", None)
    return dump() if callable(dump) else cfg


class MCPConnectionManager:
    """Owns every MCP connection of one agent loop.

    ``post_connect`` runs after each successful connect — the loop passes
    ``_apply_disabled_tools`` so the blacklist covers freshly registered
    MCP tool names. ``on_state_change`` receives a status snapshot dict on
    every transition (the gateway broadcasts it as ``mcp.status``);
    ``on_oauth_event`` receives ``(event, payload)`` from the OAuth flow
    (``oauth.pending`` / ``oauth.done``).

    ``executor_provider`` is an async callable resolving to the sandbox
    executor; it is awaited only when a connect actually happens, so a
    no-op ``sync`` never has to spin up the executor.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        post_connect: Callable[[], None] | None = None,
        on_state_change: Callable[[dict], None] | None = None,
        on_oauth_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._registry = registry
        self._post_connect = post_connect
        self.on_state_change = on_state_change
        self.on_oauth_event = on_oauth_event
        self._conns: dict[str, MCPConnection] = {}
        self._lock = asyncio.Lock()

    # ── Introspection ──────────────────────────────────────────────

    def status(self) -> list[dict]:
        """Snapshot of every known server, stable-ordered by name."""
        return [self._snapshot(c) for c in sorted(self._conns.values(), key=lambda c: c.name)]

    def tool_map(self) -> dict[str, str]:
        """Registered tool name -> owning server name."""
        return {t: c.name for c in self._conns.values() for t in c.tool_names}

    def _snapshot(self, conn: MCPConnection) -> dict:
        return {
            "name": conn.name,
            "transport": resolve_transport(conn.config) or "unknown",
            "state": conn.state,
            "connected": conn.state == "connected",
            "tool_count": len(conn.tool_names),
            "error": conn.error,
            "enabled": bool(getattr(conn.config, "enabled", True)),
        }

    def _set_state(self, conn: MCPConnection, state: MCPState, error: str | None = None) -> None:
        conn.state = state
        conn.error = error
        cb = self.on_state_change
        if cb is not None:
            try:
                cb(self._snapshot(conn))
            except Exception as e:  # noqa: BLE001 — a broken listener must not corrupt the connection
                logger.warning("MCP state listener failed for '{}': {}", conn.name, e)

    # ── Lifecycle ──────────────────────────────────────────────────

    async def connect(self, name: str, cfg: Any, *, executor_provider=None) -> dict:
        """Connect (or force-reconnect) one server.

        Already-connected servers with unchanged config are left alone;
        anything else (disconnected / error / auth_required / config
        changed) is torn down and connected fresh — this is the explicit
        retry entry point for plug.auth and install flows.
        """
        async with self._lock:
            conn = self._conns.get(name)
            if (
                conn is not None
                and conn.state == "connected"
                and _cfg_fingerprint(conn.config) == _cfg_fingerprint(cfg)
            ):
                return self._snapshot(conn)
            if conn is not None and conn.state == "connecting":
                # A handshake is already running for this server. Starting a
                # second one would leave two live transports racing to commit,
                # and each holds a subprocess for up to the handshake bound -- so
                # a retry button pressed twice costs two servers, not one.
                return self._snapshot(conn)
            conn, epoch = await self._begin_connect_locked(name, cfg)
        # This is the explicit retry entry point (plug.auth / install), so an
        # OAuth server reached from here may take the browser.
        return await self._run_connect(conn, epoch, executor_provider, interactive=True)

    async def disconnect(self, name: str, *, drop: bool = False) -> None:
        """Detach one server. ``drop=True`` forgets the record entirely
        (server removed from config); ``drop=False`` keeps it visible as
        ``disconnected`` (server merely disabled)."""
        async with self._lock:
            await self._disconnect_locked(name, drop=drop)

    async def sync(self, cfg_servers: dict, *, executor_provider=None) -> dict:
        """Reconcile live connections with the desired config.

        Returns ``{"reloaded": <servers touched>, "tools_changed": bool}``.
        Servers in ``error``/``auth_required`` with unchanged config are NOT
        retried here (see module docstring); ``connect()`` retries them.
        """
        reloaded = 0
        tools_changed = False
        pending: list[tuple[MCPConnection, object]] = []

        async with self._lock:
            desired = {n: c for n, c in cfg_servers.items() if getattr(c, "enabled", True)}

            for name in [n for n in self._conns if n not in desired]:
                conn = self._conns[name]
                had_tools = bool(conn.tool_names)
                was_live = conn.state != "disconnected"
                if name in cfg_servers:
                    # Merely disabled: keep the record visible, but track the
                    # new config so the snapshot reflects enabled=False.
                    conn.config = cfg_servers[name]
                await self._disconnect_locked(name, drop=name not in cfg_servers)
                if was_live or had_tools:
                    reloaded += 1
                    tools_changed = tools_changed or had_tools

            for name, cfg in desired.items():
                conn = self._conns.get(name)
                if conn is not None:
                    if _cfg_fingerprint(conn.config) == _cfg_fingerprint(cfg):
                        if conn.state != "disconnected":
                            continue  # connected / connecting / parked in error
                    else:
                        await self._disconnect_locked(name, drop=True)
                pending.append(await self._begin_connect_locked(name, cfg))

        # Every pending server gets its attempt before anything is re-raised.
        # _begin_connect_locked already marked them all `connecting`, and sync's
        # own guard skips a record that is not `disconnected` -- so abandoning the
        # tail on the first failure would park those servers in `connecting`
        # permanently, which no later sync would retry.
        #
        # The attempts run concurrently: they share no state but the registry
        # (single-threaded asyncio dict ops) and the commit lock, and connecting
        # serially meant one slow server delayed every server behind it -- the
        # measured cost was a whole turn spent waiting on a handshake that had
        # nothing to do with it. The executor is resolved once, under a lock,
        # because ``_start_executor`` was written for one caller at a time.
        first_error: SandboxInitError | None = None
        shared_executor = self._shared_executor_provider(executor_provider)
        results = await asyncio.gather(
            *[self._attempt_or_detach(conn, epoch, shared_executor) for conn, epoch in pending],
            return_exceptions=True,
        )
        for res in results:
            reloaded += 1
            if isinstance(res, SandboxInitError):
                first_error = first_error or res
                continue
            if isinstance(res, BaseException):
                raise res
            tools_changed = tools_changed or res["tool_count"] > 0
        if first_error is not None:
            raise first_error

        return {"reloaded": reloaded, "tools_changed": tools_changed}

    @staticmethod
    def _shared_executor_provider(executor_provider):
        """Memoise ``executor_provider`` across one sync's concurrent attempts."""
        if executor_provider is None:
            return None
        lock = asyncio.Lock()
        cache: list = []

        async def _shared():
            async with lock:
                if not cache:
                    cache.append(await executor_provider())
                return cache[0]

        return _shared

    async def _attempt_or_detach(self, conn: MCPConnection, epoch: object, executor_provider) -> dict:
        """Await one connect attempt -- until it parks on a person.

        The moment the attempt reaches the browser-authorization step it stops
        being sync's business: the server is already marked ``auth_required``
        (the oauth.pending hook did that), the URL is already published, and
        the only thing left to wait on is the user. The attempt is left
        running -- not cancelled, its PKCE state is what the authorization
        link resolves against -- and commits or aborts on its own; the epoch
        check covers anything that changes meanwhile.
        """
        parked = conn.auth_parked
        task = asyncio.ensure_future(self._run_connect(conn, epoch, executor_provider))
        if parked is None:
            return await task
        park_wait = asyncio.ensure_future(parked.wait())
        try:
            done, _ = await asyncio.wait({task, park_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            park_wait.cancel()
        if task in done:
            return await task
        task.add_done_callback(_log_detached_connect)
        return self._snapshot(conn)

    async def aclose(self) -> None:
        """Detach everything and forget all records (loop shutdown)."""
        async with self._lock:
            for name in list(self._conns):
                await self._disconnect_locked(name, drop=True)

    # ── Connect machinery ──────────────────────────────────────────

    async def _begin_connect_locked(self, name: str, cfg: Any) -> tuple[MCPConnection, object]:
        from raven.agent.tools.mcp_oauth import cancel_pending

        existing = self._conns.get(name)
        if existing is not None and existing.state == "connected":
            await self._disconnect_locked(name, drop=False)

        # A superseded attempt's authorization link must go stale NOW, not when
        # its flow times out: the new epoch below already dooms its commit, and
        # a link that still redeems would show success for an attempt whose
        # result is dropped while the new attempt waits on a click of its own.
        cancel_pending(name)

        conn = self._conns.get(name) or MCPConnection(name=name, config=cfg)
        conn.config = cfg
        epoch = object()
        conn.epoch = epoch
        conn.auth_parked = asyncio.Event()
        self._conns[name] = conn
        self._set_state(conn, "connecting")
        return conn, epoch

    async def _run_connect(
        self, conn: MCPConnection, epoch: object, executor_provider, *, interactive: bool = False
    ) -> dict:
        """The unlocked half of a connect attempt: handshake, then commit.

        ``interactive`` travels to the OAuth seam and nowhere else: it decides
        whether this attempt may open a browser, not what it connects to.
        """
        name, cfg = conn.name, conn.config
        stack = AsyncExitStack()
        await stack.__aenter__()
        # Registrations happen inside the handshake, so a cancelled attempt gives
        # us no list of what it added. The diff against this is how those names
        # are still found and removed.
        before_names = set(self._registry.names())
        try:
            executor = await executor_provider() if executor_provider is not None else None
            registered = await self._handshake_watchdog(
                name,
                connect_mcp_server(
                    name,
                    cfg,
                    self._registry,
                    stack,
                    executor=executor,
                    http_auth=await self._auth_for(conn, interactive=interactive),
                ),
            )
        except SandboxInitError as e:
            async with self._lock:
                await self._abort_attempt_locked(conn, epoch, stack, [], "error", str(e)[:_ERROR_MAX])
            raise
        except BaseException as e:
            if not isinstance(e, (Exception, BaseExceptionGroup)):
                # A cancelled turn or a shutdown, not the server's failure. Three
                # things still have to happen, and skipping any of them was a
                # worse bug than the leak this branch was added for:
                #
                # * the stack is ours and nothing else will reap it;
                # * whatever the handshake registered before it was cancelled has
                #   to come back out -- its session is inside that stack, so the
                #   agent would otherwise hold a tool it cannot call, owned by no
                #   connection and therefore unreachable by disconnect;
                # * the record must leave `connecting`. `sync` skips anything that
                #   is not `disconnected`, so a record left mid-connect is never
                #   retried again for the life of the process.
                added = [t for t in self._registry.names() if t not in before_names]
                async with self._lock:
                    await self._abort_attempt_locked(conn, epoch, stack, added, "disconnected", "")
                raise
            state = "auth_required" if self._is_auth_error(e) else "error"
            # anyio wraps the real failure in ExceptionGroup shells whose str()
            # is just "unhandled errors in a TaskGroup" — unwrap to the leaf so
            # logs and the GUI status pill name the actual cause.
            leaf: BaseException = e
            while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
                leaf = leaf.exceptions[0]
            detail = f"{type(leaf).__name__}: {leaf}" if str(leaf) else type(leaf).__name__
            async with self._lock:
                await self._abort_attempt_locked(conn, epoch, stack, [], state, detail[:_ERROR_MAX])
            logger.error("MCP server '{}': failed to connect: {}", name, detail)
            return self._snapshot(conn)

        async with self._lock:
            # `auth_required` is a live state here, not a terminal one: the
            # oauth.pending hook moves a background attempt there while its
            # handshake keeps running, and this commit is that handshake
            # finishing. The epoch is what says whether the attempt still owns
            # the record.
            if (
                self._conns.get(name) is not conn
                or conn.epoch is not epoch
                or conn.state not in ("connecting", "auth_required")
            ):
                # A disconnect or reconfigure won the race — this attempt's
                # registrations and transport are stale, drop them. Names the
                # winner now owns are left alone: the registry keys by name, so
                # unregistering ours would strip the identically-named tool the
                # winner just registered and leave it reporting a tool count the
                # registry cannot dispatch.
                self._unregister_unclaimed(registered)
                await self._close_stack(stack)
                return self._snapshot(conn)
            conn.stack = stack
            conn.tool_names = set(registered)
            if self._post_connect is not None:
                self._post_connect()
            # The blacklist may have unregistered some of the fresh names;
            # track only what survived so disconnect stays precise.
            conn.tool_names = {t for t in conn.tool_names if self._registry.has(t)}
            self._set_state(conn, "connected")
            logger.info("MCP server '{}': connected, {} tools registered", name, len(conn.tool_names))
            return self._snapshot(conn)

    async def _handshake_watchdog(self, name: str, coro) -> list[str]:
        """Await the handshake, but never forever.

        The MCP SDK's streamable-http transport can wedge: an exception in
        its HTTP auth flow (e.g. a failed dynamic client registration) dies
        in the transport's read task and never reaches ``initialize()``,
        which then waits for a response that cannot come. Without a bound
        the connection is 'connecting' for the rest of the process.

        A server parked at the browser-authorization step is exempt while
        it stays parked — that wait blocks on the user, and the OAuth flow
        enforces its own timeout.
        """
        task = asyncio.ensure_future(coro)
        deadline = asyncio.get_running_loop().time() + _AUTH_PARK_MAX
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=_HANDSHAKE_TIMEOUT)
            except asyncio.TimeoutError:
                from raven.agent.tools.mcp_oauth import auth_wait_servers

                # The exemption is bounded. A flow whose redirect registered but
                # whose callback never arrives (the transport dying inside the
                # SDK's auth path is exactly what this watchdog exists for) leaks
                # its pending entry, and an unbounded exemption would then park
                # this server in `connecting` for the life of the process.
                if name in auth_wait_servers() and asyncio.get_running_loop().time() < deadline:
                    continue
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5)
                except BaseException:  # noqa: BLE001 — reaping a cancelled, possibly wedged task
                    pass
                raise TimeoutError(f"MCP handshake made no progress for {_HANDSHAKE_TIMEOUT:.0f}s") from None
            except asyncio.CancelledError:
                if not task.cancelled():
                    # Our caller is cancelling us. The handshake is shielded (so a
                    # timeout cannot kill an OAuth park), which means it would
                    # otherwise keep running against a stack the caller is about
                    # to close -- and register its tools into the live registry
                    # afterwards. Take it with us.
                    task.cancel()
                    with suppress(BaseException):
                        await asyncio.wait_for(asyncio.shield(task), timeout=5)
                    raise  # propagate the cancellation
                # The SDK cancelled the handshake from within (a leaked anyio
                # cancel scope around a failed HTTP auth flow). Letting the
                # CancelledError propagate would kill the whole connect task
                # and park the server in 'connecting' forever; it is a
                # connect FAILURE, so surface it as one.
                raise RuntimeError(
                    "MCP handshake was aborted by the transport - usually a failed "
                    "authorization flow (see the OAuth error above in the logs)"
                ) from None

    def _unregister_unclaimed(self, names: list[str]) -> None:
        """Unregister only the names no live connection claims."""
        claimed = {t for c in self._conns.values() for t in c.tool_names}
        for t in names:
            if t not in claimed:
                self._registry.unregister(t)

    async def _abort_attempt_locked(
        self,
        conn: MCPConnection,
        epoch: object,
        stack: AsyncExitStack,
        registered: list[str],
        state: MCPState,
        error: str,
    ) -> None:
        self._unregister_unclaimed(registered)
        await self._close_stack(stack)
        if self._conns.get(conn.name) is conn and conn.epoch is epoch and conn.state in ("connecting", "auth_required"):
            self._set_state(conn, state, error or None)

    async def _disconnect_locked(self, name: str, *, drop: bool) -> None:
        from raven.agent.tools.mcp_oauth import cancel_pending

        conn = self._conns.get(name)
        if conn is None:
            return
        conn.epoch = None  # invalidates any in-flight attempt
        cancel_pending(name)
        for t in conn.tool_names:
            self._registry.unregister(t)
        conn.tool_names = set()
        if conn.stack is not None:
            await self._close_stack(conn.stack)
            conn.stack = None
        if drop:
            del self._conns[name]
        else:
            self._set_state(conn, "disconnected")

    @staticmethod
    async def _close_stack(stack: AsyncExitStack) -> None:
        try:
            await stack.aclose()
        except (RuntimeError, BaseExceptionGroup):
            pass  # MCP SDK cancel scope cleanup is noisy but harmless

    # ── OAuth seam ─────────────────────────────────────────────────

    async def _auth_for(self, conn: MCPConnection, *, interactive: bool = False):
        """httpx.Auth for this server's HTTP transport; None = no auth."""
        cfg = conn.config
        if getattr(cfg, "auth", "none") != "oauth":
            return None
        from raven.agent.tools.mcp_oauth import provider_for

        parked = conn.auth_parked

        def notify(event: str, payload: dict) -> None:
            if event == "oauth.pending":
                # The connect just parked on a person. Say so in the state
                # machine right away -- a background caller stops awaiting on
                # the event, and every poller sees who the wait is on -- while
                # the attempt itself keeps running so a completed authorization
                # still commits. Only a background connect flips the state: an
                # interactive one (plug.install / plug.auth) is being watched
                # by the flow that asked for it.
                if not interactive and conn.state == "connecting":
                    self._set_state(conn, "auth_required", "waiting for browser authorization")
                if parked is not None:
                    parked.set()
            if self.on_oauth_event is not None:
                self.on_oauth_event(event, payload)

        return await provider_for(conn.name, cfg, notify=notify, interactive=interactive)

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        """Whether a connect failure means "user must (re)authorize"."""
        from raven.agent.tools.mcp_oauth import is_auth_error

        return is_auth_error(exc)


__all__ = ["MCPConnection", "MCPConnectionManager"]
