"""Structural validation beyond pydantic field shape — the field
definition's rule table, minus what pydantic already enforces (rule 1,
mode/section pairing) and what the store enforces (rule 2, name = directory).

Three entry points because their outcomes differ:

- :func:`validate_structure` — rules 3-10 over a spec's own graph.
  Violations are **errors**: back to the LLM through the repair loop, or a
  load-time quarantine. Rule 7 (an ``instance`` needs a stateful agent) is a
  real check again: built-in agents are stateful, so a handle on one works and
  the field is neither refused here nor stripped at dispatch.
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

from raven.agent.subagent_dag._graph import _REQUIRED_NON_BLANK
from raven.playbook.types import NodeSpec, PlaybookSpec

_PARAM_REF_RE = re.compile(r"\$\{params\.([A-Za-z0-9_]+)\}")
_NODE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.(output|output_path)\s*\}\}")


def _fillable_fields() -> frozenset[str]:
    """The node fields an author may leave for the caller to write.

    Imported here rather than at module scope: ``executor`` imports this module,
    so naming it the other way round at import time is a cycle.
    """
    from raven.playbook.executor import FILLABLE_REQUIRED

    return frozenset(FILLABLE_REQUIRED)


def validate_structure(
    spec: PlaybookSpec,
    *,
    known_agents: Iterable[str] | None = None,
    allow_blank_fillable: bool = False,
) -> list[str]:
    """Rules 3-10 for one spec. Empty list means pass.

    ``known_agents`` comes from the agent table, and ``None`` means the caller has
    none -- the names are then *not checked at all* rather than checked against a
    guess. The four hardcoded names this used to default to were a fiction the
    table has since replaced: with them, a playbook naming a perfectly well
    configured ``claude_code`` was reported invalid, and one naming a deleted agent
    was reported fine.

    Every caller in the tree does have a table (the package's built-in rows are
    seeds, so it is never empty), so ``None`` is for a caller written later that
    does not -- it is not a path anything takes today.

    ``allow_blank_fillable`` separates two questions this used to answer as one:
    is the spec sound, and is it complete. A field an author deliberately left
    for the caller (``executor.FILLABLE_REQUIRED``) makes it incomplete and not
    unsound -- ``load_playbook`` asks for those values by name and the run
    proceeds. A caller checking a file on the way *into* the library wants the
    first question; the generator, which is producing a spec that has to run as
    written, wants both, so it keeps the default.
    """
    errors: list[str] = []
    param_names = set(spec.params)
    if spec.mode == "prompt":
        for ref in _PARAM_REF_RE.findall(spec.prompts or ""):
            if ref not in param_names:
                errors.append(f"prompts: ${{params.{ref}}} names no declared param")
        return errors
    return errors + validate_graph_nodes(
        spec.nodes or [], param_names, known_agents=known_agents, allow_blank_fillable=allow_blank_fillable
    )


def validate_graph_nodes(
    nodes: list[NodeSpec],
    param_names: set[str],
    *,
    known_agents: Iterable[str] | None = None,
    stateful_agents: Iterable[str] | None = None,
    allow_blank_fillable: bool = False,
) -> list[str]:
    """Graph rules over a node list — a spec's own or a prompt-mode composed
    one (rule 11: same checks, same chain, no escape hatch).

    ``allow_blank_fillable`` asks whether the graph is *sound* rather than
    whether it is *complete*: a field an author deliberately left for the caller
    (``executor.FILLABLE_REQUIRED``) is one ``load_playbook`` asks for by name,
    so a check on the way into the library must not read it as a defect. A
    blank ``subagent`` also stops the agent-name check, since there is no name
    to check yet.

    ``known_agents`` / ``stateful_agents`` are ``None`` when no agent table was
    reachable, and the checks that need one are then skipped rather than run
    against a stand-in.
    """
    errors: list[str] = []
    agents = None if known_agents is None else set(known_agents)
    ids = [n.id for n in nodes]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        errors.append(f"duplicate node ids: {dupes}")
    known_ids = set(ids)

    fillable = _fillable_fields() if allow_blank_fillable else frozenset()
    for node in nodes:
        blank = [f for f in _REQUIRED_NON_BLANK if not str(getattr(node, f, "") or "").strip()]
        reportable = [f for f in blank if f not in fillable]
        if reportable:
            errors.append(f"node {node.id!r}: missing {sorted(reportable)} -- a node cannot run without them")
        if agents is not None and "subagent" not in blank and node.subagent not in agents:
            errors.append(f"node {node.id!r}: agent {node.subagent!r} is not registered (known: {sorted(agents)})")
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

    # Rule 7: a handle on a stateless agent promises continuity the run cannot
    # deliver, and the author only finds out by reading a downstream node that
    # does not remember the upstream one. Cheaper to refuse the graph. Every
    # built-in agent *is* stateful now (raven replays its message list), so this
    # rule no longer refuses every ``instance`` ever written -- which is what it
    # did while the stand-in set was empty, and why the module docstring used to
    # claim the field was stripped at execution time instead.
    stateful = None if stateful_agents is None else set(stateful_agents)
    for node in nodes:
        if node.instance and stateful is not None and node.subagent not in stateful:
            errors.append(
                f"node {node.id!r}: instance {node.instance!r} needs a stateful agent, "
                f"and {node.subagent!r} cannot hold a session -- drop the handle"
            )

    # Rule 8: nodes sharing an instance continue one session — they must be the
    # same agent (two agents sharing a handle share no session) and must form a
    # dependency chain (a shared session cannot run concurrently).
    #
    # There was a rule 9 here: only the node opening the session could declare
    # `skills`, because a resumed raven-loop session keeps the system prompt --
    # and therefore the skill menu -- it was opened with. It is gone because its
    # premise is: `skills` is no longer a menu filter carried into that first
    # prompt. The engine folds it into the step's own prompt, which is a user
    # message appended on every node, resumed or not, so a continuation node
    # declaring skills now says something that takes effect.
    by_instance: dict[str, list[NodeSpec]] = {}
    for node in nodes:
        if node.instance:
            by_instance.setdefault(node.instance, []).append(node)
    for handle, members in sorted(by_instance.items()):
        if len({m.subagent for m in members}) > 1:
            errors.append(f"instance {handle!r} is shared across different agents")
        if len(members) > 1 and not _forms_chain(members, nodes):
            errors.append(
                f"instance {handle!r}: members have no dependency chain between them (they would run concurrently)"
            )

    errors.extend(_cycle_errors(nodes))
    return errors


def _ancestors(nodes: list[NodeSpec]) -> dict[str, set[str]]:
    """Every node's transitive dependencies, over the whole graph.

    Over the whole graph, not just over one instance group: members of a group
    are routinely ordered *through* nodes outside it -- the shipped orchestration
    guide's shared-session example is ``draft(author) -> review -> revise(author)``,
    where the two members share no direct edge. Closing only over the members
    calls that pair unordered, which is false.

    A fixpoint rather than recursion, so a cycle terminates rather than blowing
    the stack; a cycle makes each member its own ancestor, which reads as "no
    head" and is left to :func:`_cycle_errors` to report.
    """
    reach = {n.id: set(n.depends_on) for n in nodes}
    changed = True
    while changed:
        changed = False
        for nid, ups in reach.items():
            grown = ups | {a for up in ups for a in reach.get(up, ())}
            if grown != ups:
                reach[nid] = grown
                changed = True
    return reach


def _forms_chain(members: list[NodeSpec], nodes: list[NodeSpec]) -> bool:
    """True when every pair of same-instance nodes is ordered by dependencies."""
    ids = {m.id for m in members}
    reach = _ancestors(nodes)
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
        for s in node.skills or []:
            if s not in skills:
                missing.append(f"skill {s!r} (node {node.id}) not found in the current inventory")
        for m in node.mcps or []:
            if m not in mcp:
                missing.append(f"mcp {m!r} (node {node.id}) not found in the current inventory")
    return [], sorted(set(missing))
