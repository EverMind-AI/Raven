"""Spawn tool for creating background subagents."""

import re
import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from raven.agent import workdir
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.dag_graph import check_node_refs
from raven.agent.subagent.dag_store import (
    claim_node,
    duplicate_node_id,
    index_guard,
    read_session_nodes,
    release_node_claim,
)
from raven.agent.subagent.delegate import current_delegate, dispatch_charter
from raven.agent.subagent.history import NODE_ID_PATTERN, nodes_root, session_history_root
from raven.agent.subagent.instances import mint_handle
from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_capabilities import check_path_placeholders
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_placeholders import parse_placeholders
from raven.agent.subagent.prompt_render import render_template
from raven.contracts.tool import Tool

if TYPE_CHECKING:
    from raven.agent.subagent import SubagentManager
    from raven.agent.subagent.backends import AgentMeta


@dataclass(frozen=True)
class _SpawnOrigin:
    """Per-turn origin for subagent announcements, isolated per asyncio task
    (the tool is shared; a turn runs in its own lane task). Frozen +
    copy-on-write so a child task that inherited the parent's value never
    writes back through the shared reference."""

    channel: str
    chat_id: str
    session_key: str


class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    # A subagent runs its own (up to 15-iteration) loop with no internal
    # wall-clock cap, so give it a generous backstop rather than the default.
    timeout_seconds = 900.0
    # Every tool that runs a sub-agent is a blocking interaction: the run has no
    # automatic deadline (only a manual stop), so a turn stream must not clock
    # it. Kept uniform across spawn / run_subagent_dag / deep_research rather
    # than derived from whether a given one happens to return before its
    # sub-agent does -- a consumer cannot see that distinction.
    blocking_interaction = True

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._default = _SpawnOrigin(channel="cli", chat_id="direct", session_key="cli:direct")
        self._origin: ContextVar[_SpawnOrigin] = ContextVar("spawn_origin")
        self._tool_call_id: ContextVar[str | None] = ContextVar("spawn_tool_call_id", default=None)
        # Written by execute and popped by take_metadata in the loop's own task,
        # so the handoff cannot rely on a ContextVar write propagating upward.
        # Keyed by session so concurrent turns never read each other's handle.
        self._pending: dict[str, dict[str, Any]] = {}

    def _cur(self) -> _SpawnOrigin:
        return self._origin.get(None) or self._default

    def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set the origin context for subagent announcements (turn-local)."""
        self._origin.set(replace(self._cur(), channel=channel, chat_id=chat_id, session_key=session_key))

    def set_tool_call_id(self, tool_call_id: str | None) -> None:
        """Turn-local: record which tool call this spawn's run belongs to.

        A consumer that draws the run under the tool row it came from cannot get
        there from the task id alone -- it is minted inside the manager, after
        the row exists -- so the loop hands the call id in and every
        ``subagent.status`` frame carries it back out.
        """
        self._tool_call_id.set(tool_call_id)

    def take_metadata(self) -> dict[str, Any] | None:
        return self._pending.pop(self._cur().session_key, None)

    @property
    def name(self) -> str:
        return "spawn"

    def _agents(self) -> list["AgentMeta"]:
        """The roster this spawn may dispatch to -- the whole agent table.

        Built-in agents are on it now. While they were not, the model was offered
        "omit `agent` for a Raven sub-agent" and had no way to learn that
        research-raven and code-raven existed or differed, so the only agents it
        could choose *between* were the external ones.
        """
        lister = getattr(self._manager, "list_agents", None)
        return lister() if callable(lister) else []

    @property
    def description(self) -> str:
        base = (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "Multiple spawns run concurrently."
        )
        agents = self._agents()
        if agents:
            from raven.agent.subagent.backends import format_agent_listing

            listing = format_agent_listing(agents)
            base += (
                " Pick the agent for the job with `subagent` -- there is no default, so choose "
                f"deliberately from: {listing}."
            )
            # Sits with the roster rather than after the DAG pointer below: it is a
            # rule about which name to pass here, so a model that has stopped
            # reading by the time it reaches the DAG advice has still read it.
            # Withheld when the table holds nothing but the generic row, which
            # would make it name a specialist the model cannot pick.
            if any(a.name != GENERIC_AGENT for a in agents):
                base += (
                    " Prefer delegation over doing it yourself: when a single specialist on this "
                    "roster covers the whole task, spawn that one instead of carrying the work out "
                    f"with your own tools. `{GENERIC_AGENT}` is not a specialist -- it carries no "
                    "capability bias, so reach for it only when no specialist covers the work."
                )
            # The one moment a wrong choice is visible: the model is reading this
            # tool while the work is really a graph.
            base += (
                " If you are about to issue several spawns for one task, that is a DAG: use "
                "`run_subagent_dag` instead, so the independent parts run concurrently and each "
                "step's output reaches the next without a turn of yours in between."
            )
            # The result carries this path; without a word here the agent has a
            # path it does not know the use of.
            base += (
                " The result names a `Record:` path -- this call's output file. Hand it to a "
                "follow-up task with `{{ ref:@nodes/<node_id>.out.md }}`, or name the task by "
                "its `node_id` directly, instead of restating the result from memory. Beside it "
                "sits `<node_id>.memory.json` -- what the sub-agent concluded for itself, rather "
                "than the answer it gave you -- written after the call, so absence is normal."
            )
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "task_summary": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "A short title for what you are dispatching, written before the task -- the "
                    "length of a chat title, under ten words, not a sentence and not a summary "
                    "of the task. They read it in listings and announcements and the sub-agent "
                    "never does, so write it for them -- no ids, no internal shorthand."
                ),
            },
            "node_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Your name for this task (^[A-Za-z0-9_-]+$), unique across this whole "
                    "conversation -- not just this call, and not just this tool: a "
                    "run_subagent_dag node id and a spawn node_id share one namespace. It is "
                    "how a later task of either kind references this one's output, so name it "
                    "for what the task produces rather than numbering it. A task that fails "
                    "still owns its id; reusing one is refused. There is no length limit."
                ),
            },
            "prompt_template": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The task for the subagent. Placeholders: {{ <node_id>.output }} / "
                    "{{ <node_id>.output_path }} inject a finished task's output / its path, by "
                    "the node_id it ran under; {{ ref:<path> }} / {{ ref_path:<path> }} inject a "
                    "file's contents / its path; {{ inputs.<k> }} / {{ inputs.<k>.path }} inject "
                    "an input. Name the task rather than restating its result. By path it is "
                    "{{ ref:@nodes/<node_id>.out.md }}; paths also resolve under the working "
                    "directory. A task that failed, was skipped or is still running is refused "
                    "with which of those it was. The _path forms need a sub-agent the roster tags "
                    "[local-files]; for a [no-local-files] one use the contents forms. "
                    "Nothing is added around an injected value -- no heading, no label, no source "
                    "path -- so write in the template itself what the material is and where it "
                    "came from. Every key in `inputs` must be referenced by a placeholder."
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    'Per-key literal string, {"file": <path>}, or {"node": <id>} to take a '
                    "finished task's output. Exactly one of those three and nothing else in the "
                    "object: no second key beside file or node, no empty path or id, and a "
                    "number, boolean or list is refused. {{ inputs.<k> }} injects the text, "
                    "{{ inputs.<k>.path }} the file path."
                ),
            },
        }
        agents = self._agents()
        names = sorted(a.name for a in agents)
        labels, worker_lines = self._workers()
        if labels:
            # This turn's playbook named the workers, so they are what the model
            # picks between: each carries a brief the task written here has to
            # match, and a bare agent name would carry none.
            props["subagent"] = {
                "type": "string",
                "enum": labels,
                "description": (
                    "Which worker runs this task. Required: pass one of the labels "
                    "below -- the label itself, not the agent it runs on. "
                    "This task's workers, each with what it is for:\n" + worker_lines
                ),
            }
        elif names:
            props["subagent"] = {
                "type": "string",
                "enum": names,
                "description": "Which agent runs this task. Required: pick one from the list.",
            }
        stateful_names = sorted(a.name for a in agents if a.stateful)
        props["instance"] = {
            "type": "string",
            "description": (
                "Optional: a short semantic handle (e.g. 'refactor-auth') naming a "
                "conversation with a resumable sub-agent. Reuse the same handle to "
                "continue that session. Omit it and one is assigned automatically and "
                "reported when the call finishes, so any run can be continued later. "
                f"Accepted for `subagent` in {stateful_names} -- against any other one a handle "
                "continues nothing and the call is rejected."
            ),
        }
        # `subagent` is required only once there is a roster to require it from. An
        # empty one is a table whose every row failed to build; demanding a value
        # the enum cannot offer would be an unsatisfiable schema, and the manager
        # still resolves an omitted name to the generic built-in row.
        return {
            "type": "object",
            "properties": props,
            "required": (
                ["task_summary", "node_id", "prompt_template", "subagent"]
                if (labels or names)
                else ["task_summary", "node_id", "prompt_template"]
            ),
        }

    def to_schema(self) -> dict[str, Any]:
        """Render this call's own shape, fresh, for every model call.

        Authored rather than inherited on purpose: a tool that defines its own
        ``to_schema`` has *declared* a dynamic shape, and ``ToolRegistry``
        serves those live instead of from the admission snapshot. Without this,
        the roster below would be frozen at admission and a turn's worker
        table -- a per-turn thing by construction -- would never reach the model.

        The table is fixed for the whole turn, so re-rendering yields the same
        array on every iteration of it; what moves between turns is what the
        model is meant to see move.
        """
        return super().to_schema()

    def _workers(self) -> tuple[list[str], str]:
        """This turn's labels and the lines describing them, or (``[]``, "").

        Empty outside a playbook turn, which is what keeps the roster below on
        the path it took before playbooks existed.
        """
        table = current_delegate()
        if not table:
            return [], ""
        labels = table.labels()
        lines = []
        for label in labels:
            worker = table.get(label)
            if worker is None:
                continue
            brief = worker.brief.strip()
            # The agent is named after the brief, not in parentheses after the
            # label. Measured on real turns: with `- label (Agent): brief` the
            # dispatching model read the parenthesised agent as the value to
            # pass and sent the roster name, which the enum then refused. Once
            # it had been refused twice one run abandoned delegation outright
            # and did the work itself -- the opposite of what a table that
            # names workers is for.
            tail = f" [runs on {worker.agent}]"
            lines.append(f"- {label}: {brief}{tail}" if brief else f"- {label}{tail}")
        return labels, "\n".join(lines)

    def _resolve_payload(self, subagent: str | None) -> "Mapping[str, Any] | None":
        """The charter this dispatch should carry to a Raven worker, if any.

        Separate from :meth:`_resolve_worker` because the two answer different
        parties: that one answers this process (which agent, what preamble),
        this one answers the worker's own process, and only a Raven worker has
        anything to read it with.
        """
        table = current_delegate()
        if not table or not subagent:
            return None
        worker = table.get(subagent)
        return worker.payload if worker is not None else None

    def _resolve_worker(self, subagent: str | None) -> tuple[str | None, str]:
        """Map a label onto its roster agent, and hand back its charter.

        A label resolves to no backend, so nothing downstream may ever see one:
        the dispatch, the instance registry and the DAG tool are all given the
        agent name. An unknown value passes through untouched -- the roster
        check further down is the one place that refuses a name, and answering
        the same mistake here would word it twice.
        """
        table = current_delegate()
        if not table or not subagent:
            return subagent, ""
        worker = table.get(subagent)
        if worker is None:
            return subagent, ""
        return worker.agent, worker.charter

    def _is_stateful(self, agent: str | None) -> bool:
        """Whether this target can continue a handle handed back to it.

        The single predicate behind both the refusal below and the minting in
        ``execute``, so the two can never disagree about what a handle is worth.
        Read from the same roster the schema is built from. An unknown name -- and
        an omitted one, which the manager resolves to the generic built-in row --
        is left to the manager rather than pre-judged here.
        """
        if agent is None:
            return True
        meta = next((a for a in self._agents() if a.name == agent), None)
        return meta is None or meta.stateful

    def _reject_useless_instance(self, agent: str | None, instance: str | None) -> str | None:
        """Why this handle cannot work, or ``None`` when it can.

        Same gate ``run_subagent_dag`` applies to a shared ``instance``, on the
        one-call surface: passing a handle to an agent that cannot resume it is
        not a no-op the caller can see. The backend silently ignores it and
        returns a reply written as if the earlier turns never happened, which
        reads as the sub-agent forgetting rather than as a rejected argument.
        Refused before the spawn so nothing runs under the false expectation.
        """
        if not instance or self._is_stateful(agent):
            return None
        names = sorted(a.name for a in self._agents() if a.stateful)
        alt = f" Sub-agents that can: {names}." if names else ""
        return (
            f"Error: sub-agent {agent!r} is stateless, so the handle {instance!r} continues nothing -- "
            f"each run starts a fresh session regardless.{alt} Call spawn again without `instance`, "
            f"putting whatever context the run needs into `prompt_template`."
        )

    async def _render(
        self, template: str, inputs: dict[str, Any], subagent: str | None, session_key: str, node_id: str
    ) -> str:
        """``template`` with its file and input references resolved, as dispatched.

        Gates before it renders: a ``_path`` form aimed at a sub-agent the roster
        tags [no-local-files] is refused here, the way the DAG surface has always
        refused it, rather than reaching that agent as a path it cannot open and
        coming back as a confident answer about a file it never read.

        The parse is its own step ahead of the gate so a grammar error is raised
        in its own words instead of being caught by the gate and re-labelled a
        capability refusal. An unknown ``subagent`` is not pre-judged -- the
        dispatcher raises on it -- so a name with no roster row reads as
        permissive here, matching how the DAG gate treats one.

        References resolve against the turn's working directory and this
        conversation's sub-agent history, the same two roots a DAG node reads, so
        an earlier spawn's own output file is nameable. Text read from
        under the history root was written by a sub-agent, and comes back fenced
        (:func:`~raven.agent.subagent.prompt_render.render_template`).

        Args:
            template (`str`):
                The caller's ``prompt_template``.
            inputs (`dict[str, Any]`):
                The caller's ``inputs``, keyed as ``inputs.<key>`` names them.
            subagent (`str | None`):
                The agent this task is aimed at, whose capabilities gate it.
            session_key (`str`):
                The conversation this spawn was made from, which decides whose
                sub-agent history the references may reach.

        Returns:
            `str`:
                The text to dispatch.

        Raises:
            `DagValidationError`:
                On a path form aimed at a [no-local-files] sub-agent, a
                malformed placeholder, an undefined input key, a reference to
                another task's output, or a file reference that is missing or
                escapes both roots.
        """
        meta = next((a for a in self._agents() if a.name == subagent), None)
        placeholders = parse_placeholders(template)
        check_path_placeholders(
            placeholders,
            subagent or GENERIC_AGENT,
            reads_local_files=True if meta is None else meta.reads_local_files,
        )
        sdir = self._manager.session_dir_for(session_key)
        history = str(session_history_root(sdir))
        cwd = str(workdir.current() or self._manager.workspace)
        backend = LocalFileBackend()
        known = await read_session_nodes(backend, history)
        # Checked before anything is read, so an id whose task failed is
        # answered with why -- the outcome decides whether to re-do the work,
        # fix an upstream, or wait -- rather than with the leftover file a bare
        # path would have handed over.
        check_node_refs(node_id, placeholders, inputs, known)
        return await render_template(
            template,
            inputs,
            backend=backend,
            cwd=cwd,
            nodes_root=str(nodes_root(sdir)),
            roots=(cwd, history),
            known=known,
        )

    async def execute(
        self,
        task_summary: str,
        prompt_template: str | None = None,
        # After `prompt_template`, not beside `task_summary` where the schema
        # lists it: two callers pass those two positionally, and a new second
        # parameter would silently bind the template to this instead.
        node_id: str | None = None,
        subagent: str | None = None,
        instance: str | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task.

        ``agent`` and ``task`` are accepted from ``kwargs`` as the old spellings of
        ``subagent`` and ``prompt_template``. ``ToolRegistry.execute`` validates the
        schema's ``required`` list before ``execute`` runs, so a model call omitting
        either new name never reaches this fallback -- it serves callers that bypass
        the registry (direct and programmatic calls, and this repo's own tests) and a
        call that sends both spellings, where the new name wins.
        """
        org = self._cur()
        # Cleared first, ahead of every return below: an earlier stateful call
        # whose metadata went uncollected (no tool-event sink on this channel)
        # must not have its handle popped and reported as this call's own,
        # whether this call goes on to dispatch or is refused at any step below.
        self._pending.pop(org.session_key, None)
        subagent = subagent or kwargs.pop("agent", None)
        # A label is this turn's name for a worker; everything below dispatches
        # on the roster agent behind it. Resolved before the roster check so a
        # label is never reported back as an unknown agent.
        payload = self._resolve_payload(subagent)
        subagent, charter = self._resolve_worker(subagent)
        template = prompt_template or kwargs.pop("task", None)
        if not template:
            return "Error: `prompt_template` is required -- it is the task the sub-agent runs."
        node_id = (node_id or kwargs.pop("call_id", None) or "").strip()
        if not node_id:
            return (
                "Error: `node_id` is required -- it is the name a later task references this "
                "one's output by, and only you can choose it."
            )
        if not re.match(NODE_ID_PATTERN, node_id):
            return (
                f"Error: node id '{node_id}' is not usable -- ids match {NODE_ID_PATTERN} because "
                "the id is also this task's filename. Rename it."
            )
        if (refusal := self._reject_useless_instance(subagent, instance)) is not None:
            return refusal
        if inputs is not None and not isinstance(inputs, dict):
            return (
                "Error: `inputs` must be an object keyed by input name -- "
                '{"<key>": "<text>"} or {"<key>": {"file": "<path>"}}.'
            )
        # Rendered before anything is minted or dispatched: a template naming a
        # file the sub-agent cannot be given is a correctable mistake in the
        # call, and every one of them has to come back as advice rather than as
        # a run that started on a prompt with a hole in it.
        try:
            task = await self._render(template, inputs or {}, subagent, org.session_key, node_id)
            # The charter rides on the task rather than replacing it: the worker
            # gets its brief, then the thing this dispatch actually asked for.
            if charter:
                task = charter + task
        except DagValidationError as exc:
            detail = str(exc).rstrip()
            if detail and detail[-1] not in ".!?":
                detail += "."
            return f"Error: {detail} Call spawn again with the corrected prompt_template."
        # Minted rather than left empty so every run of a resumable sub-agent is
        # addressable afterwards. Filling the field the model would have filled
        # is what keeps the rest of the dispatch path unchanged.
        minted = not instance and self._is_stateful(subagent)
        if minted:
            instance = mint_handle(task_summary, fallback=subagent or GENERIC_AGENT)
        # Read, check and claim inside one guard: split across two, both a
        # spawn and a graph pass the uniqueness check and both take the id.
        # Here rather than where the record is opened, because that happens in
        # the background task this call has already returned from -- a refusal
        # raised there could only reach the model a turn later.
        history_root = str(session_history_root(self._manager.session_dir_for(org.session_key)))
        backend = LocalFileBackend()
        async with index_guard(history_root):
            known = await read_session_nodes(backend, history_root)
            if (claim := known.claimed_by(node_id)) is not None:
                taken, owner = claim
                return "Error: " + duplicate_node_id(node_id, owner, readable=known.is_readable(taken), taken_as=taken)
            await claim_node(backend, history_root, node_id, kind="spawn", started_at_ms=int(time.time() * 1000))
        # The charter rides the dispatch rather than the call signature:
        # ``SubagentBackend.run`` is frozen contract and every backend would
        # have to know a field only a Raven worker can use. The transport that
        # can carry it asks for it instead.
        with dispatch_charter(payload):
            result = await self._manager.spawn(
                node_id=node_id,
                task=task,
                task_summary=task_summary,
                origin_channel=org.channel,
                origin_chat_id=org.chat_id,
                session_key=org.session_key,
                agent=subagent,
                instance=instance,
                instance_auto=minted,
                workspace=workdir.current(),
                # The unrendered template, not `task`: the completion announcement
                # shows this verbatim with no truncation, and `task` may have
                # inlined a whole file through `{{ ref:<path> }}`.
                authored_task=template,
                tool_call_id=self._tool_call_id.get(),
            )
        # Same reason the handle below is withheld: a refusal comes back as the
        # result, not an exception, and it lands after the id was claimed. The
        # task never ran and left no record, so holding its id would burn that
        # name for the rest of the conversation over a paused queue or a full
        # hourly budget -- and the retry the refusal invites would be refused
        # again, for the wrong reason.
        if result.startswith(SPAWN_REFUSED_PREFIX):
            async with index_guard(history_root):
                await release_node_claim(backend, history_root, node_id)
        # Published only once the manager has taken the spawn. A refusal (delegation
        # paused, hourly cap) comes back as the result rather than as an exception,
        # and a handle announced for one would draw an instance row and a `new` badge
        # for a run that never started -- for every refused call, now that an unnamed
        # one carries a handle too.
        if instance and not result.startswith(SPAWN_REFUSED_PREFIX):
            self._pending[org.session_key] = {"instance": instance, "instance_auto": minted}
        return result
