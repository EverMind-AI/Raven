"""Subagent manager for background task execution."""

import asyncio
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent import workdir
from raven.agent.subagent import activity
from raven.agent.subagent.backends import (
    ABORTED_ACTION_RESULT,
    AgentMeta,
    RavenLoopBackend,
    SubagentActionAbortedError,
    SubagentBackend,
)
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.direct_chat import (
    DirectChatCreation,
    DirectChatError,
    DirectChatRecord,
    DirectTurnMeta,
)
from raven.agent.subagent.instance_state import InstanceState, instance_state_path
from raven.agent.subagent.instances import get_registry, hold_handle, mint_handle
from raven.agent.subagent.registry import AgentRegistry, AgentRow
from raven.agent.subagent_history import SpawnRecord
from raven.agent.subagent_memory import (
    TRACE_BUDGET_S,
    EverosIdentity,
    identity_from_config,
    prime_from_turn,
    record_memories,
    trace_session_id,
)
from raven.config.schema import ExecToolConfig
from raven.providers.base import LLMProvider
from raven.providers.binding import ModelBinding, resolve
from raven.sandbox import SandboxConfig, build_executor
from raven.security.trust import wrap_untrusted
from raven.tracing import semconv, trace

# One hour: a runaway re-injection loop fires fast and trips the limit quickly,
# while legitimate spawns spread over time and age out before it bites.
_SPAWN_WINDOW_SECONDS = 3600
# A status row is never worth failing -- or hanging -- a spawn over: the gate
# a wedged CLI subagent already holds must never stall behind a slow or
# corrupt registry write.
_REGISTRY_WRITE_TIMEOUT_S = 2.0
# ``spawn`` reports a refusal by returning its reason rather than raising, so a
# caller that has to tell "dispatched" from "declined" has only the string. Both
# refusals below open with this, and the spawn tool tests for it before publishing
# anything that presumes the run exists.
SPAWN_REFUSED_PREFIX = "Spawn refused: "


async def _write_spawn_status(session_key: str | None, agent: str, handle: str, status: str) -> None:
    """Best-effort registry write for one spawn's status, swallowing any failure."""
    if not session_key:
        return
    try:
        await asyncio.wait_for(
            get_registry().upsert_spawn(session_key, agent, handle, status),
            timeout=_REGISTRY_WRITE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - a status row must never fail or hang a spawn
        logger.opt(exception=True).warning(
            "Subagent instance registry write failed for {}/{!r} (status={})",
            agent,
            handle,
            status,
        )


def _host_everos_base_url() -> str:
    """The host's own everos service, used by any sub-agent that names none."""
    from raven.plugin.memory.everos._health import DEFAULT_EVEROS_BASE_URL, configured_base_url

    try:
        from raven.config.raven import load_raven_config

        return configured_base_url(load_raven_config())
    except Exception:  # noqa: BLE001 - a missing plugin config must not sink the manager
        return DEFAULT_EVEROS_BASE_URL


async def write_memory_record_for(
    *,
    directory: Path,
    filename: str,
    agent: str,
    identity: EverosIdentity,
    resolve_session_id,
    instance: str | None = None,
    budget_s: float | None = None,
    prime: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Write one call's Memory record into a local record directory."""

    async def _write(text: str) -> None:
        (directory / filename).write_text(text, encoding="utf-8")

    kwargs = {"budget_s": budget_s} if budget_s is not None else {}
    await record_memories(
        agent=agent,
        identity=identity,
        resolve_session_id=resolve_session_id,
        write=_write,
        instance=instance,
        prime=prime,
        **kwargs,
    )


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        sandbox_config: "SandboxConfig | None" = None,
        owned_ids: set[str] | None = None,
        jina_api_key: str | None = None,
        max_concurrent: int = 8,
        max_spawns_per_hour: int = 30,
        agents: list | None = None,
        session_dir: "Callable[[str], Path] | None" = None,
    ):
        from raven.config.schema import ExecToolConfig

        # Agent home. Also the working-directory fallback for a spawn that
        # captured none, which is the pre-split behaviour.
        self.workspace = workspace
        # SessionManager.session_dir, so a call record lands beside the
        # transcript of the session that made it -- including the sessions
        # whose group only the manager can resolve (raven/agent/subagent_history.py).
        self.session_dir = session_dir
        self._fallback_sessions: Any = None
        # Spine submit, late-bound (the scheduler pins its home loop at
        # construction and is built inside each entry point's run loop; this
        # manager is built in AgentLoop.__init__ in the sync prologue). Wired via
        # set_submit before any announce; the result re-injection submits a
        # SUBAGENT-origin turn.
        self._submit = None
        # Optional client-facing sink, same late-bound pattern. The injection
        # above is invisible on the wire -- the next thing a client sees is an
        # assistant turn nobody asked, so a page cannot say WHERE a delegated
        # result re-entered the conversation. This announces that seam as its
        # own event; without a sink the announce is merely unmarked, not broken.
        self._delivery_sink = None
        self._fallback = ModelBinding(provider, model or provider.get_default_model())
        self.brave_api_key = brave_api_key
        self.jina_api_key = jina_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self._sandbox_config = sandbox_config
        self._owned_ids = owned_ids
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        # (session_key, agent, handle) -> {task_id, ...}, for one-instance
        # cancellation (a stop button) without touching the rest of the
        # session's spawns. A set, not a single id: the main agent can spawn
        # the same (agent, instance) twice concurrently before the first
        # completes, and cancel_by_instance must reach every one of them, not
        # just whichever spawn happened to overwrite the slot last.
        self._instance_tasks: dict[tuple[str, str, str], set[str]] = {}
        self._gate = asyncio.Semaphore(max_concurrent)
        # Kept alongside the gate because a Semaphore does not expose the value
        # it was built with, and the TUI's spawn HUD needs the cap to render a
        # "widest level / cap" ratio rather than a bare count.
        self.max_concurrent = max_concurrent
        # Operator kill switch for delegation, toggled from the TUI's agents
        # overlay. Refuses new spawns while set; running ones are left alone,
        # because pausing is how a user stops a fan-out from growing without
        # throwing away the work already in flight.
        self._paused = False
        self._max_spawns_per_hour = max_spawns_per_hour
        # Per-session spawn timestamps (monotonic), kept per session (not
        # per-process) so one busy session can't throttle others. Each deque is
        # pruned to the rolling window on access, so it self-bounds.
        self._session_spawn_times: dict[str, deque[float]] = {}
        # The one agent table this process dispatches against, shared with the DAG
        # tool rather than built twice (see ``AgentRegistry``). The in-process
        # factory is bound after construction because it is a bound method of this
        # object.
        self._record_tasks: set[asyncio.Task] = set()
        self._session_record_tasks: dict[str, set[asyncio.Task]] = {}
        self.registry = AgentRegistry()
        self.registry.set_builtin_builder(self.build_builtin_backend)
        self._configs = list(agents or [])
        self.registry.apply(self._configs)

    def refresh_agents(self) -> None:
        """Rebuild the agent table from the last-applied configs.

        Re-running ``apply`` re-derives every row's ``AgentCaps`` and rebuilds
        the external backends, which is how a newly recorded acp capability
        snapshot (the startup backfill, a Test) reaches the live table -- the
        row's statefulness and ``AcpAgentBackend._snapshot`` were materialized
        before the snapshot existed, so without this a fresh install reports
        the agent stateful to the roster and rejects it at dispatch.
        """
        self.apply_agents(self._configs)

    def build_builtin_backend(self, row: "AgentRow", build: Any = None) -> "RavenLoopBackend":
        """An in-process raven loop for one ``builtin`` row, narrowed for one dispatch.

        The registry's factory. Everything a loop needs beyond the row -- provider,
        model, agent home, exec config, the search credentials -- lives on this
        manager, which is why the factory is injected into the registry rather than
        written there.

        ``build`` is the already-narrowed pair the registry computed (the row's
        allow-lists intersected with this dispatch's), duck-typed on
        ``tools_allow`` / ``skills_allow``. The row's own ``model`` and
        ``restrict_to_workspace`` are per-agent overrides: unset, they inherit this
        manager's, so a row that says nothing about confinement cannot loosen it.
        """
        confine = getattr(row.config, "restrict_to_workspace", None)
        return RavenLoopBackend(
            provider=self.provider,
            model=getattr(row.config, "model", None) or self.model,
            agent_home=self.workspace,
            restrict_to_workspace=self.restrict_to_workspace if confine is None else confine,
            exec_config=self.exec_config,
            brave_api_key=self.brave_api_key,
            jina_api_key=self.jina_api_key,
            web_proxy=self.web_proxy,
            tools_allow=getattr(build, "tools_allow", None),
            skills_allow=getattr(build, "skills_allow", None),
        )

    def build_role_backend(self, build: Any = None) -> "RavenLoopBackend":
        """A full-capability raven-loop backend, narrowed by ``build`` if given.

        Retained for callers that hold no row: the availability probe, and a
        dispatch to the generic agent on an installation whose table failed to
        build. Ordinary dispatch goes through the registry instead, so a node's
        agent name is what decides which backend runs it.
        """
        return RavenLoopBackend(
            provider=self.provider,
            model=self.model,
            agent_home=self.workspace,
            restrict_to_workspace=self.restrict_to_workspace,
            exec_config=self.exec_config,
            brave_api_key=self.brave_api_key,
            jina_api_key=self.jina_api_key,
            web_proxy=self.web_proxy,
            tools_allow=getattr(build, "tools_allow", None),
            skills_allow=getattr(build, "skills_allow", None),
        )

    def apply_agents(self, configs: list) -> None:
        """(Re)build the agent table from config. Hot-appliable at runtime (P4).

        One call refreshes every consumer, because they all read the same
        registry: before this, the manager's dict and the DAG tool's were
        refreshed by two separate setters that each skipped a bad entry on its
        own, so a partial failure left the two rosters disagreeing.

        The configs are remembered as the refresh source: a hot-apply that
        replaces the roster must not be rolled back by a later
        :meth:`refresh_agents` -- the snapshot backfill runs its refresh
        asynchronously and can land after a user has already changed the table.
        """
        self._configs = list(configs)
        self.registry.apply(configs)

    def _resolve_backend(self, agent: str) -> SubagentBackend:
        """The execution backend for one agent name.

        Raises rather than substituting: falling back to the in-process loop for
        an unrecognized name answers *as* that agent, with none of its history and
        no sign to the caller that a substitution happened.
        """
        backend = self.registry.backend(agent)
        if backend is None:
            raise RuntimeError(
                f"sub-agent {agent!r} is not on the agent table (or its backend failed to build); nothing was run"
            )
        return backend

    def list_agents(self) -> list[AgentMeta]:
        """Advertised capabilities of every enabled agent (for the tool descriptions)."""
        return self.registry.meta()

    def everos_identity(self, agent: str | None) -> EverosIdentity | None:
        """The declared identity for ``agent``, or ``None`` when it declared none.

        Read off the registry row rather than kept in a map of its own: the row
        holds the config the identity is declared in, so a hot ``apply_agents``
        cannot leave the two disagreeing.
        """
        row = self.registry.get(agent or "")
        if row is None:
            return None
        return identity_from_config(getattr(row.config, "everos", None), _host_everos_base_url())

    def _schedule_memory_record(
        self,
        *,
        task_id: str,
        agent: str | None,
        handle: str,
        session_key: str | None,
        directory: Path,
        filename: str,
        instance: str | None = None,
        turn: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record what this call wrote into everos, in the background.

        Never awaited by the dispatch path: everos extraction runs an LLM, and a
        sub-agent's reply must not wait on the host's bookkeeping. Callers must
        not schedule this for a call that ended via ``CancelledError``: that
        poller would be created after the cancellation sweep took its snapshot,
        leaving it unreapable (see ``cancel_all`` / ``cancel_by_session``).

        ``turn`` is only read for a ``trace`` identity, whose memories nobody
        wrote -- it is what gets primed. An ``agent`` identity's memories were
        already written by the sub-agent itself, so its resolver still looks up
        the session id the registry has on file.
        """
        identity = self.everos_identity(agent)
        if identity is None:
            return

        if identity.source == "trace":
            # The host owns both the write and the read here, so it mints the
            # join key instead of resolving one the sub-agent committed.
            session_id = trace_session_id(agent or "", task_id)
            rows = turn or []

            async def _resolve() -> str | None:
                return session_id

            async def _prime(sid: str) -> bool:
                return await prime_from_turn(identity=identity, session_id=sid, turn=rows)

            prime, budget = _prime, TRACE_BUDGET_S
        else:

            async def _resolve() -> str | None:
                agent_id = await get_registry().lookup(session_key or "default", agent or "", handle)
                return f"{identity.session_prefix}{agent_id}" if agent_id else None

            prime, budget = None, None

        task = asyncio.create_task(
            write_memory_record_for(
                directory=directory,
                filename=filename,
                agent=agent or "",
                identity=identity,
                resolve_session_id=_resolve,
                instance=instance,
                budget_s=budget,
                prime=prime,
            )
        )
        self._track_record(task, session_key)

    def _track_record(self, task: asyncio.Task, session_key: str | None) -> None:
        """Index a memory poller so the cancellation sweeps can reap it.

        Its own index, not ``_track``: that one feeds ``_session_tasks``, whose
        reader refuses a working-directory change while a session has work in
        flight -- and a poller is not work the user is waiting on.
        """
        self._record_tasks.add(task)
        if session_key:
            self._session_record_tasks.setdefault(session_key, set()).add(task)

        def _done(t: asyncio.Task) -> None:
            self._record_tasks.discard(t)
            if session_key and (s := self._session_record_tasks.get(session_key)) is not None:
                s.discard(t)
                if not s:
                    del self._session_record_tasks[session_key]

        task.add_done_callback(_done)

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """Adopt the provider a live ``/model`` switch just built.

        Only the out-of-turn fallback moves. A spawn requested during a turn
        takes that turn's binding, so a subagent follows the conversation
        that asked for it rather than whatever this manager was built with.
        """
        self._fallback = ModelBinding(provider, model)

    @property
    def provider(self) -> LLMProvider:
        return resolve(None, self._fallback).provider

    @property
    def model(self) -> str:
        return resolve(None, self._fallback).model

    def _track(
        self,
        task_id: str,
        task: asyncio.Task,
        session_key: str | None,
        instance_key: tuple[str, str, str] | None = None,
    ) -> None:
        """Index a running task so the cancellation paths can reach it, and
        un-index it when it ends."""
        self._running_tasks[task_id] = task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)
        if instance_key is not None:
            self._instance_tasks.setdefault(instance_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]
            if instance_key is not None and (ids := self._instance_tasks.get(instance_key)) is not None:
                ids.discard(task_id)
                if not ids:
                    del self._instance_tasks[instance_key]

        task.add_done_callback(_cleanup)

    def _charge_dispatch_quota(self, quota_key: str) -> bool:
        """Charge one sub-agent dispatch to this session's rolling hour.

        False when the budget is spent. The window is per session (not
        per-process) so one busy session cannot throttle others, and each deque
        is pruned on access, so it self-bounds.
        """
        now = time.monotonic()
        window = self._session_spawn_times.setdefault(quota_key, deque())
        cutoff = now - _SPAWN_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._max_spawns_per_hour:
            return False
        window.append(now)
        return True

    def charge_dag_run(self, session_key: str | None) -> str | None:
        """Charge one DAG run to the same budget as a spawn; the refusal, or None.

        Shared rather than given its own allowance because both feed the one
        thing the budget exists to stop: a run announces its result as a new
        turn, which can submit more work, with no user input anywhere in the
        loop (see ``max_subagent_spawns_per_hour``). The concurrency gate does
        not bound that -- each dispatch finishes and frees its slot for the
        next. A run counts once however many nodes it carries; the gate is what
        rations the nodes.
        """
        quota_key = session_key or "default"
        if self._charge_dispatch_quota(quota_key):
            return None
        logger.warning(
            "DAG run refused: session {!r} hit the sub-agent dispatch rate limit ({}/hour)",
            quota_key,
            self._max_spawns_per_hour,
        )
        return (
            f"Error: this session hit its sub-agent dispatch rate limit "
            f"({self._max_spawns_per_hour} per hour, counting DAG runs and spawns together). "
            f"No sub-agent was run. It recovers automatically as earlier ones age out -- if this "
            f"is unexpected, the task may be looping; reconsider the approach instead of "
            f"submitting the graph again."
        )

    def adopt_background_run(self, run_id: str, task: asyncio.Task, session_key: str | None) -> None:
        """Put a task this manager did not start under the same reach as a spawn.

        A backgrounded DAG dispatches the same detached CLI children a spawn
        does, so ``/stop`` and the shutdown sweep have to find it too -- see
        :meth:`cancel_all` for what an unreachable one leaves behind. Indexed
        here rather than only on the DAG tool so every entry point's existing
        teardown covers it with no extra wiring.
        """
        self._track(run_id, task, session_key)

    @property
    def dispatch_gate(self) -> asyncio.Semaphore:
        """The one gate every sub-agent dispatch waits on, spawns and DAG nodes alike.

        Each dispatch runs its own sandbox VM, so the host resource being
        rationed is the same whichever tool asked for it. Handed to the DAG tool
        rather than duplicated there, so ``max_concurrent_subagents`` means the
        total in flight and not a per-tool allowance that silently multiplies.
        """
        return self._gate

    async def spawn(
        self,
        task: str,
        task_summary: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        agent: str | None = None,
        instance: str | None = None,
        instance_auto: bool = False,
        workspace: Path | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background.

        ``workspace`` must be captured here, at spawn time, rather than read
        later inside the background task: the sub-agent outlives the calling
        turn, and the turn's working-directory binding is released once that
        turn returns.

        ``agent`` is required of the model (the spawn tool's schema makes it so,
        with the whole table as its enum), and a caller that omits it is
        normalized to the generic built-in row here -- the one place that
        substitution happens, instead of the eight ``agent or RAVEN_LOOP_AGENT``
        branches this used to be spread across. The name is unchanged from what
        those branches produced, so existing instance-registry rows and
        direct-chat records still resolve.
        """
        agent = agent or GENERIC_AGENT
        if self._paused:
            logger.info("Spawn refused: delegation is paused")
            return (
                f"{SPAWN_REFUSED_PREFIX}delegation is paused. The user paused sub-agent "
                "spawning; do the work in this turn instead, or ask them to resume."
            )
        quota_key = session_key or "default"
        if not self._charge_dispatch_quota(quota_key):
            logger.warning(
                "Spawn refused: session {!r} hit spawn rate limit ({}/hour)",
                quota_key,
                self._max_spawns_per_hour,
            )
            return (
                f"{SPAWN_REFUSED_PREFIX}this session hit its subagent spawn rate limit "
                f"({self._max_spawns_per_hour} per hour). It recovers automatically "
                f"as earlier spawns age out — if this is unexpected, the task may "
                f"be looping; reconsider the approach instead of spawning again."
            )
        task_id = str(uuid.uuid4())[:8]
        display_summary = task_summary or task[:30] + ("..." if len(task) > 30 else "")
        # Computed once and carried in `origin` so the concurrency index below
        # and the registry rows this spawn later writes can never drift from
        # each other, or from `CliAgentBackend`'s own handle derivation.
        handle = instance or task_id
        effective_workspace = workspace or self.workspace
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_key": quota_key,
            "agent": agent,
            "instance": instance,
            "instance_auto": instance_auto,
            "handle": handle,
            "workspace": effective_workspace,
        }
        instance_key = (quota_key, agent, handle)

        # A row before the task even exists: a spawn queued behind a full gate
        # (or a sandbox VM still booting) would otherwise have no registry row
        # at all until it starts running, making it invisible and unstoppable
        # from the UI for however long it waits.
        await _write_spawn_status(session_key, agent, handle, "pending")

        # The binding of the turn that asked for this spawn, snapshotted here
        # rather than where the task starts running: it queues behind the
        # concurrency gate and a sandbox boot first, and a switch landing in
        # that window would hand it an endpoint chosen after it was asked for.
        # A subagent has no model of its own, so it follows its conversation.
        binding = resolve(None, self._fallback)
        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_summary, origin, binding.provider, binding.model)
        )
        self._track(task_id, bg_task, session_key, instance_key)

        logger.info("Spawned subagent [{}]: {}", task_id, display_summary)
        return f"Subagent [{display_summary}] started (id: {task_id}). I'll notify you when it completes."

    def _require_addressable(self, agent: str, *, doing: str) -> None:
        """Raise unless ``agent`` can hold a direct chat at all.

        Both conditions are properties of the *agent*, not of any one instance,
        so every entry point into a direct chat has to make the same two checks
        or they drift. Called before anything is opened, written or minted.

        Disabled or absent is refused rather than allowed to fall through to
        ``_resolve_backend``, which would answer as the built-in raven loop with
        none of that agent's history and no sign to the caller that a
        substitution happened (see ``enabled_third_party`` on why a shrunk
        roster must fail loudly).

        Stateless is refused because a direct chat is a *continuation*: against
        such an agent every turn starts from nothing, so the conversation on
        screen would be a sequence of unrelated first turns that reads as the
        instance forgetting.
        """
        row = self.registry.get(agent)
        if row is None or not row.enabled:
            raise RuntimeError(
                f"Cannot {doing}: {agent!r} is disabled or no longer configured, so it cannot "
                "be addressed. Any records it already has remain on disk."
            )
        if not self.declared_stateful(agent):
            raise RuntimeError(
                f"Cannot {doing}: {agent!r} is stateless, so each turn would start a fresh "
                "conversation with no memory of this one. Spawn it with a task instead."
            )

    async def create_instance(self, *, session_key: str, agent: str) -> DirectChatCreation:
        """Mint one addressable instance of ``agent`` without running a turn.

        How a user starts a direct chat with an agent nothing has delegated to
        yet. This is not a new lifecycle: ``chat`` writes this same row itself on
        an instance's first turn. What is new is that the instance can exist
        before anything has been said to it.

        The registry row is the only thing written. The per-turn record
        directories are ``DirectChatRecord.open``'s, and no turn has run -- which
        is why the handoff entry for a creation names no path.

        The write goes to the registry directly rather than through
        ``_write_spawn_status``: that helper swallows every failure, which is
        right beside a spawn that runs regardless and wrong here, where the row
        *is* the result and a silent loss would report an instance the user
        cannot then see. ``upsert_spawn`` already tolerates a failed flush on its
        own, so only a genuine failure reaches the caller.
        """
        self._require_addressable(agent, doing="create a new instance")

        handle = mint_handle(agent)
        created_at_ms = int(time.time() * 1000)
        await get_registry().upsert_spawn(session_key, agent, handle, "idle")
        logger.info("Created sub-agent instance {}/{} for session {}", agent, handle, session_key)
        return DirectChatCreation(agent=agent, handle=handle, created_at_ms=created_at_ms)

    async def chat(
        self,
        *,
        session_key: str,
        agent: str,
        handle: str,
        text: str,
        workspace: Path | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[str, DirectTurnMeta]:
        """Run one direct-chat turn against an existing instance.

        Unlike ``spawn`` this awaits its result rather than scheduling a
        background task: the caller is a turn on the spine, and its whole job is
        to carry this reply back to one client.

        The handle lock is held for the entire turn, so a direct chat and a
        main-loop spawn addressing the same instance queue rather than
        interleave that instance's conversation. See ``hold_handle``.

        Nothing here touches the session transcript. A direct chat exists to
        keep these exchanges out of the main agent's context, so the record
        directory is the only place the turn is written.

        Raises ``RuntimeError`` up front, before anything is opened or written,
        for an agent that cannot hold a direct chat at all -- see
        ``_require_addressable`` for which two conditions and why each is fatal.
        Raises ``DirectChatError`` (carrying this turn's ``DirectTurnMeta``)
        if the backend itself raises, so the caller's per-session handoff can
        still learn about a failed turn instead of never hearing about it.

        ``on_delta`` is offered to the backend only if the backend declares it
        can stream (``SubagentBackend.streams``); a transport that cannot is not
        asked to pretend. The return value is the whole reply either way, and it
        is what the record stores -- deltas are an observation of a turn, never
        the source of truth for one. A caller that streamed therefore has to not
        deliver the return value a second time.
        """
        self._require_addressable(agent, doing=f"chat with instance {handle!r}")

        session_dir = self._session_dir(session_key)
        effective_workspace = workspace or self.workspace
        task_id = str(uuid.uuid4())[:8]
        state = self.instance_state(session_key, agent, handle)

        record = DirectChatRecord.open(session_dir, agent=agent, handle=handle, task_id=task_id, task=text)
        async with self._hold_instance_slot(session_key, agent, handle), hold_handle(session_key, agent, handle):
            await _write_spawn_status(session_key, agent, handle, "running")
            backend = self._resolve_backend(agent)
            kwargs: dict[str, Any] = {}
            if state is not None:
                kwargs["history"] = state.load()
                kwargs["on_messages"] = state.save
            if on_delta is not None and getattr(backend, "streams", False):
                kwargs["on_delta"] = on_delta
            # Collected here as the spawn lane does it: a direct turn is a turn of
            # the same instance's conversation, and without this it contributed
            # only a prompt and an answer to the instance log while a spawned
            # call beside it contributed every step.
            #
            # Indexed by instance as well, which is how the conversation view
            # reaches it: the record name is a task id no reader of an instance
            # ever saw, so without this the steps of the turn on screen were
            # published and unreachable until the log landed at turn end.
            cancelled = False
            with activity.collecting(
                live_key=record.dir.name, instance=(session_key, agent, handle), prompt=text
            ) as did:
                try:
                    executor = build_executor(
                        self._sandbox_config,
                        effective_workspace,
                        self._owned_ids,
                        self._home_volume(effective_workspace),
                    )
                    async with executor:
                        reply = await backend.run(
                            text,
                            task_id=task_id,
                            workspace=effective_workspace,
                            executor=executor,
                            session_key=session_key,
                            instance=handle,
                            provider=self.provider,
                            model=self.model,
                            **kwargs,
                        )
                except asyncio.CancelledError:
                    cancelled = True
                    await _write_spawn_status(session_key, agent, handle, "cancelled")
                    record.finish(status="cancelled", activity=did)
                    raise
                except Exception as exc:
                    await _write_spawn_status(session_key, agent, handle, "failed")
                    record.finish(status="failed", error=f"Error: {exc}", activity=did)
                    raise DirectChatError(record.meta()) from exc
                else:
                    await _write_spawn_status(session_key, agent, handle, "completed")
                    record.finish(status="completed", output=reply, activity=did)
                    return reply, record.meta()
                finally:
                    # Not for a cancelled call: this poller would be created
                    # after the cancellation sweep took its snapshot, so nothing
                    # could reap it (see cancel_all / cancel_by_session).
                    if not cancelled:
                        self._schedule_memory_record(
                            task_id=task_id,
                            agent=agent,
                            handle=handle,
                            session_key=session_key,
                            directory=record.dir,
                            filename="memory.json",
                            instance=handle,
                            turn=record.turn,
                        )

    @asynccontextmanager
    async def _hold_instance_slot(self, session_key: str, agent: str, handle: str) -> AsyncIterator[None]:
        """Index this turn's own task under the instance it is talking to.

        A direct chat runs inside the turn rather than as a spawned background
        task, so without this ``live_handles`` omits the pair and the row this
        very turn just set to ``running`` is reconciled straight back to
        ``interrupted`` -- the instance reads as dead while it is answering.

        It also makes ``cancel_by_instance`` able to stop one. The TUI does not
        use that: a direct chat runs on its own lane, so ``turn.cancel`` -- which
        looks up the session's turn -- does not reach it, and not reaching it is
        the decision (see the concurrent-direct-chats design, D3). The web
        surface does use it, an instance's work there being a background task
        with no turn behind it.

        Deliberately not registered in ``_session_tasks``. That index backs
        "cancel this session's sub-agents", and the task here is the user's own
        turn -- a caller asking to stop the session's spawns should not take the
        turn down with them.
        """
        task = asyncio.current_task()
        key = (session_key or "default", agent, handle)

        if task is None:
            yield
            return

        task_id = f"direct-{uuid.uuid4().hex[:8]}"
        self._running_tasks[task_id] = task
        self._instance_tasks.setdefault(key, set()).add(task_id)
        try:
            yield
        finally:
            self._running_tasks.pop(task_id, None)
            if (ids := self._instance_tasks.get(key)) is not None:
                ids.discard(task_id)
                if not ids:
                    del self._instance_tasks[key]

    def declared_stateful(self, agent: str | None) -> bool:
        """Whether reusing this agent's handle continues its conversation.

        Read from the one table the tool descriptions advertise, so what the model
        is told, what the spawn schema offers and what actually happens cannot
        disagree. A name that is not on the table is reported stateless: nothing
        can continue a conversation with an agent that cannot be dispatched to.
        """
        row = self.registry.get(agent or GENERIC_AGENT)
        return bool(row is not None and row.caps.stateful)

    def _is_replayed(self, agent: str) -> bool:
        """Whether raven owns this agent's conversation state.

        True for a ``builtin`` row -- an in-process loop has no session store of
        its own, so the message list raven keeps *is* its memory -- and for an
        openai entry that *declares* itself stateful, whose backend depends on the
        same replay. False for cli and acp, which resume inside their own stores.

        The declaration is consulted rather than the kind alone: an endpoint
        that ignores a system prompt continues nothing under replay, and the
        mirothinker preset says so (``presets.py``). Replaying at one anyway
        re-posts the whole transcript every turn -- growing until it trips the
        endpoint's context limit -- to buy nothing.
        """
        row = self.registry.get(agent)
        if row is None:
            return False
        if row.kind == "builtin":
            return True
        return row.kind == "openai" and row.caps.stateful

    def instance_state(self, session_key: str, agent: str | None, handle: str) -> "InstanceState | None":
        """The message list raven keeps for one instance, or ``None``.

        One derivation for every dispatch path. ``spawn`` and a DAG node used to
        have none at all, so an agent advertised as stateful started from an
        empty list on those paths and never wrote one back: reusing a handle
        read as the sub-agent having forgotten, rather than as an argument that
        was refused.
        """
        name = agent or GENERIC_AGENT
        if not self._is_replayed(name):
            return None
        return InstanceState(instance_state_path(self._session_dir(session_key), name, handle))

    @trace.instrument("subagent.run", extract=semconv.subagent)
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        task_summary: str,
        origin: dict[str, Any],
        provider: LLMProvider,
        model: str,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, task_summary)

        effective_workspace = origin.get("workspace") or self.workspace
        try:
            # Each subagent runs its own sandbox VM; gate the count so heavy
            # fan-out can't exhaust host resources.
            async with self._gate:
                executor = build_executor(
                    self._sandbox_config,
                    effective_workspace,
                    self._owned_ids,
                    self._home_volume(effective_workspace),
                )
                async with executor:
                    await self._run_subagent_inner(task_id, task, task_summary, origin, executor, provider, model)
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, task_summary, task, error_msg, origin, "error")

    def _home_volume(self, mount_root: Path) -> tuple[tuple[str, str, str], ...]:
        """Mount agent home into the sub-agent's VM unless the run's own mount covers it.

        Mirrors the loop-level executor: the sub-agent's prompt hands out
        absolute paths under agent home, so a sandboxed run that cannot see
        that tree is fenced out of the memory and skills it is told to use.
        """
        if workdir.is_within(Path(self.workspace), mount_root):
            return ()
        return ((str(self.workspace), "/agent-home", "rw"),)

    def _session_dir(self, session_key: str) -> Path:
        """The metadata directory of the session a spawn was made from.

        Falls back to a slug-less ``SessionManager`` when no resolver was
        injected -- the gateway's grouping, and the right answer for a manager
        built without one. Still the manager's own derivation rather than a
        second copy of it, so the two cannot drift.
        """
        if self.session_dir is not None:
            return self.session_dir(session_key)
        if self._fallback_sessions is None:
            from raven.session.manager import SessionManager

            self._fallback_sessions = SessionManager(Path(self.workspace))
        return self._fallback_sessions.session_dir(session_key)

    async def _run_subagent_inner(
        self,
        task_id: str,
        task: str,
        task_summary: str,
        origin: dict[str, Any],
        executor: Any,
        provider: LLMProvider,
        model: str,
    ) -> None:
        session_key = origin.get("session_key")
        agent = origin.get("agent") or GENERIC_AGENT
        handle = origin.get("handle") or task_id
        effective_workspace = origin.get("workspace") or self.workspace
        # Opened before dispatch so a call that never returns still leaves its
        # input on disk. Rooted at the session's metadata directory, not at
        # effective_workspace: the record has to outlive whatever the working
        # directory is pointed at.
        record = SpawnRecord.open(
            self._session_dir(session_key or ""),
            task_id=task_id,
            task=task,
            meta={
                "call_id": task_id,
                "session_key": session_key,
                "agent": agent,
                "task_summary": task_summary,
                "instance": origin.get("instance"),
                "instance_auto": origin.get("instance_auto", False),
                "handle": handle,
                "working_directory": str(effective_workspace),
            },
        )
        # Opened here rather than inside a backend, because this is what writes
        # the record: a backend publishes into whatever is collecting, and
        # publishing into nothing is a no-op. Every exit below therefore has the
        # tool calls and token cost the run got as far as producing -- a failed
        # run's are the ones worth keeping. Keyed into the live index by the
        # record's own directory name, so `subagent.context` can serve the run
        # while it is still in flight, and by instance so the conversation view
        # can: a spawned call is a turn of the same instance a direct chat talks
        # to, and watching it there is the same question.
        cancelled = False
        with activity.collecting(
            live_key=record.dir.name, instance=(session_key or "", agent or "", handle), prompt=task
        ) as did:
            try:
                await _write_spawn_status(session_key, agent, handle, "running")
                backend = self._resolve_backend(agent)
                # The same message list a direct chat to this handle would carry.
                # Without it an agent the roster advertises as stateful started every
                # spawn from empty and wrote nothing back, so reusing a handle read
                # as the sub-agent having forgotten the earlier turns.
                #
                # Only when the call carries an `instance` -- named by the caller, or
                # minted for it by the spawn tool when the target is resumable. With
                # neither, the handle is a fresh task id: a transcript written under it
                # would be addressable by nobody and reclaimed by nothing, pure growth
                # for a conversation that has no second turn by construction. Minting
                # is what opens this gate for a spawn nobody named, and the growth that
                # follows is the price of every such run being continuable -- accepted
                # deliberately, with reclaiming it left as follow-up work.
                state = self.instance_state(session_key or "", agent, handle) if origin.get("instance") else None
                # Held across load-run-save, not just around each half. The state
                # is a whole-file read-modify-write, so two dispatches on one
                # handle that interleave here lose whichever wrote first: the
                # second one read the list before the first appended to it and
                # then wrote its own version over the top. Nothing raises. A
                # direct chat and the cli backend already take this same lock; a
                # spawn resuming a replayed handle (openai, or any builtin agent
                # now that a graph can name one) did not, which is what made the
                # loss reachable without any playbook involved.
                async with hold_handle(session_key or "", agent, handle):
                    state_kwargs: dict[str, Any] = (
                        {"history": state.load(), "on_messages": state.save} if state is not None else {}
                    )
                    final_result = await backend.run(
                        task,
                        task_id=task_id,
                        workspace=effective_workspace,
                        executor=executor,
                        session_key=session_key,
                        instance=origin.get("instance"),
                        provider=provider,
                        model=model,
                        **state_kwargs,
                    )
                await _write_spawn_status(session_key, agent, handle, "completed")
                record.finish(status="completed", output=final_result, activity=did)
                await self._announce_result(
                    task_id, task_summary, task, final_result, origin, "ok", record_dir=str(record.dir)
                )
            except asyncio.CancelledError:
                cancelled = True
                await _write_spawn_status(session_key, agent, handle, "cancelled")
                record.finish(status="cancelled", activity=did)
                raise
            except SubagentActionAbortedError:
                await _write_spawn_status(session_key, agent, handle, "failed")
                logger.info("Subagent [{}] stopped on a terminal safety decision", task_id)
                record.finish(status="aborted", output=ABORTED_ACTION_RESULT, activity=did)
                await self._announce_result(
                    task_id, task_summary, task, ABORTED_ACTION_RESULT, origin, "error", record_dir=str(record.dir)
                )
            except Exception as e:
                await _write_spawn_status(session_key, agent, handle, "failed")
                error_msg = f"Error: {str(e)}"
                logger.error("Subagent [{}] failed: {}", task_id, e)
                record.finish(status="failed", error=error_msg, activity=did)
                await self._announce_result(
                    task_id, task_summary, task, error_msg, origin, "error", record_dir=str(record.dir)
                )
            finally:
                # Not for a cancelled call: this poller would be created after
                # the cancellation sweep took its snapshot, so nothing could
                # reap it (see cancel_all / cancel_by_session).
                if not cancelled:
                    self._schedule_memory_record(
                        task_id=task_id,
                        agent=agent,
                        handle=handle,
                        session_key=session_key,
                        directory=record.dir,
                        filename="memory.json",
                        instance=origin.get("instance"),
                        turn=record.turn,
                    )

    def set_submit(self, submit) -> None:
        self._submit = submit

    def set_delivery_sink(self, sink) -> None:
        """Late-bind where ``subagent.delivered`` events go.

        ``sink`` is an async callable ``(conversation, event_dict)``. It marks
        the seam a delegated result re-enters its conversation at, so a client
        can draw that seam instead of showing an unprompted assistant turn.
        """
        self._delivery_sink = sink

    def _emit_delivered(self, origin: dict[str, Any], payload: dict[str, Any]) -> None:
        """Fire-and-forget: a client that cannot hear this loses a marker, and
        the announce it marks must not fail with it."""
        sink = self._delivery_sink
        if sink is None:
            return
        try:
            asyncio.get_running_loop().create_task(
                sink(origin["session_key"], {"type": "subagent.delivered", "payload": payload})
            )
        except RuntimeError:
            pass  # no loop (sync CLI path): nothing is listening anyway

    async def _announce_result(
        self,
        task_id: str,
        task_summary: str,
        task: str,
        result: str,
        origin: dict[str, Any],
        status: str,
        record_dir: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the spine.

        Note: this inbound system message already triggers a main-agent turn,
        so subagent completion is event-driven end-to-end. Do NOT also
        enqueue a heartbeat SystemEvent here — that would process the same
        fact twice (double LLM cost, risk of double-notifying the user).
        """
        status_text = "completed successfully" if status == "ok" else "failed"

        # The subagent's result is attacker-influenceable (it may have fetched
        # web pages / read files), so fence it as untrusted before it re-enters
        # the main agent's context.
        fenced_result = wrap_untrusted(result, source="subagent")
        # Only a named or minted instance is addressable; a stateless call's
        # handle continues nothing, so offering it would invite a call the
        # spawn tool then refuses.
        handle_line = (
            f"\nInstance handle: {origin['instance']} -- pass it as spawn's `instance` "
            "to continue this same conversation.\n"
            if origin.get("instance")
            else ""
        )
        record_line = f"\n\nRecord: {record_dir}" if record_dir else ""
        announce_content = f"""[Subagent '{task_summary}' {status_text}]

Task: {task}
{handle_line}
Result:
{fenced_result}{record_line}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Keep technical details like the instance handle and task ids out of what you say to the user -- they stay available for your own later calls."""

        assert self._submit is not None
        mark = {"kind": "spawn", "label": task_summary, "status": status}
        self._inject(announce_content, origin, mark)
        # `content` is the text that was injected, verbatim. A client draws the
        # reader-facing part of it by dropping everything outside the untrusted
        # fence -- and a client REPLAYING this turn later reads the same string
        # from the stored entry, so both run one rule over one input and cannot
        # disagree about what was delivered.
        self._emit_delivered(origin, {**mark, "content": announce_content})
        logger.debug("Subagent [{}] announced result to {}", task_id, origin["session_key"])

    async def announce_dag_result(self, run_id: str, summary: str, origin: dict[str, str]) -> None:
        """Announce a background DAG run's outcome, the way a spawn's is announced.

        A backgrounded ``run_subagent_dag`` returns before its graph does, so
        this is the only path its result takes back to the main agent. It lives
        on the manager rather than on the DAG tool because the spine submit is
        wired here, once per entry point -- routing the announce through the
        tool instead would mean a second late-bound hookup at every one of them.

        The summary is delivered verbatim: it is the same text a foreground run
        returns as its tool result, and a graph's deliverable is its terminal
        node outputs. Framing it or asking for a two-sentence retelling -- as a
        spawn's announce does, its result being one agent's single answer --
        would put a lossy instruction between the agent and the work product.
        The untrusted fence is not part of that text and stays: node output is
        attacker-influenceable, and unlike a tool result (which the agent knows
        it asked for) this arrives shaped like an inbound message.
        """
        if self._submit is None:
            logger.warning("DAG run {} finished with no submit wired; result not announced", run_id)
            return
        injected = wrap_untrusted(summary, source="subagent")
        mark = {"kind": "dag", "label": run_id, "status": "ok", "run_id": run_id}
        self._inject(injected, origin, mark)
        # The graph's own tally names the outcome; "ok" here only means the run
        # came back at all, and the marker's job is placement, not verdict.
        self._emit_delivered(origin, {**mark, "content": injected})
        logger.debug("DAG run [{}] announced result to {}", run_id, origin["session_key"])

    def _inject(self, content: str, origin: dict[str, str], delegated: dict[str, str] | None = None) -> None:
        """Re-inject ``content`` to trigger a main-agent turn in the originating session.

        The spine path routes by conversation (= originating session) with
        origin=SUBAGENT; the reply rides emit -> hub -> outlet (source.channel
        is the originating channel). Fire-and-forget — the announce is fixed,
        the turn's output isn't read back.
        """
        from raven.spine import ChatType, Origin, Source, TurnRequest

        self._submit(
            TurnRequest(
                origin=Origin.SUBAGENT,
                source=Source(
                    channel=origin["channel"],
                    chat_id=origin["chat_id"],
                    sender_id="subagent",
                    chat_type=ChatType.DM,
                ),
                text=content,
                conversation=origin["session_key"],
                delegated=delegated,
            )
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [
            self._running_tasks[tid]
            for tid in self._session_tasks.get(session_key, [])
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # The memory pollers this session left running. Reaped here so a closing
        # session is not held open by one, and counted separately because they
        # are bookkeeping, not the sub-agents the caller asked to stop.
        records = [t for t in self._session_record_tasks.get(session_key, set()) if not t.done()]
        for t in records:
            t.cancel()
        if records:
            await asyncio.gather(*records, return_exceptions=True)
        # Drop this session's rate-limit entry on teardown: pruning empties a
        # deque but never removes the key, so without this the dict would keep
        # one entry per session for the process's life.
        self._session_spawn_times.pop(session_key, None)
        return len(tasks)

    async def cancel_by_instance(self, session_key: str, agent: str, handle: str) -> bool:
        """Cancel every spawn on one (session_key, agent, handle).

        This is the granularity a stop button needs: cancelling unwinds each
        `async with self._gate` the matched spawns are holding (or waiting
        on), releasing their concurrency slots, without touching the rest of
        the session's spawns. Usually there is exactly one live task per
        instance key, but two spawns can race onto the same key before the
        first completes, so this cancels *all* of them rather than only the
        most recent. Returns whether any live task was found.
        """
        quota_key = session_key or "default"
        task_ids = self._instance_tasks.get((quota_key, agent, handle), set())
        tasks = [t for tid in task_ids if (t := self._running_tasks.get(tid)) is not None and not t.done()]
        if not tasks:
            return False
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return True

    @property
    def paused(self) -> bool:
        """Whether new spawns are currently refused."""
        return self._paused

    def set_paused(self, paused: bool) -> bool:
        """Set the delegation pause flag. Returns the value now in effect."""
        self._paused = bool(paused)
        return self._paused

    async def cancel_by_id(self, task_id: str) -> bool:
        """Cancel one spawn by the id ``spawn`` handed back. Returns whether it was live.

        The instance-keyed variant cannot serve the overlay's kill button: rows
        there are keyed by task id, and a spawn made without an ``agent`` has no
        instance key at all.
        """
        task = self._running_tasks.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    def has_active(self, session_key: str) -> bool:
        """Whether this session has any subagent currently running."""
        return bool(self._session_tasks.get(session_key))

    def live_handles(self, session_key: str) -> set[tuple[str, str]]:
        """The (agent, handle) pairs currently in flight for one session.

        Lets a reader tell a genuinely running instance row from one orphaned
        by a gateway restart (the process-wide task index does not survive
        one, unlike the persistent registry).
        """
        quota_key = session_key or "default"
        live: set[tuple[str, str]] = set()
        for (skey, agent, handle), task_ids in self._instance_tasks.items():
            if skey != quota_key:
                continue
            if any((t := self._running_tasks.get(tid)) is not None and not t.done() for tid in task_ids):
                live.add((agent, handle))
        return live

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    async def cancel_all(self) -> int:
        """Cancel every subagent still running, across every session.

        Used at gateway shutdown: `CliAgentBackend` runs its child with
        `start_new_session=True` (its own process group), which detaches it
        from the gateway's process group, so a Ctrl-C to the gateway no
        longer reaches it. With every automatic timeout also removed, a
        wedged CLI child would otherwise be orphaned and keep running against
        the workspace forever once the gateway exits. Returns the count
        cancelled.
        """
        tasks = [t for t in self._running_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Same for the memory pollers: at shutdown an unreaped one dies pending,
        # with its httpx client never closed and its record never written.
        records = [t for t in self._record_tasks if not t.done()]
        for t in records:
            t.cancel()
        if records:
            await asyncio.gather(*records, return_exceptions=True)
        return len(tasks)
