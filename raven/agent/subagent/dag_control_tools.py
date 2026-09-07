"""Model-facing controls for an in-flight ``run_subagent_dag`` run.

``cancel_dag`` stops a run the model already submitted; ``dag_status`` reports
one run's live per-node progress, or lists the runs in flight; ``resolve_dag_node``
answers a node suspended on an exception verdict. All three are kept out of the
provider's tool schema on purpose (``ToolRegistry.hide_from_schema``): the only
place that tells the model these exist is ``run_subagent_dag``'s own acceptance
text -- and, for ``resolve_dag_node``, the exception report -- so the per-turn
tool list carries nothing a conversation that never starts a DAG has any use
for. The model reaches them by naming them through ``tool_call``, which is
registered whether or not progressive disclosure is on for exactly this reason;
the registry resolves either way, as its dispatch never consults the schema.
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import Any

from raven.agent.subagent.dag_adjudication import ABANDON, CONTINUE, DECISIONS, REPLAN
from raven.agent.subagent.dag_live import cancel_run, live_run_ids, owning_tool, resolve_node
from raven.agent.subagent.dag_reader import DagReadError
from raven.agent.subagent.dag_resume import read_run_reconciled
from raven.agent.subagent.dag_tool import _with_notices
from raven.contracts.tool import Tool, ToolResult

_PROMPT_LINE_LIMIT = 10


def _registered_tool(loop: Any) -> Any:
    """The graph tool on the model's table, when this object has one."""
    tools = getattr(loop, "tools", None)
    getter = getattr(tools, "get", None)
    return getter("run_subagent_dag") if getter is not None else None


class _ControlTool(Tool):
    """Shared plumbing: the loop to ask, and this turn's session key."""

    def __init__(self, loop: Any) -> None:
        self._loop = loop
        self._session: ContextVar[str | None] = ContextVar("dag_control_session", default=None)

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        self._session.set(session_key)

    async def _read_live(self, run_id: str) -> dict[str, Any]:
        """One run's state, live-reconciled like the TUI's graph is."""
        tool = _registered_tool(self._loop)
        if tool is None:
            raise DagReadError("no run_subagent_dag tool is registered")
        return await read_run_reconciled(
            tool,
            run_id,
            self._session.get(),
            live_runs=lambda: live_run_ids(self._loop),
        )

    async def _my_run_ids(self) -> set[str]:
        """In-flight runs this conversation owns.

        The live set is loop-wide -- one gateway loop serves every channel --
        so it is intersected with this conversation's own index before the
        model sees it. A host without a session-scoped reader degrades to
        nothing rather than leaking another conversation's ids.
        """
        live = live_run_ids(self._loop)
        tool = _registered_tool(self._loop)
        if not live or tool is None:
            return set()
        getter = getattr(tool, "session_run_ids", None)
        if getter is None:
            return set()
        try:
            session_ids = await getter(self._session.get())
        except Exception:  # noqa: BLE001 - an unreadable index answers "nothing", never "everything"
            return set()
        return live & session_ids


class CancelDagTool(_ControlTool):
    """Stop one in-flight DAG run, whichever graph tool owns it."""

    @property
    def name(self) -> str:
        return "cancel_dag"

    @property
    def description(self) -> str:
        return (
            "Stop an in-flight DAG run by its run id. Nodes already running are cancelled "
            "immediately, pending nodes are skipped, and nothing further is announced for "
            "a cancelled run. Returns every node's state as of the cancellation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run id, as given by run_subagent_dag.",
                },
            },
            "required": ["run_id"],
        }

    async def execute(self, run_id: str) -> str:
        tool = _registered_tool(self._loop)
        if tool is None:
            # Fail closed: without the session-scoped reader there is no way
            # to prove the run belongs to this conversation, and an unproven
            # cancel could stop another chat's run -- silently, since a
            # cancelled run announces nothing.
            return (
                f"Cannot cancel DAG run {run_id}: run ownership cannot be resolved here (no "
                "run_subagent_dag tool is registered), so nothing was signalled."
            )
        # Ownership first: a run id must resolve under this conversation's run
        # history before the cancel is signalled, or one chat's model could
        # stop another chat's run -- which that chat is never told about, a
        # cancelled run being silent by design.
        try:
            await tool.read_run(run_id, self._session.get())
        except DagReadError:
            return (
                f"No DAG run {run_id} in this conversation: a run id must resolve under "
                'this conversation\'s run history to be cancelled. tool_call name "dag_status" '
                "with no run_id lists the runs this conversation has in flight."
            )
        if not cancel_run(self._loop, run_id):
            return (
                f"No in-flight DAG run {run_id} to cancel: it is not running, or the id is wrong. "
                'tool_call name "dag_status" with no run_id lists the runs currently in flight.'
            )
        head = (
            f"Cancellation requested for DAG run {run_id}: running nodes are cancelled "
            "immediately, pending nodes are skipped, and nothing further is announced for this run."
        )
        try:
            run = await self._read_live(run_id)
        except DagReadError:
            return head
        return head + "\n\n" + _render_run(run)


class ResolveDagNodeTool(_ControlTool):
    """Answer a suspended node: continue it with a message, abandon it, or replan with a new graph.
    On a bound foreground run, then wait for the next report or the final result."""

    @property
    def name(self) -> str:
        return "resolve_dag_node"

    @property
    def description(self) -> str:
        return (
            "Decide what happens to a DAG node that reported it could not accomplish its "
            "task: continue it with a message, abandon it and skip its dependents, or "
            "replan -- hand over a new node list, which stops this run and starts a new one "
            "from that list. On a run started with background=false this call also waits, "
            "and returns the next report or the run's final result."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        tool = _registered_tool(self._loop)
        getter = getattr(tool, "node_schema", None)
        schema = getter() if callable(getter) else {"type": "object"}
        return {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run id, as given in the exception report."},
                "node_id": {"type": "string", "description": "The node that reported the exception."},
                "decision": {
                    "type": "string",
                    "enum": [CONTINUE, ABANDON, REPLAN],
                    "description": (
                        "'continue' sends your message to the node and lets it try again. "
                        "'abandon' fails the node and skips its dependents; the rest of the "
                        "graph carries on. 'replan' replaces what is left of the graph with "
                        "the `nodes` you supply: this run stops, and a new run starts from "
                        'your list. To stop everything instead, use tool_call name "cancel_dag".'
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "What to tell the node, required when continuing. Supply what it said "
                        "was missing. Ask the user first if only they can provide it."
                    ),
                },
                "nodes": {
                    "type": "array",
                    "items": schema,
                    "description": (
                        "The replacement graph, required when replanning. Nodes this run "
                        "already completed are NOT re-declared -- name one in depends_on and "
                        "read it with {{ <id>.output }}. Every other node needs a new id."
                    ),
                },
            },
            "required": ["run_id", "node_id"],
        }

    def blocking_for(self, params: dict[str, Any]) -> bool:
        """Blocking only when the named run is a bound foreground run.

        The registry consults this to decide whether to put a ceiling on the
        call, and the turn stream reports it as the call's blocking flag; both
        must say "may go silent" for exactly the calls that will wait on the
        graph and for no others. A background replan also waits for the answered
        run's wind-down, but that wait is bounded and short by construction: the
        replan event cancels the round in flight, and only status marking and a
        manifest write remain. If a background replan is ever observed to hang,
        this predicate is the first thing to revisit.
        """
        tool = _registered_tool(self._loop)
        is_foreground = getattr(tool, "is_foreground", None)
        run_id = params.get("run_id")
        return bool(is_foreground is not None and isinstance(run_id, str) and is_foreground(run_id))

    async def execute(
        self,
        run_id: str,
        node_id: str,
        decision: str | None = None,
        message: str | None = None,
        nodes: list[dict] | None = None,
        action: str | None = None,
    ) -> "str | ToolResult":
        # `action` is the name the model reaches for -- three runs in a row
        # (2026-09-03/04) spent a call each on "missing required decision". The
        # field stays `decision` in the schema; the alias is accepted, not taught.
        if decision is None and action is not None:
            decision = action
        if decision is None:
            return f"Error: decision is required: '{CONTINUE}', '{ABANDON}' or '{REPLAN}'."
        if decision not in DECISIONS:
            return f"Error: decision must be '{CONTINUE}', '{ABANDON}' or '{REPLAN}', not {decision!r}."
        if decision in (CONTINUE, REPLAN) and not (message or "").strip():
            what = (
                "telling it what to do differently" if decision == CONTINUE else "saying why the plan is being changed"
            )
            return (
                f"Error: {decision} on node '{node_id}' needs a message {what}. "
                "Supply what the report said was missing."
            )
        if decision == REPLAN and not nodes:
            return (
                f"Error: replanning run {run_id} needs a `nodes` list -- the graph to run "
                "instead, and no decision was recorded, so the node is still waiting."
            )
        tool = _registered_tool(self._loop)
        if tool is None:
            # Fail closed: without the session-scoped reader there is no way to
            # prove this run belongs to this conversation, and an unproven
            # resolve could answer another chat's suspended node -- one that
            # chat never asked this conversation to decide.
            return (
                f"Cannot resolve node '{node_id}' of DAG run {run_id}: run ownership cannot be "
                "resolved here (no run_subagent_dag tool is registered), so nothing was signalled."
            )
        # Ownership first: a run id must resolve under this conversation's run
        # history before the node is answered, or one chat's model could
        # resolve another chat's suspended node -- deciding continue or
        # abandon on a wait that conversation is still watching.
        try:
            await tool.read_run(run_id, self._session.get())
        except DagReadError:
            return (
                f"No DAG run {run_id} in this conversation: a run id must resolve under this "
                "conversation's run history before its nodes can be resolved."
            )
        if decision == REPLAN:
            # The live read happens here, not in prepare_replan: reconciling a run
            # needs loop-wide liveness and only this tool holds the loop. _read_live
            # is the same call dag_status makes, so the two cannot disagree.
            try:
                live = await self._read_live(run_id)
            except DagReadError as exc:
                return (
                    f"Cannot replan run {run_id}: its state could not be read ({exc}), so no "
                    "decision was recorded and the node is still waiting."
                )
            # Two tool instances can be dispatching runs at once (see
            # AgentLoop.dag_tools): the registered one answers "not found" for
            # a run the playbook engine's private instance is running, so every
            # replan-specific call below needs whichever instance actually holds
            # this run's task, desk and outbox.
            owner = owning_tool(self._loop, run_id)
            plan = await owner.prepare_replan(
                run_id, node_id, nodes or [], (message or "").strip(), self._session.get(), live
            )
            if isinstance(plan, str):
                return plan
            # Read before the hand-off: `_retire` drops the old run's outbox as
            # soon as its task ends, so by `start_replan` the lane it was asked
            # for is no longer discoverable.
            was_bound = bool(getattr(owner, "is_foreground", lambda _r: False)(run_id))
            if not resolve_node(self._loop, run_id, node_id, decision, message, plan):
                return (
                    f"Node '{node_id}' of run {run_id} is no longer waiting for a decision, so the "
                    "replan was not applied and the old run is still running as submitted. "
                    'tool_call name "dag_status" shows where every node stands.'
                )
            # Emitted here, not in start_replan: resolve_node returning False above
            # (an answer for a node nothing is waiting on any more) must announce
            # nothing, and start_replan only runs after await_finalized, by which
            # point the old run may already be gone from the web UI's live tracking.
            # start_replan records the link for anything it returns from or
            # refuses as invalid, which leaves this stretch uncovered -- the desk
            # already answered, so an interruption here (a `/stop` cancelling this
            # call is realistic) must not leave the link missing.
            # CancelledError still has to propagate, so this records and re-raises
            # rather than swallowing it.
            try:
                await owner.emit_replanned(run_id, plan)
                await owner.await_finalized(run_id)
            except BaseException:
                await owner.record_interrupted_replan(
                    run_id, plan, "Interrupted between the node hand-off and starting the replan."
                )
                raise
            return _with_notices(await owner.start_replan(run_id, plan, bound=was_bound), list(plan.notices))
        if not resolve_node(self._loop, run_id, node_id, decision, message):
            return (
                f"Node '{node_id}' of run {run_id} is no longer waiting for a decision: it timed "
                'out, the run was cancelled, or the id is wrong. tool_call name "dag_status" '
                f'arguments {{"run_id": "{run_id}"}} shows where every node stands.'
            )
        # A bound foreground run: the turn that started it is this one, blocked
        # on the graph by choice, so the decision is followed by the next thing
        # the graph has to say -- another node's report, or the final result.
        # Registered before the runner wakes: resolve_node above set the node's
        # event, which only schedules the wake, and await_run parks its taker
        # before yielding.
        await_run = getattr(tool, "await_run", None)
        event = None
        if await_run is not None:
            try:
                event = await await_run(run_id)
            except asyncio.CancelledError:
                abort = getattr(tool, "abort_run", None)
                if abort is not None:
                    abort(run_id)
                raise
        if event is not None:
            return tool.render_event(run_id, event)
        if decision == CONTINUE:
            return f"Node '{node_id}' of run {run_id} will run again with your message."
        return (
            f"Node '{node_id}' of run {run_id} is abandoned; its dependents are skipped and the "
            'rest of the graph continues. Use tool_call name "cancel_dag" to stop the whole run.'
        )


class DagStatusTool(_ControlTool):
    """Per-node progress of one run, or the ids of the runs in flight."""

    @property
    def name(self) -> str:
        return "dag_status"

    @property
    def description(self) -> str:
        return (
            "Report one DAG run's live progress by its run id (per-node status and details), "
            "or list the runs currently in flight when called without one."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run id, as given by run_subagent_dag. Omit to list in-flight runs.",
                },
            },
        }

    async def execute(self, run_id: str | None = None) -> str:
        if not run_id:
            runs = sorted(await self._my_run_ids())
            if not runs:
                return "No DAG runs are currently in flight."
            return (
                "In-flight DAG runs: "
                + ", ".join(runs)
                + '. Call tool_call name "dag_status" arguments {"run_id": "<run_id>"} '
                "for one run's per-node status."
            )
        try:
            run = await self._read_live(run_id)
        except DagReadError as exc:
            return (
                f'No DAG run {run_id} found: {exc}. tool_call name "dag_status" with no run_id '
                "lists the runs currently in flight."
            )
        return _render_run(run)


def _field(label: str, value: str) -> list[str]:
    """One ``label: value`` line; extra lines of the value keep the indent."""
    first, *rest = value.split("\n")
    return [f"    {label}: {first}", *(f"    {line}" for line in rest)]


def _prompt_lines(entry: dict[str, Any], run: dict[str, Any]) -> str:
    """The template's head, with the full prompt's path when it is cut."""
    template = entry.get("prompt_template")
    if not template:
        return "(none)"
    lines = template.splitlines()
    if len(lines) <= _PROMPT_LINE_LIMIT:
        return template
    path = entry.get("prompt_file") or f"{run['nodes_root']}/{entry['node']}.prompt.md"
    head = "\n".join(lines[:_PROMPT_LINE_LIMIT])
    return f"{head}\n... (truncated, {len(lines) - _PROMPT_LINE_LIMIT} more lines; full prompt in {path})"


def _raw(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    return "(none)" if value is None else str(value)


def _render_run(run: dict[str, Any]) -> str:
    """One run's tallies and per-node details, in the finished-summary's shape."""
    summary = run.get("summary") or {}
    total = summary.get("total") or len(run.get("files", []))
    lines = [
        f"DAG run {run['run_id']}: "
        f"{summary.get('completed', 0)} completed, {summary.get('failed', 0)} failed, "
        f"{summary.get('cancelled', 0)} cancelled, {summary.get('skipped', 0)} skipped (of {total}).",
        f"task_summary: {run.get('task_summary') or '(none)'}",
    ]
    for entry in run.get("files", []):
        lines.append(f"- {entry['node']} [{entry.get('status')}]")
        lines += _field("node_summary", entry.get("node_summary") or "(none)")
        lines += _field("subagent", entry.get("subagent") or "(none)")
        inputs = entry.get("inputs")
        lines += _field("inputs", json.dumps(inputs, ensure_ascii=False) if inputs else "(none)")
        lines += _field("prompt_template", _prompt_lines(entry, run))
        lines += _field("instance", _raw(entry, "instance"))
        lines += _field("output_file", _raw(entry, "output_file"))
        lines += _field("memory_file", _raw(entry, "memory_file"))
        lines += _field("started_at", _raw(entry, "started_at"))
        lines += _field("ended_at", _raw(entry, "ended_at"))
        if entry.get("error"):
            lines += _field("error", str(entry["error"]))
    if run.get("dir"):
        lines.append(f"Run dir: {run['dir']}")
    return "\n".join(lines)
