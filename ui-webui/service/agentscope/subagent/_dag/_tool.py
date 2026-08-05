# -*- coding: utf-8 -*-
"""The run_subagent_dag orchestration tool."""

from collections.abc import Awaitable, Callable
from typing import Any, AsyncGenerator

from ...message import TextBlock, ToolResultState
from ...permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ...tool import ToolBase, ToolChunk
from ._errors import DagValidationError
from ._graph import parse_dag_spec
from ._runner import _emit, run_dag

_DEFAULT_DESCRIPTION = (
    "Orchestrate a DAG of sub-agents with file-based message passing. "
    "Provide 'nodes': each node has a unique 'id' (letters/digits/_/- "
    "only), a 'subagent' name, a 'prompt_template', optional 'depends_on' "
    "ids, optional 'inputs', and an optional 'instance' (a stable, "
    "semantic handle - e.g. 'researcher' - reusing the same stateful "
    "sub-agent conversation across nodes). "
    "'inputs' maps a key to a literal string or a file reference "
    '{"file": "<workdir-relative path>"}. Templates may reference: '
    "{{ <id>.output }} (an upstream node's output CONTENTS) or "
    "{{ <id>.output_path }} (its output file PATH); {{ inputs.<key> }} "
    "(the literal or file CONTENTS) or {{ inputs.<key>.path }} (the file "
    "PATH); {{ ref:<path> }} (CONTENTS of a prior workdir file, e.g. an "
    "earlier run's output) or {{ ref_path:<path> }} (its PATH). Prefer the "
    "*_path forms to keep large content out of context (the sub-agent "
    "reads the file itself). Only declared dependencies and inputs may be "
    "referenced, and ref/file paths must stay within the session workdir. "
    "Intermediate outputs stay in files; you receive the terminal-node "
    "outputs plus the full list of every node's output file."
)

_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique node id."},
        "subagent": {
            "type": "string",
            "description": "Name of the sub-agent tool to run this node.",
        },
        "prompt_template": {
            "type": "string",
            "description": "Template rendered into the node's prompt file.",
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of upstream nodes that must finish first.",
        },
        "inputs": {
            "type": "object",
            "description": (
                'Per-node inputs: a literal string or {"file": path}.'
            ),
        },
        "instance": {
            "type": "string",
            "description": (
                "Optional stateful sub-agent handle. Choose a short, "
                "semantic name reflecting the instance's role (e.g. "
                "`researcher`); reuse it across nodes to continue one "
                "conversation."
            ),
        },
    },
    "required": ["id", "subagent", "prompt_template"],
}


class SubAgentDagTool(ToolBase):
    """Run a sub-agent DAG with file-based message passing."""

    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __init__(
        self,
        subagents: dict[str, Any],
        backend: Any,
        workdir: str,
        max_concurrency: int = 5,
        description: str | None = None,
        progress_publisher: (
            Callable[[str, dict], Awaitable[None]] | None
        ) = None,
    ) -> None:
        """Initialize the orchestration tool.

        Args:
            subagents (`dict[str, Any]`):
                Sub-agent name to its ``CliSubAgentTool``.
            backend (`BackendBase`):
                Session workspace backend for all file I/O.
            workdir (`str`):
                Session working directory (run dirs live under it).
            max_concurrency (`int`, defaults to `5`):
                Maximum concurrent sub-agent processes.
            description (`str | None`, optional):
                Override for the agent-facing tool description.
            progress_publisher
                (`Callable[[str, dict], Awaitable[None]] | None`, optional):
                Callback invoked with ``(name, value)`` for live progress
                events. Failures are swallowed and never fail a node.
        """
        super().__init__()
        self.name = "run_subagent_dag"
        self.description = description or _DEFAULT_DESCRIPTION
        self._subagents = subagents
        self._backend = backend
        self._workdir = workdir
        self._max_concurrency = max_concurrency
        self._progress_publisher = progress_publisher
        self.input_schema = {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "items": _NODE_SCHEMA,
                    "description": "The DAG nodes to run.",
                },
            },
            "required": ["nodes"],
        }

    async def call(  # type: ignore[override]
        self,
        nodes: list[dict],
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the DAG described by ``nodes``.

        Args:
            nodes (`list[dict]`):
                The node specs (see the tool description / schema).

        Yields:
            `ToolChunk`:
                A single terminal chunk; ``metadata`` carries
                ``run_id`` / ``dir`` / ``terminal_outputs`` / ``files`` /
                ``summary``.

        .. note:: On success, just before the terminal chunk, a
            ``dag_run_completed`` progress event is emitted via the
            configured ``progress_publisher`` carrying
            ``{"run_id": ..., "manifest": <the terminal metadata>}``.
        """
        try:
            spec = parse_dag_spec({"nodes": nodes})
            result = await run_dag(
                spec,
                subagents=self._subagents,
                backend=self._backend,
                workdir=self._workdir,
                max_concurrency=self._max_concurrency,
                progress_publisher=self._progress_publisher,
            )
        except DagValidationError as exc:
            yield ToolChunk(
                content=[TextBlock(text=f"Invalid DAG: {exc}")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        lines = [
            f"DAG run {result.run_id} complete: {result.summary}",
            f"Files under: {result.dir}",
            "",
            "Node output files:",
        ]
        for entry in result.files:
            output_file = entry["output_file"] or "(no output file)"
            lines.append(
                f"- {entry['node']} [{entry['status']}]: {output_file}",
            )
        for term in result.terminal_outputs:
            lines.append(f"\n## {term['node']}\n{term['text']}")

        chunk = ToolChunk(
            content=[TextBlock(text="\n".join(lines))],
            state=ToolResultState.SUCCESS,
            is_last=True,
            metadata={
                "run_id": result.run_id,
                "dir": result.dir,
                "terminal_outputs": result.terminal_outputs,
                "files": result.files,
                "summary": result.summary,
            },
        )
        await _emit(
            self._progress_publisher,
            "dag_run_completed",
            {"run_id": result.run_id, "manifest": chunk.metadata},
        )
        yield chunk

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Auto-run: DAG orchestration is always allowed.

        Args:
            tool_input (`dict[str, Any]`):
                The tool input for this invocation.
            context (`PermissionContext`):
                The permission context.

        Returns:
            `PermissionDecision`:
                An ALLOW decision.
        """
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Sub-agent DAG runs automatically.",
        )
