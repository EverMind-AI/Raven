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

# Placeholder kinds that hand the sub-agent a filesystem path. The content form
# to use instead comes from the placeholder itself (``Placeholder.content_form``),
# so a run-qualified reference keeps its qualifier in the advice.
_PATH_KINDS = ("output_path", "input_path", "ref_path")


# Whether attaching an MCP server to a sub-agent's session is implemented at all.
# It is not: nothing under ``raven/agent/subagent/`` reads a per-node mcp list.
# The field exists so a playbook can state the intent and be answered instead of
# silently ignored, and this flag is what keeps the answer honest -- flip it when
# the wiring lands and the notice stops firing on its own.
MCPS_IMPLEMENTED = False


@dataclass(frozen=True)
class AgentCapabilities:
    """What one configured sub-agent can do, as the roster advertises it.

    Defaults are permissive so an agent missing from the map -- a test double, a
    backend built outside the config path -- is never rejected by these checks.
    An unknown ``agent`` name is already the runner's error to raise.
    """

    stateful: bool = True
    reads_local_files: bool = True
    injectable_skills: bool = True
    injectable_mcps: bool = True
    """Whether per-node ``skills`` / ``mcps`` can be pushed into this agent's
    session at all. Only an in-process raven loop has a skill menu raven controls;
    a cli or acp agent's is its own business, so a list aimed at one does nothing."""


def validate_capabilities(
    spec: SubAgentDagSpec,
    capabilities: dict[str, AgentCapabilities],
) -> list[str]:
    """Check the graph against what its sub-agents can actually do.

    Args:
        spec (`SubAgentDagSpec`):
            The parsed graph spec.
        capabilities (`dict[str, AgentCapabilities]`):
            Per-agent capabilities, keyed by the name a node's ``agent``
            field carries. Names absent from the map are not checked.

    Returns:
        `list[str]`:
            Downgrade notices: things the graph asked for that will not happen,
            where the ask is a capability gap rather than a safety breach. The
            graph still runs -- refusing would make a playbook written for a
            better-equipped machine unusable here, and the caller can act on a
            notice. A safety gate, by contrast, refuses.

    Raises:
        `DagValidationError`:
            When a stateless sub-agent's ``instance`` handle is reused across
            nodes, a node hands a local path to a sub-agent that cannot read
            one, or a node that continues an existing session tries to change its
            skill menu. The message names the offending nodes and the edit to
            make, because the caller's only recovery is to re-submit the whole
            graph.
    """
    _check_instance_reuse(spec, capabilities)
    _check_injection_on_continuation(spec)
    for node in spec.nodes:
        _check_path_placeholders(node, capabilities)
    return _injection_notices(spec, capabilities)


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
        groups.setdefault((node.agent, node.instance), []).append(node.id)

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


def _ancestors(spec: SubAgentDagSpec) -> dict[str, set[str]]:
    """Every node's transitive dependencies, over the *whole* graph.

    Over the whole graph and not just over a group, because a group's members are
    routinely ordered *through* nodes that are not in it: the shipped
    orchestration guide's own shared-session example is
    ``draft(author) -> review -> revise(author)``, where the two members have no
    direct edge between them at all. Judging order on direct edges alone calls
    that pair unordered, which is false -- the topological sort runs ``draft``
    first -- and refuses a graph the guide teaches.

    A fixpoint rather than recursion so a cycle terminates instead of blowing the
    stack; a cycle makes every member its own ancestor, which the caller reads as
    "no single head" and leaves to the graph checks that report cycles properly.
    """
    deps = {node.id: set(node.depends_on) for node in spec.nodes}
    reach = {nid: set(edges) for nid, edges in deps.items()}
    changed = True
    while changed:
        changed = False
        for nid, ups in reach.items():
            grown = ups | {a for up in ups for a in reach.get(up, ())}
            if grown != ups:
                reach[nid] = grown
                changed = True
    return reach


def _instance_groups(spec: SubAgentDagSpec) -> dict[tuple[str, str], list[DagNodeSpec]]:
    """Nodes grouped by the session they share, keyed by ``(agent, instance)``."""
    groups: dict[tuple[str, str], list[DagNodeSpec]] = {}
    for node in spec.nodes:
        if node.instance is None:
            continue
        groups.setdefault((node.agent, node.instance), []).append(node)
    return groups


def _check_injection_on_continuation(spec: SubAgentDagSpec) -> None:
    """Reject ``skills`` / ``mcps`` on a node that continues an existing session.

    A resumed raven-loop session reuses the system prompt stored in its history
    (``backends/raven_loop.py``), and the skill menu is fixed in that first
    prompt -- so a later node's list is read by nobody. The earlier rule here
    demanded that every member of a group declare the *same* skills, which
    allowed writing the list three times where only the first had any effect;
    "do not write it after the first" is the same restriction stated so the file
    cannot lie.

    Only checked when some member actually declares something, and only then is
    the group required to form a dependency chain. Without a chain there is no
    "first" node -- the runner serializes the group but the order among nodes with
    no mutual dependency is not pinned -- so which member's skills would take
    effect is undecidable, and a graph that depends on the answer must be refused
    rather than guessed at.
    """
    for (agent, instance), members in _instance_groups(spec).items():
        if len(members) < 2:
            continue
        declaring = [n for n in members if n.skills is not None or n.mcps is not None]
        if not declaring:
            continue
        ids = {n.id for n in members}
        # The member no other member depends on, transitively. Direct edges are
        # not enough: see :func:`_ancestors`.
        reach = _ancestors(spec)
        heads = [n for n in members if not (reach.get(n.id, set()) & (ids - {n.id}))]
        if len(heads) != 1:
            raise DagValidationError(
                f"nodes {sorted(ids)} share instance '{instance}' on agent '{agent}' and declare "
                f"'skills'/'mcps', but they do not form a dependency chain, so which of them starts "
                f"the session is not decided -- and only the node that starts it can set its skills. "
                f"Chain them with 'depends_on' so the first one is unambiguous, or drop the "
                f"'skills'/'mcps' fields. Then call run_subagent_dag again with the corrected graph."
            )
        offenders = sorted(n.id for n in declaring if n.id != heads[0].id)
        if offenders:
            raise DagValidationError(
                f"nodes {offenders} declare 'skills'/'mcps' while continuing the session that node "
                f"'{heads[0].id}' opened (shared instance '{instance}' on agent '{agent}'). A resumed "
                f"session keeps the skill menu it was opened with, so those fields would silently do "
                f"nothing. Move them onto '{heads[0].id}', or give these nodes their own instance if "
                f"they need a different menu. Then call run_subagent_dag again with the corrected graph."
            )


def _injection_notices(spec: SubAgentDagSpec, capabilities: dict[str, AgentCapabilities]) -> list[str]:
    """What the graph declared that will not take effect, one line each.

    A notice rather than a rejection: these are capability gaps, and a graph that
    a playbook shipped from a better-equipped machine should still run here with
    the parts that work. The caller is told so a wrong result is attributable.
    """
    notices: list[str] = []
    for node in spec.nodes:
        caps = capabilities.get(node.agent)
        if node.skills is not None and caps is not None and not caps.injectable_skills:
            notices.append(
                f"node '{node.id}': agent '{node.agent}' cannot take injected skills "
                f"(only a built-in raven agent can), so its 'skills' list is ignored"
            )
        if node.mcps is not None and not MCPS_IMPLEMENTED:
            notices.append(
                f"node '{node.id}': attaching mcp servers to a sub-agent session is not implemented "
                f"yet, so its 'mcps' list is ignored"
            )
        elif node.mcps is not None and caps is not None and not caps.injectable_mcps:
            notices.append(
                f"node '{node.id}': agent '{node.agent}' cannot take injected mcp servers, "
                f"so its 'mcps' list is ignored"
            )
    return notices


def _check_path_placeholders(
    node: DagNodeSpec,
    capabilities: dict[str, AgentCapabilities],
) -> None:
    """Reject path placeholders aimed at an agent that cannot read local files."""
    caps = capabilities.get(node.agent)
    if caps is None or caps.reads_local_files:
        return

    offenders: list[str] = []
    for ph in parse_placeholders(node.prompt_template):
        if ph.kind not in _PATH_KINDS:
            continue
        offenders.append(f"{ph.raw} -> use {ph.content_form()}")
    if not offenders:
        return

    raise DagValidationError(
        f"node '{node.id}' passes local file paths to sub-agent '{node.agent}', which the "
        f"roster tags [no-local-files]: it cannot open them, so the path would reach it as "
        f"meaningless text. Replace each with the content form ({'; '.join(offenders)}), or "
        f"move the node to a sub-agent tagged [local-files]. Then call run_subagent_dag again "
        f"with the corrected graph."
    )


__all__ = ["MCPS_IMPLEMENTED", "AgentCapabilities", "validate_capabilities"]
