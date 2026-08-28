"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from raven.agent import workdir
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.history import dag_root, session_history_root
from raven.agent.subagent.instances import mint_handle
from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_capabilities import check_path_placeholders
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_placeholders import parse_placeholders
from raven.agent.subagent.prompt_render import needs_a_graph, node_form_refusal, render_template
from raven.agent.tools.base import Tool

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
            # directory it does not know the use of.
            base += (
                " The result names a `Record:` directory holding this call's prompt and output. "
                "Its `out.md` is what to hand a follow-up task -- reference it with "
                "`{{ ref:<that directory>/out.md }}` instead of restating the result from memory. "
                "The directory may also hold `memory.json` -- what the sub-agent concluded for "
                "itself, rather than the answer it gave you -- written after the call, so absence "
                "is normal."
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
            "prompt_template": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The task for the subagent. Placeholders: {{ ref:<path> }} / "
                    "{{ ref_path:<path> }} inject a file's contents / its path; "
                    "{{ inputs.<k> }} / {{ inputs.<k>.path }} inject an input. Paths resolve "
                    "under the working directory and this conversation's sub-agent history, so "
                    "the `Record:` directory of an earlier spawn is readable: hand its "
                    "`out.md` to the next task with {{ ref:<record dir>/out.md }} rather than "
                    "restating it. The _path forms need a sub-agent the roster tags "
                    "[local-files]; for a [no-local-files] one use the contents forms. "
                    "Nothing is added around an injected value -- no heading, no label, no source "
                    "path -- so write in the template itself what the material is and where it "
                    "came from. Every key in `inputs` must be referenced by a placeholder."
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    'Per-key literal string or {"file": <path>}. {{ inputs.<k> }} injects the '
                    "text, {{ inputs.<k>.path }} the file path."
                ),
            },
        }
        agents = self._agents()
        names = sorted(a.name for a in agents)
        if names:
            props["subagent"] = {
                "type": "string",
                "enum": names,
                "description": "Which agent runs this task. Required: pick one from the list.",
            }
        # One enum over every agent's modes, because the schema has one `subagent`
        # property and JSON Schema cannot make one property's enum depend on
        # another's value. Which ids belong to which agent is therefore said in
        # the description, and a mode aimed at an agent that does not have it is
        # refused at dispatch rather than by the schema.
        by_agent = [(a.name, a.modes) for a in agents if a.modes]
        if by_agent:
            props["mode"] = {
                "type": "string",
                "enum": sorted({m.id for _name, modes in by_agent for m in modes}),
                "description": (
                    "Optional: how much effort the agent spends. Omit it and the agent's own "
                    "default is used, which is the right choice unless the request says otherwise. "
                    + " ".join(
                        f"{name}: "
                        + "; ".join(f"{m.id} - {(m.description or m.name or m.id).rstrip('.')}" for m in modes)
                        + "."
                        for name, modes in by_agent
                    )
                ),
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
                ["task_summary", "prompt_template", "subagent"] if names else ["task_summary", "prompt_template"]
            ),
        }

    def _mode_refusal(self, agent: str | None, mode: str | None) -> str:
        """Why this agent cannot be run in ``mode``, or ``""`` when it can.

        The schema's enum is the union over every agent's modes (one property
        cannot depend on another's value), so the per-agent check is here. Said
        as a refusal the model can act on rather than passed down to fail at the
        agent: a mode it will not accept is a fact this side already knows.
        """
        if not mode:
            return ""
        meta = next((a for a in self._agents() if a.name == agent), None)
        if meta is None:
            return ""
        offered = [m.id for m in meta.modes]
        if mode in offered:
            return ""
        if not offered:
            return f"Error: `{agent}` offers no modes. Call spawn again without `mode`."
        return f"Error: `{agent}` has no mode `{mode}`. Its modes are: {', '.join(offered)}."

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

    async def _render(self, template: str, inputs: dict[str, Any], subagent: str | None, session_key: str) -> str:
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
        an earlier spawn's ``Record:`` directory is nameable. Text read from
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
        # Ahead of the gate: a spawn has no graph, so a placeholder naming a node
        # is refused for that rather than for the capability it also happens to
        # need. The gate speaking first pointed the model at `{{ <id>.output }}`,
        # which this surface refuses on the next turn -- two turns to learn one
        # thing, and the first answer was advice that cannot work here.
        for ph in placeholders:
            if needs_a_graph(ph, inputs):
                raise node_form_refusal(ph.raw)
        check_path_placeholders(
            placeholders,
            subagent or GENERIC_AGENT,
            reads_local_files=True if meta is None else meta.reads_local_files,
        )
        sdir = self._manager.session_dir_for(session_key)
        history = str(session_history_root(sdir))
        cwd = str(workdir.current() or self._manager.workspace)
        return await render_template(
            template,
            inputs,
            backend=LocalFileBackend(),
            cwd=cwd,
            runs_root=str(dag_root(sdir)),
            roots=(cwd, history),
        )

    async def execute(
        self,
        task_summary: str,
        prompt_template: str | None = None,
        subagent: str | None = None,
        instance: str | None = None,
        inputs: dict[str, Any] | None = None,
        mode: str | None = None,
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
        template = prompt_template or kwargs.pop("task", None)
        if not template:
            return "Error: `prompt_template` is required -- it is the task the sub-agent runs."
        if (refusal := self._reject_useless_instance(subagent, instance)) is not None:
            return refusal
        # The same pre-dispatch question the DAG runner asks (dag_tool.py, "Last
        # of the pre-dispatch checks"): an agent that runs work on the owner's
        # machines is asked whether it has one, and a task whose work has
        # nowhere to go is refused while the owner is still here to be asked.
        # It was only on the DAG path, and the failure that bought this line was
        # a single spawn: the on-call agent booted, found the registry empty,
        # and the owner learned it minutes in, one sub-agent run too late.
        # Silent when nothing could be established -- see dag_machines.
        if subagent:
            from raven.agent.subagent.dag_machines import refusal as machines_refusal
            from raven.agent.subagent.dag_machines import verdict_for_async

            if (verdict := await verdict_for_async([subagent])) and verdict.usable == 0:
                # Registration has to be one runnable line. Measured 2026-08-27,
                # before `ops connection` was lifted into this install: the model
                # collected every answer from the owner, found no command to put
                # them in, and hand-ran the work over ssh instead.
                return machines_refusal(verdict) + (
                    "\n\nOnce the owner has answered, register the machine yourself "
                    "and spawn again:\n"
                    "  raven ops connection add --non-interactive --id <id> "
                    "--name <name> --transport ssh --host <addr> --port <port> "
                    "--user <user> --key <keypath> --software '<installed, with paths>' "
                    "--budget-unit minute --concurrency 1\n"
                    "When the machine is this very computer, use --transport local with "
                    "no host/port/user/key -- everything it asks is knowable here, so "
                    "you may register without waiting for the owner, but tell them in "
                    "your reply what you wrote down (name, budget unit, jobs at once): "
                    "the registry is theirs, and this row is what every later campaign "
                    "reads as fact."
                )
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
            task = await self._render(template, inputs or {}, subagent, org.session_key)
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
        if refusal := self._mode_refusal(subagent, mode):
            return refusal
        result = await self._manager.spawn(
            task=task,
            task_summary=task_summary,
            mode=mode,
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
        # Published only once the manager has taken the spawn. A refusal (delegation
        # paused, hourly cap) comes back as the result rather than as an exception,
        # and a handle announced for one would draw an instance row and a `new` badge
        # for a run that never started -- for every refused call, now that an unnamed
        # one carries a handle too.
        if instance and not result.startswith(SPAWN_REFUSED_PREFIX):
            self._pending[org.session_key] = {"instance": instance, "instance_auto": minted}
        return result
