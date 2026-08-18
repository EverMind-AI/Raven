"""The native ``run_subagent_dag`` Raven tool (req4).

Exposes the decoupled DAG subsystem to the main agent as an ordinary Raven
``Tool`` (string in / string out). It builds its own name->SubagentBackend map
from third-party subagent config (sharing the req5 adapter layer), runs the DAG
over a local file backend, and returns a readable summary.

A run is backgrounded by default, like ``spawn``: the call returns as soon as
the graph is accepted and the result comes back later as an announced turn.
``background=false`` keeps the old behaviour of blocking until the graph
finishes and returning the summary as the tool result.

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
from dataclasses import dataclass
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
from raven.agent.subagent_dag import SubAgentDagSpec, make_run_id, parse_dag_spec
from raven.agent.subagent_dag._capabilities import AgentCapabilities, validate_capabilities
from raven.agent.subagent_dag._errors import DagValidationError
from raven.agent.subagent_dag._graph import validate_and_order
from raven.agent.subagent_dag._reader import read_node as _read_node
from raven.agent.subagent_dag._reader import read_run as _read_run
from raven.agent.subagent_dag._store import SessionNodes, index_guard, read_session_nodes
from raven.agent.subagent_dag.backend import LocalFileBackend
from raven.agent.subagent_dag.runner import ProgressPublisher, run_dag
from raven.agent.subagent_history import dag_root, session_history_root
from raven.agent.tools.base import Tool, ToolResult

# Sink: (conversation_id, event_name, payload) -> awaitable. Late-bound by the
# host (gateway wires it to the web channel's emitter).
ProgressSink = Callable[[str, str, dict], Awaitable[None]]

# (run_id, summary_text, origin) -> awaitable. How a backgrounded run's result
# reaches the main agent; the host supplies ``SubagentManager.announce_dag_result``.
DagAnnouncer = Callable[[str, str, dict], Awaitable[None]]

# (run_id, task, session_key) -> None. Hands a backgrounded run to the host's
# sub-agent lifecycle, so `/stop` and the shutdown sweep reach its CLI children;
# the host supplies ``SubagentManager.adopt_background_run``.
TaskAdopter = Callable[[str, "asyncio.Task", str | None], None]

# (session_key) -> refusal text, or None to proceed. Charges this run to the
# shared sub-agent dispatch budget; the host supplies
# ``SubagentManager.charge_dag_run``.
QuotaCharger = Callable[[str | None], "str | None"]


@dataclass(frozen=True)
class _DagOrigin:
    """Per-turn reply address for a run's progress and its announce.

    Isolated per asyncio task the same way ``spawn``'s is (the tool is shared;
    a turn runs in its own lane task), and captured into a backgrounded run at
    call time -- the run outlives the turn that started it, so reading the
    context variable later would find whatever turn came next.
    """

    channel: str
    chat_id: str
    conversation: str


@dataclass(frozen=True)
class _RunDirs:
    """The directories a run works in, read from the turn that submitted it.

    ``workdir`` is the nodes' cwd and what ``{{ ref:<path> }}`` resolves
    against; ``run_root`` is where the prompt/output records go;
    ``subagents_root`` is its parent, the second directory a file reference may
    resolve into. All three come from turn-local state that a backgrounded run
    outlives, so they are resolved at call time rather than looked up once the
    graph is already running.
    """

    workdir: str
    run_root: str
    subagents_root: str


# How long a terminal event may take when a run ends without a manifest. It is
# also emitted from a cancelled task, where an unbounded await can hang a
# gateway shutdown behind a sink that is already closing.
_CLOSE_TIMEOUT_SECONDS = 5.0

# The shipped orchestration guide. Named here so the tool description can send
# the agent to it: this description has room for "what" and "when", not for the
# node-wiring rules, so a caller working from the schema alone reliably gets the
# graph shape wrong. Pass ``guide_skill_id=None`` to drop the pointer when the
# skill is not installed — better no instruction than one that 404s.
GUIDE_SKILL_ID = "local/subagent-dag-orchestration"

_NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "Node id (^[A-Za-z0-9_-]+$), unique across this whole conversation, not just this graph: "
                "it is how a later graph names this node's output. Reusing an id an earlier run took is "
                "rejected -- pick a fresh one (plan_v2, research_pricing) rather than repeating a generic "
                "one, and to re-do work under the same name give the node a new id."
            ),
        },
        "subagent": {"type": "string", "description": "Name of a configured third-party agent to run this node."},
        "prompt_template": {
            "type": "string",
            "description": (
                "Node prompt. Placeholders: {{ <node>.output }} / {{ <node>.output_path }} inject a "
                "node's output text/path; {{ inputs.<k> }} / {{ inputs.<k>.path }} inject an input; "
                "{{ ref:<path> }} / {{ ref_path:<path> }} read a file. <node> is either a node of this "
                "graph listed in this node's depends_on, or any node an earlier run in this conversation "
                "completed, since ids are unique across it. To read a run's file directly, "
                "{{ ref:@runs/<run_id>/<node>.out.md }}. The _path forms need a "
                "sub-agent the roster tags [local-files]; for a [no-local-files] one use the contents "
                "forms instead, and every _path form must name a file that already exists."
            ),
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ids this node depends on. A node of this graph runs first; a node an earlier run in "
                "this conversation completed is already done, so listing it only records the "
                "dependency. Either way you may read it with {{ <node>.output }}."
            ),
        },
        "inputs": {
            "type": "object",
            "description": (
                'Per-key literal string, {"file": <path>}, or {"node": <id>} to take another node\'s '
                "output -- a dependency of this node, or any node from an earlier run in this "
                "conversation. {{ inputs.<k> }} injects the text, {{ inputs.<k>.path }} the file path."
            ),
        },
        "instance": {
            "type": "string",
            "description": (
                "Optional stable handle; nodes sharing it run sequentially and reuse one sub-agent "
                "session, including across separate runs in this conversation. Only give the same "
                "handle to several nodes of a sub-agent the roster tags [stateful] -- elsewhere it "
                "carries no context and the graph is rejected. Omit it and the node starts from a "
                "clean session every time. To order nodes without sharing a session, use depends_on."
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
    # tools, so a long-running DAG is ended by hand, not by a clock. Declared
    # for every call, background or not, the same way ``spawn`` declares it
    # while also returning before its sub-agent does -- see the note there.
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
        is_paused: "Callable[[], bool] | None" = None,
        gate: asyncio.Semaphore | None = None,
        announce: DagAnnouncer | None = None,
        adopt: TaskAdopter | None = None,
        state_for: "Callable[[str, str | None, str], Any] | None" = None,
        charge: QuotaCharger | None = None,
    ) -> None:
        # Read through to SubagentManager's flag rather than mirroring it: this
        # tool dispatches to its own backends without ever calling ``spawn``, so
        # a pause set from the agents overlay would otherwise stop single spawns
        # while a graph kept fanning out behind a HUD reading "paused".
        self._is_paused = is_paused
        self._guide_skill_id = guide_skill_id
        self._workspace = workspace
        self._session_dir = session_dir
        self._fallback_sessions: Any = None
        self._backend = LocalFileBackend()
        # One gate for every run this tool starts, not one per run: several can
        # be in flight at once now that the default is to background them. The
        # host passes the sub-agent manager's, so spawns count against it too.
        self._gate = gate if gate is not None else asyncio.Semaphore(max_concurrency)
        self._announce = announce
        # The manager's instance-state derivation, so a node that names an
        # `instance` continues that conversation on the same terms `spawn` and a
        # direct chat do. Injected rather than imported: this tool is built from
        # the same config as the manager but does not own one.
        self._state_for = state_for
        self._adopt = adopt
        self._charge = charge
        # A direct publisher (tests) and/or a late-bound conversation-keyed sink.
        self._publisher_override = progress_publisher
        self._sink: ProgressSink | None = None
        self._default_origin = _DagOrigin(channel="cli", chat_id="direct", conversation="cli:direct")
        self._origin: ContextVar[_DagOrigin | None] = ContextVar("dag_origin", default=None)
        self._tool_call_id: ContextVar[str | None] = ContextVar("dag_tool_call_id", default=None)
        self._subagents: dict[str, Any] = {}
        self._subagent_meta: list[AgentMeta] = []
        self._capabilities: dict[str, AgentCapabilities] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        # Strong references to in-flight background runs. Without them the event
        # loop only weakly references a bare create_task, and a run can be
        # garbage-collected mid-graph.
        self._runs: dict[str, asyncio.Task] = {}
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
        """Turn-local: record where this turn's progress and announces are addressed."""
        self._origin.set(
            _DagOrigin(channel=channel, chat_id=chat_id, conversation=session_key or f"{channel}:{chat_id}")
        )

    def _turn_conversation(self) -> str | None:
        """This turn's conversation, or None outside a turn.

        Distinct from ``origin.conversation``, which substitutes a default so a
        run always has somewhere to report: a history root must stay unresolved
        when no turn set one, or a read between turns would look under a key
        nothing was ever written to.
        """
        turn = self._origin.get()
        return turn.conversation if turn is not None else None

    async def _session_nodes(self) -> SessionNodes:
        """What this session's earlier runs did with each node id.

        Runs during validation, so it must not create anything: a rejected
        graph has to leave the session's directories exactly as it found them.
        The injected resolver only reads, but the fallback ``SessionManager``
        creates ``sessions/`` when it is constructed -- so with no resolver and
        no ``sessions/`` yet there is provably no history to read, and building
        one just to learn that would itself be the write we are avoiding.

        The root must be the one the run itself will claim ids into -- the same
        ``_run_root(_turn_conversation())`` pair ``_dirs`` resolves. Validating
        against one index while claiming into another would refuse nothing and
        detect nothing.
        """
        if self._session_dir is None and not (Path(self._workspace) / "sessions").is_dir():
            return SessionNodes()
        root = self._run_root(self._turn_conversation())
        # Guarded like every other read that decides something, so this cannot
        # see a half-written index. It is still only a pre-check -- ``run_dag``
        # repeats it inside the guard that also claims the ids.
        async with index_guard(root):
            return await read_session_nodes(self._backend, root)

    def _reference_roots(self) -> tuple[str, ...]:
        """The directories a node's file references may resolve into.

        The turn's working directory and this conversation's sub-agent history,
        matching what ``run_dag`` derives -- computed here as well so a graph
        naming an unreachable file is refused in the caller's own turn, before
        any node is dispatched.
        """
        roots = [str(workdir.current() or self._workspace)]
        if (history := self._history_root()) is not None:
            roots.append(history)
        return tuple(roots)

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
        return str(dag_root(self._session_dir_for(session_key)))

    def _session_dir_for(self, session_key: str | None) -> Path:
        """This conversation's session directory, the parent of both roots.

        Shared by ``_run_root`` and ``_history_root`` so the run directory and
        the reference root can never be derived from different sessions.
        """
        key = session_key or self._turn_conversation() or ""
        if self._session_dir is not None:
            return Path(self._session_dir(key))
        if self._fallback_sessions is None:
            from raven.session.manager import SessionManager

            self._fallback_sessions = SessionManager(Path(self._workspace))
        return Path(self._fallback_sessions.session_dir(key))

    def _history_root(self) -> str | None:
        """This conversation's sub-agent history root, or None if it has none.

        ``<session_dir>/subagents/`` -- the second directory a node's file
        reference may resolve into, holding this conversation's DAG runs and
        ``spawn`` records. Narrower than agent home deliberately: see
        :func:`._paths.check_confined`.

        ``None`` rather than a path when no resolver is injected and no
        ``sessions/`` exists, because building a ``SessionManager`` to find out
        would create the directory -- and validation must leave a rejected
        graph's session exactly as it found it. With no history there is also
        nothing for a reference to name.
        """
        if self._session_dir is None and not (Path(self._workspace) / "sessions").is_dir():
            return None
        return str(session_history_root(self._session_dir_for(None)))

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

    def _emitter(self, conversation: str | None, call_id: str | None) -> ProgressPublisher:
        """A publisher bound to one call's conversation and tool row.

        Both are turn-local context variables, and a backgrounded run reports
        long after that context is gone -- so they are read once, here, and
        closed over rather than looked up per event.
        """

        async def publish(name: str, value: dict) -> None:
            if call_id is not None:
                value = {**value, "tool_call_id": call_id}
            if self._publisher_override is not None:
                await self._publisher_override(name, value)
            if self._sink is not None and conversation is not None:
                await self._sink(conversation, name, value)

        return publish

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
            "whole graph -- do not issue a separate call per node. The graph runs in the background and "
            "its result is announced to you when it finishes, so do not poll it and do not re-submit it. "
            f"{guide}"
            f"Available sub-agents for the `subagent` field: {names}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "nodes": {"type": "array", "items": self._node_schema(), "description": "The DAG nodes (a flat list)."},
                "background": {
                    "type": "boolean",
                    "description": (
                        "Default true: return as soon as the run starts and get the result as an "
                        "announcement when the graph finishes, leaving you free to work meanwhile. "
                        "Set false only when you cannot continue without the outputs -- that blocks "
                        "until every node is done and returns the full summary as this call's result."
                    ),
                },
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

    async def execute(self, nodes: list[dict], background: bool = True, **kwargs: Any) -> str:
        # Backstop, not the primary control: no in-process sub-agent backend
        # registers this tool today. It fires only if one ever does, so the
        # failure is a refusal rather than a silent recursive fan-out.
        if IN_SUBAGENT_RUN.get():
            return (
                "Error: run_subagent_dag is not available inside a sub-agent run — "
                "only the main agent orchestrates DAGs. Complete the assigned task directly."
            )
        return await self._execute(nodes, background)

    async def run_with_roles(
        self,
        nodes: list[dict],
        *,
        roles: dict[str, Any],
        role_capabilities: dict[str, AgentCapabilities],
        background: bool = True,
    ) -> str:
        """Run a graph whose nodes dispatch to caller-built backends.

        The playbook executor's entry: ``roles`` maps a role name to a backend
        instance built for this run (whitelisted tools/skills), merged over the
        configured roster for the roster check and the dispatch map alike. Not
        LLM-facing — the tool schema and ``execute`` are unchanged, so a model
        (or injected text) cannot smuggle backends in through a tool call.
        """
        return await self._execute(nodes, background, extra_subagents=roles, extra_capabilities=role_capabilities)

    async def _execute(
        self,
        nodes: list[dict],
        background: bool,
        extra_subagents: dict[str, Any] | None = None,
        extra_capabilities: dict[str, AgentCapabilities] | None = None,
    ) -> str:
        subagents = {**self._subagents, **(extra_subagents or {})}
        capabilities = {**self._capabilities, **(extra_capabilities or {})}
        # Refused whole rather than per node, and ahead of validation, for the
        # same reason validation runs early: a refused graph must cost zero
        # sub-agent dispatches.
        if self._is_paused is not None and self._is_paused():
            return (
                "Error: delegation is paused. The user paused sub-agent spawning; "
                "do the work in this turn instead, or ask them to resume."
            )
        # Validation is a distinct phase, ahead of the run, in both modes: a
        # graph that fails any check costs zero sub-agent dispatches and is
        # rejected in the caller's own turn, so a rejection is always cheap
        # enough for the model to just fix and re-submit. Backgrounding must not
        # turn a malformed graph into an announcement that arrives a turn later.
        try:
            spec = parse_dag_spec({"nodes": nodes})
            validate_and_order(spec, self._reference_roots(), await self._session_nodes())
            # ``capabilities``, not ``self._capabilities``: a caller-supplied role
            # (the playbook executor's entry) is only in the merged map, and the
            # capability pre-check has to see it or every such node is unknown.
            validate_capabilities(spec, capabilities)
            # ``run_dag`` checks the roster too, but it does so inside the run --
            # which a backgrounded call has already returned from. Checked here
            # as well so a misspelled name is still a refusal the model can fix
            # in the same turn, not an announcement a turn later.
            for node in spec.nodes:
                if subagents.get(node.subagent) is None:
                    raise DagValidationError(f"node '{node.id}' names unknown sub-agent '{node.subagent}'")
        except DagValidationError as exc:
            return self._validation_error(exc)

        origin = self._origin.get() or self._default_origin
        session_dir = self._session_dir_for(self._turn_conversation())
        dirs = _RunDirs(
            workdir=str(workdir.current() or self._workspace),
            run_root=str(dag_root(session_dir)),
            subagents_root=str(session_history_root(session_dir)),
        )
        # Charged after validation so a rejected graph costs no budget, and
        # before either mode starts so the refusal is the caller's own result.
        if self._charge is not None and (refusal := self._charge(origin.conversation)) is not None:
            return refusal
        call_id = self._tool_call_id.get()
        run_id = make_run_id()
        cancel = asyncio.Event()
        self._cancels[run_id] = cancel

        if not background:
            return await self._run(spec, run_id, cancel, origin, dirs, call_id, subagents=subagents)

        task = asyncio.create_task(
            self._run_and_announce(spec, run_id, cancel, origin, dirs, call_id, subagents=subagents)
        )
        self._runs[run_id] = task
        task.add_done_callback(lambda _t: self._runs.pop(run_id, None))
        if self._adopt is not None:
            self._adopt(run_id, task, origin.conversation)
        return ToolResult(
            model_text=(
                f"DAG run {run_id} started in the background ({len(spec.nodes)} nodes). "
                "I'll report the result when it finishes -- keep working, and do not submit this graph again."
            ),
            display_text=f"DAG {run_id}: {len(spec.nodes)} nodes started",
        )

    async def _run_and_announce(
        self,
        spec: SubAgentDagSpec,
        run_id: str,
        cancel: asyncio.Event,
        origin: _DagOrigin,
        dirs: _RunDirs,
        call_id: str | None,
        subagents: dict[str, Any] | None = None,
    ) -> None:
        """Run a backgrounded graph, then send its summary back as a turn."""
        result = await self._run(spec, run_id, cancel, origin, dirs, call_id, subagents=subagents)
        if cancel.is_set():
            # A stop the user asked for. ``run_dag`` still returns normally,
            # with every unfinished node skipped, but announcing that would
            # spend a turn narrating what they just cancelled -- which is why
            # a cancelled spawn stays silent too.
            logger.info("DAG run {} was stopped; not announcing a result", run_id)
            return
        if self._announce is None:
            logger.info("DAG run {} finished with no announcer wired; result reaches no one", run_id)
            return
        try:
            await self._announce(
                run_id,
                str(getattr(result, "model_text", result)),
                {"channel": origin.channel, "chat_id": origin.chat_id, "session_key": origin.conversation},
            )
        except Exception as exc:  # noqa: BLE001 - a failed announce must not also lose the log line
            logger.error("DAG run {} finished but its result could not be announced: {}", run_id, exc)

    @staticmethod
    async def _close_graph(emit: ProgressPublisher, run_id: str, node_count: int, detail: dict) -> None:
        """Tell a drawn graph the run is over when it ended with no manifest.

        ``dag_run_started`` has already drawn the nodes by the time a collapse
        or a stop lands, and the drawing only settles on a terminal event.
        Blocking, the same turn also delivered the outcome as the tool result,
        so a graph left mid-flight was visible next to it; backgrounded, the
        tool row already says "started", leaving the graph reading as still
        running until a reload reconciles it against ``active_run_ids``.

        The manifest carries no counts because the run produced none -- what it
        asserts is that there will be no more events, plus why. A consumer
        settles the nodes from the absence of a ``files`` entry, not from the
        detail (ui-tui/src/domain/dagRun.ts, ``fromCompletion``).

        Bounded and guarded: this is also reached from a cancelled task, where
        the sink may be on its way out, and a close that hangs or raises must
        not outweigh the outcome the caller is already carrying.
        """
        try:
            await asyncio.wait_for(
                emit("dag_run_completed", {"run_id": run_id, "manifest": {**detail, "summary": {"total": node_count}}}),
                timeout=_CLOSE_TIMEOUT_SECONDS,
            )
        except Exception as emit_exc:  # noqa: BLE001
            logger.error("DAG run {} ended but its graph could not be closed: {}", run_id, emit_exc)

    async def _run(
        self,
        spec: SubAgentDagSpec,
        run_id: str,
        cancel: asyncio.Event,
        origin: _DagOrigin,
        dirs: _RunDirs,
        call_id: str | None,
        subagents: dict[str, Any] | None = None,
    ) -> str | ToolResult:
        """Execute one validated graph and render its outcome."""
        emit = self._emitter(origin.conversation, call_id)
        try:
            result = await run_dag(
                spec,
                subagents=subagents if subagents is not None else self._subagents,
                backend=self._backend,
                workdir=dirs.workdir,
                run_root=dirs.run_root,
                subagents_root=dirs.subagents_root,
                progress_publisher=emit,
                semaphore=self._gate,
                session_key=origin.conversation,
                state_for=self._state_for,
                run_id=run_id,
                cancel=cancel,
            )
        except DagValidationError as exc:
            return self._validation_error(exc)
        except Exception as exc:  # noqa: BLE001
            # Named, like every other shape this method returns: a backgrounded
            # run's outcome reaches the agent a turn later as a message of its
            # own, so a bare error is one it cannot attribute to any of the
            # graphs it has in flight. The summary carries that itself rather
            # than the announce framing it -- see ``announce_dag_result``.
            await self._close_graph(emit, run_id, len(spec.nodes), {"error": str(exc)})
            return f"Error running DAG {run_id}: {exc}"
        except asyncio.CancelledError:
            # The other way a run is stopped. ``dag.cancel`` sets the event and
            # lets ``run_dag`` return, so the graph settles on its own manifest;
            # ``/stop`` and the shutdown sweep instead cancel the task, a route
            # this branch opened by adopting the run into the manager's index.
            # Without this the two disagree and only one of them closes.
            #
            # Served at shutdown too rather than only for a live stop: the sink
            # is on its way out there and awaiting inside a cancelled task is
            # fragile, which is what the bound in ``_close_graph`` is for. A
            # close that loses the race changes nothing -- the task ends
            # cancelled either way.
            await self._close_graph(emit, run_id, len(spec.nodes), {"stopped": True})
            raise
        finally:
            self._cancels.pop(run_id, None)

        # A terminal event carrying the authoritative manifest, so the web UI can
        # rebuild / finalize the graph (and survive a reload).
        await emit(
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
