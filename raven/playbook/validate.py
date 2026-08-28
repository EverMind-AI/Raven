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

from raven.agent.subagent.dag_graph import _REQUIRED_NON_BLANK
from raven.playbook.params import param_refs
from raven.playbook.types import NodeSpec, PlaybookSpec

_NODE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)\.(output|output_path)\s*\}\}")


_REFERENCE_RULE = (
    "a secret may only be referenced from an mcpServers env or headers value, "
    "where the host substitutes it and nothing downstream sees it"
)


def unusable_mcp_servers(spec: PlaybookSpec, param_names: set[str] | None = None) -> dict[str, str]:
    """The spec's own MCP server definitions that cannot be honoured, and why.

    Notes, not errors, and the distinction is the whole point: ``mcpServers`` is
    an optional section on top of a playbook that otherwise runs. A definition
    this cannot use costs the run that server -- it must not cost the run. An
    error here reaches :func:`raven.playbook.store.load_playbook` as a raised
    ``ValueError``, and the runtime answers that by dropping the whole playbook
    out of the library: a saved procedure stops existing because an optional
    section was written wrong.

    Three ways one is unusable, all silent otherwise:

    * a reference to a param nobody declared substitutes to itself, so the server
      is handed the literal ``{{ params.X }}`` text as its password;
    * a definition with neither a command nor a url resolves to
      ``invalid_transport`` at dispatch, a node or two after the file that caused
      it;
    * the section cannot be honoured in ``prompt`` mode at all. The definitions
      reach a graph as a run-scoped hand-off the executor makes when *it*
      dispatches; prompt mode never reaches that call -- the executor returns
      composition guidance and the model submits its graph through the public
      ``run_subagent_dag`` in a later turn, after the scope is gone and through a
      signature that has no ``mcpServers`` parameter. That signature has none
      deliberately: a definition a model can supply is a command line a model can
      supply.

    Keyed by server name so a caller can drop exactly those and keep the rest.
    ``prompt`` mode drops every one of them, since the reason is the mode.
    """
    names = param_names if param_names is not None else set(spec.params or {})
    unusable: dict[str, str] = {}
    for name, cfg in (spec.mcp_servers or {}).items():
        if spec.mode == "prompt":
            unusable[name] = (
                "a prompt-mode playbook cannot carry server definitions -- its graph is composed by the "
                "caller in a later turn, which cannot be handed them. Use mode 'dag', where this playbook "
                "dispatches the graph itself, or name only servers the host configures"
            )
            continue
        for field_name, mapping in (("env", cfg.env), ("headers", cfg.headers)):
            for key, value in (mapping or {}).items():
                for ref in param_refs(value):
                    if ref not in names:
                        unusable[name] = f"{field_name}.{key}: params.{ref} names no declared param"
        if name not in unusable and not cfg.command and not cfg.url:
            unusable[name] = "needs a command (stdio) or a url (http/sse)"
    return unusable


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
    param_names = set(spec.params)
    # Not here: an unusable ``mcpServers`` entry is a note, not an error -- see
    # :func:`unusable_mcp_servers` for why an optional section must not be able
    # to take the whole playbook out of the library.
    errors: list[str] = []
    if spec.mode == "prompt":
        for ref in param_refs(spec.prompts or ""):
            if ref not in param_names:
                errors.append(f"prompts: params.{ref} names no declared param")
        # A secret referenced here is not an error. It cannot be honoured -- see
        # ``_REFERENCE_RULE`` -- and the reference is withheld at fill time with a
        # log line (``params.fill_param_refs_without_secrets``). Refusing instead
        # made the whole playbook fail to load, which drops it out of the library:
        # an optional section, written wrong, costing a saved procedure.
        return errors
    return errors + validate_graph_nodes(
        spec.nodes or [],
        param_names,
        known_agents=known_agents,
        allow_blank_fillable=allow_blank_fillable,
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
        for ref in param_refs(node.prompt_template):
            if ref not in param_names:
                errors.append(f"node {node.id!r}: params.{ref} names no declared param")
            # A secret here is withheld at fill time, not refused at the file --
            # see the note in :func:`validate_structure`.

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
    # Rule 9: only the node opening a shared session may set its skill or MCP
    # menu. A resumed raven-loop session keeps the system prompt it opened with,
    # so injection fields on continuation nodes cannot take effect.
    by_instance: dict[str, list[NodeSpec]] = {}
    for node in nodes:
        if node.instance:
            by_instance.setdefault(node.instance, []).append(node)
    reach = _ancestors(nodes)
    for handle, members in sorted(by_instance.items()):
        if len({m.subagent for m in members}) > 1:
            errors.append(f"instance {handle!r} is shared across different agents")
        if len(members) > 1 and not _forms_chain(members, nodes):
            errors.append(
                f"instance {handle!r}: members have no dependency chain between them (they would run concurrently)"
            )
        if len(members) < 2:
            continue
        declaring = [m for m in members if m.skills is not None or m.mcps is not None]
        if not declaring:
            continue
        ids = {m.id for m in members}
        heads = [m for m in members if not (reach.get(m.id, set()) & (ids - {m.id}))]
        if len(heads) != 1:
            continue
        offenders = sorted(m.id for m in declaring if m.id != heads[0].id)
        if offenders:
            errors.append(
                f"nodes {offenders} declare skills or mcps while continuing instance {handle!r} "
                f"opened by node {heads[0].id!r}; a resumed session keeps its opening skill menu, "
                f"so move those fields to {heads[0].id!r} or use a separate instance"
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
    # A playbook that ships its own definition of a server is not naming an
    # unknown one: the definition travels with the file, so the receiving
    # machine's inventory is not the authority on it.
    mcp = set(known_mcp) | set(spec.mcp_servers or {})
    missing: list[str] = []
    for node in spec.nodes or []:
        for s in node.skills or []:
            if s not in skills:
                missing.append(f"skill {s!r} (node {node.id}) not found in the current inventory")
        for m in node.mcps or []:
            if m not in mcp:
                missing.append(f"mcp {m!r} (node {node.id}) not found in the current inventory")
    return [], sorted(set(missing))
