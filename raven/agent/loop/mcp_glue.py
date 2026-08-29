"""MCP lifecycle glue: connect, sync, prewarm, apply, close. Bodies moved
verbatim from main.py.
"""

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


class McpGlueMixin:
    """MCP lifecycle glue: connect, sync, prewarm, apply, close. Bodies moved
    verbatim from main.py."""

    async def _mcp_executor(self):
        """The sandbox executor an MCP connect should run under, started."""
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
            asyncio.get_running_loop().create_task(sink(method, params))
        except RuntimeError:
            pass  # no loop (a sync CLI path): nothing is listening anyway

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

        Gating moves the tool array when a server connects or disconnects, which
        costs the prompt-cache prefix. That is not a new cost: the server's own
        tools appear and disappear at exactly those moments, so the array was
        already moving. What it buys is that the array does not carry these five
        the rest of the time.

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

        One line per enabled server whose tools are absent from this turn for a
        reason the model cannot see, and the two reasons need different lines:

        * ``auth_required`` -- the wait is on a person, and the answer names who
          can end it. Without this the model reads an unauthorized plugin as a
          capability that does not exist and says so.
        * ``connecting`` -- the handshake is still running. Reachable on any turn
          that overtakes it, which since ``prewarm_mcp`` is the ordinary shape of
          a first turn rather than a rarity: the turn no longer waits, so it can
          be assembled while servers are still coming up.

        Rendered into the runtime-context block, not the system prompt -- the
        set changes turn to turn and must never be cached with the prefix.
        """
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
        notices = []
        for snap in mgr.status():
            if not snap.get("enabled", True):
                continue
            if snap["state"] == "auth_required":
                notices.append(
                    f"MCP plugin '{snap['name']}': installed, awaiting authorization. Its tools are "
                    f"absent from this turn's definitions until it is authorized -- by the `plugin` "
                    f"tool's authorize action, or by the user in the plugin panel."
                )
            elif snap["state"] == "connecting":
                notices.append(
                    f"MCP plugin '{snap['name']}': installed, still connecting. Its tools are absent "
                    f"from this turn's definitions and register themselves when the handshake "
                    f"finishes, so they are available from a later turn without anyone acting."
                )
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
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
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

    def mcp_config_changed(self, cfg_servers: dict) -> bool:
        """Whether :meth:`apply_mcp_config` would do anything, without doing it.

        The gate in front of every caller that can fire on a timer -- the reload
        RPC, a config-file watch. Answering it stays in memory, so an unchanged
        config never reaches a transport.
        """
        return self.mcp_manager.config_changed(cfg_servers)

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
        self._mcp_servers = cfg_servers
        # The blacklist is re-applied by the manager's post_connect hook, on every
        # connect rather than only the first -- an MCP server can register a name
        # that is also in disabled_tools.
        report = await self.mcp_manager.apply_config(
            cfg_servers, executor_provider=self._mcp_executor, attempts=attempts
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
        with suppress(BaseException):
            await task
        # Cancelling is not enough on its own. The cancel reaches the handshake
        # through the SDK, and the manager cannot tell that apart from the
        # transport aborting the flow itself -- so the record lands in ``error``,
        # which no later apply or reload retries. Measured: without this the
        # server was gone until a restart.
        attempts = self._mcp_prewarm_attempts
        self._mcp_prewarm_attempts = {}
        if self._mcp_manager is not None and attempts:
            with suppress(Exception):
                await self._mcp_manager.reset_for_retry(attempts)

    async def close_mcp(self) -> None:
        """Close MCP connections and the sandbox executor."""
        await self.reap_mcp_prewarm()
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_manager = None
        self._mcp_connected = False  # reset so _connect_mcp() can reconnect after close
        self._mcp_connecting = False  # reset so a concurrent caller isn't permanently blocked
        await self.close_executor()  # always runs, even when no MCP servers are configured
