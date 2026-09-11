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
``background=false`` blocks instead -- until the graph finishes, or until a
node reports it could not accomplish its task, whichever comes first. That
report is the call's result; the agent answers it with ``resolve_dag_node``,
which returns the next report or the final result. The run is *bound* to the
turn that started it: no adjudication deadline runs while that turn lives, and
when it ends the run is *released* and behaves as a backgrounded one from then
on (see ``raven.agent.subagent.dag_adjudication.Outbox``).

Live progress (``dag_run_started`` / ``dag_node_updated`` / ``dag_run_completed``)
rides a late-bound sink — NOT the spine. The turn's conversation is delivered
per-turn via ``set_context`` (the loop calls it, like spawn/message); the sink
(wired by the host to the page's emitter) fans the event to that
conversation's subscribers, where the service translates it to an AgentScope
CustomEvent the web UI's DAG graph already renders.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent import workdir
from raven.agent.subagent.backends.base import IN_SUBAGENT_RUN
from raven.agent.subagent.dag_adjudication import AdjudicationDesk, Final, Outbox, ReplanPlan, Report, Stopped
from raven.agent.subagent.dag_capabilities import AgentCapabilities, validate_capabilities
from raven.agent.subagent.dag_graph import DagNodeSpec, SubAgentDagSpec, parse_dag_spec, validate_and_order
from raven.agent.subagent.dag_mcp_scope import run_mcp_scope
from raven.agent.subagent.dag_reader import read_node as _read_node
from raven.agent.subagent.dag_reader import read_run as _read_run
from raven.agent.subagent.dag_reader import run_dir_of
from raven.agent.subagent.dag_runner import DagRunResult, ExceptionAnnouncer, ProgressPublisher, run_dag
from raven.agent.subagent.dag_store import (
    RUNNING,
    DagRunStore,
    SessionNodes,
    index_guard,
    make_run_id,
    read_registry,
    read_session_nodes,
)
from raven.agent.subagent.dag_verdict import Verdict, describe_failure, judge, tail
from raven.agent.subagent.history import dag_root, nodes_root, session_history_root
from raven.agent.subagent.instances import mint_handle
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent_memory import EverosIdentity
from raven.config.raven import SubagentDagConfig
from raven.config.schema import MCPServerConfig
from raven.contracts.tool import Tool, ToolResult
from raven.security.trust import wrap_untrusted

if TYPE_CHECKING:
    from raven.agent.subagent.registry import AgentRegistry

# Sink: (conversation_id, event_name, payload) -> awaitable. Late-bound by the
# host (the page mount wires it to the page's emitter).
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

    def as_dict(self) -> dict[str, str]:
        """The shape the announcers take: channel, chat, and session key."""
        return {"channel": self.channel, "chat_id": self.chat_id, "session_key": self.conversation}


@dataclass(frozen=True)
class _RunDirs:
    """The directories a run works in, read from the turn that submitted it.

    ``workdir`` is the nodes' cwd and what ``{{ ref:<path> }}`` resolves
    against; ``run_root`` is where this run's ``graph.json`` and
    ``manifest.json`` go; ``nodes_root`` is the flat, session-wide root where
    each node's own prompt/output/memory files go instead; ``subagents_root``
    is their common parent, the second directory a file reference may resolve
    into. All four come from turn-local state that a backgrounded run
    outlives, so they are resolved at call time rather than looked up once the
    graph is already running.
    """

    workdir: str
    run_root: str
    nodes_root: str
    subagents_root: str


@dataclass(frozen=True)
class _NodeBuild:
    """One node's narrowing, in the shape the registry's factory reads.

    Only ``skills`` reaches here. MCP selection stays on the node and is resolved
    per dispatch by the backend.
    """

    skills_allow: list[str] | None
    tools_allow: list[str] | None = None


@dataclass(frozen=True)
class _DispatchBackend:
    """One node backend with its cross-process MCP decision settled at pre-flight.

    The pre-flight grant is what the notices were written from. It is not what
    every attempt dispatches with: a node continued after its credential landed
    (a secret stored on the playbook page, a server authorized) has to dial with
    the definition as it reads now, so the grant is resolved again per attempt.
    A backend is wrapped with a grant only when it resolves one, so a resolver
    is always there to ask.
    """

    backend: Any
    mcp_grant: Any = None
    drop_mcps: bool = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)

    async def run(self, *args: Any, **kwargs: Any) -> str:
        if self.drop_mcps:
            kwargs.pop("mcps", None)
        elif self.mcp_grant is not None:
            resolver = getattr(self.backend, "resolve_mcp_grant_async", None) or self.backend.resolve_mcp_grant
            grant = resolver(kwargs.get("mcps"))
            kwargs["mcp_grant"] = await grant if inspect.isawaitable(grant) else grant
        return await self.backend.run(*args, **kwargs)


@dataclass
class Preflight:
    """What the pre-dispatch checks settled, for the two callers that dispatch from it.

    ``capabilities`` rides along because it is one snapshot of a hot-appliable table:
    ``_execute`` needs it again below this phase, and taking it twice would straddle
    this phase's awaits.

    ``spec`` is handed back rather than left to the caller because the phase is
    allowed to amend it, not because it currently does -- the one amendment it had,
    stamping each node with the machine its work was bound for, went away with the
    host-side machine checks.
    """

    spec: SubAgentDagSpec
    backends: dict[str, Any]
    notices: list[str]
    capabilities: dict[str, AgentCapabilities]


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

_FOREGROUND_REPORT_TAIL = (
    "This call returned before the graph finished. The graph is still running and this node "
    "is waiting for your decision. Decide with resolve_dag_node, which returns the next such "
    "report or the run's final result. The deadline above is paused while this turn runs; if "
    "you end the turn without deciding, the report is re-sent to you as a message and the "
    "deadline starts."
)

_NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "Node id (^[A-Za-z0-9_-]+$), unique across this whole conversation -- not just this "
                "graph, and not just this tool: a graph node and a spawn's node_id share one namespace. "
                "It is how a later task of either kind names this node's output. Reusing an id is "
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
                "graph listed in this node's depends_on, or any task this conversation already "
                "completed and left an output. One that failed, was skipped, was cancelled or is "
                "still running keeps its id but is refused, saying which. To read a node's files "
                "directly, "
                "{{ ref:@nodes/<node>.out.md }} or {{ ref:@nodes/<node>.prompt.md }}. The _path forms need a "
                "sub-agent the roster tags [local-files]; for a [no-local-files] one use the contents "
                "forms instead, and every _path form must name a file that already exists. "
                "Every key in inputs must be referenced by a placeholder. A long prompt belongs in a "
                "file referenced with {{ ref:<path> }}, so the graph JSON stays small enough to write "
                "in one reply."
            ),
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ids this node depends on. A node of this graph runs first; a task this conversation "
                "already completed with an output is already done, so listing it only records the "
                "dependency. Either way you may read it with {{ <node>.output }}."
            ),
        },
        "inputs": {
            "type": "object",
            "description": (
                'Per-key literal string, {"file": <path>}, or {"node": <id>} to take another node\'s '
                "output -- a dependency of this node, or any task this conversation already completed "
                "with an output. Exactly one of those three and nothing else in the object: no second "
                "key beside file or node, no empty path or id, and a number, boolean or list is "
                "refused. {{ inputs.<k> }} injects the text, {{ inputs.<k>.path }} the file path."
            ),
        },
        "instance": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^\S(?:.*\S)?$",
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
    # Manual stop (request_cancel / ``subagent.interrupt`` on the run id /
    # ``subagent.cancel_session``) replaces the timer
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
        announce_exception: ExceptionAnnouncer | None = None,
        adopt: TaskAdopter | None = None,
        state_for: "Callable[[str, str | None, str], Any] | None" = None,
        everos_for: "Callable[[str], EverosIdentity | None] | None" = None,
        mode_for: "Callable[[str, str | None, str], str | None] | None" = None,
        charge: QuotaCharger | None = None,
        ask: "Ask | None" = None,
        control_reachable: "Callable[[], bool] | None" = None,
        control_advert: "Callable[[str], str | None] | None" = None,
        provider_for: "Callable[[], Any] | None" = None,
        binding_for: "Callable[[], tuple[Any, str | None]] | None" = None,
        verdict_config: "SubagentDagConfig | None" = None,
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
        self._announce_exception = announce_exception
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
        # The manager's mode resolution, so a node that names an `instance` runs at
        # the effort level a user set on that instance, on the same terms `spawn`
        # and a direct chat do. Injected for the same reason as `state_for`: this
        # tool is built from the same config as the manager but does not own one.
        self._mode_for = mode_for
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
        self._control_advert = control_advert
        # The judge's model call. Injected rather than imported: this tool is built
        # from the same config as the loop but holds no provider of its own, and an
        # unwired host (tests, offline entry points) simply skips the judgement.
        self._provider_for = provider_for
        # The running turn's (provider, model), read per dispatch for the reason
        # `provider_for` is: the loop's binding is a property over the turn, and a
        # graph dispatched under a switched model has to carry it to its nodes.
        self._binding_for = binding_for
        self._verdict_config = verdict_config if verdict_config is not None else SubagentDagConfig()
        self._cancels: dict[str, asyncio.Event] = {}
        self._desks: dict[str, AdjudicationDesk] = {}
        self._outboxes: dict[str, Outbox] = {}
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

        Guarded on ``_history_root()`` -- the same root ``run_dag`` now locks
        to claim ids -- so the pre-check and the claim can never disagree
        about which registry they mean.
        """
        if (history := self._history_root()) is None:
            return SessionNodes()
        # Guarded like every other read that decides something, so this cannot
        # see a half-written registry. It is still only a pre-check -- ``run_dag``
        # repeats it inside the guard that also claims the ids.
        async with index_guard(history):
            return await read_session_nodes(self._backend, history)

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

    def resolve_node(
        self, run_id: str, node_id: str, decision: str, message: str | None, plan: "ReplanPlan | None" = None
    ) -> bool:
        """Answer one suspended node. False when nothing was waiting on it."""
        desk = self._desks.get(run_id)
        if desk is None or not desk.resolve(node_id, decision, message, plan):
            return False
        # The outbox owes a re-send for every report it handed over and nobody
        # decided; this is the decision, so that debt is settled.
        if (outbox := self._outboxes.get(run_id)) is not None:
            outbox.answered(node_id)
        return True

    def is_awaiting_decision(self, run_id: str, node_id: str) -> bool:
        """Whether ``node_id`` of ``run_id`` is suspended waiting for an answer.

        Exactly the condition :meth:`resolve_node` fails on -- both read this
        run's desk, and ``AdjudicationDesk.resolve`` refuses for the same reason
        ``is_open`` answers False -- so asking first refuses nothing the hand-off
        would have accepted.

        It is asked first because a replan's costs are paid before that hand-off,
        inside ``prepare_replan``: the confirm question, the dispatch quota and
        the minted instances, none of which is refunded when the hand-off then
        finds nobody waiting. See the call site in
        :mod:`raven.agent.subagent.dag_control_tools`.
        """
        desk = self._desks.get(run_id)
        return desk is not None and desk.is_open(node_id)

    def is_foreground(self, run_id: str) -> bool:
        """Whether ``run_id`` is a foreground run still bound to the turn that started it."""
        outbox = self._outboxes.get(run_id)
        return outbox is not None and outbox.bound

    async def await_run(self, run_id: str) -> "Report | Final | Stopped | None":
        """The next event of a bound foreground run, or None when there is no such run to wait on."""
        outbox = self._outboxes.get(run_id)
        if outbox is None or not outbox.bound:
            return None
        return await outbox.take()

    def abort_run(self, run_id: str) -> None:
        """Hard-cancel a run whose awaiting tool call was cancelled.

        The outbox is stopped first so a release that races this (the turn's own
        finally) finds nothing to re-send for a run that is being killed.
        """
        outbox = self._outboxes.get(run_id)
        if outbox is None:
            # Only a foreground run is one a caller can be blocked on; a background
            # graph must survive its caller's cancellation.
            return
        outbox.stop()
        task = self._runs.get(run_id)
        if task is not None and not task.done():
            task.cancel()

    async def release_turn(self, conversation: str, *, flush: bool) -> None:
        """The turn on ``conversation`` has ended: release every foreground run it still binds.

        ``flush`` is False when the turn was cancelled: the user just stopped the
        agent, and re-raising the run's open questions at them is noise. A run
        released this way still announces what happens to it later.
        """
        for outbox in list(self._outboxes.values()):
            if outbox.bound and outbox.conversation == conversation:
                try:
                    await outbox.release(flush=flush)
                except Exception:  # noqa: BLE001 - one run must not strand the conversation's others
                    logger.opt(exception=True).warning("DAG run outbox could not be released")

    def render_event(self, run_id: str, event: "Report | Final | Stopped") -> "str | ToolResult":
        """One outbox event as a tool result, addressed to the agent that is waiting."""
        if isinstance(event, Report):
            # Fenced like an announced report is (SubagentManager.announce_dag_exception):
            # the report quotes the node's output and transcript.
            return ToolResult(
                # No advertisement appended here: a Report is built only past
                # `Outbox.put_report`'s awaiting-decision gate, and a suspending
                # node implies `deliverable` and `remaining > 0`, so the report
                # this wraps always carries resolve_dag_node's schema already.
                # Appending it again emitted the definition twice, once inside
                # the fence as node evidence and once trusted.
                model_text=wrap_untrusted(event.text, source="subagent") + "\n\n" + _FOREGROUND_REPORT_TAIL,
                display_text=f"DAG {run_id}: node {event.node_id} needs a decision",
            )
        if isinstance(event, Final):
            return event.result
        if isinstance(event, Stopped):
            return f"DAG run {run_id} was stopped before it finished."
        raise TypeError(f"not an outbox event: {event!r}")

    def _advert_for(self, *names: str) -> str:
        """The named hidden control tools' definitions, ready to append to a report.

        Empty string when no renderer is wired (a tool built without a loop, and
        every test that does the same), so a caller can concatenate without
        branching. Failures are swallowed for the reason the reachability
        predicate's are: this text is an aid, and losing it must not turn an
        accepted submission into an error result.
        """
        if self._control_advert is None:
            return ""
        rendered: list[str] = []
        for name in names:
            try:
                text = self._control_advert(name)
            except Exception:  # noqa: BLE001
                continue
            if text:
                rendered.append(f"{name}: {text}")
        return ("\n\n" + "\n".join(rendered)) if rendered else ""

    def _report_announcer(self, run_id: str, origin: _DagOrigin):
        async def _announce(node_id: str, text: str, *, awaiting_decision: bool, informational: bool = False) -> None:
            if self._announce_exception is None:
                logger.info("DAG run {} node {} reported after release with no announcer wired", run_id, node_id)
                return
            await self._announce_exception(
                run_id,
                node_id,
                text,
                origin.as_dict(),
                awaiting_decision=awaiting_decision,
                informational=informational,
            )

        return _announce

    def _final_announcer(self, run_id: str, origin: _DagOrigin):
        async def _announce(result: Any) -> None:
            if self._announce is None:
                logger.info("DAG run {} finished after release with no announcer wired", run_id)
                return
            await self._announce(run_id, str(getattr(result, "model_text", result)), origin.as_dict())

        return _announce

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

    def _nodes_root(self, session_key: str | None) -> str:
        """This session's flat node root, where node artifacts are written and read.

        Sibling of ``_run_root``, derived the same way, but node artifacts
        (prompt/output/transcript/memory) live here rather than under any one
        run's directory: a node id is unique for the whole conversation, not
        just the run that dispatched it.
        """
        return str(nodes_root(self._session_dir_for(session_key)))

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

    def _history_root(self, session_key: str | None = None) -> str | None:
        """This conversation's sub-agent history root, or None if it has none.

        ``<session_dir>/subagents/`` -- the second directory a node's file
        reference may resolve into, holding this conversation's DAG runs, its
        node registry, and ``spawn`` records. Narrower than agent home
        deliberately: see :func:`raven.agent.subagent.prompt_paths.check_confined`.

        ``None`` rather than a path when no resolver is injected and no
        ``sessions/`` exists, because building a ``SessionManager`` to find out
        would create the directory -- and validation must leave a rejected
        graph's session exactly as it found it. With no history there is also
        nothing for a reference to name.

        ``session_key`` defaults to the current turn, like ``_run_root``, so a
        caller resolving another conversation's registry (``session_run_ids``)
        does not have to reach past this method to build the path itself.
        """
        if self._session_dir is None and not (Path(self._workspace) / "sessions").is_dir():
            return None
        return str(session_history_root(self._session_dir_for(session_key)))

    async def read_run(self, run_id: str, session_key: str | None = None) -> dict:
        """One run's durable structure + per-node state, read back from disk.

        The live progress events are not replayed anywhere, so this is the only
        way a consumer that missed them (a reloaded browser tab) can rebuild the
        graph. Serves in-flight runs too -- see :func:`dag_reader.read_run`.
        """
        return await _read_run(self._backend, self._run_root(session_key), run_id, self._nodes_root(session_key))

    async def session_run_ids(self, session_key: str | None = None) -> set[str]:
        """Every run id this conversation's node registry records.

        The live run set is loop-wide and the registry is per session; the
        intersection of the two is what one conversation's model may see, which
        is how the control tools scope their listing and their cancel.
        """
        history = self._history_root(session_key)
        if history is None:
            return set()
        registry = await read_registry(self._backend, history)
        return {str(run["run_id"]) for run in registry["runs"] if run.get("run_id")}

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
            self._nodes_root(session_key),
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
    def incomplete_hint(self) -> str:
        # The graph JSON is the one tool call whose size scales with prose the
        # model writes into it. The escape hatch already exists -- a prompt in a
        # file travels as a reference -- but the generic "smaller form" advice
        # sent the model toward cutting content instead (measured 2026-09-02: a
        # graph carrying the task rules verbatim in every node hit the reply cap
        # mid-write).
        return (
            "write each long node prompt to a file first and put {{ ref:<path> }} in "
            "prompt_template -- the graph then carries the reference, not the text. "
            "Splitting the work into two runs also works: a later graph may depend on "
            "this one's nodes by id. Do not shorten prompts by dropping task rules."
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
                        "Set false only when you cannot continue without the outputs: the call then "
                        "blocks until the graph finishes or a node reports it could not accomplish "
                        "its task, whichever comes first, and answering that report with "
                        "resolve_dag_node resumes the wait."
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

    def node_schema(self) -> dict[str, Any]:
        """This tool's node schema, for a caller that accepts a graph on its behalf.

        Public so ``resolve_dag_node`` advertises the same node shape rather than a
        copy of it: the ``subagent`` enum is built from the hot-appliable agent
        table, and a second copy would drift from ``run_subagent_dag``'s.
        """
        return self._node_schema()

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
        self,
        nodes: list[dict],
        task_summary: str = "",
        background: bool = True,
        confirm: bool = False,
        mcp_servers: "Mapping[str, MCPServerConfig] | Callable[[], Mapping[str, MCPServerConfig]] | None" = None,
        mcp_scope: str | None = None,
        mcp_credential_gaps: "Callable[[], frozenset[str]] | None" = None,
        **kwargs: Any,
    ) -> str:
        """Run one graph. ``mcp_servers`` is this run's own MCP definitions.

        Not a model-facing argument: it is absent from :meth:`parameters`, and
        ``run_mcp_scope`` accepts only already-validated ``MCPServerConfig``
        objects, so the only caller that can fill it is one holding a parsed
        config -- today the playbook engine, handing over the ``mcpServers``
        section its spec shipped. See :mod:`raven.agent.subagent.dag_mcp_scope`
        for why the definitions are scoped rather than merged into the host's.
        """
        # Backstop, not the primary control: no in-process sub-agent backend
        # registers this tool today. It fires only if one ever does, so the
        # failure is a refusal rather than a silent recursive fan-out.
        if IN_SUBAGENT_RUN.get():
            return (
                "Error: run_subagent_dag is not available inside a sub-agent run — "
                "only the main agent orchestrates DAGs. Complete the assigned task directly."
            )
        # Wraps the whole call, not just the grant loop: a backgrounded run keeps
        # the context this task held when ``create_task`` copied it, so an
        # in-process node re-resolving its grant mid-run still finds the run's own
        # definitions. Reset on the way out, so the turn that dispatched a
        # background run does not carry them into whatever it does next.
        with run_mcp_scope(mcp_servers, scope=mcp_scope, credential_gaps=mcp_credential_gaps):
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
        # Validation is a distinct phase, ahead of the run, in both modes: a
        # graph that fails any check costs zero sub-agent dispatches and is
        # rejected in the caller's own turn, so a rejection is always cheap
        # enough for the model to just fix and re-submit. Backgrounding must not
        # turn a malformed graph into an announcement that arrives a turn later.
        try:
            spec = parse_dag_spec({"task_summary": task_summary, "nodes": nodes, "confirm": confirm})
            validate_and_order(spec, self._reference_roots(), await self._session_nodes())
            pre = await self._preflight(spec)
            spec, dispatch_backends, notices, capabilities = pre.spec, pre.backends, pre.notices, pre.capabilities
        except DagValidationError as exc:
            return self._validation_error(exc)

        origin = self._origin.get() or self._default_origin
        session_dir = self._session_dir_for(self._turn_conversation())
        dirs = _RunDirs(
            workdir=str(workdir.current() or self._workspace),
            run_root=str(dag_root(session_dir)),
            nodes_root=str(nodes_root(session_dir)),
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
        return _with_notices(
            await self._dispatch(spec, run_id, dirs, dispatch_backends, auto_instances, origin, call_id, background),
            notices,
        )

    async def _dispatch(
        self,
        spec: SubAgentDagSpec,
        run_id: str,
        dirs: _RunDirs,
        backends: dict[str, Any],
        auto_instances: frozenset[str],
        origin: _DagOrigin,
        call_id: str | None,
        background: bool,
    ) -> str | ToolResult:
        """Start a validated, minted spec running and return its first result.

        The shared tail of `_execute` and `_submit_replan`: both have already
        validated, preflighted, and minted instances by the time they call this,
        and what is left -- creating the cancel event, the outbox, the task, and
        either parking on the first event or building the background message --
        is identical between a fresh submission and an applied replan.
        """
        cancel = asyncio.Event()
        self._cancels[run_id] = cancel

        outbox: Outbox | None = None
        if not background:
            outbox = Outbox(
                conversation=origin.conversation,
                announce_report=self._report_announcer(run_id, origin),
                announce_final=self._final_announcer(run_id, origin),
            )
            self._outboxes[run_id] = outbox

        task = asyncio.create_task(
            self._run_detached(spec, run_id, cancel, origin, dirs, call_id, auto_instances, backends, outbox)
        )
        self._runs[run_id] = task

        def _retire(_t: "asyncio.Task") -> None:
            # A task cancelled before its first tick never enters _run, whose
            # finally is what normally retires the run's cancel/desk entries --
            # and that window is real: _adopt indexes the task immediately, so
            # a same-tick /stop or the shutdown sweep cancels it un-started.
            # Left behind, active_run_ids() lists the dead run forever and its
            # node rows stay pinned running. The pops are idempotent with the
            # finally's own.
            self._runs.pop(run_id, None)
            self._cancels.pop(run_id, None)
            self._desks.pop(run_id, None)
            # Dropping the outbox here is what makes `resolve_dag_node`'s shape
            # load-bearing rather than stylistic: `release_turn` reaches a run
            # through this index, so a run that finishes before a taker exists has
            # nowhere left to deliver. The resolve tool resolves and awaits with no
            # yield between, which is why no such window opens today. Guarded by
            # test_the_resolve_tool_parks_its_taker_before_it_yields, which reads
            # that shape out of the source: a behavioural test only catches a yield
            # once it lasts long enough to lose the race, and the claim here is
            # about any yield at all.
            self._outboxes.pop(run_id, None)

        task.add_done_callback(_retire)
        if self._adopt is not None:
            self._adopt(run_id, task, origin.conversation)
        if outbox is not None:
            # Registered before the task's first tick: create_task only schedules
            # it, and take() parks its taker before yielding, so the run cannot
            # produce an event into an empty tray.
            try:
                event = await outbox.take()
            except asyncio.CancelledError:
                # The user stopped the agent while it was blocked on this graph.
                # A blocking call means "I am waiting on this", so the graph goes
                # with the turn, as it did when the call ran the graph inline.
                self.abort_run(run_id)
                raise
            if task.done():
                # ``put_final`` hands this event to our taker before ``_run_detached``
                # returns, so the task's own done-callback (``_retire`` above) is still
                # queued a tick behind this wakeup -- a caller inspecting ``_runs`` the
                # instant this call returns, as a finished run with no suspension does,
                # must not see a task that is done in every sense but bookkeeping.
                _retire(task)
            return self.render_event(run_id, event)
        if self._control_reachable is None:
            reachable = True
        else:
            # Fail closed: the predicate runs after the background task is
            # already created, so a failure here must mute the hint rather
            # than turn an accepted submission into an error result.
            try:
                reachable = self._control_reachable()
            except Exception:  # noqa: BLE001
                reachable = False
        # None of the three are in your tool schema, so the hint names the route
        # as well as the name -- a model that looks one up and does not find it
        # reads the whole advertisement as stale.
        controls = (
            f'Check its progress with tool_call name "dag_status" arguments {{"run_id": "{run_id}"}}, '
            f'and stop it with tool_call name "cancel_dag" arguments {{"run_id": "{run_id}"}}. '
            "If a node reports it could not accomplish its task, you will be told, and you answer the "
            'same way with "resolve_dag_node". These three are not in your tool list; tool_call is how '
            "you reach them." + self._advert_for("dag_status", "cancel_dag") + "\n\n"
            if reachable
            else ""
        )
        return ToolResult(
            model_text=(
                f"DAG run {run_id} started in the background ({len(spec.nodes)} nodes). "
                + controls
                + "I'll report the result when it finishes -- keep working, and do not submit this graph again."
            ),
            display_text=f"DAG {run_id}: {len(spec.nodes)} nodes started",
        )

    async def _submit_replan(self, plan: "ReplanPlan", *, bound: bool) -> "str | ToolResult":
        """`_execute`'s post-validation half, replayed for an already-validated plan.

        A second, cheap check regardless: the old run's id and its nodes' ids only
        became free again once `await_finalized` returned, and a fresh submission
        could have raced into one of them during that wait. Session nodes are read
        plain here, with no overlay -- the old run has finalized by now, so its own
        index entry already carries real statuses. Raises `DagValidationError`
        rather than returning a refusal string, so `start_replan` can tell "the
        link never started" apart from every other refusal shape and record it
        that way.
        """
        spec = parse_dag_spec({"task_summary": plan.task_summary, "nodes": list(plan.nodes), "confirm": plan.confirm})
        validate_and_order(spec, self._reference_roots(), await self._session_nodes())
        origin = self._origin.get() or self._default_origin
        session_dir = self._session_dir_for(self._turn_conversation())
        dirs = _RunDirs(
            workdir=str(workdir.current() or self._workspace),
            run_root=str(dag_root(session_dir)),
            nodes_root=str(nodes_root(session_dir)),
            subagents_root=str(session_history_root(session_dir)),
        )
        call_id = self._tool_call_id.get()
        return await self._dispatch(
            spec, plan.run_id, dirs, plan.backends, plan.auto_instances, origin, call_id, background=not bound
        )

    async def prepare_replan(
        self, run_id: str, from_node: str, nodes: list[dict], reason: str, session_key: str | None, live: dict
    ) -> "ReplanPlan | str":
        """Validate a replacement graph and charge for it. The plan, or a refusal.

        Runs the same phases a submitted graph runs, in the same order and for the
        same reasons (see ``_execute``): a refused graph must cost zero dispatches
        and be refused in the caller's own turn.

        The successor's run id is minted here, before the decision is handed over,
        because the wind-down names it in every node reason it writes.

        ``live`` is passed in rather than read here: reconciling a run needs
        loop-wide liveness (``dag_live.live_run_ids``) and this tool holds no
        reference to the agent loop -- the control tool does, and its own
        ``_read_live`` already asks exactly that question with the right source.
        """
        if self._is_paused is not None and self._is_paused():
            return (
                "Error: delegation is paused. The user paused sub-agent spawning; "
                "do the work in this turn instead, or ask them to resume."
            )
        try:
            raw_graph = json.loads(await self._read_graph_json(run_id, session_key))
            raw_graph.pop("replan", None)
            old = parse_dag_spec(raw_graph)
            spec = parse_dag_spec({"task_summary": old.task_summary, "nodes": nodes, "confirm": old.confirm})
            validate_and_order(spec, self._reference_roots(), await self._session_nodes_for_replan(run_id, live))
            pre = await self._preflight(spec)
        except DagValidationError as exc:
            return self._validation_error(exc)
        origin = self._origin.get() or self._default_origin
        if pre.spec.confirm and not await self._confirmed(pre.spec, origin):
            return (
                "The user did not approve this replan, so nothing was run and the old run is "
                "still running as submitted. Ask them what to change before replanning again."
            )
        if self._charge is not None and (refusal := self._charge(origin.conversation)) is not None:
            return refusal
        spec, auto = self._mint_missing_instances(pre.spec, pre.capabilities)
        return ReplanPlan(
            run_id=make_run_id(),
            from_node=from_node,
            reason=reason,
            nodes=tuple(spec.nodes),
            backends=pre.backends,
            auto_instances=auto,
            notices=tuple(pre.notices),
            task_summary=spec.task_summary,
            confirm=spec.confirm,
        )

    async def _read_graph_json(self, run_id: str, session_key: str | None) -> str:
        """One run's submitted graph, as text."""
        root = self._run_root(session_key)
        path = self._backend.join_path(run_dir_of(self._backend, root, run_id), "graph.json")
        return (await self._backend.read_file(path)).decode("utf-8", errors="replace")

    async def _session_nodes_for_replan(self, run_id: str, live: dict) -> SessionNodes:
        """This session's node index, with the replanned run's live state laid over it.

        The run has not finalized when this is asked, so its index entry carries no
        per-node ``status`` and every one of its nodes reads back ``running`` --
        neither reusable nor readable. The live read is the more current of the two
        by construction: it is what the finalize about to happen will write. Without
        the overlay, a reference to a node that has plainly completed is refused
        with "that run left it with no output".
        """
        known = await self._session_nodes()
        state = dict(known.state)
        has_output = dict(known.has_output)
        for entry in live.get("files") or []:
            state[entry["node"]] = entry.get("status") or state.get(entry["node"], RUNNING)
            # The live read is the output half's only source too: the registry
            # entry this overlays is the one that has not been written yet, so
            # leaving it behind marks the node completed and unreadable at once
            # -- the very refusal the overlay exists to prevent.
            has_output[entry["node"]] = bool(entry.get("output_file") or entry.get("output"))
        return SessionNodes(owner=dict(known.owner), state=state, has_output=has_output)

    async def await_finalized(self, run_id: str) -> None:
        """Wait for one run's task to end, so its per-node outcomes are on disk.

        The successor's validation reads them, and a run whose index entry has no
        statuses reads back as still running. Returns at once for a run id not in
        this instance's own ``_runs`` -- true both when it already ended and when
        it never started here, because a second graph tool instance is running it
        (see ``dag_live``'s module docstring). Callers must resolve the owning
        instance first, with ``dag_live.owning_tool``, or this returns instantly
        without having waited for anything.
        """
        task = self._runs.get(run_id)
        if task is not None and not task.done():
            await asyncio.wait([task])

    async def _record_link(self, run_id: str, entry: dict[str, Any]) -> None:
        """Note the replan link on `run_id`'s own graph.json."""
        session_key = self._turn_conversation()
        store = DagRunStore(
            self._backend,
            self._run_root(session_key),
            run_id,
            nodes_root=self._nodes_root(session_key),
            registry_root=self._history_root(session_key) or self._run_root(session_key),
        )
        await store.record_replan(entry)

    @staticmethod
    def _link_entry(plan: "ReplanPlan", *, started: bool, error: str | None = None) -> dict[str, Any]:
        """The replan link's shape, in the one place that decides it.

        Both writers -- the ordinary hand-off and the interrupted one -- record
        the same four facts about the decision and differ only in whether the
        successor started and why not. Written out twice, a field added for one
        reader silently reaches only half the records that reader will meet.
        """
        entry: dict[str, Any] = {
            "run_id": plan.run_id,
            "from_node": plan.from_node,
            "reason": plan.reason,
            "decided_at": int(time.time() * 1000),
            "started": started,
        }
        if error is not None:
            entry["error"] = error
        return entry

    async def start_replan(self, run_id: str, plan: "ReplanPlan", *, bound: bool = False) -> "str | ToolResult":
        """Start the successor run and note the link on the run it replaces."""
        # Built before the submission, so `decided_at` stamps when the decision
        # was taken rather than when its dispatch happened to return.
        entry = self._link_entry(plan, started=True)
        try:
            result = await self._submit_replan(plan, bound=bound)
        except DagValidationError as exc:
            entry["started"] = False
            entry["error"] = str(exc)
            result = self._validation_error(exc)
        await self._record_link(run_id, entry)
        return result

    async def record_interrupted_replan(self, run_id: str, plan: "ReplanPlan", reason: str) -> None:
        """Note an aborted hand-off on ``run_id``'s own graph.json.

        For the stretch between a successful ``resolve_node`` and ``start_replan``:
        ``emit_replanned`` and ``await_finalized`` run in it, and an interruption
        there (a ``/stop`` cancelling this call is realistic) would otherwise
        leave the link unrecorded -- even though the desk already answered and
        every node's wind-down reason already names ``plan.run_id`` as the
        successor. Without this, a reader of ``run_id``'s graph.json sees nodes
        pointing at a run this file never admits was even attempted.

        This narrows that window rather than closing it. ``start_replan`` records
        the link once ``_submit_replan`` has returned or raised
        ``DagValidationError``, so a cancellation from inside the submission --
        a bound dispatch parks on the successor's first event, the longest await
        in the hand-off -- escapes both guards and still leaves the link
        unwritten.
        """
        await self._record_link(run_id, self._link_entry(plan, started=False, error=reason))

    async def _emit_replanned(self, run_id: str, plan: "ReplanPlan") -> None:
        origin = self._origin.get() or self._default_origin
        emit = self._emitter(origin.conversation, self._tool_call_id.get())
        await emit(
            "dag_run_replanned",
            {
                "run_id": run_id,
                "replan_run_id": plan.run_id,
                "from_node": plan.from_node,
                "reason": plan.reason,
            },
        )

    async def emit_replanned(self, run_id: str, plan: "ReplanPlan") -> None:
        """Tell the wire a replan is under way, before the old run winds down.

        Called by the control tool right after the node hand-off succeeds, and
        before it waits out the old run's finish -- the web UI's live tracking
        for a run is keyed by ``run_id`` and is dropped the moment that run's own
        ``dag_run_completed`` arrives, so this has to land before that happens,
        not after ``start_replan`` returns.
        """
        await self._emit_replanned(run_id, plan)

    async def _preflight(self, spec: SubAgentDagSpec) -> Preflight:
        """Every pre-dispatch check that needs live registry data.

        Shared by ``_execute`` and the replan path so a replacement graph cannot
        reach dispatch through checks a submitted graph has to pass. Raises
        ``DagValidationError`` for a refusal; a capability or MCP gap comes back
        as a notice instead, on the terms those two already had.
        """
        capabilities = self._capability_map()
        notices = validate_capabilities(spec, capabilities)
        backends: dict[str, Any] = {}
        # ``run_dag`` checks the table too, but it does so inside the run --
        # which a backgrounded call has already returned from. Checked here
        # as well so a misspelled name is still a refusal the model can fix
        # in the same turn, not an announcement a turn later.
        for node in spec.nodes:
            row = self._registry.get(node.subagent)
            if row is None:
                raise DagValidationError(f"node '{node.id}' names unknown sub-agent '{node.subagent}'")
            if not row.enabled:
                raise DagValidationError(
                    f"node '{node.id}' names agent '{node.subagent}', which is turned off on this "
                    f"machine -- enable it in the agents settings, or point the node at another agent"
                )
            backend = self._resolve_node(node)
            resolver = getattr(backend, "resolve_mcp_grant", None)
            if row.injectable.mcps and resolver is not None:
                # The async form when there is one: see
                # SubagentManager._preflight_mcp for why the acp backend must
                # not capture the adapter's login shell on this thread.
                async_resolver = getattr(backend, "resolve_mcp_grant_async", None)
                grant = await async_resolver(node.mcps) if async_resolver is not None else resolver(node.mcps)
                # A notice, never a refusal, whichever backend it is. MCP is a
                # capability a node asked for on top of the work, so a server
                # that could not be resolved costs the node its tools and must
                # not cost the graph its run: refusing here throws away every
                # other node too, over an optional attachment. The caller is
                # told which node lost what, and the node's own reply carries
                # the same sentence.
                if note := grant.note_text():
                    notices.append(f"node '{node.id}': {note}")
                if getattr(backend, "kind", None) != "raven-loop":
                    backend = _DispatchBackend(backend, mcp_grant=grant)
            elif node.mcps is not None:
                backend = _DispatchBackend(backend, drop_mcps=True)
            backends[node.id] = backend
        return Preflight(spec=spec, backends=backends, notices=notices, capabilities=capabilities)

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

    async def _run_detached(
        self,
        spec: SubAgentDagSpec,
        run_id: str,
        cancel: asyncio.Event,
        origin: _DagOrigin,
        dirs: _RunDirs,
        call_id: str | None,
        auto_instances: frozenset[str],
        dispatch_backends: dict[str, Any],
        outbox: Outbox | None,
    ) -> None:
        """Run a graph as its own task, then hand the result on.

        A foreground run puts it in its outbox: the tool call awaiting the run
        takes it, or, if the turn has ended by then, the outbox announces it. A
        backgrounded run announces it directly, as before.
        """
        try:
            result = await self._run(
                spec, run_id, cancel, origin, dirs, call_id, auto_instances, dispatch_backends, outbox=outbox
            )
        except asyncio.CancelledError:
            if outbox is not None:
                outbox.stop()
            raise
        except Exception as exc:  # noqa: BLE001 - the awaiting call has no other way to learn the run died
            # `_run` already turns a `run_dag` failure into this text; a failure of
            # `_run` itself used to propagate to the inline caller. Detached, it would
            # die with the task and leave the outbox's taker waiting forever, so it
            # takes the same shape and travels the same route.
            logger.opt(exception=True).error("DAG run {} raised outside run_dag: {}", run_id, exc)
            result = f"Error running DAG {run_id}: {exc}"
        if getattr(result, "replanned_into", None):
            # Checked before the outbox: a released outbox stays in `_outboxes`
            # exactly like a bound one, so `put_final` would reach it here too
            # and announce the raw `DagRunResult` this branch exists to suppress
            # (`_run` skips rendering one when replanned -- see its docstring).
            # The resolve call already returned both halves of the decision to
            # the agent; announcing "3 completed, 1 failed, 2 skipped" here
            # would narrate what it just decided -- the same reason a cancelled
            # run stays silent one branch below.
            logger.info("DAG run {} was replanned into {}; not announcing a result", run_id, result.replanned_into)
            return
        if outbox is not None:
            await outbox.put_final(result, stopped=cancel.is_set())
            return
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
                origin.as_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - a failed announce must not also lose the log line
            logger.error("DAG run {} finished but its result could not be announced: {}", run_id, exc)

    @staticmethod
    async def _close_graph(emit: ProgressPublisher, run_id: str, node_count: int, detail: dict) -> None:
        """Tell a drawn graph the run is over when it ended with no manifest.

        ``dag_run_started`` has already drawn the nodes by the time a collapse
        or a stop lands, and the drawing only settles on a terminal event. When
        the call that reaches here already delivered the run's outcome as its
        tool result, a graph left mid-flight was visible right next to it;
        otherwise -- backgrounded from the start, or a foreground call that
        already returned a report in an earlier turn -- the graph reads as
        still running until a reload reconciles it against ``active_run_ids``.

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

    def _judge_node(self) -> "Callable[..., Awaitable[Verdict]] | None":
        """The verdict call, or None when this host cannot make one.

        None rather than a no-op: the runner reads it as "do not judge", which is
        the previous behaviour, and an unwired host (a test, an offline entry
        point) gets that behaviour without configuring anything.
        """
        cfg = self._verdict_config
        if self._provider_for is None or not cfg.verdict_enabled:
            return None

        async def _judge(
            *, node: Any, store: Any, output: str, error: str, crashed: bool, output_limited: bool = False
        ) -> Verdict:
            # Resolved here, not when this tool was built: the loop's provider is
            # a property over the running turn's binding, so a session that
            # switched model must reach the judge.
            provider = self._provider_for()
            if provider is None:
                return Verdict(accomplished=True)
            evidence, complete = await self._node_evidence(store, node.id, cfg.evidence_budget_chars)
            prompt = await self._node_prompt(store, node.id)
            if crashed:
                return await describe_failure(
                    provider,
                    prompt=prompt,
                    error=error,
                    evidence=evidence,
                    evidence_complete=complete,
                    model=cfg.verdict_model,
                    timeout_s=cfg.verdict_timeout_seconds,
                    output_limited=output_limited,
                )
            return await judge(
                provider,
                prompt=prompt,
                output=output,
                evidence=evidence,
                evidence_complete=complete,
                model=cfg.verdict_model,
                timeout_s=cfg.verdict_timeout_seconds,
                output_limited=output_limited,
            )

        return _judge

    async def _node_prompt(self, store: Any, node_id: str) -> str:
        try:
            return await store.read_text(store.prompt_path(node_id))
        except Exception:  # noqa: BLE001 - the judge can work from output alone
            return ""

    async def _node_evidence(self, store: Any, node_id: str, budget: int) -> tuple[str, bool]:
        """The transcript tail and whether there was a transcript at all.

        The cli lane publishes only a live console and writes no transcript file,
        so its nodes are judged on task and output alone -- and say so, rather
        than letting a thin judgement pass for a well-evidenced one.
        """
        try:
            text = await store.read_text(store.transcript_path(node_id))
        except Exception:  # noqa: BLE001 - no transcript is a fact about the lane, not an error
            return "", False
        if not text.strip():
            return "", False
        return tail(text, budget), True

    async def _run(
        self,
        spec: SubAgentDagSpec,
        run_id: str,
        cancel: asyncio.Event,
        origin: _DagOrigin,
        dirs: _RunDirs,
        call_id: str | None,
        auto_instances: frozenset[str],
        dispatch_backends: dict[str, Any],
        outbox: Outbox | None = None,
    ) -> str | ToolResult | DagRunResult:
        """Execute one validated graph and render its outcome.

        Returns the raw ``DagRunResult`` rather than rendering it when the run
        was replanned -- there is no outcome of its own left to narrate, and
        ``_run_detached`` needs ``replanned_into`` on the object it gets back.
        """
        emit = self._emitter(origin.conversation, call_id)
        desk = AdjudicationDesk()
        self._desks[run_id] = desk
        announce_exception = self._announce_exception
        released: asyncio.Event | None = None
        if outbox is not None:

            async def _to_outbox(
                _run_id: str,
                node_id: str,
                report: str,
                _origin: dict[str, str],
                *,
                awaiting_decision: bool,
                informational: bool = False,
            ) -> None:
                # Whether a notification is dropped depends on whether a blocking call is
                # still there to be handed the summary, which is the outbox's own state.
                await outbox.put_report(
                    node_id, report, awaiting_decision=awaiting_decision, informational=informational
                )

            announce_exception = _to_outbox
            released = outbox.released
        try:
            provider, model = self._binding_for() if self._binding_for is not None else (None, None)
            result = await run_dag(
                spec,
                resolve=lambda node: dispatch_backends.get(node.id),
                backend=self._backend,
                workdir=dirs.workdir,
                run_root=dirs.run_root,
                nodes_root=dirs.nodes_root,
                history_root=dirs.subagents_root,
                subagents_root=dirs.subagents_root,
                progress_publisher=emit,
                semaphore=self._gate,
                session_key=origin.conversation,
                state_for=self._state_for,
                everos_for=self._everos_for,
                mode_for=self._mode_for,
                capabilities=self._capability_map(),
                run_id=run_id,
                cancel=cancel,
                auto_instances=auto_instances,
                desk=desk,
                judge_node=self._judge_node(),
                announce_exception=announce_exception,
                origin=origin.as_dict(),
                max_continuations=self._verdict_config.max_continuations,
                adjudication_timeout_s=self._verdict_config.adjudication_timeout_seconds,
                control_reachable=self._control_reachable,
                control_advert=self._control_advert,
                released=released,
                provider=provider,
                model=model,
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
            # The other way a run is stopped. The model's ``cancel_dag`` tool
            # (via ``cancel_dag_run``) sets the event and
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
            self._desks.pop(run_id, None)

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

        if result.replanned_into:
            # Narrating "N completed, M failed" here would be for a run the agent
            # itself just decided to end; _run_detached reads this attribute to
            # skip announcing it, and the resolve call already returned both
            # halves of the decision to the agent.
            return result

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
