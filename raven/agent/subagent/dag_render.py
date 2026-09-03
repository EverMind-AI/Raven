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
from raven.agent.subagent.dag_store import SessionNodes, memory_path_in, output_path_in
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_placeholders import Placeholder, iter_placeholders
from raven.agent.subagent.prompt_render import (
    check_input_contract,
    read_text,
    require_exists,
    resolve_file_placeholder,
)
from raven.security.trust import wrap_untrusted


async def render_prompt(
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    output_paths: dict[str, str],
    runs_root: str | None = None,
    roots: tuple[str, ...] | None = None,
    session_nodes: SessionNodes | None = None,
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
        output_paths (`dict[str, str]`):
            Mapping of completed dependency id to its output file path.
        runs_root (`str | None`):
            This session's DAG run history root, which ``@runs/`` references
            resolve against. Omit it and such a reference is rejected rather
            than silently resolved against ``cwd``.
        roots (`tuple[str, ...] | None`):
            Absolute directories a file reference may resolve into. Re-checked
            here as well as in ``dag_graph`` because this module is reachable
            without that validation pass.
        session_nodes (`SessionNodes | None`):
            What this session's earlier runs did with each node id. What lets
            an ``output``/``output_path`` reference, or a ``{"node": ...}``
            input, name a node this run did not produce.

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
                output_paths=output_paths,
                runs_root=runs_root,
                roots=roots,
                session_nodes=session_nodes,
            )
        parts.append(cache[ph.raw])
        last = end
    parts.append(template[last:])
    rendered = "".join(parts)
    upstream = _upstream_memory_lines(
        node,
        backend=backend,
        runs_root=runs_root,
        run_id=run_id,
        by_id=by_id,
        capabilities=capabilities,
        session_nodes=session_nodes,
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
    runs_root: str | None,
    run_id: str | None,
    by_id: dict[str, DagNodeSpec] | None,
    capabilities: dict[str, AgentCapabilities] | None,
    session_nodes: SessionNodes | None,
) -> list[str]:
    """One ``- <id>: <path>`` line per upstream whose record this node may read.

    Empty when the paths cannot be named (no run history root or run id), when
    the node has no upstream, or when its sub-agent cannot open local paths --
    the same capability the path placeholders are gated on, for the same reason:
    a path reaches an agent running elsewhere as meaningless text.
    """
    if not runs_root or not run_id or by_id is None:
        return []
    caps = (capabilities or {}).get(node.subagent)
    if caps is not None and not caps.reads_local_files:
        return []
    lines: list[str] = []
    for dep in _transitive_upstream(node, by_id):
        owner = run_id if dep in by_id else (session_nodes.owner.get(dep) if session_nodes else None)
        if owner is None:
            continue
        lines.append(f"- {dep}: {memory_path_in(backend, runs_root, owner, dep)}")
    return lines


async def _resolve(
    ph: Placeholder,
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    output_paths: dict[str, str],
    runs_root: str | None,
    roots: tuple[str, ...] | None,
    session_nodes: SessionNodes | None = None,
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
        output_paths (`dict[str, str]`):
            Dependency id to output file path.
        runs_root (`str | None`):
            Root that ``@runs/`` references resolve against.
        roots (`tuple[str, ...] | None`):
            Roots a file reference may resolve into.
        session_nodes (`SessionNodes | None`):
            What this session's earlier runs did with each node id.

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
        runs_root=runs_root,
        roots=roots,
    )
    if shared is not None:
        return shared
    if ph.kind == "input":
        spec = node.inputs[ph.name]
        resolved = _output_path(
            str(spec["node"]), output_paths, backend, runs_root, session_nodes, f"input '{ph.name}'"
        )
        return wrap_untrusted(await read_text(backend, resolved, cwd, what=f"input '{ph.name}'"), source="subagent")
    if ph.kind == "input_path":
        spec = node.inputs[ph.name]
        resolved = _output_path(
            str(spec["node"]), output_paths, backend, runs_root, session_nodes, f"input '{ph.name}'"
        )
        return await require_exists(backend, resolved, ph.raw)
    resolved = _output_path(ph.name, output_paths, backend, runs_root, session_nodes, ph.raw)
    if ph.kind == "output":
        return wrap_untrusted(await read_text(backend, resolved, cwd, what=ph.raw), source="subagent")
    return await require_exists(backend, resolved, ph.raw)


def _output_path(
    node_id: str,
    output_paths: dict[str, str],
    backend: Any,
    runs_root: str | None,
    session_nodes: SessionNodes | None,
    what: str,
) -> str:
    """Locate one node's output file, in this run or an earlier one.

    A node id names one node per conversation (``validate_and_order`` enforces
    that), so the id alone is enough and no run qualifier is accepted anywhere.
    Pinning one specific run is what the ``{{ ref:@runs/<run_id>/... }}`` file
    form is for -- it reads the path directly and consults no index, which is
    also what reaches a run recorded before ids were indexed.

    This run's own ``output_paths`` wins over the session history: a node of
    this graph has just been written and is the nearer meaning of the id.

    Args:
        node_id (`str`):
            The node whose output is wanted.
        output_paths (`dict[str, str]`):
            Completed nodes of this run, id to output path.
        backend (`BackendBase`):
            Backend supplying the environment's path semantics.
        runs_root (`str | None`):
            This session's DAG history root.
        session_nodes (`SessionNodes | None`):
            What this session's earlier runs did with each node id.
        what (`str`):
            Label for the error message.

    Returns:
        `str`:
            The absolute path of that node's ``.out.md``.

    Raises:
        `DagValidationError`:
            When the node cannot be located.
    """
    if node_id in output_paths:
        return output_paths[node_id]
    owner = (session_nodes or SessionNodes()).owner.get(node_id)
    if owner is None or runs_root is None:
        raise DagValidationError(
            f"{what} names node '{node_id}', which neither this graph nor an earlier run in this conversation produced",
        )
    return output_path_in(backend, runs_root, owner, node_id)
