"""The ``run_subagent_dag`` tool: orchestrate a graph of sub-agent tasks.

This subsystem lives beside the single-call ``spawn`` surface and shares its
prompt-template layer (``prompt_*`` modules), but stays otherwise independent
of Raven's kernel: it carries its own graph model, ready-set scheduler, and
on-disk run store, is exposed as an ordinary optional Raven tool, and is NOT
routed through ``Origin.SUBAGENT`` or the spine scheduler. It is deliberately
absent from ``subagent/__init__.py`` -- re-exporting it there would make every
``SubagentManager`` import pull in the graph model, the store, and the runner.

Exposes the decoupled DAG subsystem to the main agent as an ordinary Raven
``Tool`` (string in / string out). It resolves each node's agent through the
shared agent table (:class:`raven.agent.subagent.registry.AgentRegistry`, normally
the sub-agent manager's), runs the DAG over a local file backend, and returns a
readable summary.

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
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent import workdir
from raven.agent.subagent.backends.base import IN_SUBAGENT_RUN
from raven.agent.subagent.dag_capabilities import AgentCapabilities, validate_capabilities
from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec, parse_dag_spec, validate_and_order
from raven.agent.subagent.dag_machines import refusal as machines_refusal
from raven.agent.subagent.dag_machines import unnamed_machine
from raven.agent.subagent.dag_machines import verdict_for_async as machines_verdict_async
from raven.agent.subagent.dag_machines import with_facts as with_machine_facts
from raven.agent.subagent.dag_reader import read_node as _read_node
from raven.agent.subagent.dag_reader import read_run as _read_run
from raven.agent.subagent.dag_runner import ProgressPublisher, run_dag
from raven.agent.subagent.dag_store import SessionNodes, index_guard, make_run_id, read_index, read_session_nodes
from raven.agent.subagent.history import dag_root, session_history_root
from raven.agent.subagent.instances import mint_handle
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent_memory import EverosIdentity
from raven.agent.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from raven.agent.subagent.registry import AgentRegistry

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

# (conversation_id, question) -> whether the user approved. The graph-level
# ``confirm`` gate's only route to a human; hosts that have no way to ask leave it
# unwired, and see ``_confirmed`` for what happens then.
Ask = Callable[[str, str], Awaitable[bool]]


def _with_notices(result: "str | ToolResult", notices: list[str]) -> "str | ToolResult":
    """Prepend downgrade notices to whatever the run is reporting.

    On the model-facing text only. A notice says a field the graph carried will
    not take effect, which is something the caller has to know when it reads the
    output; the display line is a one-liner for a transcript row and has no room
    for it.
    """
    if not notices:
        return result
    head = "Note: " + "; ".join(notices) + "."
    if isinstance(result, ToolResult):
        return ToolResult(model_text=f"{head}\n\n{result.model_text}", display_text=result.display_text)
    return f"{head}\n\n{result}"


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


@dataclass(frozen=True)
class _NodeBuild:
    """One node's narrowing, in the shape the registry's factory reads.

    Only ``skills`` reaches here: ``mcps`` has nothing to attach to yet (the
    graph is told so by a downgrade notice), and ``tools`` is not a node field --
    a node narrows what its agent may consult, not what it may do.
    """

    skills_allow: list[str] | None
    tools_allow: list[str] | None = None


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
        "subagent": {
            "type": "string",
            "description": "Name of the agent that runs this node, from the roster.",
        },
        "node_summary": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A short title for this step, written before its prompt -- the length of a "
                "chat title, under ten words, not a sentence and not a summary of the "
                "prompt. It is this node's row in the run, read by someone watching the "
                "graph, so name the step and leave the detail to the prompt."
            ),
        },
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
                "conversation. {{ inputs.<k> }} injects the text, {{ inputs.<k>.path }} the file path. "
                "One key is reserved: 'machine' names which of the owner's machines this node's work "
                "runs on, and a node on an agent that runs work on them is rejected without it. Settle "
                "it when you write the graph -- every node in the run is then told what that machine is "
                "and works to it, and code written before the machine is known is written for an "
                "imagined one."
            ),
        },
        "instance": {
            "type": "string",
            "description": (
                "Optional stable handle; nodes sharing it run sequentially and reuse one sub-agent "
                "session, including across separate runs in this conversation. Only give the same "
                "handle to several nodes of a sub-agent the roster tags [stateful] -- elsewhere it "
                "carries no context and the graph is rejected. Omit it and a [stateful] "
                "sub-agent's node is given one automatically, reported in the run summary when "
                "the node finishes, so that node can be continued later; a node on any other "
                "sub-agent gets none and cannot be continued. To order nodes without sharing a "
                "session, use depends_on."
            ),
        },
    },
    "required": ["id", "subagent", "node_summary", "prompt_template"],
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
        registry: "AgentRegistry | None" = None,
        agents: list | None = None,
        progress_publisher: ProgressPublisher | None = None,
        max_concurrency: int = 5,
        guide_skill_id: str | None = GUIDE_SKILL_ID,
        session_dir: "Callable[[str], Path] | None" = None,
        is_paused: "Callable[[], bool] | None" = None,
        gate: asyncio.Semaphore | None = None,
        announce: DagAnnouncer | None = None,
        adopt: TaskAdopter | None = None,
        state_for: "Callable[[str, str | None, str], Any] | None" = None,
        everos_for: "Callable[[str], EverosIdentity | None] | None" = None,
        charge: QuotaCharger | None = None,
        ask: "Ask | None" = None,
        control_reachable: "Callable[[], bool] | None" = None,
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
        # The manager's everos identity lookup, so a node whose sub-agent
        # declares one leaves a Memory record on the same terms `spawn` and a
        # direct chat do. Injected for the same reason as `state_for`: this
        # tool is built from the same config as the manager but does not own one.
        self._everos_for = everos_for
        self._adopt = adopt
        self._charge = charge
        # A direct publisher (tests) and/or a late-bound conversation-keyed sink.
        self._publisher_override = progress_publisher
        self._sink: ProgressSink | None = None
        self._default_origin = _DagOrigin(channel="cli", chat_id="direct", conversation="cli:direct")
        self._origin: ContextVar[_DagOrigin | None] = ContextVar("dag_origin", default=None)
        self._tool_call_id: ContextVar[str | None] = ContextVar("dag_tool_call_id", default=None)
        self._ask = ask
        # Whether the control tools have a call path this turn. Injected by
        # the loop (the controller's predicate); unwired hosts advertise, which
        # is the old contract a test or an offline entry point expects.
        self._control_reachable = control_reachable
        self._cancels: dict[str, asyncio.Event] = {}
        # Strong references to in-flight background runs. Without them the event
        # loop only weakly references a bare create_task, and a run can be
        # garbage-collected mid-graph.
        self._runs: dict[str, asyncio.Task] = {}
        # The one agent table, normally the sub-agent manager's. This tool used to
        # build a second one from the same config, which meant a hot-apply refreshed
        # two maps through two setters that each skipped a bad entry independently:
        # "the manager has hermes, the DAG tool does not" was reachable. A caller
        # with no manager (tests, an offline CLI path) may hand configs instead and
        # get a private table -- which is not the same thing as a second copy of a
        # shared one, since nothing else reads it.
        from raven.agent.subagent.registry import AgentRegistry as _AgentRegistry

        if registry is not None:
            self._registry = registry
        else:
            self._registry = _AgentRegistry()
            self._registry.apply(agents or [])

    @property
    def registry(self) -> "AgentRegistry":
        """The agent table this tool dispatches against."""
        return self._registry

    def set_agents(self, configs: list) -> None:
        """(Re)apply config to this tool's own table. Hot-appliable (P4).

        A no-op path for a tool sharing the manager's registry -- the manager's
        ``apply_agents`` already refreshed it, and re-applying here would rebuild
        every external backend a second time. Kept for callers that gave this tool
        its own table.
        """
        self._registry.apply(configs)

    def _capability_map(self) -> dict[str, AgentCapabilities]:
        """The table's rows as the pre-check reads them.

        Derived per call rather than cached beside the backends: the point of
        holding the registry is that there is one place a capability can come
        from, and a cache here would be a second one that a hot-apply could leave
        stale.
        """
        return {
            row.name: AgentCapabilities(
                stateful=row.caps.stateful,
                reads_local_files=row.caps.reads_local_files,
                injectable_skills=row.injectable.skills,
                injectable_mcps=row.injectable.mcps,
            )
            for row in self._registry.enabled()
        }

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
        (raven/agent/subagent/history.py). Falls back to a slug-less manager --
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
        :func:`raven.agent.subagent.prompt_paths.check_confined`.

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
        graph. Serves in-flight runs too -- see :func:`dag_reader.read_run`.
        """
        return await _read_run(self._backend, self._run_root(session_key), run_id)

    async def session_run_ids(self, session_key: str | None = None) -> set[str]:
        """Every run id this conversation's index records.

        The live run set is loop-wide and the index is per session; the
        intersection of the two is what one conversation's model may see, which
        is how the control tools scope their listing and their cancel.
        """
        entries = await read_index(self._backend, self._run_root(session_key))
        return {str(entry["run_id"]) for entry in entries if entry.get("run_id")}

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
        if cancelled := summary.get("cancelled", 0):
            parts.append(f"{cancelled} cancelled")
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
        names = self._registry.roster_text() or "(none configured)"
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
                "task_summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "A short title for the whole graph, written before the nodes -- the "
                        "length of a chat title, under ten words, not a sentence and not a "
                        "summary. Name the goal, not the nodes."
                    ),
                },
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
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "Default false. Set true to have the user approve the graph before any node "
                        "runs -- for work with effects outside this machine (publishing, sending, "
                        "spending). They see every step, so approving is approving all of them."
                    ),
                },
            },
            "required": ["task_summary", "nodes"],
        }

    def _node_schema(self) -> dict[str, Any]:
        """Node schema with ``subagent`` constrained to the agent table.

        A misspelled name is only caught in ``run_dag``, and it rejects the
        whole graph, so one typo costs the entire call; the enum moves that to
        the schema.

        Deep-copied rather than annotated in place: ``_NODE_SCHEMA`` is a module
        constant shared by every instance, and the table is hot-appliable. An
        empty table omits the enum rather than emitting ``enum: []`` -- that
        matches nothing while ``agent`` stays required, which some providers
        reject as an unsatisfiable tool schema. Empty is reachable at runtime:
        a table whose every row failed to build.
        """
        schema = deepcopy(_NODE_SCHEMA)
        if names := self._registry.names():
            schema["properties"]["subagent"]["enum"] = names
        return schema

    def _resolve_node(self, node: DagNodeSpec) -> Any:
        """The backend one node dispatches to, or ``None`` if its agent is unknown.

        Handed to ``run_dag`` instead of a name->backend map, because narrowing is
        per node and not per agent: two nodes may name one agent with different
        skill lists, and a map keyed by name cannot hold both. It is also what
        removed the synthetic ``pb-<node>`` names -- a playbook step used to reach
        its own pre-built backend under an invented agent name, which is what made
        every playbook node unattributable in a trace.
        """
        build = _NodeBuild(skills_allow=node.skills) if node.skills is not None else None
        backend = self._registry.backend(node.subagent, build=build)
        # Where this conversation keeps its records, handed over for the same
        # reason ``SubagentManager._resolve_backend`` hands it over, and because
        # this lane can be the one that goes first: an acp backend builds its
        # resident unprompted-turn recorder on the first prompt it sends and
        # keeps the resolver bound by then, so a graph dispatching before any
        # spawn or direct chat would leave that connection unable to record.
        binder = getattr(backend, "bind_session_dir", None)
        if callable(binder):
            binder(self._session_dir_for)
        return backend

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

    def _mint_missing_instances(
        self, spec: SubAgentDagSpec, capabilities: dict[str, AgentCapabilities]
    ) -> tuple[SubAgentDagSpec, frozenset[str]]:
        """Give every stateful node that named no instance a fresh handle.

        ``capabilities`` must be the map the caller is actually dispatching
        against -- an agent absent from it falls back to
        ``AgentCapabilities()``'s permissive default and is minted for, which is
        the right way round: a test double or a row whose caps could not be read
        should get a handle it may not need rather than be denied one it does.

        Returns the rewritten spec and the ids it minted for. The ids travel
        separately because the handle itself carries no mark: once it is in the
        `instance` field, a minted one and a chosen one are the same string, and
        the manifest is the only place that difference is still worth having.
        """
        minted: set[str] = set()
        nodes: list[DagNodeSpec] = []
        for node in spec.nodes:
            caps = capabilities.get(node.subagent, AgentCapabilities())
            if node.instance or not caps.stateful:
                nodes.append(node)
                continue
            minted.add(node.id)
            nodes.append(node.model_copy(update={"instance": mint_handle(node.id)}))
        return spec.model_copy(update={"nodes": nodes}), frozenset(minted)

    async def execute(
        self, nodes: list[dict], task_summary: str = "", background: bool = True, confirm: bool = False, **kwargs: Any
    ) -> str:
        # Backstop, not the primary control: no in-process sub-agent backend
        # registers this tool today. It fires only if one ever does, so the
        # failure is a refusal rather than a silent recursive fan-out.
        if IN_SUBAGENT_RUN.get():
            return (
                "Error: run_subagent_dag is not available inside a sub-agent run — "
                "only the main agent orchestrates DAGs. Complete the assigned task directly."
            )
        return await self._execute(nodes, background, confirm=confirm, task_summary=task_summary)

    async def _execute(
        self,
        nodes: list[dict],
        background: bool,
        confirm: bool = False,
        task_summary: str = "",
    ) -> str:
        # Refused whole rather than per node, and ahead of validation, for the
        # same reason validation runs early: a refused graph must cost zero
        # sub-agent dispatches.
        if self._is_paused is not None and self._is_paused():
            return (
                "Error: delegation is paused. The user paused sub-agent spawning; "
                "do the work in this turn instead, or ask them to resume."
            )
        capabilities = self._capability_map()
        # Validation is a distinct phase, ahead of the run, in both modes: a
        # graph that fails any check costs zero sub-agent dispatches and is
        # rejected in the caller's own turn, so a rejection is always cheap
        # enough for the model to just fix and re-submit. Backgrounding must not
        # turn a malformed graph into an announcement that arrives a turn later.
        try:
            spec = parse_dag_spec({"task_summary": task_summary, "nodes": nodes, "confirm": confirm})
            validate_and_order(spec, self._reference_roots(), await self._session_nodes())
            notices = validate_capabilities(spec, capabilities)
            # ``run_dag`` checks the table too, but it does so inside the run --
            # which a backgrounded call has already returned from. Checked here
            # as well so a misspelled name is still a refusal the model can fix
            # in the same turn, not an announcement a turn later.
            for node in spec.nodes:
                if self._registry.get(node.subagent) is None:
                    raise DagValidationError(f"node '{node.id}' names unknown sub-agent '{node.subagent}'")
                if not self._registry.get(node.subagent).enabled:  # type: ignore[union-attr]
                    raise DagValidationError(
                        f"node '{node.id}' names agent '{node.subagent}', which is turned off on this "
                        f"machine -- enable it in the agents settings, or point the node at another agent"
                    )
            # Last of the pre-dispatch checks because it is the only one that
            # leaves this process: an agent that runs work on the owner's
            # machines is asked whether it has one, and a graph whose work has
            # nowhere to go is refused while the owner is still here to be asked.
            # Silent when nothing could be established -- see dag_machines.
            if verdict := await machines_verdict_async([n.subagent for n in spec.nodes]):
                if verdict.usable == 0:
                    raise DagValidationError(machines_refusal(verdict))
                if problem := unnamed_machine(spec.nodes, verdict):
                    raise DagValidationError(problem)
                # Settled once and told to every node: the one writing the code
                # needs what the machine has, and the one reporting afterwards
                # needs to say where the numbers came from.
                spec = spec.model_copy(update={"nodes": with_machine_facts(spec.nodes, verdict)})
        except DagValidationError as exc:
            return self._validation_error(exc)

        origin = self._origin.get() or self._default_origin
        session_dir = self._session_dir_for(self._turn_conversation())
        dirs = _RunDirs(
            workdir=str(workdir.current() or self._workspace),
            run_root=str(dag_root(session_dir)),
            subagents_root=str(session_history_root(session_dir)),
        )
        # Ahead of the charge: a graph the user turns down must not spend budget
        # either. Behind validation, so a graph that could never run does not get
        # a confirmation prompt.
        if spec.confirm and not await self._confirmed(spec, origin):
            return (
                "The user did not approve this graph, so nothing was run. Do not re-submit it; "
                "ask them what to change, or do the work another way."
            )
        # Charged after validation so a rejected graph costs no budget, and
        # before either mode starts so the refusal is the caller's own result.
        if self._charge is not None and (refusal := self._charge(origin.conversation)) is not None:
            return refusal
        call_id = self._tool_call_id.get()
        run_id = make_run_id()
        # After validation so a rejected graph mints nothing, and before either
        # mode starts so the foreground and background paths share one site.
        spec, auto_instances = self._mint_missing_instances(spec, capabilities)
        cancel = asyncio.Event()
        self._cancels[run_id] = cancel

        if not background:
            result = await self._run(spec, run_id, cancel, origin, dirs, call_id, auto_instances)
            return _with_notices(result, notices)

        task = asyncio.create_task(self._run_and_announce(spec, run_id, cancel, origin, dirs, call_id, auto_instances))
        self._runs[run_id] = task
        task.add_done_callback(lambda _t: self._runs.pop(run_id, None))
        if self._adopt is not None:
            self._adopt(run_id, task, origin.conversation)
        controls = ""
        if self._control_reachable is None:
            controls = f'Check its progress with dag_status("{run_id}") and stop it with cancel_dag("{run_id}"). '
        else:
            # Fail closed: the predicate runs after the background task is
            # already created, so a failure here must mute the hint rather
            # than turn an accepted submission into an error result.
            try:
                reachable = self._control_reachable()
            except Exception:  # noqa: BLE001
                reachable = False
            if reachable:
                controls = f'Check its progress with dag_status("{run_id}") and stop it with cancel_dag("{run_id}"). '
        return _with_notices(
            ToolResult(
                model_text=(
                    f"DAG run {run_id} started in the background ({len(spec.nodes)} nodes). "
                    + controls
                    + "I'll report the result when it finishes -- keep working, and do not submit this graph again."
                ),
                display_text=f"DAG {run_id}: {len(spec.nodes)} nodes started",
            ),
            notices,
        )

    async def _confirmed(self, spec: SubAgentDagSpec, origin: _DagOrigin) -> bool:
        """Ask the user to approve this graph. True when they did.

        The gate is graph-level and there is exactly one of it, which puts a
        requirement on what the question shows: approving a graph means approving
        every step in it. The per-node lines are what that buys so far -- an id
        and an agent name per step, rather than a bare "run 6 nodes?".

        Not yet what the design asks for. It calls for marking the steps whose
        effects reach outside this machine, so a yes is informed; that is still
        missing, and the honest reason is that the criterion it proposes -- the
        node's agent holding a write-capable tool or mcp -- cannot discriminate
        today. Every built-in agent carries write_file / edit_file / exec, so the
        mark would land on every node of a typical graph and inform nobody. It
        needs a narrower notion of "reaches outside" (publishing, sending,
        spending) than "can write", and that notion does not exist yet.

        With no ask channel wired the graph runs. Not every surface has a way to
        put a question to a human (a cron trigger, an IM channel with no
        interactive reply), and letting the absence of one disable the feature
        outright would be a worse failure than proceeding -- the same trade-off a
        playbook's own top-level confirm already makes. It is recorded at info
        level so the decision is visible in a log rather than only in this comment.
        """
        if self._ask is None:
            logger.info(
                "DAG run asked for confirmation but no ask channel is wired; dispatching {} node(s) unconfirmed",
                len(spec.nodes),
            )
            return True
        lines = [f"- {node.id}: {node.subagent}" for node in spec.nodes]
        question = "Run this {} step graph?\n{}".format(len(spec.nodes), "\n".join(lines))
        try:
            answer = await self._ask(origin.conversation, question)
        except Exception as exc:  # noqa: BLE001 - an unreachable asker is a "no", not a crash
            logger.warning("DAG confirmation could not be delivered: {}", exc)
            return False
        return bool(answer)

    async def _run_and_announce(
        self,
        spec: SubAgentDagSpec,
        run_id: str,
        cancel: asyncio.Event,
        origin: _DagOrigin,
        dirs: _RunDirs,
        call_id: str | None,
        auto_instances: frozenset[str],
    ) -> None:
        """Run a backgrounded graph, then send its summary back as a turn."""
        result = await self._run(spec, run_id, cancel, origin, dirs, call_id, auto_instances)
        if cancel.is_set():
            # A stop the user asked for. ``run_dag`` still returns normally,
            # with a running node recorded ``cancelled`` and a pending one
            # skipped, but announcing that would spend a turn narrating what
            # they just cancelled -- which is why a cancelled spawn stays
            # silent too.
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
        auto_instances: frozenset[str],
    ) -> str | ToolResult:
        """Execute one validated graph and render its outcome."""
        emit = self._emitter(origin.conversation, call_id)
        try:
            result = await run_dag(
                spec,
                resolve=self._resolve_node,
                backend=self._backend,
                workdir=dirs.workdir,
                run_root=dirs.run_root,
                subagents_root=dirs.subagents_root,
                progress_publisher=emit,
                semaphore=self._gate,
                session_key=origin.conversation,
                state_for=self._state_for,
                everos_for=self._everos_for,
                capabilities=self._capability_map(),
                run_id=run_id,
                cancel=cancel,
                auto_instances=auto_instances,
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
            f"{result.summary.get('cancelled', 0)} cancelled, "
            f"{result.summary.get('skipped', 0)} skipped (of {result.summary.get('total', 0)}).",
            f"Run dir: {result.dir}",
            "",
            "Node output files:",
        ]
        for entry in result.files:
            of = entry.get("output_file") or "(no output file)"
            handle = entry.get("instance")
            tag = f" (instance: {handle})" if handle else ""
            lines.append(f"- {entry['node']} [{entry['status']}]{tag}: {of}")
            if entry.get("error"):
                lines.append(f"    error: {entry['error']}")
        if result.terminal_outputs:
            lines.append("")
            lines.append("Terminal outputs:")
            for term in result.terminal_outputs:
                lines.append(f"### {term['node']}")
                lines.append(term["text"])
        return ToolResult(model_text="\n".join(lines), display_text=self._result_label(result))
