"""Safe agent capabilities exposed to Playbook generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from raven.agent.subagent import dag_capabilities
from raven.agent.subagent.prompt_capabilities import check_path_placeholders
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_placeholders import parse_placeholders

if TYPE_CHECKING:
    from raven.agent.subagent.registry import AgentRegistry
    from raven.playbook.types import NodeSpec, PlaybookSpec


@dataclass(frozen=True)
class PlaybookAgentProfile:
    """The only per-agent fields a Playbook generation model may see."""

    description: str
    stateful: bool
    reads_local_files: bool
    injectable_skills: bool
    injectable_mcps: bool


class AgentProfileSource(Protocol):
    """Return the enabled agent table as it stands for this generation."""

    def __call__(self) -> dict[str, PlaybookAgentProfile]: ...


def agent_profiles_from_registry(registry: "AgentRegistry") -> dict[str, PlaybookAgentProfile]:
    """Project enabled registry rows into the model-safe Playbook view.

    MCP injection is advertised only when both the agent and the DAG runtime
    support it. Registry transport/configuration fields deliberately do not
    cross this boundary.
    """
    return {
        row.name: PlaybookAgentProfile(
            description=row.description,
            stateful=row.caps.stateful,
            reads_local_files=row.caps.reads_local_files,
            injectable_skills=row.injectable.skills,
            injectable_mcps=row.injectable.mcps and dag_capabilities.MCPS_IMPLEMENTED,
        )
        for row in registry.enabled()
    }


def validate_agent_capabilities(
    spec: "PlaybookSpec",
    profiles: dict[str, PlaybookAgentProfile],
) -> list[str]:
    """Return generator-repair errors for DAG choices the runtime cannot honor."""
    return validate_node_capabilities(spec.nodes or [], profiles)


def validate_node_capabilities(
    nodes: list["NodeSpec"],
    profiles: dict[str, PlaybookAgentProfile],
) -> list[str]:
    """Return repair errors for node choices the runtime cannot honor."""
    errors: list[str] = []
    for node in nodes:
        profile = profiles.get(node.subagent)
        if profile is None:
            continue
        if node.instance is not None and not profile.stateful:
            errors.append(
                f"node '{node.id}' sets instance on stateless agent '{node.subagent}'; "
                "remove instance or choose a stateful agent"
            )
        if node.skills is not None and not profile.injectable_skills:
            errors.append(
                f"node '{node.id}' sets skills on agent '{node.subagent}', which does not support "
                "skill injection; remove skills or choose an agent with injectableSkills=true"
            )
        if node.mcps is not None and not profile.injectable_mcps:
            errors.append(
                f"node '{node.id}' sets mcps on agent '{node.subagent}', which cannot receive MCP "
                "injection at runtime; remove mcps"
            )
        if not profile.reads_local_files:
            try:
                check_path_placeholders(
                    parse_placeholders(node.prompt_template),
                    node.subagent,
                    reads_local_files=False,
                    subject=f"node '{node.id}'",
                    escape_hatch=", or choose an agent with readsLocalFiles=true",
                )
            except DagValidationError as exc:
                errors.append(str(exc))
    return errors


__all__ = [
    "AgentProfileSource",
    "PlaybookAgentProfile",
    "agent_profiles_from_registry",
    "validate_agent_capabilities",
    "validate_node_capabilities",
]
