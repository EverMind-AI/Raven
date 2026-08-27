# -*- coding: utf-8 -*-
"""The sub-agent DAG spec models and structural validation."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from raven.agent.subagent.dag_store import RUNNING, UNRECORDED, SessionNodes
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_paths import check_confined
from raven.agent.subagent.prompt_placeholders import parse_placeholders
from raven.agent.subagent.prompt_render import check_inputs_referenced

# A node id becomes a path component (``<id>.prompt.md``; ``dag_reader.py`` re-checks
# it with its own copy of this charset before joining a web-supplied id into a
# path), so it stays a closed set.
_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
# An agent name, by contrast, is only ever a table key: looked up in the
# registry, grouped for the capability check, and echoed into status
# JSON. It reaches no path and no shell, so it accepts any name the config layer
# accepts. Holding it to the id charset meant a DAG could not name an agent that
# `spawn` dispatches to happily -- "General Audit" and 研究员 are legal agent
# names, and the node was rejected for the name alone. Edge whitespace is still
# refused because it is invisible: " coder" would fail the table lookup against
# a name that looks identical to the configured one.
# Empty is allowed by the *pattern* and refused by ``validate_and_order``, which
# is the split that lets a playbook ship a node with a field deliberately left
# for the model to fill: the file has to be loadable for the gap to be reported,
# and the graph has to be refused if it reaches dispatch still blank. A pattern
# that rejected empty would make the first impossible.
_AGENT_PATTERN = r"^(?:\S(?:.*\S)?)?$"

# Fields a node cannot run without. A playbook may leave one blank on purpose --
# ``load_playbook`` reports it as a gap for the model to fill -- so "blank" has to
# survive parsing and be caught here instead.
_REQUIRED_NON_BLANK = ("subagent", "prompt_template", "node_summary")


class DagNodeSpec(BaseModel):
    """One node in a sub-agent DAG -- and the only definition of a node.

    A playbook's ``nodes[]`` is this model too (``raven.playbook.types``
    re-exports it), so the two cannot drift the way three separate definitions
    did: none of them was a superset, a playbook could declare ``skills`` the DAG
    had no field for, and the agent field was spelled differently on each side.

    Both wire spellings are accepted and neither is going away. A playbook file is
    camelCase (``promptTemplate``) because it is a file humans write and review in
    git; the JSON Schema handed to the model is snake_case (``prompt_template``)
    because that is the contract it already learned. The unification is at this
    model, via the camel alias generator, not at either wire format.

    Attributes:
        id (`str`):
            Node id, unique across the session rather than just this
            graph -- it is how a later graph names this node's output.
        subagent (`str`):
            Which agent runs this node: a name on the agent table, exactly as the
            roster advertises it. Any kind will do -- a built-in raven agent, a
            cli agent, an acp one. The field names a role rather than a
            provenance: whichever row it points at, that row runs this step as a
            child of the calling turn. The table it indexes is spelled
            ``subagents.agents[]`` -- the container lists identities, this field
            says which one performs the step.
        node_summary (`str`):
            A short title, for the user, naming what this node does -- the
            length of a chat title, under ten words, not a sentence. Blank
            survives the parse and is refused by ``validate_and_order`` for the
            same reason the two fields above are: a playbook may leave it for
            the model to fill.
        prompt_template (`str`):
            Template rendered into the node's prompt file.
        depends_on (`list[str]`):
            Ids of upstream nodes. One in this graph must complete
            first; one an earlier run of this session completed already
            has, so naming it only records the dependency.
        skills (`list[str] | None`):
            Skills to narrow this node's session to. ``None`` means the agent's
            own menu, an empty list means no skills at all. Three-valued because
            "this step gets no skills" is a real instruction and folding it into
            ``None`` advertises its opposite. Only an agent whose row is
            ``injectable.skills`` can take these; elsewhere the graph is accepted
            with a downgrade notice rather than rejected, capability gaps being
            reported and safety gates being refused.
        mcps (`list[str] | None`):
            MCP servers to attach to this node's session, on the same three-valued
            terms. The field exists so a playbook can declare the intent and get a
            straight answer; wiring an MCP client into a sub-agent session is
            separate work, so today every value here downgrades with a notice.
        inputs (`dict[str, object]`):
            Per-node inputs; each value is a literal string, a file
            reference of the form ``{"file": "<path>"}``, or another
            node's output as ``{"node": "<id>"}``.
        instance (`str | None`):
            Optional stateful sub-agent handle; a short, semantic name
            (e.g. ``researcher``) reused across nodes to continue one
            conversation.
    """

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    id: str = Field(pattern=_ID_PATTERN)
    subagent: str = Field(default="", pattern=_AGENT_PATTERN)
    node_summary: str = Field(
        default="",
        description=(
            "A short title for this step, written before its prompt -- the length of a "
            "chat title, under ten words, not a sentence and not a summary of the "
            "prompt. It is this node's row in the run, read by someone watching the "
            "graph, so name the step and leave the detail to the prompt."
        ),
    )
    prompt_template: str = ""
    depends_on: list[str] = Field(default_factory=list)
    skills: list[str] | None = None
    mcps: list[str] | None = None
    inputs: dict[str, object] = Field(default_factory=dict)
    instance: str | None = None


class SubAgentDagSpec(BaseModel):
    """A whole sub-agent DAG: a flat list of nodes with edges, plus its one gate.

    ``task_summary`` is a short title, for the user, naming what the whole graph is for.
    It has no gap-filling path the way a node's summary does -- it is always
    written by the model through the tool schema or by the playbook executor --
    so blank is refused at parse rather than deferred to a later check.

    ``confirm`` is graph-level and there is deliberately no node-level
    counterpart: a node-level gate would need the runner to pause and resume
    mid-graph, while "approve this graph" is already a complete act -- what the
    approver is shown is the whole graph, so agreeing to it is agreeing to every
    step in it. It lives here rather than only in a playbook because a playbook's
    fields must be a subset of a graph's; a gate that only a playbook could ask
    for was the last field still violating that.

    Defaults off, which is today's behaviour for a model-composed graph.
    """

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    task_summary: str = Field(min_length=1)
    nodes: list[DagNodeSpec]
    confirm: bool = False


def parse_dag_spec(data: dict) -> SubAgentDagSpec:
    """Parse a raw graph dict into a validated :class:`SubAgentDagSpec`.

    Args:
        data (`dict`):
            The ``{"task_summary": ..., "nodes": [...]}`` mapping submitted by the agent.

    Returns:
        `SubAgentDagSpec`:
            The parsed spec (field-level validation only).

    Raises:
        `DagValidationError`:
            When the raw data fails field-level validation (bad id /
            subagent charset, unknown keys, wrong types).
    """
    try:
        return SubAgentDagSpec.model_validate(data)
    except ValidationError as exc:
        raise DagValidationError(f"invalid DAG spec: {exc}") from exc


def graph_deps(node: DagNodeSpec, by_id: dict[str, DagNodeSpec]) -> list[str]:
    """Return the node's dependencies that are nodes of this graph.

    ``depends_on`` may also name a node an earlier run of this session
    completed. That one is already satisfied and has no status in this
    run, so every place that orders, waits on, or cascades over an edge
    has to skip it -- and each of them asks here rather than filtering
    on its own.

    Args:
        node (`DagNodeSpec`):
            The dependent node.
        by_id (`dict[str, DagNodeSpec]`):
            This graph's nodes, keyed by id.

    Returns:
        `list[str]`:
            The subset of ``node.depends_on`` present in ``by_id``.
    """
    return [dep for dep in node.depends_on if dep in by_id]


def validate_and_order(
    spec: SubAgentDagSpec,
    roots: tuple[str, ...] | None = None,
    session_nodes: SessionNodes | None = None,
) -> list[str]:
    """Validate graph structure and return a topological node order.

    Checks: unique ids; every ``depends_on`` resolves, to a node of this
    graph or to one an earlier run of this session completed; the graph
    is acyclic; every ``{{ dep.output* }}`` references a declared
    dependency (default-deny); every ``{{ inputs.key }}`` references a
    declared input; every file reference lands inside ``roots``.

    Args:
        spec (`SubAgentDagSpec`):
            The parsed graph spec.
        roots (`tuple[str, ...] | None`):
            Absolute directories a file reference may resolve into --
            the session workdir and this conversation's sub-agent
            history (``<session_dir>/subagents/``). Passed by the caller
            that knows them, so a graph naming an unreachable file is
            refused before any node is dispatched rather than failing
            the node that reads it. See
            :func:`raven.agent.subagent.prompt_paths.check_confined` for what
            ``None`` falls back to.
        session_nodes (`SessionNodes | None`):
            What this session's earlier runs did with each node id
            (``dag_store.read_session_nodes``). Node ids are unique per
            session, so this both refuses a graph that reuses one and
            makes an earlier run's *completed* node addressable by id
            alone.

    Returns:
        `list[str]`:
            Node ids in a topological (dependencies-first) order.

    Raises:
        `DagValidationError`:
            When any structural rule is violated.
    """
    nodes = spec.nodes
    ids = [node.id for node in nodes]
    if len(ids) != len(set(ids)):
        raise DagValidationError("node ids must be unique")

    # A field a playbook left blank on purpose has to be refused here rather than
    # at parse time: ``load_playbook`` needs the file to load so it can report the
    # blank as a gap for the model to fill, and this is the point past which a
    # blank can no longer be filled by anyone.
    for node in nodes:
        blank = [f for f in _REQUIRED_NON_BLANK if not str(getattr(node, f, "") or "").strip()]
        if blank:
            raise DagValidationError(
                f"node '{node.id}' is missing {sorted(blank)} -- a node cannot run without them. "
                f"If this came from a playbook that left them for you to fill, call load_playbook "
                f"again with `fills` for that node; otherwise put the values in the graph."
            )
    id_set = set(ids)
    by_id = {node.id: node for node in nodes}
    known = session_nodes or SessionNodes()

    for node in nodes:
        # Session-wide, not graph-wide: a node id is how a later graph names
        # this one's output, so reusing an id would leave two nodes answering
        # to one name and silently resolve to whichever ran last.
        if (owner := known.owner.get(node.id)) is not None:
            # Only offer the reference when there is one to make: for a node
            # that failed, was skipped or is still being written, reading it is
            # refused too, and advising both leaves no way forward at all.
            advice = (
                f"Rename it, or drop this node and reference '{node.id}' directly (no depends_on needed)"
                if known.is_readable(node.id)
                else "Rename it -- that run left it with no output, so there is nothing to reference either"
            )
            raise DagValidationError(
                f"node id '{node.id}' is already used by run '{owner}'; ids are unique per conversation. {advice}",
            )
        for dep in node.depends_on:
            if dep not in id_set:
                _check_earlier_dep(node.id, dep, known)
        # Runs after the loop above, which leaves every id in depends_on either
        # in this graph or completed in an earlier run -- both readable, which
        # is what the reference checks take a declared dependency to mean.
        _validate_refs(node, roots, known)

    return _topological_order(by_id)


def _check_earlier_dep(node_id: str, dep: str, known: SessionNodes) -> None:
    """Check a ``depends_on`` entry naming no node of this graph.

    Such an entry is an edge out of the graph, so the only thing it can
    name is a node an earlier run of this session already finished. It
    orders nothing -- that run is over -- but it states the dependency,
    and stating it is what lets the graph be read as a whole.

    Args:
        node_id (`str`):
            The dependent node's id.
        dep (`str`):
            The id it depends on, known not to be in this graph.
        known (`SessionNodes`):
            What this session's earlier runs did with each node id.

    Raises:
        `DagValidationError`:
            When the id names a node that produced no output, or names
            nothing this session has run.
    """
    if known.is_readable(dep):
        return
    raise DagValidationError(
        _unreadable(node_id, f"'{dep}'", dep, known)
        or f"node '{node_id}' depends on unknown '{dep}' -- name a node of this graph, or one an earlier run completed"
    )


def _validate_refs(
    node: DagNodeSpec,
    roots: tuple[str, ...] | None = None,
    session_nodes: SessionNodes | None = None,
) -> None:
    """Enforce default-deny on a node's template references.

    Default-deny still holds inside the graph: an ``output`` reference to a
    sibling must go through ``depends_on``, since that edge is what orders the
    two. A node from an earlier run needs no edge -- it has already finished --
    so an id this session's history knows is accepted on its own. It may still
    be declared, which is why ``declared_deps`` is not a graph-local set; the
    caller has already refused any dep that is neither.

    Args:
        node (`DagNodeSpec`):
            The node whose ``prompt_template`` is checked.
        roots (`tuple[str, ...] | None`):
            Roots a file reference may resolve into.
        session_nodes (`SessionNodes | None`):
            What this session's earlier runs did with each node id.

    Raises:
        `DagValidationError`:
            On a dependency or input reference that resolves to nothing, an
            ``input_path`` on an input that has no path, or a ``ref``/file-input
            path that lands outside every root.
    """
    declared_deps = set(node.depends_on)
    known = session_nodes or SessionNodes()
    check_inputs_referenced(node.prompt_template, node.inputs, prefix=f"node '{node.id}' ")
    for ph in parse_placeholders(node.prompt_template):
        if ph.kind in ("output", "output_path"):
            _check_output_ref(node, ph, declared_deps, known)
        if ph.kind in ("input", "input_path") and ph.name not in node.inputs:
            raise DagValidationError(
                f"node '{node.id}' references unknown input '{ph.name}'",
            )
        if ph.kind == "input_path":
            spec = node.inputs.get(ph.name)
            if not isinstance(spec, dict) or not ({"file", "node"} & set(spec)):
                raise DagValidationError(
                    f"node '{node.id}' input '{ph.name}' is not a file or node input, so '.path' cannot be referenced",
                )
        if ph.kind in ("ref", "ref_path"):
            check_confined(ph.name, what="ref", roots=roots)
    for key, value in node.inputs.items():
        if not isinstance(value, dict):
            continue
        if "file" in value:
            check_confined(str(value["file"]), what=f"input '{key}' file", roots=roots)
        elif "node" in value:
            _check_node_input(node, key, value, declared_deps, known)


def _check_output_ref(
    node: DagNodeSpec,
    ph: object,
    declared_deps: set[str],
    known: SessionNodes,
) -> None:
    """Check one ``{{ <id>.output* }}`` reference resolves to something readable.

    Args:
        node (`DagNodeSpec`):
            The referencing node.
        ph (`Placeholder`):
            The output placeholder.
        declared_deps (`set[str]`):
            Ids listed in this node's ``depends_on``.
        known (`SessionNodes`):
            What this session's earlier runs did with each node id.

    Raises:
        `DagValidationError`:
            When the id is neither a declared dependency nor a completed node
            of an earlier run in this session.
    """
    name = ph.name  # type: ignore[attr-defined]
    if name in declared_deps or known.is_readable(name):
        return
    raise DagValidationError(
        _unreadable(node.id, f"'{name}'", name, known)
        or f"node '{node.id}' references undeclared dependency '{name}' -- add it to depends_on if it is in "
        f"this graph, else name a node an earlier run completed"
    )


def _unreadable(node_id: str, what: str, target: str, known: SessionNodes) -> str | None:
    """Explain why a node this session has run cannot be read, if that is why.

    Which of these applies decides the caller's next move entirely -- retry the
    work, fix an upstream, or wait -- and all three look identical from a
    missing output file, which is what this is here to avoid. Returns ``None``
    for an id no run ever claimed: that is not a state, and how to phrase it
    depends on how the reference was written, which the caller knows.

    Args:
        node_id (`str`):
            The referencing node, named in the message.
        what (`str`):
            How the reference was written, e.g. ``"'plan'"`` or
            ``"input 'prev'"``.
        target (`str`):
            The referenced node id.
        known (`SessionNodes`):
            What this session's earlier runs did with each node id.

    Returns:
        `str | None`:
            The error message, or ``None`` when ``target`` is simply unknown.
    """
    state, owner = known.state.get(target), known.owner.get(target)
    if state == "failed":
        return f"node '{node_id}' references {what}, which failed in run '{owner}' and wrote no output. Re-do it under a new id"
    if state == "skipped":
        return f"node '{node_id}' references {what}, which run '{owner}' skipped, so it wrote no output. Re-do it under a new id"
    if state == "cancelled":
        return f"node '{node_id}' references {what}, which was stopped mid-run in run '{owner}', so it wrote no output. Re-do it under a new id"
    if state == RUNNING:
        # Deliberately not "put both in one graph": that node's id is taken, so
        # this graph cannot re-create it, and suggesting otherwise sends the
        # caller into the uniqueness refusal. Nor "wait" alone -- a run that
        # died before finalizing looks exactly like one still going, and waiting
        # for it never ends.
        return (
            f"node '{node_id}' references {what}, which run '{owner}' has not finished writing. Its id is "
            f"taken, so re-submit once that run reports, or use a different id here"
        )
    if state == UNRECORDED:
        return (
            f"node '{node_id}' references {what}: run '{owner}' recorded no outcome for it. Read the file "
            f"instead: {{{{ ref:@runs/{owner}/{target}.out.md }}}}"
        )
    return None


def _check_node_input(
    node: DagNodeSpec,
    key: str,
    spec: dict,
    declared_deps: set[str],
    known: SessionNodes,
) -> None:
    """Check a ``{"node": <id>}`` input resolves to something readable.

    Args:
        node (`DagNodeSpec`):
            The owning node.
        key (`str`):
            The input key.
        spec (`dict`):
            The input value.
        declared_deps (`set[str]`):
            Ids listed in this node's ``depends_on``.
        known (`SessionNodes`):
            What this session's earlier runs did with each node id.

    Raises:
        `DagValidationError`:
            When the referenced node has no output to read, or the input
            carries keys that do not belong to this form.
    """
    if extra := set(spec) - {"node"}:
        raise DagValidationError(
            f"node '{node.id}' input '{key}' mixes a node reference with {sorted(extra)}; a node input takes "
            f"only 'node' -- an id already names one node per conversation, so there is nothing more to qualify",
        )
    target = str(spec["node"])
    if target not in declared_deps and not known.is_readable(target):
        raise DagValidationError(
            _unreadable(node.id, f"input '{key}' -> '{target}'", target, known)
            or f"node '{node.id}' input '{key}' names unknown node '{target}' -- add it to depends_on if it "
            f"is in this graph, else name a node an earlier run completed"
        )


def _topological_order(by_id: dict[str, DagNodeSpec]) -> list[str]:
    """Kahn topological sort; raise on a cycle.

    Args:
        by_id (`dict[str, DagNodeSpec]`):
            Nodes keyed by id.

    Returns:
        `list[str]`:
            A dependencies-first ordering of node ids.

    Raises:
        `DagValidationError`:
            When the graph contains a cycle.
    """
    indegree = {nid: len(graph_deps(node, by_id)) for nid, node in by_id.items()}
    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, node in by_id.items():
        for dep in graph_deps(node, by_id):
            dependents[dep].append(nid)

    ready = sorted(nid for nid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for child in sorted(dependents[nid]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()

    if len(order) != len(by_id):
        raise DagValidationError("graph contains a cycle")
    return order
