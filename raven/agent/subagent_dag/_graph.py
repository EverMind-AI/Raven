# -*- coding: utf-8 -*-
"""The sub-agent DAG spec models and structural validation."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ._errors import DagValidationError
from ._paths import check_confined
from ._placeholders import parse_placeholders

# A node id becomes a path component (``<id>.prompt.md``; ``_reader.py`` re-checks
# it with its own copy of this charset before joining a web-supplied id into a
# path), so it stays a closed set.
_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
# A sub-agent name, by contrast, is only ever a roster key: looked up in the
# name->backend map, grouped for the capability check, and echoed into status
# JSON. It reaches no path and no shell, so it accepts any name the config layer
# accepts. Holding it to the id charset meant a DAG could not name an agent that
# `spawn` dispatches to happily -- "General Audit" and 研究员 are legal agent
# names, and the node was rejected for the name alone. Edge whitespace is still
# refused because it is invisible: " coder" would fail the roster lookup against
# a name that looks identical to the configured one.
_SUBAGENT_PATTERN = r"^\S(?:.*\S)?$"


class DagNodeSpec(BaseModel):
    """One node in a sub-agent DAG.

    Attributes:
        id (`str`):
            Unique node id within the graph.
        subagent (`str`):
            Name of the configured third-party sub-agent that runs this
            node, exactly as the roster advertises it.
        prompt_template (`str`):
            Template rendered into the node's prompt file.
        depends_on (`list[str]`):
            Ids of upstream nodes that must complete first.
        inputs (`dict[str, object]`):
            Per-node inputs; each value is a literal string or a file
            reference of the form ``{"file": "<path>"}``.
        instance (`str | None`):
            Optional stateful sub-agent handle; a short, semantic name
            (e.g. ``researcher``) reused across nodes to continue one
            conversation.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    subagent: str = Field(pattern=_SUBAGENT_PATTERN)
    prompt_template: str
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, object] = Field(default_factory=dict)
    instance: str | None = None


class SubAgentDagSpec(BaseModel):
    """A whole sub-agent DAG: a flat list of nodes with edges."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[DagNodeSpec]


def parse_dag_spec(data: dict) -> SubAgentDagSpec:
    """Parse a raw graph dict into a validated :class:`SubAgentDagSpec`.

    Args:
        data (`dict`):
            The ``{"nodes": [...]}`` mapping submitted by the agent.

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


def validate_and_order(spec: SubAgentDagSpec) -> list[str]:
    """Validate graph structure and return a topological node order.

    Checks: unique ids; every ``depends_on`` resolves; the graph is
    acyclic; every ``{{ dep.output* }}`` references a declared
    dependency (default-deny); every ``{{ inputs.key }}`` references a
    declared input.

    Args:
        spec (`SubAgentDagSpec`):
            The parsed graph spec.

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
    id_set = set(ids)
    by_id = {node.id: node for node in nodes}

    for node in nodes:
        for dep in node.depends_on:
            if dep not in id_set:
                raise DagValidationError(
                    f"node '{node.id}' depends on unknown '{dep}'",
                )
        _validate_refs(node)

    return _topological_order(by_id)


def _validate_refs(node: DagNodeSpec) -> None:
    """Enforce default-deny on a node's template references.

    Args:
        node (`DagNodeSpec`):
            The node whose ``prompt_template`` is checked.

    Raises:
        `DagValidationError`:
            On a dependency or input reference that is not declared, an
            ``input_path`` on a non-file input, or a ``ref``/file-input
            path that escapes the session workdir.
    """
    declared_deps = set(node.depends_on)
    for ph in parse_placeholders(node.prompt_template):
        if ph.kind in ("output", "output_path") and ph.name not in (declared_deps):
            raise DagValidationError(
                f"node '{node.id}' references undeclared dependency '{ph.name}'",
            )
        if ph.kind in ("input", "input_path") and ph.name not in node.inputs:
            raise DagValidationError(
                f"node '{node.id}' references unknown input '{ph.name}'",
            )
        if ph.kind == "input_path":
            spec = node.inputs.get(ph.name)
            if not isinstance(spec, dict) or "file" not in spec:
                raise DagValidationError(
                    f"node '{node.id}' input '{ph.name}' is not a file input, so '.path' cannot be referenced",
                )
        if ph.kind in ("ref", "ref_path"):
            check_confined(ph.name, what="ref")
    for key, value in node.inputs.items():
        if isinstance(value, dict) and "file" in value:
            check_confined(str(value["file"]), what=f"input '{key}' file")


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
    indegree = {nid: len(node.depends_on) for nid, node in by_id.items()}
    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, node in by_id.items():
        for dep in node.depends_on:
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
