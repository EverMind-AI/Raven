"""Context engine factory — one engine.

:func:`build_context_engine` returns one :class:`ContextAssembler` over an
ordered list of :class:`SegmentBuilder`s, each owning one section of the
system prompt:

- **Identity / Bootstrap / ActiveSkills** — the host's own sections.
- **Memory** — ``backend.recall(user_id=...)`` (segment 4, ``# Memory``).
- **History** — the strategy ``context.history.strategy`` names (see
  :mod:`raven.context_engine.history`): ``compacting`` or the Curator.
  Owns ``*history``.

With ``context.cache.turn_scratch`` on (the default), ``# Memory``, the section
that moves with the query, is appended to the turn's user message instead of
the system prompt.

Retrieved skills never enter a segment. A :class:`SkillForgeRouter` over 1-3
sources feeds the :class:`ScentMenu`, a few name and description lines in the
turn's user envelope, and the ``find_skill`` tool; the model reads a body with
``read_skill``. The router is assembled from up to three sources:

- :class:`LocalSkillSource` — always; wraps the builder's existing
  ``LocalPool`` + ``SkillRegistry`` (no second disk scan).
- :class:`BackendSkillSource` — only when a ``backend`` is wired. Bridges
  ``backend.recall(agent_id=...)`` into the router under the configured
  ``memory.backend`` name.
- :class:`HubSkillSource` — always mounted: the shared Skill Hub marketplace
  (or the URL the config names), gated per search on the live switch.

With no ``backend`` the engine still constructs: the recall lane yields
``[]`` and the router runs Local-only, so the agent boots even when no
memory plugin is installed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from loguru import logger

from raven.agent.context import ContextBuilder
from raven.config.live import LiveConfig, curator_pin, skill_hub_enabled
from raven.context_engine import history as history_strategies
from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.scent import ScentMenu
from raven.context_engine.segments import (
    ActiveSkillsSegmentBuilder,
    BootstrapSegmentBuilder,
    IdentitySegmentBuilder,
    MemorySegmentBuilder,
)
from raven.contracts.context import ContextEngine
from raven.contracts.llm_provider import LLMProvider
from raven.providers.pool import live_pin_resolver

if TYPE_CHECKING:
    from raven.agent.subagent.backends import AgentMeta
    from raven.config.raven import (
        ContextConfig,
        MemoryConfig,
        SkillForgeConfig,
        SkillForgeRouterConfig,
    )
    from raven.contracts.memory import MemoryBackend
    from raven.memory_engine import SkillForgeRouter
    from raven.providers.pool import ProviderPool
    from raven.skill_hub import SkillHubClient


def build_context_engine(
    *,
    workspace: Path,
    config: "ContextConfig",
    builder: ContextBuilder,
    provider: LLMProvider,
    model: str,
    context_window_tokens: int,
    get_tool_definitions: Callable[[], list[dict]],
    list_subagents: "Callable[[], list[AgentMeta]] | None" = None,
    get_tool_notices: Callable[[], list[str]] | None = None,
    get_session_notices: Callable[[str], list[str]] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    backend: "MemoryBackend | None" = None,
    memory_config: "MemoryConfig | None" = None,
    skill_forge_router_config: "SkillForgeRouterConfig | None" = None,
    skill_forge_config: "SkillForgeConfig | None" = None,
    skill_hub_client: "SkillHubClient | None" = None,
    provider_pool: "ProviderPool | None" = None,
    blocklist_reader: "Callable[[], frozenset[str]] | None" = None,
    task_board: bool = False,
) -> ContextEngine:
    """Build the one :class:`ContextAssembler` from a flat SegmentBuilder list.

    ``provider_pool``, when supplied, is what turns a subsystem's pinned model
    into a pinned model *and its own credential*. Without it a pin has no
    credential of its own and the subsystem follows the conversation's model.

    ``builder`` is read only for the shared ``MemoryStore`` and
    ``LocalSkillCatalog`` it holds. The one configured name read here is the
    history strategy's, and it is resolved by the registry, which raises on a
    name it does not know -- at startup, not at the first turn.
    """
    from raven.config.raven import (
        MemoryConfig as _MemoryConfig,
    )
    from raven.config.raven import (
        SkillForgeRouterConfig as _SkillForgeRouterConfig,
    )

    if memory_config is None:
        memory_config = _MemoryConfig()
    if skill_forge_router_config is None:
        skill_forge_router_config = _SkillForgeRouterConfig()

    # One reader for the curator pin and the Hub switch: the file is re-parsed
    # only when its bytes change, so the subsystems asking per call cost one
    # read between them.
    live = LiveConfig()

    router = _build_router(
        builder=builder,
        backend=backend,
        memory_config=memory_config,
        skill_forge_router_config=skill_forge_router_config,
        skill_hub_client=skill_hub_client,
        hub_enabled_reader=lambda: skill_hub_enabled(live),
    )

    builders = [
        IdentitySegmentBuilder(
            workspace,
            list_subagents=list_subagents,
            get_tool_definitions=get_tool_definitions,
            has_memory_backend=backend is not None,
            task_board=task_board,
        ),
        BootstrapSegmentBuilder(workspace),
        MemorySegmentBuilder(
            builder.memory,
            backend,
            user_id=memory_config.user_id,
            memory_top_k=memory_config.memory_top_k,
        ),
        ActiveSkillsSegmentBuilder(builder.skills, get_tool_definitions=get_tool_definitions),
    ]
    strategy = history_strategies.resolve(config.history.strategy)
    builders.append(
        strategy.build(
            pin_resolver=live_pin_resolver(
                provider_pool,
                lambda: curator_pin(live),
                key="context.curator_model",
                follower="the curator",
            ),
            workspace=workspace,
            config=config,
            provider=provider,
            model=model,
            context_window_tokens=context_window_tokens,
            get_tool_definitions=get_tool_definitions,
            now_fn=now_fn,
        )
    )
    from raven.skill_hub.policy import SkillPolicy

    # The menu is where the everos and hub sources first meet the policy: it
    # enforces the blocklist and the safety bar at advertising time, and
    # read_skill / use_skill enforce them again on the detail metadata.
    scent = ScentMenu(
        router,
        policy=SkillPolicy.create(
            min_safety=skill_forge_router_config.hub.min_safety,
            blocklist=(getattr(skill_forge_config, "blocklist", None) if skill_forge_config is not None else None),
            blocklist_reader=blocklist_reader,
        ),
    )
    dropped = frozenset(getattr(config, "drop_segments", None) or ())
    # The history slot's builder is named for its strategy now; ``curator`` is
    # what a config written before the registry calls it, so either name drops
    # it. Without it nothing mints refs, and the engine must not say otherwise.
    history_builder = builders[-1].name
    history_dropped = bool(dropped & {history_builder, "curator"})
    if history_dropped:
        dropped |= {history_builder}
    if dropped:
        builders = [b for b in builders if b.name not in dropped]
    if strategy.name != "curator" and (
        getattr(config, "curator_model", None) or getattr(config, "curator_provider", None)
    ):
        logger.warning(
            "context.curatorModel / curatorProvider are set but context.history.strategy is {!r}: they apply "
            "to the curator strategy only; this strategy summarises on the turn's model unless "
            "context.history.compacting.summary.model names one",
            strategy.name,
        )
    engine = ContextAssembler(
        builders,
        get_tool_definitions,
        now_fn=now_fn,
        get_tool_notices=get_tool_notices,
        get_session_notices=get_session_notices,
        scent=scent,
        owns_compaction=strategy.owns_compaction,
        mints_source_refs=strategy.mints_source_refs and not history_dropped,
        turn_scratch=getattr(getattr(config, "cache", None), "turn_scratch", True),
    )
    # The find_skill tool searches through the same router the scent menu reads.
    engine.skills_router = router
    return engine


def _build_router(
    *,
    builder: ContextBuilder,
    backend: "MemoryBackend | None",
    memory_config: "MemoryConfig",
    skill_forge_router_config: "SkillForgeRouterConfig",
    skill_hub_client: "SkillHubClient | None" = None,
    hub_enabled_reader: "Callable[[], bool] | None" = None,
) -> "SkillForgeRouter":
    """Assemble the 1-to-3 source SkillForgeRouter the scent menu and find_skill share."""
    from raven.memory_engine import (
        BackendSkillSource,
        HubSkillSource,
        LocalSkillSource,
        SkillForgeRouter,
    )

    weights = skill_forge_router_config.weights or {}

    # ── Source 1: Local (always) ────────────────────────────────────
    # Reuse the builder's in-memory BM25 index / registry — no second
    # disk scan.
    local_source = LocalSkillSource(
        pool=builder.skills.pool,
        registry=builder.skills.registry,
    )
    if "local" in weights:
        local_source.weight = float(weights["local"])
    sources = [local_source]

    # ── Source 2: the memory backend (conditional on backend) ───────
    if backend is not None:
        backend_name = memory_config.backend
        backend_source = BackendSkillSource(
            backend=backend,
            agent_id=memory_config.agent_id,
            name=backend_name,
        )
        if backend_name in weights:
            backend_source.weight = float(weights[backend_name])
        sources.append(backend_source)

    # ── Source 3: Hub (mounted when a client exists) ────────────────
    # The source is asked per search and gates itself on the live config, so
    # the switch needs no rebuild in either direction. Its client outlives the
    # switch for the same reason; only the URL is fixed at construction. A
    # refused override leaves no client, and then no source either: mounting
    # one would still point it at a URL this process refused. The host-built
    # client is reused when provided, so discovery and retrieval share one
    # connection pool.
    hub_cfg = skill_forge_router_config.hub
    from raven.skill_hub import SkillHubClient, hub_api_key, resolve_hub_endpoint

    client = skill_hub_client
    if client is None:
        endpoint = resolve_hub_endpoint(hub_cfg.endpoint)
        if endpoint is not None:
            client = SkillHubClient(
                endpoint,
                api_key=hub_api_key(hub_cfg),
                timeout_s=hub_cfg.timeout_s,
                source=hub_cfg.source,
            )
    if client is not None:
        sources.append(
            HubSkillSource(
                client,
                weight=float(weights.get("hub", 0.85)),
                min_safety=hub_cfg.min_safety,
                enabled_reader=hub_enabled_reader,
            )
        )

    return SkillForgeRouter(
        sources=sources,
        over_fetch_factor=skill_forge_router_config.over_fetch_factor,
        dedup_by=skill_forge_router_config.dedup_by,
        rrf_k=skill_forge_router_config.rrf_k,
    )
