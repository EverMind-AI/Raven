"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.agent import workdir
from raven.agent.subagent.instances import mint_handle
from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX
from raven.agent.subagent_dag._errors import DagValidationError
from raven.agent.subagent_dag._paths import RUNS_PREFIX, check_confined
from raven.agent.tools.base import Tool
from raven.security.trust import wrap_untrusted

if TYPE_CHECKING:
    from raven.agent.subagent import SubagentManager
    from raven.agent.subagent.backends import AgentMeta


_INPUTS_HEADING = (
    "Inputs handed to you with this task. They are the material to work from; if one is "
    "missing or empty, say so in your answer rather than inventing what it would have said."
)


class SpawnInputError(ValueError):
    """An ``inputs`` entry cannot be turned into material for the sub-agent."""


def _block(key: str, source: Path | None, text: str) -> str:
    read_from = f" (read from {source})" if source is not None else ""
    return f"--- input '{key}'{read_from} ---\n{text}\n--- end input '{key}' ---"


def _read_input_file(key: str, path: str, roots: tuple[str, ...]) -> str:
    """Read one ``{"file": ...}`` input, confined to the roots a dispatch may reach.

    The contents are fenced as untrusted data: the likeliest file here is an
    earlier spawn's ``out.md``, which is sub-agent-authored, and any other one
    may hold whatever a run fetched. The same rule a DAG node's references
    follow -- the caller's own ``task`` is the instruction, injected material is
    data.

    The ``@runs/`` form is refused rather than resolved: it addresses a DAG run
    history, and a spawn has none, so resolving it would land on a directory
    that never exists and report a missing file instead of a wrong reference.
    """
    if path.startswith(RUNS_PREFIX):
        raise SpawnInputError(
            f"input {key!r} path {path!r} names a DAG run history, which a spawn does not have -- "
            "give the file's own path, such as the out.md in an earlier call's `Record:` directory"
        )
    try:
        check_confined(path, what=f"input '{key}' file", roots=roots)
    except DagValidationError as exc:
        raise SpawnInputError(str(exc)) from exc
    resolved = Path(path) if Path(path).is_absolute() else Path(roots[0]) / path
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SpawnInputError(f"input {key!r} file {path!r} could not be read: {exc.strerror or exc}") from exc
    return _block(key, resolved, wrap_untrusted(text, source="file"))


def _resolve_input(key: str, spec: Any, roots: tuple[str, ...]) -> str:
    if isinstance(spec, dict):
        if "file" not in spec:
            raise SpawnInputError(f'input {key!r} must be a string or {{"file": <path>}}')
        return _read_input_file(key, str(spec["file"]), roots)
    return _block(key, None, str(spec))


def _with_inputs(task: str, inputs: Any, roots: tuple[str, ...]) -> str:
    """Place the resolved ``inputs`` before ``task``.

    Resolved here rather than handed to the backend as paths: a sub-agent the
    roster tags ``[no-local-files]`` has no way to open one, and a spawn's whole
    prompt is the single string the backend receives.
    """
    if not isinstance(inputs, dict):
        raise SpawnInputError("`inputs` must be an object keyed by input name")
    blocks = [_resolve_input(key, spec, roots) for key, spec in inputs.items()]
    return "\n\n".join([_INPUTS_HEADING, *blocks, task])


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
        # Written by execute and popped by take_metadata in the loop's own task,
        # so the handoff cannot rely on a ContextVar write propagating upward.
        # Keyed by session so concurrent turns never read each other's handle.
        self._pending: dict[str, dict[str, Any]] = {}

    def _cur(self) -> _SpawnOrigin:
        return self._origin.get(None) or self._default

    def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set the origin context for subagent announcements (turn-local)."""
        self._origin.set(replace(self._cur(), channel=channel, chat_id=chat_id, session_key=session_key))

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
            from raven.agent.subagent.builtin_agents import GENERIC_AGENT

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
                "step's output reaches the next through a file."
            )
            # The result carries this path; without a word here the agent has a
            # directory it does not know the use of.
            base += (
                " The result names a `Record:` directory holding this call's prompt and output. "
                "Its `out.md` is what to pass as a later call's `inputs` to hand this run's work "
                "to the next sub-agent. It may also hold `memory.json` -- what the sub-agent "
                "concluded for itself, rather than the answer it gave you -- written after the "
                "call, so absence is normal."
            )
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "task_summary": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "One line telling the user what you are dispatching, written before the task. "
                    "Around 200 characters at most. They read it in listings and announcements and "
                    "the sub-agent never does, so write it for them -- no ids, no internal shorthand."
                ),
            },
            "task": {
                "type": "string",
                "description": "The task for the subagent to complete",
            },
            "inputs": {
                "type": "object",
                "description": (
                    "Optional: the material this task works from, per key -- a literal string, or "
                    '{"file": <path>} for a file. Each one is read now and placed before `task`, so a '
                    "sub-agent that cannot open local files still receives the contents. This is how "
                    "one sub-agent's work reaches the next: hand over the out.md in the earlier call's "
                    "`Record:` directory instead of retyping its result into `task`. Paths resolve "
                    "inside the session working directory and this conversation's sub-agent history; "
                    "anything else is refused and nothing is dispatched."
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
            "required": ["task_summary", "task", "subagent"] if names else ["task_summary", "task"],
        }

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
            f"putting whatever context the run needs into `task`."
        )

    async def execute(
        self,
        task_summary: str,
        task: str,
        subagent: str | None = None,
        instance: str | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task.

        ``agent`` is accepted from ``kwargs`` because that is what this parameter
        was called until the field was unified on ``subagent``. Left to fall into
        ``kwargs`` it would be swallowed and ``subagent`` would stay ``None``,
        which ``SubagentManager.spawn`` resolves to the generic built-in row --
        so the old spelling would run the task on a different agent than the one
        it named, and report success. A rejection would be honest too, but this
        is a rename we made: doing what the call plainly means costs three lines.
        """
        subagent = subagent or kwargs.pop("agent", None)
        if (refusal := self._reject_useless_instance(subagent, instance)) is not None:
            return refusal
        org = self._cur()
        # Before the handle is minted and before anything is dispatched: an input
        # the caller named and this call could not read is the one case where
        # running anyway produces exactly the failure this parameter exists to
        # stop -- a sub-agent working from material that never arrived. The roots
        # are resolved only when there is something to resolve against them:
        # deriving them reaches for the session directory.
        authored_task: str | None = None
        if inputs:
            try:
                rendered = _with_inputs(task, inputs, self._manager.reference_roots(org.session_key))
            except SpawnInputError as exc:
                return f"Error: {exc}"
            authored_task, task = task, rendered
        # Minted rather than left empty so every run of a resumable sub-agent is
        # addressable afterwards. Filling the field the model would have filled
        # is what keeps the rest of the dispatch path unchanged.
        minted = not instance and self._is_stateful(subagent)
        if minted:
            from raven.agent.subagent.builtin_agents import GENERIC_AGENT

            instance = mint_handle(task_summary, fallback=subagent or GENERIC_AGENT)
        # Cleared before the call rather than only on the stateless path: an earlier
        # stateful call whose metadata went uncollected (no tool-event sink on this
        # channel) must not have its handle popped and reported as this call's own,
        # and that is just as true when this call is refused below.
        self._pending.pop(org.session_key, None)
        result = await self._manager.spawn(
            task=task,
            task_summary=task_summary,
            origin_channel=org.channel,
            origin_chat_id=org.chat_id,
            session_key=org.session_key,
            agent=subagent,
            instance=instance,
            instance_auto=minted,
            workspace=workdir.current(),
            authored_task=authored_task,
        )
        # Published only once the manager has taken the spawn. A refusal (delegation
        # paused, hourly cap) comes back as the result rather than as an exception,
        # and a handle announced for one would draw an instance row and a `new` badge
        # for a run that never started -- for every refused call, now that an unnamed
        # one carries a handle too.
        if instance and not result.startswith(SPAWN_REFUSED_PREFIX):
            self._pending[org.session_key] = {"instance": instance, "instance_auto": minted}
        return result
