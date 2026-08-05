# -*- coding: utf-8 -*-
"""Roster-dependent validation of a DAG spec: what each sub-agent can do.

Separate from :mod:`._graph`, which validates the graph against itself (ids,
edges, cycles, declared refs) and needs no knowledge of the configured agents.
The checks here compare the graph against the *roster*, so they can only run
where the roster is known -- the tool -- and they run before any node is
dispatched: a rejected graph must cost zero sub-agent runs, since these two
mistakes would otherwise burn a full multi-minute fan-out to produce output the
main agent then has to discard.

Both failures are silent rather than loud without this: reusing an ``instance``
handle on a stateless agent still *runs*, it just serializes the nodes and
starts a fresh session each time, so the downstream node reads a reply written
as if the earlier turns never happened. Handing a path to an agent that cannot
see this filesystem likewise runs, and comes back with a plausible answer about
a file it never opened.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._errors import DagValidationError
from ._graph import DagNodeSpec, SubAgentDagSpec
from ._placeholders import parse_placeholders

# Placeholder kinds that hand the sub-agent a filesystem path, mapped to the
# content-passing form to use instead when it cannot read local files.
_PATH_KINDS: dict[str, str] = {
    "output_path": "{{{{ {name}.output }}}}",
    "input_path": "{{{{ inputs.{name} }}}}",
    "ref_path": "{{{{ ref:{name} }}}}",
}


@dataclass(frozen=True)
class AgentCapabilities:
    """What one configured sub-agent can do, as the roster advertises it.

    Defaults are permissive so an agent missing from the map -- a test double, a
    backend built outside the config path -- is never rejected by these checks.
    An unknown ``subagent`` name is already the runner's error to raise.
    """

    stateful: bool = True
    reads_local_files: bool = True


def validate_capabilities(
    spec: SubAgentDagSpec,
    capabilities: dict[str, AgentCapabilities],
) -> None:
    """Check the graph against what its sub-agents can actually do.

    Args:
        spec (`SubAgentDagSpec`):
            The parsed graph spec.
        capabilities (`dict[str, AgentCapabilities]`):
            Per-sub-agent capabilities, keyed by the name a node's ``subagent``
            field carries. Names absent from the map are not checked.

    Raises:
        `DagValidationError`:
            When a stateless sub-agent's ``instance`` handle is reused across
            nodes, or a node hands a local path to a sub-agent that cannot read
            one. The message names the offending nodes and the edit to make,
            because the caller's only recovery is to re-submit the whole graph.
    """
    _check_instance_reuse(spec, capabilities)
    for node in spec.nodes:
        _check_path_placeholders(node, capabilities)


def _check_instance_reuse(
    spec: SubAgentDagSpec,
    capabilities: dict[str, AgentCapabilities],
) -> None:
    """Reject an ``instance`` handle shared by 2+ nodes of a stateless agent.

    A single node carrying a handle is left alone: it reuses nothing, so it is
    at worst redundant. Grouped by ``(subagent, instance)`` rather than by
    handle alone -- two different agents sharing a handle share no session
    either way, and that is the runner's serialization semantics, not a
    capability error.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for node in spec.nodes:
        if node.instance is None:
            continue
        groups.setdefault((node.subagent, node.instance), []).append(node.id)

    for (subagent, instance), node_ids in groups.items():
        if len(node_ids) < 2:
            continue
        caps = capabilities.get(subagent)
        if caps is None or caps.stateful:
            continue
        raise DagValidationError(
            f"nodes {sorted(node_ids)} share instance '{instance}' on sub-agent '{subagent}', "
            f"which is stateless: a reused handle cannot carry context there, it only forces "
            f"those nodes to run one after another. Either drop 'instance' and pass what the "
            f"later node needs through a '{{{{ <dep>.output }}}}' placeholder on a declared "
            f"'depends_on', or move them to a sub-agent the roster tags [stateful]. "
            f"Then call run_subagent_dag again with the corrected graph."
        )


def _check_path_placeholders(
    node: DagNodeSpec,
    capabilities: dict[str, AgentCapabilities],
) -> None:
    """Reject path placeholders aimed at an agent that cannot read local files."""
    caps = capabilities.get(node.subagent)
    if caps is None or caps.reads_local_files:
        return

    offenders: list[str] = []
    for ph in parse_placeholders(node.prompt_template):
        replacement = _PATH_KINDS.get(ph.kind)
        if replacement is None:
            continue
        offenders.append(f"{ph.raw} -> use {replacement.format(name=ph.name)}")
    if not offenders:
        return

    raise DagValidationError(
        f"node '{node.id}' passes local file paths to sub-agent '{node.subagent}', which the "
        f"roster tags [no-local-files]: it cannot open them, so the path would reach it as "
        f"meaningless text. Replace each with the content form ({'; '.join(offenders)}), or "
        f"move the node to a sub-agent tagged [local-files]. Then call run_subagent_dag again "
        f"with the corrected graph."
    )


__all__ = ["AgentCapabilities", "validate_capabilities"]
