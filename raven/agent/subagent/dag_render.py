# -*- coding: utf-8 -*-
"""Render a DAG node's prompt template into concrete prompt text.

The template is the dispatching model's own words and stays verbatim; every
value substituted *into* it -- a file's contents, another node's output -- is
fenced as untrusted data first. A node output is sub-agent-authored and a
referenced file may hold anything a run fetched, so neither is an instruction
this prompt is entitled to carry. The ``_path`` forms inject no content and
are left alone, as are literal inputs: the author typed those here.
"""

from typing import Any

from raven.agent.subagent.dag_capabilities import AgentCapabilities
from raven.agent.subagent.dag_graph import DagNodeSpec, graph_deps
from raven.agent.subagent.dag_store import memory_path_in, output_path_in
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_placeholders import Placeholder, iter_placeholders
from raven.agent.subagent.prompt_render import (
    check_input_contract,
    read_node_output,
    resolve_file_placeholder,
)


async def render_prompt(
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    nodes_root: str | None = None,
    roots: tuple[str, ...] | None = None,
    run_id: str | None = None,
    by_id: dict[str, "DagNodeSpec"] | None = None,
    capabilities: dict[str, "AgentCapabilities"] | None = None,
) -> str:
    """Render ``node.prompt_template`` into the node's prompt text.

    The template is rebuilt in a single left-to-right pass over the
    placeholder spans, so a resolved value that itself contains
    ``{{ ... }}`` text is never re-scanned or re-substituted. Each
    distinct placeholder is resolved once and reused for duplicates.

    Args:
        node (`DagNodeSpec`):
            The node whose template is rendered.
        backend (`BackendBase`):
            Backend used to read referenced files.
        cwd (`str`):
            Directory that relative reference paths resolve against.
        nodes_root (`str | None`):
            This session's flat node-artifact root, which ``@nodes/``
            references resolve against. Omit it and such a reference is
            rejected rather than silently resolved against ``cwd``.
        roots (`tuple[str, ...] | None`):
            Absolute directories a file reference may resolve into. Re-checked
            here as well as in ``dag_graph`` because this module is reachable
            without that validation pass.

    Returns:
        `str`:
            The fully substituted prompt text.

    Raises:
        `DagValidationError`:
            When ``inputs.<key>.path`` targets a non-file input, a
            ``ref``/file-input path escapes its root, or a ``_path`` form
            names a file that does not exist.
    """
    check_input_contract(node.prompt_template, node.inputs, prefix=f"node '{node.id}' ")
    template = node.prompt_template
    parts: list[str] = []
    last = 0
    cache: dict[str, str] = {}
    for start, end, ph in iter_placeholders(template):
        parts.append(template[last:start])
        if ph.raw not in cache:
            cache[ph.raw] = await _resolve(
                ph,
                node,
                backend=backend,
                cwd=cwd,
                nodes_root=nodes_root,
                roots=roots,
            )
        parts.append(cache[ph.raw])
        last = end
    parts.append(template[last:])
    rendered = "".join(parts)
    upstream = _upstream_memory_lines(
        node,
        backend=backend,
        nodes_root=nodes_root,
        run_id=run_id,
        by_id=by_id,
        capabilities=capabilities,
    )
    if upstream:
        rendered = f"{rendered}\n\n{_MEMORY_BLOCK_HEADING}\n\n" + "\n".join(upstream) + f"\n\n{_MEMORY_BLOCK_NOTE}"
    return rendered


_MEMORY_BLOCK_HEADING = "## Upstream memory records"

_MEMORY_BLOCK_NOTE = (
    "These are written asynchronously after a node finishes, so a file may not exist yet, or may\n"
    'carry \'"status": "pending"\'. Either way that upstream has no distilled memory available yet:\n'
    "proceed without it rather than waiting for it or treating its absence as an error."
)


def _transitive_upstream(node: DagNodeSpec, by_id: dict[str, DagNodeSpec]) -> list[str]:
    """Every node this one depends on, directly or through another, in id order.

    Walks ``depends_on`` rather than ``graph_deps`` at the first level so an
    upstream belonging to an earlier run is kept: it has no node in this graph,
    but it does have a record, and the walk simply cannot recurse past it.
    """
    seen: set[str] = set()
    frontier = list(node.depends_on)
    while frontier:
        dep = frontier.pop()
        if dep in seen:
            continue
        seen.add(dep)
        upstream = by_id.get(dep)
        if upstream is not None:
            frontier.extend(graph_deps(upstream, by_id))
    seen.discard(node.id)
    return sorted(seen)


def _upstream_memory_lines(
    node: DagNodeSpec,
    *,
    backend: Any,
    nodes_root: str | None,
    run_id: str | None,
    by_id: dict[str, DagNodeSpec] | None,
    capabilities: dict[str, AgentCapabilities] | None,
) -> list[str]:
    """One ``- <id>: <path>`` line per upstream whose record this node may read.

    Empty when the paths cannot be named (no node root or run id), when the
    node has no upstream, or when its sub-agent cannot open local paths -- the
    same capability the path placeholders are gated on, for the same reason: a
    path reaches an agent running elsewhere as meaningless text.
    """
    if not nodes_root or not run_id or by_id is None:
        return []
    caps = (capabilities or {}).get(node.subagent)
    if caps is not None and not caps.reads_local_files:
        return []
    lines: list[str] = []
    for dep in _transitive_upstream(node, by_id):
        lines.append(f"- {dep}: {memory_path_in(backend, nodes_root, dep)}")
    return lines


async def _resolve(
    ph: Placeholder,
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    nodes_root: str | None,
    roots: tuple[str, ...] | None,
) -> str:
    """Resolve one placeholder to its replacement string.

    Everything that does not name a graph node is the shared layer's to
    resolve; what is left here is exactly what needs the graph.

    Args:
        ph (`Placeholder`):
            The placeholder to resolve.
        node (`DagNodeSpec`):
            The owning node (for input lookup).
        backend (`BackendBase`):
            Backend used to read files.
        cwd (`str`):
            Directory for relative path resolution.
        nodes_root (`str | None`):
            Root that ``@nodes/`` references resolve against.
        roots (`tuple[str, ...] | None`):
            Roots a file reference may resolve into.

    Returns:
        `str`:
            The replacement text.

    Raises:
        `DagValidationError`:
            On ``input_path`` for a literal (non-file) input, a ``ref``/file
            path that escapes its root, or a ``_path`` form naming a file
            that does not exist.
    """
    shared = await resolve_file_placeholder(
        ph,
        node.inputs,
        backend=backend,
        cwd=cwd,
        nodes_root=nodes_root,
        roots=roots,
    )
    if shared is not None:
        return shared
    if ph.kind in ("input", "input_path"):
        target = str(node.inputs[ph.name]["node"])
        _output_path(target, backend, nodes_root, f"input '{ph.name}'")
        return await read_node_output(ph, target, backend=backend, cwd=cwd, nodes_root=str(nodes_root))
    _output_path(ph.name, backend, nodes_root, ph.raw)
    return await read_node_output(ph, ph.name, backend=backend, cwd=cwd, nodes_root=str(nodes_root))


def _output_path(node_id: str, backend: Any, nodes_root: str | None, what: str) -> str:
    """Locate one node's output file.

    A node id names one node per conversation (``validate_and_order`` enforces
    that) and every node writes into one flat root, so the id is the whole
    address. Whether that output is *readable* is a separate question, answered
    from the registry while the graph is validated.

    Args:
        node_id (`str`):
            The node whose output is wanted.
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        nodes_root (`str | None`):
            This session's flat node root.
        what (`str`):
            How the reference was written, for the error message.

    Returns:
        `str`:
            The output file's path.

    Raises:
        `DagValidationError`:
            When this caller has no node root to resolve against.
    """
    if nodes_root is None:
        raise DagValidationError(f"{what} names node '{node_id}', but this call has no node history to read it from")
    return output_path_in(backend, nodes_root, node_id)
