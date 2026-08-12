"""The native ``run_subagent_dag`` Raven tool (req4).

Exposes the decoupled DAG subsystem to the main agent as an ordinary Raven
``Tool`` (string in / string out). It builds its own name->SubagentBackend map
from third-party subagent config (sharing the req5 adapter layer), runs the DAG
over a local file backend, and returns a readable summary.

Live progress (``dag_run_started`` / ``dag_node_updated`` / ``dag_run_completed``)
rides a late-bound sink — NOT the spine. The turn's conversation is delivered
per-turn via ``set_context`` (the loop calls it, like spawn/message); the sink
(wired by the gateway to the web channel's emitter) fans the event to that
conversation's subscribers, where the service translates it to an AgentScope
CustomEvent the web UI's DAG graph already renders.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent import workdir
from raven.agent.subagent.backends import (
    AgentMeta,
    build_third_party_backend,
    enabled_third_party,
    format_agent_listing,
    third_party_agent_meta,
)
from raven.agent.subagent.backends.base import IN_SUBAGENT_RUN
from raven.agent.subagent_dag import make_run_id, parse_dag_spec
from raven.agent.subagent_dag._capabilities import AgentCapabilities, validate_capabilities
from raven.agent.subagent_dag._errors import DagValidationError
from raven.agent.subagent_dag._graph import validate_and_order
from raven.agent.subagent_dag._reader import read_node as _read_node
from raven.agent.subagent_dag._reader import read_run as _read_run
from raven.agent.subagent_dag.backend import LocalFileBackend
from raven.agent.subagent_dag.runner import ProgressPublisher, run_dag
from raven.agent.subagent_history import dag_root
from raven.agent.tools.base import Tool, ToolResult

# Sink: (conversation_id, event_name, payload) -> awaitable. Late-bound by the
# host (gateway wires it to the web channel's emitter).
ProgressSink = Callable[[str, str, dict], Awaitable[None]]

# The shipped orchestration guide. Named here so the tool description can send
# the agent to it: this description has room for "what" and "when", not for the
# node-wiring rules, so a caller working from the schema alone reliably gets the
# graph shape wrong. Pass ``guide_skill_id=None`` to drop the pointer when the
# skill is not installed — better no instruction than one that 404s.
GUIDE_SKILL_ID = "local/subagent-dag-orchestration"

_NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique node id (^[A-Za-z0-9_-]+$)."},
        "subagent": {"type": "string", "description": "Name of a configured third-party agent to run this node."},
        "prompt_template": {
            "type": "string",
            "description": (
                "Node prompt. Placeholders: {{ <dep>.output }} / {{ <dep>.output_path }} inject a "
                "dependency's output text/path; {{ inputs.<k> }} / {{ inputs.<k>.path }} inject a literal "
                "or file input; {{ ref:<path> }} / {{ ref_path:<path> }} read a workspace file. A node may "
                "only reference ids listed in its depends_on. The _path forms need a sub-agent the roster "
                "tags [local-files]; for a [no-local-files] one use the contents forms instead."
            ),
        },
        "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Ids this node depends on."},
        "inputs": {"type": "object", "description": 'Per-key literal string, or {"file": <path>}.'},
        "instance": {
            "type": "string",
            "description": (
                "Optional stable handle; nodes sharing it run sequentially and reuse one sub-agent "
                "session. Only give the same handle to several nodes of a sub-agent the roster tags "
                "[stateful] -- elsewhere it carries no context and the graph is rejected. To order "
                "nodes without sharing a session, use depends_on."
            ),
        },
    },
    "required": ["id", "subagent", "prompt_template"],
    "additionalProperties": False,
}


class SubAgentDagTool(Tool):
    """Orchestrate a graph of sub-agent tasks in one call (file-based passing)."""

    timeout_seconds = 1800.0
    # Manual stop (request_cancel / the cancel RPCs) replaces the timer
    # ceiling: the registry skips asyncio.wait_for for blocking_interaction
    # tools, so a long-running DAG is ended by hand, not by a clock.
    blocking_interaction = True

    def __init__(
        self,
        *,
        workspace: Path,
        third_party_subagents: list | None = None,
        progress_publisher: ProgressPublisher | None = None,
        max_concurrency: int = 5,
        guide_skill_id: str | None = GUIDE_SKILL_ID,
        session_dir: "Callable[[str], Path] | None" = None,
    ) -> None:
        self._guide_skill_id = guide_skill_id
        self._workspace = workspace
        self._session_dir = session_dir
        self._fallback_sessions: Any = None
        self._backend = LocalFileBackend()
        self._max_concurrency = max_concurrency
        # A direct publisher (tests) and/or a late-bound conversation-keyed sink.
        self._publisher_override = progress_publisher
        self._sink: ProgressSink | None = None
        self._conversation: ContextVar[str | None] = ContextVar("dag_conversation", default=None)
        self._tool_call_id: ContextVar[str | None] = ContextVar("dag_tool_call_id", default=None)
        self._subagents: dict[str, Any] = {}
        self._subagent_meta: list[AgentMeta] = []
        self._capabilities: dict[str, AgentCapabilities] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self.set_third_party_subagents(third_party_subagents or [])

    def set_third_party_subagents(self, configs: list) -> None:
        """(Re)build the node executor map from config. Hot-appliable (P4).

        The advertised roster and the capability map the pre-check enforces are
        captured alongside the executors, from the same loop, so the description
        can never advertise an agent whose backend failed to build -- nor gate a
        graph on a capability claim the roster never printed.
        """
        built: dict[str, Any] = {}
        meta: list[AgentMeta] = []
        capabilities: dict[str, AgentCapabilities] = {}
        for cfg in enabled_third_party(configs):
            name = getattr(cfg, "name", None)
            try:
                built[name] = build_third_party_backend(cfg)
                entry = third_party_agent_meta(cfg)
                meta.append(entry)
                capabilities[entry.name] = AgentCapabilities(
                    stateful=entry.stateful,
                    reads_local_files=entry.reads_local_files,
                )
            except Exception as e:  # noqa: BLE001 - a bad entry must not sink the tool
                logger.warning("Skipping third-party subagent {!r} for DAG nodes: {}", name, e)
        self._subagents = built
        self._subagent_meta = meta
        self._capabilities = capabilities

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        """Turn-local: record the conversation progress events should fan out to."""
        self._conversation.set(session_key or f"{channel}:{chat_id}")

    def set_tool_call_id(self, tool_call_id: str | None) -> None:
        """Turn-local: record which tool call this run's progress belongs to.

        A consumer that draws the graph under the tool row it came from cannot
        get there from ``run_id`` alone -- one turn may issue several DAG calls,
        and the run ids are minted inside ``execute``, after the row exists.
        """
        self._tool_call_id.set(tool_call_id)

    def set_progress_sink(self, sink: ProgressSink | None) -> None:
        """Late-bind the host sink (gateway -> web emitter)."""
        self._sink = sink

    def request_cancel(self, run_id: str) -> bool:
        """Signal a stop for one in-flight run. Returns whether it was live."""
        event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def active_run_ids(self) -> list[str]:
        """Ids of runs currently accepting a cancel request."""
        return list(self._cancels)

    def _run_root(self, session_key: str | None) -> str:
        """This session's DAG history root, where run dirs are written and read.

        Derived from the session key alone, so a read lands on exactly what
        ``execute`` wrote whether or not a turn is running -- reads mostly
        arrive *between* turns (a reloaded tab, a gateway restarted mid-run).
        Deliberately independent of the session's working directory: repointing
        that must not orphan the history already recorded.

        The directory comes from ``SessionManager.session_dir`` so it tracks the
        transcript's group even where this process would have chosen another
        (raven/agent/subagent_history.py). Falls back to a slug-less manager --
        the gateway's grouping -- when no resolver was injected.
        """
        key = session_key or self._conversation.get() or ""
        if self._session_dir is not None:
            return str(dag_root(self._session_dir(key)))
        if self._fallback_sessions is None:
            from raven.session.manager import SessionManager

            self._fallback_sessions = SessionManager(Path(self._workspace))
        return str(dag_root(self._fallback_sessions.session_dir(key)))

    async def read_run(self, run_id: str, session_key: str | None = None) -> dict:
        """One run's durable structure + per-node state, read back from disk.

        The live progress events are not replayed anywhere, so this is the only
        way a consumer that missed them (a reloaded browser tab) can rebuild the
        graph. Serves in-flight runs too -- see :func:`_reader.read_run`.
        """
        return await _read_run(self._backend, self._run_root(session_key), run_id)

    async def read_node(
        self,
        run_id: str,
        node_id: str,
        *,
        max_output_chars: int = 20000,
        session_key: str | None = None,
    ) -> dict:
        """One node's rendered prompt and (truncated) output, read back from disk."""
        return await _read_node(
            self._backend,
            self._run_root(session_key),
            run_id,
            node_id,
            max_output_chars=max_output_chars,
        )

    async def _emit_progress(self, name: str, value: dict) -> None:
        if (call_id := self._tool_call_id.get()) is not None:
            value = {**value, "tool_call_id": call_id}
        if self._publisher_override is not None:
            await self._publisher_override(name, value)
        conv = self._conversation.get()
        if self._sink is not None and conv is not None:
            await self._sink(conv, name, value)

    @property
    def name(self) -> str:
        return "run_subagent_dag"

    def display_call(self, args: dict[str, Any]) -> str | None:
        """Node count plus the first few ids.

        Without this the UI's generic preview walks the arguments blob and lands
        on whichever string it reaches first -- a single node id, which reads as
        if the call were about that one node.
        """
        nodes = args.get("nodes")
        if not isinstance(nodes, list):
            return None
        ids = [str(node.get("id", "?")) for node in nodes if isinstance(node, dict)]
        if not ids:
            return None
        elided = f" (+{len(ids) - 3} more)" if len(ids) > 3 else ""
        return f"{len(ids)} nodes: {', '.join(ids[:3])}{elided}"

    @staticmethod
    def _result_label(result: Any) -> str:
        """One-line outcome for the transcript row.

        The model-facing text lists every node on its own line, so the loop's
        200-char clamp cuts it off after the first. The tally leads here so it
        survives that clamp however long the workspace path turns out to be.
        """
        summary = result.summary or {}
        parts = [f"{summary.get('completed', 0)}/{summary.get('total', 0)} completed"]
        failed = [str(entry.get("node")) for entry in result.files if entry.get("status") == "failed"]
        if failed:
            elided = f" +{len(failed) - 3}" if len(failed) > 3 else ""
            parts.append(f"{len(failed)} failed ({', '.join(failed[:3])}{elided})")
        if skipped := summary.get("skipped", 0):
            parts.append(f"{skipped} skipped")
        return f"DAG {result.run_id}: {', '.join(parts)} -- outputs in {result.dir}"

    @property
    def description(self) -> str:
        # Same roster rendering as ``spawn``: picking the agent for a node needs
        # the same information as picking one for a spawn, and more of it — a
        # node's `instance` field only makes sense once you know which agents
        # are stateful, and a downstream node's prompt_template has to be
        # written against the shape of what the upstream one returns.
        names = format_agent_listing(self._subagent_meta) or "(none configured)"
        guide = ""
        if self._guide_skill_id:
            guide = (
                "REQUIRED FIRST STEP: unless the orchestration guide is already in your context, call "
                f'`read_skill("{self._guide_skill_id}")` and follow it before calling this tool. '
                "It defines how to wire nodes, the placeholder syntax, the concurrency limit, and "
                "when a single `spawn` is the better choice. Do not design the graph from this "
                "description and the node schema alone. "
            )
        # When to reach for a DAG at all is the always-injected guide's job, not
        # this description's: the model reads the digest before it picks a tool,
        # and two resident surfaces stating the trigger differently is how they
        # drift. What stays here is how to call it.
        return (
            "Orchestrate two or more sub-agent tasks as a single DAG instead of calling sub-agents "
            "one at a time. Independent nodes run concurrently; a node's output is passed to its "
            "dependents through files (large outputs never enter your context). One call carries the "
            "whole graph -- do not issue a separate call per node. "
            f"{guide}"
            f"Available sub-agents for the `subagent` field: {names}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "nodes": {"type": "array", "items": self._node_schema(), "description": "The DAG nodes (a flat list)."}
            },
            "required": ["nodes"],
        }

    def _node_schema(self) -> dict[str, Any]:
        """Node schema with ``subagent`` constrained to the configured roster.

        A misspelled name is only caught in ``run_dag``, and it rejects the
        whole graph, so one typo costs the entire call; the enum moves that to
        the schema.

        Deep-copied rather than annotated in place: ``_NODE_SCHEMA`` is a module
        constant shared by every instance, and the roster is hot-appliable. An
        empty roster omits the enum rather than emitting ``enum: []`` -- that
        matches nothing while ``subagent`` stays required, which some providers
        reject as an unsatisfiable tool schema. Empty is reachable at runtime:
        ``apply_third_party_subagents([])`` clears the roster of an
        already-registered tool.
        """
        schema = deepcopy(_NODE_SCHEMA)
        if names := sorted(self._subagents):
            schema["properties"]["subagent"]["enum"] = names
        return schema

    def _validation_error(self, exc: DagValidationError) -> str:
        """Render a rejected graph as the tool result, with the way back.

        The guide pointer is repeated here rather than left to the tool
        description: a validation failure is the one moment we know the model
        got the graph shape wrong, and by then the description has long since
        scrolled past as static text it read once. Omitted when no guide is
        installed -- see ``GUIDE_SKILL_ID`` -- so the retry advice never sends
        the agent after a skill that cannot resolve.
        """
        detail = str(exc).rstrip()
        if detail and detail[-1] not in ".!?":
            detail += "."
        message = f"Error: invalid DAG — {detail} No sub-agent was run."
        if self._guide_skill_id:
            message += (
                f' Call `read_skill("{self._guide_skill_id}")` for the wiring rules, then retry '
                "with a corrected `nodes` list."
            )
        return message

    async def execute(self, nodes: list[dict], **kwargs: Any) -> str:
        # Backstop, not the primary control: no in-process sub-agent backend
        # registers this tool today. It fires only if one ever does, so the
        # failure is a refusal rather than a silent recursive fan-out.
        if IN_SUBAGENT_RUN.get():
            return (
                "Error: run_subagent_dag is not available inside a sub-agent run — "
                "only the main agent orchestrates DAGs. Complete the assigned task directly."
            )
        # Validation is a distinct phase, ahead of the run: a graph that fails
        # any check costs zero sub-agent dispatches, so a rejection is always
        # cheap enough for the model to just fix and re-submit.
        try:
            spec = parse_dag_spec({"nodes": nodes})
            validate_and_order(spec)
            validate_capabilities(spec, self._capabilities)
        except DagValidationError as exc:
            return self._validation_error(exc)

        run_id = make_run_id()
        cancel = asyncio.Event()
        self._cancels[run_id] = cancel
        try:
            result = await run_dag(
                spec,
                subagents=self._subagents,
                backend=self._backend,
                workdir=str(workdir.current() or self._workspace),
                run_root=self._run_root(self._conversation.get()),
                progress_publisher=self._emit_progress,
                max_concurrency=self._max_concurrency,
                session_key=self._conversation.get(),
                run_id=run_id,
                cancel=cancel,
            )
        except DagValidationError as exc:
            return self._validation_error(exc)
        except Exception as exc:  # noqa: BLE001
            return f"Error running DAG: {exc}"
        finally:
            self._cancels.pop(run_id, None)

        # A terminal event carrying the authoritative manifest, so the web UI can
        # rebuild / finalize the graph (and survive a reload).
        await self._emit_progress(
            "dag_run_completed",
            {
                "run_id": result.run_id,
                "manifest": {
                    "dir": result.dir,
                    "files": result.files,
                    "terminal_outputs": result.terminal_outputs,
                    "summary": result.summary,
                },
            },
        )

        lines: list[str] = [
            f"DAG run {result.run_id} finished: "
            f"{result.summary.get('completed', 0)} completed, "
            f"{result.summary.get('failed', 0)} failed, "
            f"{result.summary.get('skipped', 0)} skipped (of {result.summary.get('total', 0)}).",
            f"Run dir: {result.dir}",
            "",
            "Node output files:",
        ]
        for entry in result.files:
            of = entry.get("output_file") or "(no output file)"
            lines.append(f"- {entry['node']} [{entry['status']}]: {of}")
            if entry.get("error"):
                lines.append(f"    error: {entry['error']}")
        if result.terminal_outputs:
            lines.append("")
            lines.append("Terminal outputs:")
            for term in result.terminal_outputs:
                lines.append(f"### {term['node']}")
                lines.append(term["text"])
        return ToolResult(model_text="\n".join(lines), display_text=self._result_label(result))
