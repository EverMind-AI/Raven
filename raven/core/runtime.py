"""build_runtime: the one place a running agent is assembled from config.

Every entrance used to derive the same bundles from the same config keys in
its own prologue, and the three copies drifted often enough to need a parity
guard. The mapping lives here once now: an entrance brings its transport-side
wiring (policy and host) and takes back a runtime; what it may NOT do is
derive a cargo bundle by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import raven.agent.loop as agent_loop
from raven.agent.loop.bundles import EngineWiring, HostWiring, SubagentWiring, ToolWiring, TurnPolicy
from raven.core import plugin_stack, token_wise_stack

if TYPE_CHECKING:
    from raven.agent.loop.main import AgentLoop
    from raven.contracts.llm_provider import LLMProvider


@dataclass
class RavenRuntime:
    """One assembled generation of the agent: the loop plus the parts an
    entrance still needs handles to after construction."""

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
) -> RavenRuntime:
    """Assemble a runtime generation from the two config trees.

    ``policy`` and ``host`` are the transport's own wiring and pass through
    untouched; everything cargo-shaped is derived here, identically for every
    entrance.
    """
    from raven.agent.tools._deliverables import DeliverableStore
    from raven.config.paths import get_deliverables_path

    if provider_pool is None:
        from raven.core.helpers import load_runtime_config
        from raven.providers.pool import ProviderPool

        # Every entrance wants the same pool over the same loader; deriving
        # it here is what keeps it out of the entrances' hands.
        provider_pool = ProviderPool(lambda: load_runtime_config(None, None))
    plugin_registry = plugin_stack.build_plugin_registry(ec_config)
    backend = plugin_stack.maybe_build_memory_backend(config.workspace_path, ec_config, registry=plugin_registry)
    plugin_tools = plugin_stack.build_plugin_tools(config.workspace_path, ec_config, registry=plugin_registry)
    strategies = token_wise_stack.install_from_config(
        ec_config.token_wise,
        supports_caching=token_wise_stack.caching_probe(provider),
    )
    if deliverables is None:
        deliverables = DeliverableStore(get_deliverables_path())

    loop = agent_loop.AgentLoop(
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        session_manager=session_manager,
        provider_pool=provider_pool,
        router=router,
        sandbox_config=config.tools.sandbox,
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
        ),
        policy=policy or TurnPolicy(),
        host=host or HostWiring(),
    )
    loop.configure_personalization(config.agents.defaults.enable_personalization)
    return RavenRuntime(
        loop=loop,
        plugin_registry=plugin_registry,
        backend=backend,
        strategies=strategies,
        deliverables=deliverables,
    )
