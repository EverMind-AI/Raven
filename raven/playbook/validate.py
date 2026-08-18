"""Structural validation beyond pydantic field shape — the field
definition's rule table, minus what pydantic already enforces (rule 1,
mode/section pairing) and what the store enforces (rule 2, name = directory).

Three entry points because their outcomes differ:

- :func:`validate_structure` — rules 3-10 over a spec's own graph.
  Violations are **errors**: back to the LLM through the repair loop, or a
  load-time quarantine. Rule 7 is relaxed in v1: every builtin agent is
  stateless, so ``instance`` is stripped with a note at execution time
  rather than rejected here — hand-written playbooks keep working and the
  field keeps its meaning for the day stateful agents register.
- :func:`validate_graph_nodes` — the same graph rules over a bare node
  list (rule 11: a prompt-mode composed graph passes the same checks
  before the same execution chain).
- :func:`check_assets` — skills/mcps against the live inventories: unknown
  entries degrade into missing-capability notes, they never block.

Nothing here calls an LLM or touches disk.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from raven.playbook.types import BUILTIN_AGENTS, NodeSpec, PlaybookSpec

_PARAM_REF_RE = re.compile(r"\$\{params\.([A-Za-z0-9_]+)\}")
_NODE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.(output|output_path)\s*\}\}")


def validate_structure(spec: PlaybookSpec, *, known_agents: Iterable[str] = BUILTIN_AGENTS) -> list[str]:
    """Rules 3-10 for one spec. Empty list means pass."""
    errors: list[str] = []
    param_names = set(spec.params)
    if spec.mode == "prompt":
        for ref in _PARAM_REF_RE.findall(spec.prompts or ""):
            if ref not in param_names:
                errors.append(f"prompts: ${{params.{ref}}} names no declared param")
        return errors
    return errors + validate_graph_nodes(spec.nodes or [], param_names, known_agents=known_agents)


def validate_graph_nodes(
    nodes: list[NodeSpec],
    param_names: set[str],
    *,
    known_agents: Iterable[str] = BUILTIN_AGENTS,
    stateful_agents: Iterable[str] = (),
) -> list[str]:
    """Graph rules over a node list — a spec's own or a prompt-mode composed
    one (rule 11: same checks, same chain, no escape hatch)."""
    errors: list[str] = []
    agents = set(known_agents)
    ids = [n.id for n in nodes]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        errors.append(f"duplicate node ids: {dupes}")
    known_ids = set(ids)

    for node in nodes:
        if node.agent not in agents:
            errors.append(f"node {node.id!r}: agent {node.agent!r} is not registered (known: {sorted(agents)})")
        for dep in node.depends_on:
            if dep not in known_ids:
                errors.append(f"node {node.id!r}: dependsOn {dep!r} names unknown node")
        allowed = set(node.depends_on)
        for ref, _kind in _NODE_REF_RE.findall(node.prompt_template):
            if ref not in allowed:
                errors.append(f"node {node.id!r}: {{{{ {ref}.* }}}} references a node not in its dependsOn")
        for ref in _PARAM_REF_RE.findall(node.prompt_template):
            if ref not in param_names:
                errors.append(f"node {node.id!r}: ${{params.{ref}}} names no declared param")

    # Rule 7, and the reason it is an error rather than a note: a handle on a
    # stateless agent promises continuity the run cannot deliver, and the author
    # only finds out by reading a downstream node that does not remember the
    # upstream one. Cheaper to refuse the graph. ``stateful_agents`` is empty by
    # default because every builtin runs in-process with no resume mechanism;
    # the registry passes the real set once an agent can hold a session.
    stateful = set(stateful_agents)
    for node in nodes:
        if node.instance and node.agent not in stateful:
            errors.append(
                f"node {node.id!r}: instance {node.instance!r} needs a stateful agent, "
                f"and {node.agent!r} cannot hold a session -- drop the handle"
            )
        # A confirm gate exists to stop an irreversible step. Honouring it needs
        # a pause/resume the executor does not have, and a note after the fact
        # arrives once the step has already run -- so an unenforceable gate
        # fails the graph instead of being downgraded to a warning.
        if node.confirm:
            errors.append(
                f"node {node.id!r}: confirm is declared but this release cannot enforce a "
                "node-level gate; remove it rather than rely on it"
            )

    # Rules 8/9: nodes sharing an instance continue one session — they must
    # form a dependency chain (a shared session cannot run concurrently) and
    # carry identical toolsets (the session's tools are fixed at start).
    by_instance: dict[str, list[NodeSpec]] = {}
    for node in nodes:
        if node.instance:
            by_instance.setdefault(node.instance, []).append(node)
    for handle, members in sorted(by_instance.items()):
        if len({m.agent for m in members}) > 1:
            errors.append(f"instance {handle!r} is shared across different agents")
        if len({(tuple(sorted(m.skills)), tuple(sorted(m.mcps))) for m in members}) > 1:
            errors.append(f"instance {handle!r}: members declare different skills/mcps (a session's toolset is fixed)")
        if len(members) > 1 and not _forms_chain(members):
            errors.append(
                f"instance {handle!r}: members have no dependency chain between them (they would run concurrently)"
            )

    errors.extend(_cycle_errors(nodes))
    return errors


def _forms_chain(members: list[NodeSpec]) -> bool:
    """True when every pair of same-instance nodes is ordered by dependencies."""
    ids = {m.id for m in members}
    reach: dict[str, set[str]] = {m.id: set(m.depends_on) for m in members}
    changed = True
    while changed:
        changed = False
        for nid, ups in reach.items():
            add = set()
            for up in ups:
                add |= reach.get(up, set())
            if not add <= ups:
                ups |= add
                changed = True
    for a in ids:
        for b in ids:
            if a < b and b not in reach.get(a, set()) and a not in reach.get(b, set()):
                return False
    return True


def _cycle_errors(nodes: list[NodeSpec]) -> list[str]:
    node_ids = {n.id for n in nodes}
    deps = {n.id: set(n.depends_on) & node_ids for n in nodes}
    ready = [nid for nid, ups in deps.items() if not ups]
    seen: set[str] = set()
    while ready:
        nid = ready.pop()
        seen.add(nid)
        for other, ups in deps.items():
            if other not in seen and ups <= seen and other not in ready:
                ready.append(other)
    stuck = sorted(set(deps) - seen)
    return [f"dependency cycle involving nodes: {stuck}"] if stuck else []


def check_assets(
    spec: PlaybookSpec,
    known_skills: Iterable[str],
    known_mcp: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Skills/mcps existence against the live inventories.

    Returns ``(errors, missing_capabilities)`` — nothing here errors today
    (agents are checked by :func:`validate_structure`); unknown skills/mcps
    degrade into notes so the playbook stays usable, just annotated."""
    skills = set(known_skills)
    mcp = set(known_mcp)
    missing: list[str] = []
    for node in spec.nodes or []:
        for s in node.skills:
            if s not in skills:
                missing.append(f"skill {s!r} (node {node.id}) not found in the current inventory")
        for m in node.mcps:
            if m not in mcp:
                missing.append(f"mcp {m!r} (node {node.id}) not found in the current inventory")
    return [], sorted(set(missing))
