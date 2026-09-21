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
from collections.abc import Iterable, Mapping
from itertools import combinations

from raven.agent.subagent.dag_graph import collect_static_graph_errors
from raven.playbook.agent_spec import LABEL_RE
from raven.playbook.params import param_refs
from raven.playbook.stint_spec import DEFAULT_ISOLATION
from raven.playbook.types import NodeSpec, PlaybookSpec

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
    guess, because a guessed list reports a well-configured agent invalid and a
    deleted one fine.

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
    if spec.mode == "stint":
        return errors + validate_roles(spec, known_agents=known_agents)
    return errors + validate_graph_nodes(
        spec.nodes or [],
        param_names,
        known_agents=known_agents,
        allow_blank_fillable=allow_blank_fillable,
    )


def validate_roles(spec: PlaybookSpec, *, known_agents: Iterable[str] | None = None) -> list[str]:
    """What a role table has to satisfy that the model alone cannot check.

    The shape of the table -- unique labels, resolvable dependencies, no cycle --
    is on :class:`PlaybookSpec` itself, because it needs nothing but the table.
    What needs the rest of the world is here: the roster the roles are cast
    from, the params their prompts reference, and the one overlap in the
    ownership grid that has no sensible reading.
    """
    param_names = set(spec.params)
    roles = spec.roles or []
    errors: list[str] = []
    names = set(known_agents) if known_agents is not None else None

    for role in roles:
        if names is not None and role.name not in names:
            errors.append(f"role {role.label!r}: no agent named {role.name!r} on this machine")
        if not re.fullmatch(LABEL_RE, role.label):
            # A label becomes part of a node id; one the graph refuses used to
            # surface when round 1 compiled, after the record and worktree existed.
            errors.append(
                f"role {role.label!r}: a label is letters, digits, dots, dashes and underscores, starting with a letter or digit"
            )
        for ref in param_refs(role.prompt_template):
            if ref not in param_names:
                errors.append(f"role {role.label!r}: params.{ref} names no declared param")
        if not role.prompt_template.strip():
            errors.append(f"role {role.label!r}: promptTemplate is what the role is told, and it is empty")

    # Two owners of one path is the one overlap with no reading: an owner beside
    # an appender is the shape the grid is built on, and two appenders is
    # order-independent, so neither of those is a contest.
    owners: dict[str, list[str]] = {}
    for role in roles:
        for pattern in role.owns:
            owners.setdefault(pattern, []).append(role.label)
    for pattern, claimants in sorted(owners.items()):
        if len(set(claimants)) > 1:
            errors.append(f"{pattern} is owned by {', '.join(sorted(set(claimants)))}; one path has one owner")

    errors.extend(_concurrent_and_enforced(roles))
    errors.extend(_read_fences_nobody_holds(roles))
    errors.extend(_enforced_without_a_tree_to_undo(spec))

    verify_names = [entry.name for entry in (spec.verify or [])]
    duplicated = sorted({name for name in verify_names if verify_names.count(name) > 1})
    if duplicated:
        errors.append(f"two verify entries share the name {', '.join(duplicated)}")
    if spec.verify and not any(role.verify_after for role in roles):
        errors.append(f"verify declares {', '.join(sorted(set(verify_names)))} and no role runs any of them")

    paths = [entry.path for entry in (spec.memory or [])]
    repeated = sorted({path for path in paths if paths.count(path) > 1})
    if repeated:
        errors.append(f"memory names {', '.join(repeated)} twice, with two sets of limits")
    return errors


def _enforced_without_a_tree_to_undo(spec: PlaybookSpec) -> list[str]:
    """A hard boundary declared over the person's own branch.

    ``isolation: none`` runs the round in the checkout the person is standing
    in, on the branch they are on. Undoing a stray write there is
    ``git restore`` against that tree, so a file *they* touched while the round
    ran is indistinguishable from a role that wrote outside its paths: it is
    reverted and copied into ``violations/``.

    Refused at load for the same reason ``_concurrent_and_enforced`` is: the
    combination does not fail, it silently does the wrong thing to somebody
    else's work. Both ways out are named, because which one is right depends on
    whether the boundary or the shared tree is the point.
    """
    if (spec.isolation or DEFAULT_ISOLATION) != "none":
        return []
    held = sorted(
        role.label for role in (spec.roles or []) if (role.owns or role.appends) and role.enforce.write == "hard"
    )
    if not held:
        return []
    return [
        f"isolation: none works the person's own checkout and branch, and {', '.join(held)} would be held "
        "to declared paths there -- a stray write is undone by putting the tree back, which would put "
        "their own uncommitted work back with it. Use isolation: branch, which gives the run a branch of "
        "its own in the same checkout, or set enforce.write: soft on those roles."
    ]


def _read_fences_nobody_holds(roles: list) -> list[str]:
    """A role told its reads are fenced, where nothing fences them.

    ``enforce.read: hard`` reaches the guard text and stops there: it swaps in
    "Reading outside the paths above is refused at the tool gate" and changes
    nothing else. No gate refuses the read, so the sentence is false, and a role
    that believes it will not open the file it needed to do its work.

    Refused at load rather than shipped as prose to fix later, on the rule this
    repo already states for the same case elsewhere: a boundary announced as
    enforced and unenforceable is worse than one announced as prompt policy.
    ``reads`` under the default ``soft`` still points a role at where to start,
    and says so in those words.
    """
    return [
        f"{role.label} asks for enforce.read: hard, which nothing enforces yet -- "
        f"the grade reaches the prompt and no gate refuses a read. Use soft, which says "
        f"the same paths are where to start rather than a fence."
        for role in roles
        if getattr(getattr(role, "enforce", None), "read", "soft") == "hard"
    ]


def _concurrent_and_enforced(roles: list) -> list[str]:
    """Two roles that can run at once, where one of them is held to its paths.

    A plan has one checkout and the roles share it, so what a role wrote is
    measured as the difference the *tree* shows since that role started. Two
    roles running at once are two sets of changes in one tree: the first to
    finish is graded against everything both of them wrote, and its judge
    reverts and quarantines the other's work for being outside its own paths.

    Observed, not feared. Two roles with disjoint ``owns``, each writing only
    what it owned: the first judged came back as `dev-a wrote 1 path(s) it may
    not write: src/b/work.py`, and `src/b/work.py` was gone. Nothing errors --
    it reads as a role that would not stay in its lane.

    So the combination is refused where it is declared, rather than left to
    surface as violations in round four. Two ways out, and the message names
    both: order the roles, or say that their boundaries are not enforced. What
    would make it work properly is a checkout per concurrent role, which is a
    merge problem and not a validation rule.
    """
    order = {role.label: index for index, role in enumerate(roles)}
    reaches: dict[str, set[str]] = {role.label: set() for role in roles}

    def walk(label: str, seen: set[str]) -> set[str]:
        if label in seen:
            return set()
        seen.add(label)
        found: set[str] = set()
        for role in roles:
            if role.label != label:
                continue
            for dependency in role.depends_on:
                found.add(dependency)
                found |= walk(dependency, seen)
        return found

    for role in roles:
        reaches[role.label] = walk(role.label, set())

    def held(label: str) -> bool:
        role = next(one for one in roles if one.label == label)
        return bool(role.owns or role.appends) and role.enforce.write == "hard"

    errors = []
    for first, second in combinations(sorted(order, key=order.__getitem__), 2):
        if second in reaches[first] or first in reaches[second]:
            continue
        if not (held(first) or held(second)):
            continue
        errors.append(
            f"{first} and {second} wait on nothing between them, so they run at once in one checkout, "
            f"and what {'both are' if held(first) and held(second) else 'one is'} held to is measured off "
            "that one tree -- each would have the other's work reverted. Put one after the other with "
            "dependsOn, or set enforce.write: soft on both."
        )
    return errors


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
    fillable = _fillable_fields() if allow_blank_fillable else frozenset()
    errors = collect_static_graph_errors(nodes, allowed_blank_fields=fillable, check_paths=False)
    agents = None if known_agents is None else set(known_agents)

    for node in nodes:
        if agents is not None and node.subagent.strip() and node.subagent not in agents:
            errors.append(f"node {node.id!r}: agent {node.subagent!r} is not registered (known: {sorted(agents)})")
        for ref in param_refs(node.prompt_template):
            if ref not in param_names:
                errors.append(f"node {node.id!r}: params.{ref} names no declared param")
            # A secret here is withheld at fill time, not refused at the file --
            # see the note in :func:`validate_structure`.
        for key, value in node.inputs.items():
            for ref in _nested_param_refs(value):
                errors.append(
                    f"node {node.id!r}: input {key!r} contains params.{ref}; params belong directly in "
                    "promptTemplate and must not be copied into node inputs"
                )

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
    # Rule 9: only the node opening a shared session may set its skill menu. A
    # resumed raven-loop session keeps the system prompt it opened with, so a
    # continuation node's skills cannot take effect. MCP grants are different:
    # the DAG runtime resolves them per dispatch, so a continuation may replace
    # or clear them.
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
        declaring = [m for m in members if m.skills is not None]
        if not declaring:
            continue
        ids = {m.id for m in members}
        heads = [m for m in members if not (reach.get(m.id, set()) & (ids - {m.id}))]
        if len(heads) != 1:
            continue
        offenders = sorted(m.id for m in declaring if m.id != heads[0].id)
        if offenders:
            errors.append(
                f"nodes {offenders} declare skills while continuing instance {handle!r} "
                f"opened by node {heads[0].id!r}; a resumed session keeps its opening skill menu, "
                f"so move that field to {heads[0].id!r} or use a separate instance"
            )

    return errors


def _nested_param_refs(value: object) -> set[str]:
    """Return param references nested in a node input value."""
    if isinstance(value, str):
        return set(param_refs(value))
    if isinstance(value, Mapping):
        return {ref for nested in value.values() for ref in _nested_param_refs(nested)}
    if isinstance(value, list | tuple):
        return {ref for nested in value for ref in _nested_param_refs(nested)}
    return set()


def _ancestors(nodes: list[NodeSpec]) -> dict[str, set[str]]:
    """Every node's transitive dependencies, over the whole graph.

    Over the whole graph, not just over one instance group: members of a group
    are routinely ordered *through* nodes outside it -- the shipped orchestration
    guide's shared-session example is ``draft(author) -> review -> revise(author)``,
    where the two members share no direct edge. Closing only over the members
    calls that pair unordered, which is false.

    A fixpoint rather than recursion, so a cycle terminates rather than blowing
    the stack; a cycle makes each member its own ancestor, which reads as "no
    head"; the cycle has already been reported by
    :func:`collect_static_graph_errors`.
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
