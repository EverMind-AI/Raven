"""Per-server MCP connection lifecycle.

The live path for every MCP server the agent loop talks to: each server owns
a private ``AsyncExitStack`` so it can be attached, detached, and reconnected
independently while the loop runs (plugin install/uninstall, re-auth, config
edits -- no restart).

Ordering constraint worth flagging: ``disconnect`` withdraws the server's
tools *before* closing its stack, so the agent never sees a tool whose session
is already gone. A turn already in flight had those tools in its prompt, so its
next call lands on the registry's not-found answer, which names both readings
("unloaded, or the name is wrong") rather than accusing the model of guessing.

Locking model: the manager lock guards only the connection map and the
begin/commit edges of a connect. The transport handshake itself runs
*outside* the lock — an OAuth connect legitimately blocks for minutes while
the user is in the browser, and the 5s ``reload.mcp`` poll must keep getting
answers meanwhile. Each connect attempt carries an ``epoch`` token; commit
verifies the record still wants this attempt (same epoch, still
``connecting``) and rolls the attempt back otherwise, so a concurrent
disconnect/config-change during the handshake wins cleanly.

State transitions (``apply_config``/``connect`` drive them):

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
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from raven.agent.tools.registry import ToolRegistry
from raven.mcp.client import Connected, connect_mcp_server, resolve_transport
from raven.mcp.oauth import OAUTH_FLOW_TIMEOUT
from raven.mcp.report import ApplyReport
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
# hits this. A literal here would invert the moment that timeout is raised.
_REAP_ROUNDS = 5
"""How many times ``reap_attempts`` re-reads the task sets before giving up.

More than one because a connect can be registered while the previous batch is
being awaited; bounded because the alternative is a shutdown a busy reload can
hold open indefinitely."""

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
    state: MCPState = "disconnected"
    error: str | None = None
    epoch: object | None = None
    """Identity of the in-flight connect attempt; commit checks it so a
    disconnect/reconfigure that raced the handshake invalidates the result."""
    auth_parked: asyncio.Event | None = None
    """Set when the current attempt reaches the browser-authorization step,
    so a caller that must not wait on a person can stop awaiting it."""
    session: Any = None
    """The live ``mcp.ClientSession``, for callers that address a server by name
    rather than through one of its tool wrappers.

    Written only at commit, under the same epoch check that accepts the tool
    registrations, and cleared the moment the stack closes. That is what makes
    ``session_of`` safe without a generation of its own: a losing attempt never
    stores its session, so nobody can be handed one whose stack is gone."""
    capabilities: Any = None
    """``ServerCapabilities`` from this server's handshake, or None until it
    connects. Which primitives it offers is stated once, at initialize, and
    cannot be asked again."""


def _cfg_fingerprint(cfg: Any) -> Any:
    """Comparable view of a server config, for change detection."""
    dump = getattr(cfg, "model_dump", None)
    return dump() if callable(dump) else cfg


def _drain_exception(task: "asyncio.Future") -> None:
    """Mark a finished task's exception retrieved, so asyncio stays quiet.

    A task nobody holds is not an error here -- see the call site -- but an
    unretrieved exception is printed at loop shutdown, and a wall of stack after
    a run that degraded correctly reads as a crash.
    """
    if task.cancelled():
        return
    task.exception()


class MCPConnectionManager:
    """Owns every MCP connection of one agent loop.

    ``post_connect`` runs after each successful connect — the loop passes
    ``_report_reserved_disabled_tools``, so an off switch naming a freshly
    registered MCP tool is reported if it names one of the meta-tools the loop
    owns. Withholding the tool itself needs nothing here: it is decided per
    request from the tool array. ``on_state_change`` receives a status snapshot on
    every transition (the gateway broadcasts it as ``mcp.status``);
    ``on_oauth_event`` receives ``(event, payload)`` from the OAuth flow
    (``oauth.pending`` / ``oauth.done``).

    ``executor_provider`` is an async callable resolving to the sandbox
    executor; it is awaited only when a connect actually happens, so a
    no-op ``apply_config`` never has to spin up the executor.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        post_connect: Callable[[], None] | None = None,
        on_state_change: Callable[[dict], None] | None = None,
        on_oauth_event: Callable[[str, dict], None] | None = None,
        allow_auth_park: bool = True,
    ) -> None:
        self._registry = registry
        self._post_connect = post_connect
        self.on_state_change = on_state_change
        self.on_oauth_event = on_oauth_event
        # A manager owned by a short-lived batch (a playbook pre-flight) cannot
        # hold its caller for a browser round-trip: nobody is standing by to
        # finish one, and the run must not pay the flow timeout per server.
        self._allow_auth_park = allow_auth_park
        self._conns: dict[str, MCPConnection] = {}
        # Handshakes still running. Held because every wait on one is shielded,
        # so a parked OAuth attempt outlives its waiter -- and a task still
        # pending when the loop closes has its exception printed by
        # ``asyncio.run`` itself, whoever did or did not retrieve it. `aclose`
        # is where they get reaped.
        self._handshakes: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        # Names already warned about, so a config poll does not repeat itself.
        self._warned_names: set[str] = set()
        # Every live attempt task. A cancel of whoever started a sync reaches the
        # coroutine that was awaiting an attempt, not the attempt itself, and a
        # deliberately detached one has no awaiter at all -- so shutdown needs a
        # handle on them or the handshakes, transports and stdio children outlive
        # the manager that owns them.
        self._attempt_tasks: set[asyncio.Task] = set()

    # ── Introspection ──────────────────────────────────────────────

    def status(self) -> list[dict]:
        """Snapshot of every known server, stable-ordered by name."""
        return [self._snapshot(c) for c in sorted(self._conns.values(), key=lambda c: c.name)]

    def session_of(self, server: str) -> Any:
        """The live session for one server, or None when it has none right now.

        The seam a caller uses when it holds a server *name* and nothing else --
        the resource and prompt meta-tools, which take ``server`` as an argument
        and so have no tool wrapper to borrow a session from.

        Safe without a generation of its own, because of where the session is
        written rather than what is checked here: only a committing attempt
        stores one, under the epoch check, and ``_disconnect_locked`` clears it
        in the same breath as closing the stack. A losing attempt therefore
        never publishes its session, so there is no way to be handed one whose
        stack is gone.

        ``connected`` is required on top of that: a record parked in
        ``auth_required`` may still have a live attempt behind it, and handing
        out its half-built session would let a caller talk to a server the user
        has not finished authorizing.
        """
        conn = self._conns.get(server)
        if conn is None or conn.state != "connected":
            return None
        return conn.session

    def servers_offering(self, primitive: str) -> list[str]:
        """Connected servers whose handshake declared ``primitive``, sorted.

        ``primitive`` is a field name on the SDK's ``ServerCapabilities`` --
        ``resources`` or ``prompts``. A server states what it offers once, at
        initialize, and cannot be asked again, so this reads what was captured
        then.

        The gate for whether the meta-tools exist at all. Most MCP servers offer
        only tools; advertising ``read_mcp_resource`` to a deploy where nothing
        serves resources spends schema on five calls that can only fail. This
        moves the tool list when a server connects or disconnects, which costs
        the prompt-cache prefix -- but that list was already moving at exactly
        those moments, because the server's own tools appear and disappear with
        it.
        """
        out = []
        for name, conn in self._conns.items():
            if conn.state != "connected" or conn.capabilities is None:
                continue
            if getattr(conn.capabilities, primitive, None) is not None:
                out.append(name)
        return sorted(out)

    def tool_map(self) -> dict[str, str]:
        """Registered tool name -> owning server name."""
        return {n: ref.server for n in self._registry.names() if (ref := self._registry.origin_of(n))}

    def _snapshot(self, conn: MCPConnection) -> dict:
        from raven.mcp.oauth import pending_url

        return {
            "name": conn.name,
            "transport": resolve_transport(conn.config) or "unknown",
            "state": conn.state,
            "connected": conn.state == "connected",
            "tool_count": len(self._registry.names_from(conn.name)),
            "error": conn.error,
            "enabled": bool(getattr(conn.config, "enabled", True)),
            # Present on every snapshot, null included. A reader that seeds this
            # from a pull needs the later `mcp.status` to carry the key in order
            # to clear it -- omitted, the merge leaves a settled server showing
            # the authorization link it was parked on.
            "auth_url": pending_url(conn.name),
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

    async def connect(self, name: str, cfg: Any, *, executor_provider=None, interactive: bool = True) -> dict:
        """Connect (or force-reconnect) one server.

        Already-connected servers with unchanged config are left alone;
        anything else (disconnected / error / auth_required / config
        changed) is torn down and connected fresh — this is the explicit
        retry entry point for plug.auth and install flows.

        ``interactive`` defaults to True because this *is* the explicit retry
        entry point: a caller pressing it is watching. A caller that is only
        relaying the result to someone elsewhere -- the agent's ``plugin`` tool,
        whose asker may be on an IM channel -- passes False, so the flow mints
        the URL without taking this host's screen.
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
        return await self._run_connect(conn, epoch, executor_provider, interactive=interactive)

    async def disconnect(self, name: str, *, drop: bool = False) -> None:
        """Detach one server. ``drop=True`` forgets the record entirely
        (server removed from config); ``drop=False`` keeps it visible as
        ``disconnected`` (server merely disabled).

        """
        async with self._lock:
            await self._disconnect_locked(name, drop=drop)

    async def executor_lost(self, reason: str) -> list[str]:
        """Every connected stdio server just lost its child process: the
        sandbox executor those transports ran under closed. Detach them and
        park the records in ``error`` -- the state the retry door
        (``connect``/authorize) owns -- instead of letting ``connected``
        stand for processes that are gone, which no reload would ever retry
        (the config did not change). HTTP and SSE transports do not ride the
        executor and are untouched; the next connect brings a fresh executor,
        because connects take a provider, not an instance.
        """
        lost: list[str] = []
        async with self._lock:
            for conn in list(self._conns.values()):
                if conn.state != "connected" or resolve_transport(conn.config) != "stdio":
                    continue
                conn.epoch = None
                for t in self._registry.names_from(conn.name):
                    self._registry.unregister(t)
                if conn.stack is not None:
                    await self._close_stack(conn.stack)
                    conn.stack = None
                conn.session = None
                conn.capabilities = None
                self._set_state(conn, "error", reason)
                lost.append(conn.name)
        if lost:
            logger.warning("MCP: sandbox executor closed under connected stdio server(s): {}", ", ".join(lost))
        return lost

    def config_changed(self, cfg_servers: dict) -> bool:
        """Whether :meth:`apply_config` would do anything -- without doing it.

        This and :meth:`apply_config` are a pair, split because their costs
        differ by kind, not by degree: this one compares config in memory and
        is safe to call on a timer, that one talks to transports. Anything that
        can fire often asks here first. (An earlier revision declared the pair
        as two ``Protocol``
        classes; nothing referenced them, this repo runs no type checker, so
        they asserted a contract that nothing could check. The contract is
        stated here instead, where a reader of the implementation sees it.)

        The cheap gate in front of the expensive path: no connect, no
        subprocess, no network. Everything that can fire often (the reload RPC,
        a file-watch tick) asks this first, so a poll against unchanged config
        costs one ``model_dump`` per configured server instead of a reconnect
        storm. Cheap, not free -- worth knowing before putting it on a tight
        timer with a large server set.

        A server parked in ``error``/``auth_required`` with unchanged config
        reads as unchanged on purpose, matching what ``apply_config`` does with
        it: retrying a dead server on every poll is the storm this gate exists
        to prevent, and ``connect()`` is the explicit retry.
        """
        desired = {n: c for n, c in cfg_servers.items() if getattr(c, "enabled", True)}
        if set(desired) - set(self._conns):
            return True
        for name, conn in self._conns.items():
            if name not in desired:
                # A record config no longer wants. Only counts as work if there
                # is something to take away -- a record already down with no
                # tools is what a previous apply left behind. Note this must
                # not return the negative case: another record further along
                # may still have changed.
                if self._registry.names_from(name) or conn.state != "disconnected":
                    return True
            elif _cfg_fingerprint(conn.config) != _cfg_fingerprint(desired[name]):
                return True
            elif conn.state == "disconnected":
                return True
        return False

    def _warn_unsanitary_names(self, cfg_servers: dict) -> None:
        """Say so when a server name will not survive into its tools' names.

        Not a rejection. A config file that this build refuses costs the user a
        raven that will not start, over a character -- too steep for a problem
        whose whole effect is that some names get rewritten. What is worth a
        line is that the rewriting is otherwise silent: the names the model
        sees, and the ones ``ext.list`` and the web panel show, are not the ones
        the config file spells, and nothing else says so.

        Deliberately *not* claimed here: that a ``disabled_tools`` entry stops
        matching. It does not. ``spellings`` generates the pre-sanitising form
        for every origin and ``resolve_configured`` tests an entry against them,
        which is what :func:`raven.mcp.naming.legacy_tool_name` exists for.

        Measured before choosing this: all 40 MCP servers in the shipped
        catalogue are already clean, and dashes survive sanitising, so the
        warning should be rare in practice.
        """
        from raven.mcp.naming import PREFIX, SEPARATOR
        from raven.providers.tool_names import is_sanitary, sanitary_form

        for name in cfg_servers:
            if name in self._warned_names or is_sanitary(name):
                continue
            self._warned_names.add(name)
            logger.warning(
                "MCP server '{}' has characters no provider accepts in a tool name; its tools "
                "register under '{}' instead, which is the name the model and the panels see. "
                "Existing tools.disabled_tools entries keep matching either spelling.",
                name,
                f"{PREFIX}{SEPARATOR}{sanitary_form(name)}{SEPARATOR}<tool>",
            )

    async def apply_config(
        self, cfg_servers: dict, *, executor_provider=None, attempts: dict | None = None
    ) -> ApplyReport:
        """Reconcile live connections with the desired config.

        Reconciling, not restarting: a server whose config is unchanged and
        whose connection is live is not touched at all, so applying config
        while turns are running costs nothing for the servers nobody edited.
        Servers in ``error``/``auth_required`` with unchanged config are NOT
        retried here (see module docstring); ``connect()`` retries them.

        """
        reloaded = 0
        tools_changed = False
        pending: list[tuple[MCPConnection, object]] = []

        self._warn_unsanitary_names(cfg_servers)
        async with self._lock:
            desired = {n: c for n, c in cfg_servers.items() if getattr(c, "enabled", True)}

            for name in [n for n in self._conns if n not in desired]:
                conn = self._conns[name]
                had_tools = bool(self._registry.names_from(name))
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
                        # Recorded before the teardown, like the removal branch
                        # above: withdrawing a server's tools moves the
                        # model-facing surface whether or not the reconnect puts
                        # anything back, and a reconnect that fails or comes
                        # back empty otherwise reported the surface unchanged.
                        tools_changed = tools_changed or bool(self._registry.names_from(name))
                        await self._disconnect_locked(name, drop=True)
                pending.append(await self._begin_connect_locked(name, cfg))

        # Every pending server gets its attempt before anything is re-raised.
        # _begin_connect_locked already marked them all `connecting`, and the
        # guard above skips a record that is not `disconnected` -- so abandoning
        # the tail on the first failure would park those servers in `connecting`
        # permanently, which no later apply would retry.
        #
        # The attempts run concurrently: they share no state but the registry
        # (single-threaded asyncio dict ops) and the commit lock, and connecting
        # serially meant one slow server delayed every server behind it -- the
        # measured cost was a whole turn spent waiting on a handshake that had
        # nothing to do with it. The executor is resolved once, under a lock,
        # because ``_start_executor`` was written for one caller at a time.
        # Filled before the gather, so a caller whose apply is cancelled mid-flight
        # still holds what it began. It belongs to the caller and not to this
        # object: a manager-wide slot is overwritten by whichever apply started
        # most recently, and a reaper reading that slot resets the attempt the
        # NEWER apply owns -- which discards that attempt's transport on commit
        # while the loop still reports MCP as connected. See `reset_for_retry`.
        if attempts is not None:
            attempts.update({c.name: e for c, e in pending})

        first_error: SandboxInitError | None = None
        shared_executor = self._shared_executor_provider(executor_provider)
        results = await asyncio.gather(
            *[self._attempt_or_detach(conn, epoch, shared_executor) for conn, epoch in pending],
            return_exceptions=True,
        )
        for res in results:
            # ``reloaded`` counts records touched, not connections that came up:
            # the detach branch above counts too, and a detach is not a connect.
            # A caller wanting "did it work" reads the per-server states.
            reloaded += 1
            if isinstance(res, SandboxInitError):
                first_error = first_error or res
                continue
            if isinstance(res, BaseException):
                raise res
            tools_changed = tools_changed or res["tool_count"] > 0
        if first_error is not None:
            raise first_error

        return ApplyReport(reloaded=reloaded, tools_changed=tools_changed)

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
        self._attempt_tasks.add(task)
        task.add_done_callback(self._attempt_tasks.discard)
        if parked is None:
            return await task
        park_wait = asyncio.ensure_future(parked.wait())
        try:
            done, _ = await asyncio.wait({task, park_wait}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # ``asyncio.wait`` does not cancel what it waits on, so without this
            # the handshake -- and the stdio child and transport inside it --
            # survives the cancel and keeps using a sandbox executor the
            # canceller is about to close. Taken with us and awaited, so the
            # attempt's own abort path has run by the time this returns.
            task.cancel()
            with suppress(BaseException):
                await task
            raise
        finally:
            park_wait.cancel()
        if task in done:
            return await task
        task.add_done_callback(_log_detached_connect)
        return self._snapshot(conn)

    async def reap_attempts(self) -> int:
        """Cancel and await every live attempt. Returns how many were live.

        The one place attempts are reaped. Two kinds reach here and neither has
        an awaiter that can stop it: one whose sync was cancelled (``asyncio.wait``
        does not cancel what it waits on), and one deliberately detached at the
        browser-authorization step -- right while the process lives, a leak once
        it is stopping.

        Both levels, because neither set covers the other. ``_attempt_tasks``
        holds only what ``apply_config`` started: ``connect()`` -- the explicit
        retry behind ``plug.auth`` and the installs -- awaits ``_run_connect``
        inline, so there is no attempt task to hold and its shielded handshake is
        the only handle anyone has on it. Going the other way, cancelling an
        attempt does reach the handshake inside it (``_handshake_watchdog`` takes
        its shielded task down when its own caller is cancelled), so for an
        ``apply_config`` attempt the second pass finds nothing.

        Re-read rather than snapshotted, because a connect can be started while
        this is awaiting the last batch -- a reload or an install landing on a
        stack that is shutting down. Bounded so a caller that never stops
        starting them cannot hold shutdown open.
        """
        reaped: set[asyncio.Task] = set()
        for _ in range(_REAP_ROUNDS):
            attempts = [t for t in self._attempt_tasks if not t.done()]
            # Whatever no attempt task spoke for -- see the docstring.
            live = attempts + [t for t in self._handshakes if not t.done()]
            if not live:
                return len(reaped)
            reaped.update(attempts)
            for t in live:
                t.cancel()
            with suppress(BaseException):
                await asyncio.wait(live, timeout=5)
        # Neither set is cleared anywhere: the done callback each task carries is
        # what removes it, and dropping a reference to one this call did not
        # cancel is how a handshake registered mid-await survived an `aclose`
        # that then reported itself finished.
        logger.warning(
            "MCP: still starting connects after {} reap rounds; shutting down with {} left",
            _REAP_ROUNDS,
            len([t for t in (*self._attempt_tasks, *self._handshakes) if not t.done()]),
        )
        return len(reaped)

    async def reset_for_retry(self, attempts: dict) -> list[str]:
        """Make one cancelled ``apply_config``'s attempts retryable.

        ``apply_config`` skips any record that is not ``disconnected``, and a
        reload deliberately does not retry an ``error`` row -- both correct for a
        server that failed on its own, and both fatal for one whose attempt was
        cancelled out from under it, which is then never tried again for the life
        of the process. This is the seam that makes such a cancel recoverable, so
        it is called by whoever did the cancelling.

        ``attempts`` is the name/epoch mapping THAT apply filled, held by the
        caller for the life of its own call. Never a scan of every record and
        never a shared "most recent apply" slot: both reach attempts this caller
        never began, and resetting one of those is worse than the bug this fixes.
        Its commit checks the state being rewritten, so the transport is
        discarded while the loop still reports MCP as connected, and the server
        is lost with no error anywhere.

        The epoch is the discriminator even within the mapping: a record whose
        epoch has moved on was taken over after this apply began, by a
        ``plug.auth`` or another apply, and belongs to that one now.
        ``connected`` and ``auth_required`` are left alone regardless -- the
        first holds a live session, the second a park whose attempt is still
        running and commits when the user clicks.
        """
        async with self._lock:
            names = []
            for name, epoch in attempts.items():
                conn = self._conns.get(name)
                if conn is None or conn.epoch is not epoch:
                    continue
                if conn.state in ("error", "connecting"):
                    self._set_state(conn, "disconnected", None)
                    names.append(name)
            return names

    async def aclose(self) -> None:
        """Detach everything and forget all records (loop shutdown)."""
        # Before the detach rather than after it, and that is the whole of the
        # difference: detaching is what wakes a parked handshake into failing, so
        # reaping first means there is no failure to keep out of the loop's
        # shutdown report rather than one that has to be swallowed after the
        # fact. It also has to come first for its own reason -- a handshake still
        # running holds a stack the detach is about to close.
        await self.reap_attempts()
        async with self._lock:
            for name in list(self._conns):
                await self._disconnect_locked(name, drop=True)

    # ── Connect machinery ──────────────────────────────────────────

    async def _begin_connect_locked(self, name: str, cfg: Any) -> tuple[MCPConnection, object]:
        from raven.mcp.oauth import cancel_pending

        existing = self._conns.get(name)
        if existing is not None and existing.state == "connected":
            # Withdrawn before the new attempt starts rather than after it
            # fails: a reconnect that does not come back would otherwise leave
            # the model holding tools whose session is gone. What a turn already
            # in flight sees instead is the registry's miss, which names both
            # readings (see ``ToolRegistry.execute``).
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
            result = await self._handshake_watchdog(
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
                # * the record must leave `connecting`. `apply_config` skips a
                #   record that is not `disconnected`, so one left mid-connect is
                #   never retried again for the life of the process.
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
                self._take_back(conn, epoch, result.names)
                await self._close_stack(stack)
                return self._snapshot(conn)
            conn.stack = stack
            conn.session = result.session
            conn.capabilities = result.capabilities
            # No local copy of ``registered``: the blacklist may have just
            # unregistered some of those names, and the registry is what knows.
            live = self._registry.names_from(name)
            self._set_state(conn, "connected")
            # After the state flip, not before: post_connect runs the loop's
            # meta-tool sync, and ``servers_offering`` only counts a record
            # once it is "connected" -- pre-flip, the sync could never see the
            # server this commit just connected, so a plug.auth connect of the
            # sole resources server never gained (or, on a forced reconnect,
            # silently lost) the five meta-tools. The old order guarded a
            # blacklist that unregistered names at connect; it is report-only
            # now.
            if self._post_connect is not None:
                self._post_connect()
            logger.info("MCP server '{}': connected, {} tools registered", name, len(live))
            return self._snapshot(conn)

    async def _handshake_watchdog(self, name: str, coro) -> "Connected":
        """Await the handshake, but never forever.

        The MCP SDK's streamable-http transport can wedge: an exception in
        its HTTP auth flow (e.g. a failed dynamic client registration) dies
        in the transport's read task and never reaches ``initialize()``,
        which then waits for a response that cannot come. Without a bound
        the connection is 'connecting' for the rest of the process.

        A server parked at the browser-authorization step is exempt while
        it stays parked - that wait blocks on the user, and the OAuth flow
        enforces its own timeout. Only an *interactive* park earns that, though:
        a background connect nobody was asked to authorize has no click coming,
        so extending its leash buys nothing and costs the caller the whole flow
        timeout before the same degrade happens anyway.
        """
        task = asyncio.ensure_future(coro)
        # Retrieving the exception keeps the "never retrieved" warning away; the
        # set keeps the task reachable so `aclose` can reap one that is still
        # parked, which is the half that silences `asyncio.run`'s own shutdown
        # report. Neither alone is enough.
        task.add_done_callback(_drain_exception)
        self._handshakes.add(task)
        task.add_done_callback(self._handshakes.discard)
        deadline = asyncio.get_running_loop().time() + _AUTH_PARK_MAX
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=_HANDSHAKE_TIMEOUT)
            except asyncio.TimeoutError:
                from raven.mcp.oauth import auth_wait_servers

                # The exemption is bounded. A flow whose redirect registered but
                # whose callback never arrives (the transport dying inside the
                # SDK's auth path is exactly what this watchdog exists for) leaks
                # its pending entry, and an unbounded exemption would then park
                # this server in `connecting` for the life of the process.
                if name in auth_wait_servers(parkable_only=True) and asyncio.get_running_loop().time() < deadline:
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

    def _take_back(self, conn: MCPConnection, epoch: object, names: list[str]) -> None:
        """Unregister what a dead attempt added, unless a newer one owns it now.

        Ownership is the epoch, not a stored list of names. Two attempts on one
        server register the *same* names -- the registry keys by name, so the
        second registration replaces the first and no name-keyed record can say
        which attempt a name belongs to. The record's epoch can: it names the
        attempt the record currently wants.

        Three states, not two, and collapsing the last two was a bug. A record
        whose epoch is ``None`` wants *no* attempt -- ``_disconnect_locked``
        clears it as its first act -- which is not the same as a newer attempt
        having taken over, and reading it as one left a disabled server
        advertising a tool whose stack had already been closed. The change probe
        then read that orphan through ``names_from`` and answered "still work to
        do" on every poll, forever.

        | ``current.epoch``    | Means                     | These names       |
        | -------------------- | ------------------------- | ----------------- |
        | this ``epoch``       | the record still wants us | come back out     |
        | ``None``             | the record wants nobody   | come back out     |
        | another epoch        | a newer attempt owns them | are left alone    |
        | record gone entirely | nobody will ever reap it  | come back out     |

        Only the third may be left: stripping those would leave the winner
        advertising tools the registry can no longer dispatch.
        """
        current = self._conns.get(conn.name)
        if current is not None and current.epoch is not None and current.epoch is not epoch:
            return
        for t in names:
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
        self._take_back(conn, epoch, registered)
        await self._close_stack(stack)
        if self._conns.get(conn.name) is conn and conn.epoch is epoch and conn.state in ("connecting", "auth_required"):
            self._set_state(conn, state, error or None)

    async def _disconnect_locked(self, name: str, *, drop: bool) -> None:
        """Detach one server's transport and withdraw its tools."""
        from raven.mcp.oauth import cancel_pending

        conn = self._conns.get(name)
        if conn is None:
            return
        conn.epoch = None  # invalidates any in-flight attempt
        # Detach, not a newer attempt: this is the last thing a short-lived host
        # does, and a waiter told it was superseded sends the reader after a
        # second flow that never existed.
        cancel_pending(name, reason=f"MCP server {name!r} was detached before authorization completed")
        for t in self._registry.names_from(name):
            self._registry.unregister(t)
        if conn.stack is not None:
            await self._close_stack(conn.stack)
            conn.stack = None
        # Dropped with the stack that owns them: the session is dead the moment
        # the stack closes, and the capabilities describe a connection that no
        # longer exists.
        conn.session = None
        conn.capabilities = None
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
        from raven.mcp.oauth import provider_for

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

        return await provider_for(
            conn.name, cfg, notify=notify, interactive=interactive, can_park=self._allow_auth_park
        )

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        """Whether a connect failure means "user must (re)authorize"."""
        from raven.mcp.oauth import is_auth_error

        return is_auth_error(exc)


__all__ = ["MCPConnection", "MCPConnectionManager"]
