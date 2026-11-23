"""MCP lifecycle glue: connect, sync, prewarm, apply, close."""

from __future__ import annotations

from typing import TYPE_CHECKING

from raven.agent.loop._shared import (
    SandboxInitError,
    asyncio,
    logger,
    suppress,
)

if TYPE_CHECKING:
    from raven.mcp.manager import MCPConnectionManager
    from raven.mcp.report import ApplyReport


async def _swallow_terminal_cancel(task: "asyncio.Task") -> None:
    """Await a task that was just cancelled, swallowing only ITS terminal state.

    Awaiting a cancelled task re-raises ``CancelledError`` -- the same type a
    cancellation addressed to the awaiting task itself arrives as. Swallowing
    both (a bare ``suppress``) let a second supersession's cancel vanish here:
    the superseded apply survived its own cancellation, started its stale
    handshake, and starved the newest admitted answer behind it. The two are
    told apart by the current task's own cancellation count.
    """
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:  # noqa: BLE001 - the predecessor's failure is not ours to re-raise
        pass


_MCP_AUTH_REMAIN = (
    "installed, awaiting authorization. Its tools remain in this conversation's tool list, but "
    "calls to them fail until it is authorized -- by the `plugin` tool's authorize action, or by "
    "the user in the plugin panel."
)
_MCP_CONNECTING_REMAIN = (
    "installed, still connecting. Its tools remain in this conversation's tool list, but calls to "
    "them fail until the handshake finishes."
)
_MCP_DISCONNECTED_REMAIN = (
    "disconnected. Its tools remain in this conversation's tool list, and calls to them fail until it reconnects."
)
_MCP_DISCONNECTED_JOIN = (
    "disconnected. Its tools are absent from this conversation's tool list; they join at the next compaction "
    "or new conversation after it reconnects."
)
_MCP_DISABLED_REMAIN = (
    "disabled in settings. Its tools remain in this conversation's tool list, and calls to them "
    "fail until it is enabled again."
)
_MCP_DISABLED_JOIN = (
    "disabled in settings. Its tools are absent from this conversation's tool list; they join at the "
    "next compaction or new conversation after it is enabled again."
)
_MCP_REMOVED_REMAIN = (
    "no longer loaded. Its tools remain in this conversation's tool list, and calls to them fail "
    "until it is added back."
)
_MCP_REMOVED_JOIN = (
    "no longer loaded. Its tools are absent from this conversation's tool list; they join at the next "
    "compaction or new conversation if it is added back."
)
_MCP_AUTH_LIVE = (
    "installed, awaiting authorization. Its tools are absent from this turn's definitions until "
    "it is authorized -- by the `plugin` tool's authorize action, or by the user in the plugin "
    "panel."
)
_MCP_AUTH_JOIN = (
    "installed, awaiting authorization. Its tools are absent from this conversation's tool list; "
    "after it is authorized they join at the next compaction or new conversation -- by the "
    "`plugin` tool's authorize action, or by the user in the plugin panel."
)
_MCP_CONNECTING_LIVE = (
    "installed, still connecting. Its tools are absent from this turn's definitions and register "
    "themselves when the handshake finishes, so they are available from a later turn without "
    "anyone acting."
)
_MCP_CONNECTING_JOIN = (
    "installed, still connecting. Its tools join this conversation's tool list at the next "
    "compaction or new conversation."
)


class McpGlueMixin:
    """MCP lifecycle glue: connect, sync, prewarm, apply, close. Bodies moved
    verbatim from main.py."""

    async def mcp_executor_provider(self):
        """The sandbox executor an MCP connect should run under, started.

        Public by design (paper: contracts/mcp_host.py): the market's
        authorize path passes this bound method to
        ``MCPConnectionManager.connect`` -- the same provider the loop's own
        reconciles hand over.
        """
        await self._start_executor()
        return self._executor

    def set_mcp_event_sink(self, sink) -> None:
        """Late-bind where per-server MCP events go (same shape as the DAG sink).

        ``sink`` is an async callable ``(method, params)``. Without it the manager
        still works, but every state change is silent -- and a client waiting on
        ``mcp.status`` / ``oauth.pending`` has no way to learn that an OAuth
        connect finished, which is most of what makes a browser round-trip
        completable at all.
        """
        self._mcp_event_sink = sink

    def _emit_mcp_event(self, method: str, params: dict) -> None:
        """Fire-and-forget bridge: the manager's callbacks are sync, the sink is not."""
        sink = self._mcp_event_sink
        if sink is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(sink(method, params))
        except RuntimeError:
            return  # no loop (a sync CLI path): nothing is listening anyway
        # Held rather than dropped: a bare create_task is collectable while it
        # is the only reference to a running task (the rule this file states
        # beside the prewarm), and a collected task silently drops an
        # ``mcp.status`` / ``oauth.pending`` frame.
        self._mcp_event_tasks.add(task)
        task.add_done_callback(self._mcp_event_tasks.discard)

    @property
    def mcp_manager_if_started(self) -> "MCPConnectionManager | None":
        """The connection organ if one exists -- never created by asking.

        The console's status peek must not build a manager just to learn that
        nothing is connected; the lazy ``mcp_manager`` property below is for
        operators about to use one. The object returned is the organ itself,
        so the door-roster guard pins who may spell this too.
        """
        return self._mcp_manager

    @property
    def mcp_manager(self) -> "MCPConnectionManager":
        """The per-server connection lifecycle, created on first use.

        Lazy rather than built in ``__init__`` because a loop with no MCP servers
        configured should not carry one, and because the registry it writes into
        is assembled after ``__init__`` in some entry points.
        """
        if self._mcp_manager is None:
            from raven.mcp.manager import MCPConnectionManager

            self._mcp_manager = MCPConnectionManager(
                self.tools,
                # MCP servers can register a name that is also blacklisted (e.g.
                # ``mcp_<server>_search``), and the manager records what survived,
                # so the blacklist has to be re-applied on every connect.
                post_connect=self._after_mcp_connect,
                on_state_change=lambda snap: self._emit_mcp_event("mcp.status", snap),
                on_oauth_event=lambda event, payload: self._emit_mcp_event(event, payload),
            )
        return self._mcp_manager

    def _after_mcp_connect(self) -> None:
        """Re-apply the blacklist, then re-gate the MCP meta-tools.

        Runs on every connect, not only the first: an MCP server can register a
        name that is also in ``disabled_tools``, and a server that just arrived
        may be the first one to serve resources or prompts.
        """
        self._report_reserved_disabled_tools()
        self._sync_mcp_meta_tools()

    def _sync_mcp_meta_tools(self) -> None:
        """Register the resource / prompt meta-tools iff some server serves them.

        Five schemas that no deploy without MCP should pay for, and that a deploy
        whose servers offer only tools should not pay for either -- most servers
        offer only tools, and advertising ``read_mcp_resource`` to them spends
        context on calls that can only fail.

        Gating no longer moves a session's tool array: the array is frozen per
        session, so these five enter or leave it only at a refresh point, like
        any other registration. Registering and unregistering here still keeps
        the live view correct -- the next capture sees the current answer -- and
        a captured array keeps their names (calls to them fail) until that
        refresh. What it buys is that a fresh array does not carry these five
        when no server serves them.

        Called after every connect and after every config apply, because both can
        change the answer -- and idempotent, so calling it when nothing moved
        registers and unregisters nothing.

        Idempotence is the reason these five names are not the operator's to
        switch off. The predicate is ``all(...)`` over the set, so one name
        missing reads as "the set is not installed" and puts the whole set back;
        anything else that removes a single member turns every connect into an
        unregister-and-re-register of all of them. ``_withheld_tool_names``
        therefore skips them by name, which makes this method their sole owner:
        they exist exactly while a connected server serves the primitive.
        """
        manager = self._mcp_manager
        if manager is None:
            return
        from raven.mcp.prompts import PROMPT_TOOL_NAMES, prompt_tools
        from raven.mcp.resources import RESOURCE_TOOL_NAMES, resource_tools

        for primitive, names, build in (
            ("resources", RESOURCE_TOOL_NAMES, lambda: resource_tools(manager, workspace=self.workspace)),
            ("prompts", PROMPT_TOOL_NAMES, lambda: prompt_tools(manager)),
        ):
            wanted = bool(manager.servers_offering(primitive))
            present = all(self.tools.has(n) for n in names)
            if wanted and not present:
                for tool in build():
                    self.tools.register(tool)
                logger.info("MCP: {} meta-tools registered", primitive)
            elif not wanted and any(self.tools.has(n) for n in names):
                for n in names:
                    self.tools.unregister(n)
                logger.info("MCP: {} meta-tools withdrawn -- no server offers them", primitive)

    def _mcp_tool_notices(self) -> list[str]:
        """Host facts about MCP tools the definitions cannot carry.

        One line per server whose tools the model cannot use, and the states
        need different lines:

        * ``auth_required`` -- the wait is on a person, and the answer names who
          can end it. Without this the model reads an unauthorized plugin as a
          capability that does not exist and says so.
        * ``connecting`` -- the handshake is still running. Reachable on any turn
          that overtakes it, which since ``prewarm_mcp`` is the ordinary shape of
          a first turn rather than a rarity: the turn no longer waits, so it can
          be assembled while servers are still coming up.
        * ``connected`` while the catalog is folded -- nothing else in the prompt
          names an installed server, so a model that does not think to search
          reports a capability it holds as missing. ``tool_search``'s own
          description says a catalog exists; only this says what is in it.
        * connected, disconnected, or disabled while its tools are OUTSIDE the
          session's frozen array -- the array is not recomputed until a new
          session or a compaction, so the line says what state the server is in
          and when its tools join. A server that connects mid-session is
          reachable through ``tool_call`` only if the model knows the name and
          arguments; the honest fact is the refresh point.

        The array is the session's snapshot (``ToolRegistry.get_definitions``):
        captured once and refreshed only at a new session, a compaction, or a
        log id change. A line the log already holds for a server whose tools a
        refresh then drops stays: the server is still down, so the notice is
        still true, and only its wording moves from "remain in the list" to
        "are absent from it". Dropping the key instead would make the consumer
        clear a live note. Which servers the log holds a line for is read from
        the log at every assembly, so a ``/clear`` starts with none and a
        reopened or restarted session with exactly what it holds. This line states an inventory and promises no route, which
        is what keeps it true. This block is rendered once per turn, while
        the fold is decided per model call against an array a
        ``before_iteration`` hook may narrow first
        (``HookDecision.modified_tools``) -- so a turn-level sentence about what
        this request folded, or about reaching it through ``tool_search`` /
        ``tool_call``, can be falsified after it is written. "This server is
        connected and offers N tools" cannot.

        The folded count is the server's tools the array carries, not the
        manager's ``tool_count``: that one counts every wrapper the server
        registered, including one the operator switched off, which no surface
        offers. The fold gate is only about noise -- below it every schema is in
        the array and the line tells the model nothing it cannot see.

        Rendered into the runtime-context block, not the system prompt -- the
        set changes turn to turn and must never be cached with the prefix. This
        is the current state; the engine sends a line only when it differs from
        what the replayed history already says (``runtime_context.notice_changes``),
        which is why each line is built by ``mcp_notice``.
        """
        # Deferred: ``raven.context_engine`` imports back out of this tier.
        from raven.context_engine.runtime_context import mcp_notice

        # getattr: the engine factory takes this callable during __init__,
        # before the manager attribute is assigned further down.
        mgr = getattr(self, "_mcp_manager", None)
        if mgr is None:
            return []
        # Stated as fact, not as a directive: this block's own header says
        # "metadata only, not instructions", and the model is told to treat it
        # that way -- an imperative here would be either ignored or a fence
        # violation. The fact alone is enough to stop it reporting a missing
        # capability.
        controller = getattr(self, "tool_search_controller", None)
        folded = controller is not None and controller.search_visible()
        offered: set[str] = set()
        if folded:
            offered = {d["function"]["name"] for d in self.tools.get_definitions()}
        # None outside a turn: the pre-freeze behaviour, where the array is the
        # live view and a server's tools appear as soon as it connects.
        snapshot = self.tools.session_definition_names()
        snapshot_servers = self.tools.session_definition_servers() if snapshot is not None else frozenset()
        # A server whose tools leave the array keeps its key while the log
        # holds a note about it: the next line says the tools are absent,
        # which is true, instead of letting the key vanish and the consumer
        # announce a clear while the server is still down.
        notified = self.tools.notified_mcp_servers() if snapshot is not None else frozenset()
        seen: set[str] = set()
        notices: list[str] = []

        def emit(server: str, text: str) -> None:
            notices.append(mcp_notice(server, text))

        for snap in mgr.status():
            name = snap["name"]
            seen.add(name)
            snap_names = set(self.tools.session_definition_names_of_server(name)) if snapshot is not None else set()
            live_names = set(self.tools.names_from(name))
            if not snap.get("enabled", True):
                if snap_names:
                    emit(name, _MCP_DISABLED_REMAIN)
                elif name in notified:
                    emit(name, _MCP_DISABLED_JOIN)
                continue
            state = snap["state"]
            if state == "auth_required":
                if snapshot is None:
                    text = _MCP_AUTH_LIVE
                elif snap_names:
                    text = _MCP_AUTH_REMAIN
                else:
                    text = _MCP_AUTH_JOIN
                emit(name, text)
            elif state == "connecting":
                if snapshot is None:
                    text = _MCP_CONNECTING_LIVE
                elif snap_names:
                    text = _MCP_CONNECTING_REMAIN
                else:
                    text = _MCP_CONNECTING_JOIN
                emit(name, text)
            elif snap["connected"]:
                added = live_names - snap_names
                gone = snap_names - live_names
                if snapshot is not None and snap_names and (added or gone):
                    # A reconnect can change the server's tool set; the frozen
                    # array kept the old names. Name both halves rather than
                    # reporting the connection as fully in the list.
                    parts: list[str] = []
                    if added:
                        parts.append(
                            f"{len(added)} new tool(s) join this conversation's tool list at the next "
                            "compaction or new conversation"
                        )
                    if gone:
                        parts.append(
                            f"{len(gone)} tool(s) that are no longer registered remain in the list and "
                            "calls to them fail"
                        )
                    emit(name, "connected; " + "; ".join(parts) + ".")
                elif snapshot is not None and not snap_names and live_names:
                    emit(
                        name,
                        f"connected, offering {len(live_names)} tool(s); they join this conversation's "
                        "tool list at the next compaction or new conversation.",
                    )
                elif folded:
                    mine = [n for n in live_names if n in offered]
                    if mine:
                        emit(name, f"connected, offering {len(mine)} tool(s).")
            elif snap_names:
                emit(name, _MCP_DISCONNECTED_REMAIN)
            elif name in notified:
                emit(name, _MCP_DISCONNECTED_JOIN)
        # A server removed outright has no row in status() at all. Its names may
        # still be in the frozen array, or the refresh may already have dropped
        # them; the wording follows the array, the key stays in both cases.
        for name in sorted((snapshot_servers | notified) - seen):
            emit(name, _MCP_REMOVED_REMAIN if name in snapshot_servers else _MCP_REMOVED_JOIN)
        return notices

    def prewarm_mcp(self) -> None:
        """Start the one-time MCP connect without waiting for it.

        Every path to MCP that is not ``run()``:

        * ``build_rpc_stack``, at assembly. This is the one that matters, because
          it is early enough that no turn has arrived yet.
        * ``run_turn``, on every turn. Idempotent, so it costs three boolean
          reads once MCP is up; what it still covers is each host that never
          reaches the line above -- ``raven tui``, which has no other MCP path at
          all -- plus the reconnect after ``close_mcp`` and the retry after an
          assembly-time prewarm that failed.

        Fire and forget on purpose: a turn must not inherit this wait, which once
        held a message for 10m35s. The turn is assembled from whatever is up, and
        ``_mcp_tool_notices`` is what tells the model why a configured server's
        tools are absent. Call order matters at assembly: the MCP event sink must
        be bound first, or the authorization URL an OAuth server parks on is
        minted with nobody to publish it to.
        """
        if self._mcp_connected or self._mcp_connecting:
            return
        live = self._live_mcp_servers()
        if live is not None:
            # Consume the admitted live answer rather than the construction
            # snapshot: an edit that landed after boot must not have the stale
            # set connected over it -- and a racing caller that wins the claim
            # below applies the same truth, which is what makes the supersede
            # path in ``reconcile_mcp_from_live`` safe to retry.
            self._mcp_servers = live
        if not self._mcp_servers:
            return
        # Claimed here rather than inside the task, which is the whole reason
        # this does not just call ``_connect_mcp``: a coroutine handed to
        # create_task does not run until the loop next yields, and a turn
        # arriving in that gap would read the flag as unset and run the entire
        # blocking connect itself -- the exact wait this exists to remove.
        self._mcp_connecting = True
        # This prewarm's own attempt token, held here for the reap. Not read back
        # off the manager: a manager-wide record of "the most recent apply" is
        # overwritten by a config apply that starts meanwhile, and the reap would
        # then reset that newer apply's attempt instead of its own.
        attempts: dict[str, object] = {}
        self._mcp_prewarm_attempts = attempts

        async def _prewarm() -> None:
            try:
                await self.apply_mcp_config(self._mcp_servers, attempts=attempts)
            except SandboxInitError as exc:
                # Not fatal the way it is in run(): that caller shuts the loop
                # down because it owns the executor, while a turn here runs
                # with whatever came up and stdio servers simply stay down.
                logger.error("MCP prewarm: the sandbox could not start, so stdio servers stay down: {}", exc)
            except Exception:
                logger.exception("MCP prewarm failed; the servers it did not reach join a later turn")
            finally:
                self._mcp_connecting = False

        # Held rather than dropped: a bare create_task is collectable while it
        # is the only reference to a running task.
        self._mcp_prewarm_task = asyncio.create_task(_prewarm())

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers, awaited (one-time).

        For ``run()``, which has nothing else to do until the runtime is up and
        which acts on ``SandboxInitError`` -- so it must reach that handler
        rather than a log line. Every other caller wants ``prewarm_mcp``: a turn
        must never inherit this wait, which once held a message for 10m35s.
        """
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        # Set flag synchronously before the first await — asyncio is single-threaded so no
        # context switch occurs here; a lock is not needed for this mutual-exclusion pattern.
        self._mcp_connecting = True
        try:
            # Sets ``_mcp_connected`` itself, so every path that brings MCP
            # up agrees on the flag rather than only this one.
            await self.apply_mcp_config(self._mcp_servers)
        finally:
            self._mcp_connecting = False

    def reconcile_mcp_from_live(self) -> None:
        """Apply an out-of-band edit of ``tools.mcpServers`` at the turn boundary.

        The RPC write paths call ``apply_mcp_config`` themselves; what this
        covers is the file changed behind the process's back -- a hand edit,
        another process -- which today only takes effect on whichever frontends
        remember to poll the reload RPC. Checked here because the turn boundary
        is when the answer is next consumed: an idle process deliberately does
        not reconcile, since no turn means no consumer for the tools.

        Fire and forget for the same reason ``prewarm_mcp`` is -- a turn must
        not inherit a handshake. What this can do to the running turn is
        asymmetric, and deliberately so: an *attach* cannot grow the turn's
        array (``ToolRegistry.turn_scope`` freezes arrivals), while a *detach*
        can still take tools out mid-turn -- that is the tightening rule, and a
        call that races the teardown gets the worded absent-tool answer from
        ``ToolRegistry.execute`` rather than a bare miss.

        Lane ownership is one rule with no owner exempt: WHOEVER STILL AGREES
        WITH THE FILE KEEPS THE LANE. A pending prewarm, and equally a pending
        apply this method itself started earlier, holds the lane only while
        the file matches the set it is applying; the moment the file says
        otherwise the stale attempt is reaped -- cancelled, its servers
        returned to a retryable state -- and the live answer is applied. A
        handshake can park for minutes (OAuth), and an owner exempt from
        supersession would defer an addition indefinitely and keep a revoked
        server's handshake alive, breaking the tightening rule.
        """
        servers = self._live_mcp_servers()
        if servers is None:
            return
        pending = self._mcp_reconcile_task
        if pending is not None and not pending.done():
            if self._same_mcp_config(servers, self._mcp_reconcile_target or {}):
                return
            self._spawn_live_apply(servers, prior=(pending, self._mcp_reconcile_attempts))
            return
        if self._mcp_connecting or (not self._mcp_connected and bool(self._mcp_servers)):
            if self._same_mcp_config(servers, self._mcp_servers):
                return
            self._spawn_live_apply(servers, prior=None)
            return
        if not self.mcp_config_changed(servers):
            return
        self._spawn_live_apply(servers, prior=None)

    def _spawn_live_apply(self, servers: dict, *, prior: "tuple[asyncio.Task, dict] | None") -> None:
        """Start the background apply of the admitted live answer, superseding
        whatever stale attempt still holds the lane.

        ``prior`` is a pending apply this reconcile started earlier, captured
        before the bookkeeping below overwrites it. A pending prewarm needs no
        handle: ``reap_mcp_prewarm`` is idempotent and reaps it by its own
        bookkeeping (and no-ops when there is none). Both reaps return their
        servers to a retryable state -- a cancelled handshake left in ``error``
        would be deliberately never retried.
        """
        attempts: dict[str, object] = {}
        self._mcp_reconcile_attempts = attempts
        self._mcp_reconcile_target = servers

        async def _run() -> None:
            try:
                await self.reap_mcp_prewarm()
                if prior is not None:
                    prior_task, prior_attempts = prior
                    prior_task.cancel()
                    try:
                        await _swallow_terminal_cancel(prior_task)
                    finally:
                        # The predecessor's attempt reset survives THIS task's
                        # own cancellation: each layer settles the one before
                        # it even while dying, so a chain of supersessions
                        # leaves no attempt parked in ``error``.
                        if self._mcp_manager is not None and prior_attempts:
                            with suppress(Exception):
                                await self._mcp_manager.reset_for_retry(prior_attempts)
                await self.apply_mcp_config(servers, attempts=attempts)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MCP live reconcile failed; the servers it did not reach keep their state")

        # Held rather than dropped: a bare create_task is collectable while it
        # is the only reference to a running task.
        self._mcp_reconcile_task = asyncio.create_task(_run())

    @staticmethod
    def _same_mcp_config(a: dict, b: dict) -> bool:
        """Whether two desired server sets are the same config, by the same
        per-server fingerprint the manager reconciles with."""
        from raven.mcp.manager import _cfg_fingerprint

        return {n: _cfg_fingerprint(c) for n, c in a.items()} == {n: _cfg_fingerprint(c) for n, c in b.items()}

    def _live_mcp_servers(self) -> dict | None:
        """The MCP server set the file names now, or ``None`` for "no answer".

        The portable Agent Plugin packages' servers ride along: they are a
        fact about the packages on disk, not an answer the file gives, so a
        file that says nothing about the host's own servers still leaves them
        in place.
        """
        from raven.config.live import mcp_server_configs

        return self._with_package_servers(mcp_server_configs(self._live_config))

    @property
    def package_mcp_server_names(self) -> frozenset[str]:
        """The package servers this loop was assembled with.

        Fixed for the loop's generation: ``agentPlugins.mcpPackages`` is read
        when the runtime is built, so a package allowed or withheld since then
        differs from this set until the next start.
        """
        return frozenset(getattr(self, "_package_mcp_servers", None) or ())

    def _with_package_servers(self, servers: dict | None) -> dict | None:
        """The host's servers with the packages' servers added.

        ``None`` stays ``None``: that is the "no answer" the live reader
        returns for an absent or torn ``tools.mcpServers``, and the caller
        keeps the set the process already has. Turning it into a real answer
        here would make the next reconcile detach every host server.

        A host server always wins a name collision: the operator configured
        it, and a package may not replace it.
        """
        packages = getattr(self, "_package_mcp_servers", None)
        if not packages or servers is None:
            return servers
        merged = dict(packages)
        merged.update(servers)
        return merged

    def mcp_config_changed(self, cfg_servers: dict) -> bool:
        """Whether :meth:`apply_mcp_config` would do anything, without doing it.

        The gate in front of every caller that can fire on a timer -- the reload
        RPC, a config-file watch. Answering it stays in memory, so an unchanged
        config never reaches a transport.

        The answer is taken over the set :meth:`apply_mcp_config` would apply,
        package servers included: a caller that passes only the file's servers
        would otherwise hear "changed" every time a package contributes one,
        and prompt for a reload that does nothing.
        """
        return self.mcp_manager.config_changed(self._with_package_servers(cfg_servers) or {})

    async def apply_mcp_config(self, cfg_servers: dict, *, attempts: dict | None = None) -> "ApplyReport":
        """Reconcile live MCP connections with ``cfg_servers``.

        The entry point for everything that changes the server set while the loop
        runs -- a market install, an uninstall, an enable/disable, a config edit.
        Each server owns its own transport, so one can be attached or detached
        without restarting raven, and a disabled server is disconnected here
        rather than left running with its tools registered.

        Reconciling, not restarting: servers whose config did not change are not
        touched, so this is safe to call while turns are running.
        """
        servers = self._with_package_servers(cfg_servers)
        self._mcp_servers = servers
        # The blacklist is re-applied by the manager's post_connect hook, on every
        # connect rather than only the first -- an MCP server can register a name
        # that is also in disabled_tools.
        report = await self.mcp_manager.apply_config(
            servers, executor_provider=self.mcp_executor_provider, attempts=attempts
        )
        # After the apply, not only after a connect: a *detach* can take the last
        # server that served resources with it, and no connect fires for that.
        self._sync_mcp_meta_tools()
        # Whatever brought MCP up, it is up. Left unset, the one-shot lazy
        # connect would still run later and re-walk every server this apply
        # already attached, and the surfaces that read this flag as "MCP is
        # live" would report a working server as disconnected.
        self._mcp_connected = True
        return report

    async def reap_mcp_prewarm(self) -> None:
        """Cancel and reap an in-flight prewarm, leaving its servers retryable.

        Must run before anything the handshake is standing on is torn down --
        the manager, and the sandbox executor a stdio transport is spawned into.
        A handshake that loses its executor mid-flight is recorded as the
        server's ``error`` state, and an ``error`` row with unchanged config is
        deliberately never retried by a reload; cancelling instead takes the
        attempt through the manager's abort path, which returns the record to
        ``disconnected`` and leaves ``_mcp_connected`` false, so the next turn's
        prewarm connects it again.
        """
        task = self._mcp_prewarm_task
        self._mcp_prewarm_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await _swallow_terminal_cancel(task)
        finally:
            # Cancelling is not enough on its own. The cancel reaches the
            # handshake through the SDK, and the manager cannot tell that apart
            # from the transport aborting the flow itself -- so the record lands
            # in ``error``, which no later apply or reload retries. Measured:
            # without this the server was gone until a restart. In a ``finally``
            # so the reset also survives THIS reap's caller being cancelled
            # mid-await by a second supersession.
            attempts = self._mcp_prewarm_attempts
            self._mcp_prewarm_attempts = {}
            if self._mcp_manager is not None and attempts:
                with suppress(Exception):
                    await self._mcp_manager.reset_for_retry(attempts)

    async def close_mcp(self) -> None:
        """Close MCP connections and the sandbox executor."""
        await self.reap_mcp_prewarm()
        task = self._mcp_reconcile_task
        self._mcp_reconcile_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            # While the manager is still attached: nothing is connected now,
            # so this withdraws the five meta-tools. After the None below the
            # sync is a no-op, and a reconnect would find them bound to this
            # dead manager (the presence check would then skip re-registering
            # them against the new one).
            self._sync_mcp_meta_tools()
            self._mcp_manager = None
        self._mcp_connected = False  # reset so _connect_mcp() can reconnect after close
        self._mcp_connecting = False  # reset so a concurrent caller isn't permanently blocked
        await self.close_executor()  # always runs, even when no MCP servers are configured
