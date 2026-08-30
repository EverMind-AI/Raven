"""build_runtime: the one place a running agent is assembled from config.

The mapping from config keys to cargo bundles lives here once: an entrance
brings its transport-side wiring (policy and host) and takes back a runtime;
what it may NOT do is derive a cargo bundle by hand. The parity guard
(``test_cli_agent_loop_parity.py``) catches a copy that drifts.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable

import raven.agent.loop as agent_loop
from raven.agent.loop.bundles import EngineWiring, HostWiring, SubagentWiring, ToolWiring, TurnPolicy
from raven.core import eval_stack, hooks_stack, plugin_stack, token_wise_stack

if TYPE_CHECKING:
    from raven.agent.loop.main import AgentLoop
    from raven.contracts.llm_provider import LLMProvider


@dataclass(frozen=True)
class RavenRuntime:
    """One assembled generation of the agent: the loop plus the parts an
    entrance still needs handles to after construction.

    Frozen: FREEZE is the last of the composition phases (COLLECT, ADMIT,
    BIND, START, FREEZE), and this is its machine. After construction a
    generation is sealed -- a change is generation N+1 through the swap path,
    never an in-place mutation of N. The members it holds stay live objects
    with their own lifecycles; what cannot change is which objects this
    generation is made of.
    """

    loop: "AgentLoop"
    plugin_registry: Any
    backend: Any
    strategies: Any
    deliverables: Any

    async def dispose(self) -> None:
        """Retire this generation: quiesce the loop, then its stores.

        Ordering is load-bearing and mirrors the gateway's shutdown path:
        sub-agents are cancelled before the MCP servers close (a live
        sub-agent turn may still be using an MCP tool), the loop stops
        before the backend drains (stopping is what ends the writers), and
        the backend stops last so in-flight store/feedback calls spawned
        during loop teardown can complete. Used at generation swap; process
        shutdown keeps its own sequence in the gateway, where the
        process-lifetime transports are interleaved.
        """
        await self.loop.subagents.cancel_all()
        await self.loop.close_mcp()
        self.loop.stop()
        if self.backend is not None:
            await self.loop.drain_backend_stores()
            await self.backend.stop()

    def discard(self) -> None:
        """Drop a candidate that never served.

        A built-but-never-started generation holds no started resources --
        no backend.start(), no MCP connections, no running loop -- so there is
        nothing to stop; dispose()'s sequence assumes a generation that ran.
        The method exists so a superseded candidate is dropped on purpose,
        not by falling out of scope.
        """
        return None


def build_runtime(
    config: Any,
    ec_config: Any,
    *,
    provider: "LLMProvider",
    session_manager: Any = None,
    provider_pool: Any = None,
    router: Any = None,
    workdir_resolver: Any = None,
    deliverables: Any = None,
    policy: TurnPolicy | None = None,
    host: HostWiring | None = None,
    context_engine: Any = None,
    executor: Any = None,
) -> RavenRuntime:
    """Assemble a runtime generation from the two config trees.

    ``policy`` and ``host`` are the transport's own wiring and pass through
    untouched; everything cargo-shaped is derived here, identically for every
    entrance.

    In the composition draft's phase names: the plugin stack below runs
    COLLECT (discovery over the fixed sources plus ``plugins.dirs``) and
    ADMIT (manifest validation, the config-slice admission); the rest of this
    function is BIND -- pure in-memory assembly a caller may still discard.
    START stays with the entrance (``loop.start()``, backend start, MCP
    connect: everything with a side effect and a dispose handle), and the
    frozen ``RavenRuntime`` this returns is FREEZE.
    """
    from raven.agent.tools.deliverables import DeliverableStore
    from raven.config.paths import get_deliverables_path

    if provider_pool is None:
        from raven.core.config_stack import load_runtime_config
        from raven.providers.pool import ProviderPool

        # Every entrance wants the same pool over the same loader; deriving
        # it here is what keeps it out of the entrances' hands.
        provider_pool = ProviderPool(lambda: load_runtime_config(None, None))
    plugin_registry = plugin_stack.build_plugin_registry(ec_config)
    backend = plugin_stack.maybe_build_memory_backend(
        config.workspace_path, ec_config, registry=plugin_registry, notify=(host.notify if host is not None else None)
    )
    plugin_tools = plugin_stack.build_plugin_tools(
        config.workspace_path, ec_config, registry=plugin_registry, provider=provider
    )
    plugin_hooks = plugin_stack.build_plugin_hooks(
        config.workspace_path, ec_config, registry=plugin_registry, provider=provider
    )
    strategies = token_wise_stack.install_from_config(
        ec_config.token_wise,
        supports_caching=token_wise_stack.caching_probe(provider),
    )
    if deliverables is None:
        deliverables = DeliverableStore(get_deliverables_path())

    host = host or HostWiring()
    eval_engine = None
    if ec_config.eval_engine.enabled:
        from raven.memory_engine import MemoryStore

        eval_engine = eval_stack.build_eval_stack(
            provider=provider,
            memory=MemoryStore(config.workspace_path),
            config=ec_config.eval_engine,
        )
    if eval_engine is not None or plugin_hooks:
        host = replace(
            host,
            hooks=hooks_stack.build_hooks_stack(
                eval_engine=eval_engine, plugin_hooks=plugin_hooks, extra_hooks=host.hooks
            ),
        )
    loop = agent_loop.AgentLoop(
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        session_manager=session_manager,
        provider_pool=provider_pool,
        router=router,
        sandbox_config=config.tools.sandbox,
        executor=executor,
        mcp_servers=config.tools.mcp_servers,
        tools=ToolWiring(
            brave_api_key=config.tools.web.search.api_key or None,
            jina_api_key=config.tools.web.jina_api_key or None,
            web_proxy=config.tools.web.proxy or None,
            media_config=config.effective_media_config(),
            deep_research_config=config.tools.deep_research,
            exec_config=config.tools.exec,
            ask_user_config=config.tools.ask_user,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            tool_search_config=config.tools.tool_search,
            plugin_tools=plugin_tools,
            deliverables=deliverables,
        ),
        subagents=SubagentWiring(
            max_concurrent_subagents=config.agents.defaults.max_concurrent_subagents,
            max_subagent_spawns_per_hour=config.agents.defaults.max_subagent_spawns_per_hour,
            agents=config.subagents.agents,
            workdir_resolver=workdir_resolver,
            subagent_dag_config=ec_config.subagent_dag,
            subagent_questions_config=ec_config.subagent_questions,
        ),
        engine=EngineWiring(
            strategies=strategies,
            context_window_tokens=config.agents.defaults.context_window_tokens,
            playbook_config=config.playbooks,
            skill_forge_config=ec_config.skill_forge,
            skill_forge_router_config=ec_config.skill_forge.router,
            context_config=ec_config.context,
            runtime_config=ec_config.runtime,
            memory_config=ec_config.memory,
            backend=backend,
            context_engine=context_engine,
        ),
        policy=policy or TurnPolicy(),
        host=host,
    )
    loop.configure_personalization(config.agents.defaults.enable_personalization)
    return RavenRuntime(
        loop=loop,
        plugin_registry=plugin_registry,
        backend=backend,
        strategies=strategies,
        deliverables=deliverables,
    )


@dataclass
class SwapCandidate:
    """Generation N+1, fully built and waiting for N's turn boundary."""

    config: Any
    ec_config: Any
    provider: Any
    router: Any
    runtime: RavenRuntime


class SwapCoordinator:
    """Serializes generation swaps from every trigger (SIGHUP, gateway.reload).

    One swap at a time: the slot is claimed at BUILD start and released only
    immediately before the new generation's loop starts running, so a request
    that completes while the next generation is still being wired is refused
    rather than stopping a loop that has not started (AgentLoop.run sets
    _running on entry, so a stop() landing before it would be overwritten and
    the candidate stranded). Accepted requests are also rate-limited: a swap
    cancels every in-flight turn and reconnects MCP, so a reload storm must
    not be a way to keep the daemon permanently rebuilding.
    """

    def __init__(self, *, min_interval_s: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.generation = 1
        # Claimed from birth: generation 1 is being wired until the serving
        # loop's first release(), and a swap accepted in that window would
        # stop a loop that has not started -- the same hole, at boot.
        self._in_flight = True
        self._booting = True
        self._swapping = False
        self._candidate: SwapCandidate | None = None
        self._last_accept: float | None = None
        self._tasks: set[asyncio.Task] = set()
        self._clock = clock
        self._min_interval_s = min_interval_s

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    def begin(self) -> str | None:
        """Claim the swap slot; None when claimed, else the refusal reason."""
        if self._in_flight:
            return "booting" if self._booting else "swap_in_flight"
        now = self._clock()
        if self._last_accept is not None and now - self._last_accept < self._min_interval_s:
            return "too_soon"
        self._in_flight = True
        self._last_accept = now
        return None

    def abort(self) -> None:
        """The build failed: give the slot back, nothing was staged."""
        self._in_flight = False

    def stage(self, candidate: SwapCandidate) -> None:
        if self._candidate is not None:
            self._candidate.runtime.discard()
        self._candidate = candidate

    def take(self) -> SwapCandidate | None:
        """The serving loop stopped: hand over the candidate, if one is staged."""
        candidate, self._candidate = self._candidate, None
        if candidate is not None:
            self._swapping = True
        return candidate

    def release(self) -> None:
        """Called right before the (new) generation's loop starts; no await
        may sit between this call and ``agent.run()``."""
        if self._swapping:
            self.generation += 1
            self._swapping = False
        self._booting = False
        self._in_flight = False

    def track(self, task: asyncio.Task) -> None:
        """Hold a reference to a trigger task so it cannot be collected mid-build."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
