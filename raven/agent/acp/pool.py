"""One live ACP connection per agent, shared by every caller in this process.

Why a singleton rather than a field on the backend: ``SubagentManager`` and
``SubAgentDagTool`` each build their own backend object from the same config
(``loop/main.py`` hands the list to both setters), so a pool owned by a backend
instance would start two server processes for one agent -- one for ``spawn``, one
for ``run_subagent_dag`` -- neither aware of the other. Backends are also rebuilt
on every hot config apply, which would leak a process each time.

So the pool is module state keyed by agent name, and a backend is only its
client. That also matches the transport: ACP sessions live *inside* a
connection, so sharing the connection is what makes two sessions of the same
agent possible at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from raven.agent.acp import protocol
from raven.agent.acp.client import AcpClient, end_drain
from raven.agent.acp.journal import open_journal
from raven.agent.acp.permissions import auto_approver

# Budget for the one `initialize` a new connection owes, when the caller does
# not say. Generous because an adapter fetched by `npx` may be downloading
# itself on the first connect -- but an entry's own `readyTimeoutMs` is
# documented as exactly this budget, so a caller that has one passes it.
_HANDSHAKE_TIMEOUT_S = 120.0

SessionSink = Callable[[str, dict[str, Any]], Awaitable[None]]
"""Receives ``(method, params)`` for one session's notifications."""

# The parameters of `acquire` that decide what process gets launched. Named here
# rather than inlined so `test_the_launch_key_covers_every_launch_parameter` can
# hold the two in step: a parameter added to `acquire` and not to the key would
# be silently discarded for the life of the process, which is the exact bug this
# key exists to prevent.
LAUNCH_PARAMS = ("command", "cwd", "env")


def launch_key(*, command: str, cwd: str | None, env: dict[str, str] | None) -> str:
    """A digest of what a connection was launched from.

    Deliberately over the *effective* arguments rather than over the agent's
    config: `cwd` falls back to the calling task's workspace, so two configs
    that read identically can still need two processes. `ready_timeout_ms` is
    absent for the opposite reason -- it is how long the caller waits for the
    handshake, not something the process is launched with, so changing it must
    not throw away a working connection.
    """
    raw = json.dumps({"command": command, "cwd": cwd, "env": env or {}}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class _SessionRouter:
    """Fans one connection's notifications out to the session that owns them.

    A connection is shared, so ``session/update`` for two concurrent tasks
    arrives interleaved on one stream. Routing on ``params.sessionId`` is what
    keeps one task's tool calls out of another's transcript.
    """

    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._sinks: dict[str, SessionSink] = {}

    def attach(self, session_id: str, sink: SessionSink) -> None:
        self._sinks[session_id] = sink

    def detach(self, session_id: str, sink: SessionSink) -> None:
        """Remove ``sink`` only if it is still the one attached.

        Identity-checked for the same reason the spawn test registry checks it:
        an unconditional ``pop`` would let a finishing task tear down a *later*
        task's routing, and the symptom would be that task silently receiving no
        updates and being reported as an empty turn.
        """
        if self._sinks.get(session_id) is sink:
            del self._sinks[session_id]

    async def dispatch(self, method: str, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        sink = self._sinks.get(session_id) if isinstance(session_id, str) else None
        if sink is None:
            # Not an error: an agent may notify about a session raven has already
            # finished with (a late usage_update), and a connection-level
            # notification carries no sessionId at all.
            logger.debug("acp agent {!r}: unrouted {} for session {!r}", self._agent, method, session_id)
            return
        await sink(method, params)


class _Connection:
    """A client, the router whose lifetime is tied to it, and one lock per session."""

    def __init__(
        self,
        client: AcpClient,
        router: _SessionRouter,
        launch: str = "",
        initialize: Any = None,
        handshake_bytes: int = 0,
    ) -> None:
        self.client = client
        self.router = router
        self.handshake_bytes = handshake_bytes
        """Where the ``initialize`` exchange ends in the journal.

        A call records the range it occupied, which starts after this; naming
        the boundary is what lets a reader of one call still see the handshake
        that established the connection it ran on."""
        # What this process was launched from, so a later dispatch can tell
        # whether the connection it is about to reuse still matches its config.
        self.launch_key = launch
        self.initialize = initialize
        """What the agent answered to ``initialize`` on this connection.

        Kept because it is the authoritative capability report for the process
        actually serving requests, where a stored snapshot only describes the one
        that was verified."""
        self._session_locks: dict[str, asyncio.Lock] = {}

    @property
    def alive(self) -> bool:
        return self.client.alive

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """Serialises turns on one session id.

        Necessary, not defensive. ACP's ``session/update`` notifications carry a
        session id and nothing that identifies which request they belong to, so
        two prompts in flight on one session produce a single interleaved stream
        that cannot be split back apart. Two tasks *can* land on one session: a
        stateful agent resolves the same ``instance`` handle to the same id, and
        ``spawn`` -- unlike the DAG, which already serialises same-instance nodes
        -- puts no ordering on that.

        Sequential turns are also what a conversation means, so waiting is the
        correct behaviour rather than a limitation. Locks are kept per connection
        and die with it.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock


class AcpConnectionPool:
    """Process-wide registry of live ACP connections, one per agent name."""

    def __init__(self) -> None:
        self._connections: dict[str, _Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    async def acquire(
        self,
        *,
        name: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        on_request: Any = None,
        ready_timeout_s: float | None = None,
    ) -> _Connection:
        """The live connection for ``name``, starting or restarting it if needed.

        Serialised per agent so two concurrent first tasks cannot each launch a
        server. A connection whose process has exited is replaced rather than
        handed out: the caller would otherwise get a client whose every request
        fails with "connection is not open".

        A connection launched from arguments this call does not share is
        replaced for the same reason. Keyed on the name alone, the `command`,
        `cwd` and `env` handed in here were read once and then discarded, so an
        operator who edited an agent's command -- and confirmed it with Test,
        which launches its own un-pooled client and therefore handshakes the new
        one -- kept dispatching to the process launched from the old config for
        the life of the raven process.
        """
        key = launch_key(command=command, cwd=cwd, env=env)
        async with self._lock_for(name):
            existing = self._connections.get(name)
            if existing is not None and existing.alive and existing.launch_key == key:
                return existing
            if existing is not None:
                reason = "connection is dead" if not existing.alive else "launch config changed"
                logger.info("acp agent {!r}: {}, relaunching", name, reason)
                self._connections.pop(name, None)
                await existing.client.close()

            router = _SessionRouter(name)
            # Opened before launch so the handshake is the head of the file: the
            # `initialize` exchange belongs to the connection, not to whichever
            # call happened to be the one that started it.
            journal = open_journal(name)
            client = await AcpClient.launch(
                name=name,
                command=command,
                cwd=cwd,
                env=env,
                # Defaulted here rather than at the caller: answering a
                # permission request is a property of raven-as-an-ACP-client,
                # not of whichever backend happens to hold the turn, and every
                # caller leaving it unset is how an unanswered request came to
                # cancel turns.
                on_request=on_request if on_request is not None else auto_approver(name, observe=router.dispatch),
                on_notification=router.dispatch,
                journal=journal,
            )
            # The handshake belongs to establishing the connection, not to the
            # first caller: ACP has no usable state before `initialize`, and an
            # agent is entitled to reject anything sent ahead of it. Two of the
            # three servers measured here tolerate a `session/new` without one
            # and simply work; `codex-acp` answers `-32603 Internal error`, which
            # is the correct reading of the protocol and the one to build against.
            try:
                initialize = await client.request(
                    "initialize",
                    protocol.initialize_params(),
                    timeout=ready_timeout_s or _HANDSHAKE_TIMEOUT_S,
                )
            except BaseException:
                # A connection that never handshook is unusable, and leaving the
                # process running would leak it behind a failed acquire. Catching
                # BaseException rather than Exception because cancellation is the
                # likeliest way out of this await -- an adapter fetched by `npx`
                # can sit here for a minute -- and a connection abandoned here is
                # in nobody's bookkeeping: it never reached `_connections`, so
                # shutdown cannot find it either.
                await client.close()
                raise
            # After the handshake, so a call's own range starts where its own
            # work does and a reader can still find the handshake at [0, here).
            connection = _Connection(
                client, router, key, initialize, handshake_bytes=journal.offset if journal is not None else 0
            )
            self._connections[name] = connection
            return connection

    async def drop(self, name: str) -> None:
        """Close and forget one agent's connection, if any."""
        async with self._lock_for(name):
            connection = self._connections.pop(name, None)
        if connection is not None:
            await connection.client.close()

    async def close_all(self) -> None:
        """Close every connection. For process shutdown and for tests."""
        names = list(self._connections)
        for name in names:
            await self.drop(name)

    def live_agents(self) -> list[str]:
        return [name for name, conn in self._connections.items() if conn.alive]


_POOL: AcpConnectionPool | None = None


def get_pool() -> AcpConnectionPool:
    """The process-wide pool. Created on first use."""
    global _POOL
    if _POOL is None:
        _POOL = AcpConnectionPool()
    return _POOL


async def close_pool() -> None:
    """Close every pooled connection and forget the pool.

    Separate from ``close_all`` so a test can return the process to a clean
    state, rather than leaving a pool whose connections are all dead. Leaving
    drain mode is part of that clean state: the flag is process-global, so a
    test that set it would otherwise change how the next one cancels.
    """
    global _POOL
    end_drain()
    if _POOL is not None:
        await _POOL.close_all()
        _POOL = None


__all__ = ["AcpConnectionPool", "SessionSink", "close_pool", "get_pool"]
