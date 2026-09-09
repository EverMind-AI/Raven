"""Construction bundles for AgentLoop: forty-five keywords become five groups.

Each bundle is one wiring concern, mirroring the mixin split; ``AgentLoop``
takes the five bundles and nothing else, so a wiring field has one address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolWiring:
    """Everything the built-in tool set is constructed from."""

    exec_config: Any = None
    ask_user_config: Any = None
    search_api_key: str | None = None
    jina_api_key: str | None = None
    web_proxy: str | None = None
    # Which vendor each web tool calls, and every vendor's key by name. The two
    # scalars above stay as the Serper / Jina carriers the loops always had.
    web_search_provider: str = "serper"
    web_fetch_provider: str = "jina"
    web_provider_keys: dict[str, str] | None = None
    restrict_to_workspace: bool = False
    disabled_tools: list[str] | None = None
    tool_search_config: Any = None
    media_config: Any = None
    deep_research_config: Any = None
    plugin_tools: Any = None
    plugin_tool_gates: Any = None
    deliverables: Any = None


@dataclass
class SubagentWiring:
    """Nested-agent orchestration: roster, limits, dag and question routing."""

    agents: list | None = None
    max_concurrent_subagents: int = 8
    max_subagent_spawns_per_hour: int = 30
    subagent_dag_config: Any = None
    subagent_questions_config: Any = None
    workdir_resolver: Any = None


@dataclass
class EngineWiring:
    """The optional organs: context assembly, memory, skills, playbooks."""

    context_config: Any = None
    runtime_config: Any = None
    context_window_tokens: int | None = None
    strategies: Any = None
    skill_forge_config: Any = None
    skill_forge_router_config: Any = None
    memory_config: Any = None
    compaction_config: Any = None
    backend: Any = None
    playbook_config: Any = None
    # The instance socket beside the config socket: a built ContextEngine the
    # shell binds as-is instead of building its own.
    context_engine: Any = None


@dataclass
class TurnPolicy:
    """How a turn runs: budget, recovery, interactivity, clock."""

    max_iterations: int = 40
    empty_recovery: Any = None
    interactive: bool = True
    now_fn: Any = None


@dataclass
class HostWiring:
    """Handles the hosting surface injects: hooks, sinks, host services."""

    hooks: Any = None
    cron_service: Any = None
    channels_config: Any = None
    # How the host shows a plugin's actionable notice; None leaves it to the log.
    notify: Any = None


# Which bundle each wiring field lives in; tests read captured kwargs through it.
FIELD_OWNER: dict[str, str] = {}
for _bundle_name, _cls in (
    ("tools", ToolWiring),
    ("subagents", SubagentWiring),
    ("engine", EngineWiring),
    ("policy", TurnPolicy),
    ("host", HostWiring),
):
    for _f in _cls.__dataclass_fields__:
        FIELD_OWNER[_f] = _bundle_name


def resolve_wiring(
    tools: ToolWiring | None,
    subagents: SubagentWiring | None,
    engine: EngineWiring | None,
    policy: TurnPolicy | None,
    host: HostWiring | None,
) -> tuple[ToolWiring, SubagentWiring, EngineWiring, TurnPolicy, HostWiring]:
    """Every bundle a caller left out is the default one."""
    return (
        tools or ToolWiring(),
        subagents or SubagentWiring(),
        engine or EngineWiring(),
        policy or TurnPolicy(),
        host or HostWiring(),
    )
