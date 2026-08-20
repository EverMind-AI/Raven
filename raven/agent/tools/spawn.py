"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from raven.agent import workdir
from raven.agent.subagent.instances import mint_handle
from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX
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

            listing = format_agent_listing(agents)
            base += (
                " Pick the agent for the job with `agent` -- there is no default, so choose "
                f"deliberately from: {listing}."
            )
            # The one moment a wrong choice is visible: the model is reading this
            # tool while the work is really a graph.
            base += (
                " If you are about to issue several spawns for one task, that is a DAG: use "
                "`run_subagent_dag` instead, so the independent parts run concurrently and each "
                "step's output reaches the next through a file."
            )
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "task": {
                "type": "string",
                "description": "The task for the subagent to complete",
            },
            "label": {
                "type": "string",
                "description": "Optional short label for the task (for display)",
            },
        }
        agents = self._agents()
        names = sorted(a.name for a in agents)
        if names:
            props["agent"] = {
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
                f"Accepted for `agent` in {stateful_names} -- against any other one a handle "
                "continues nothing and the call is rejected."
            ),
        }
        # `agent` is required only once there is a roster to require it from. An
        # empty one is a table whose every row failed to build; demanding a value
        # the enum cannot offer would be an unsatisfiable schema, and the manager
        # still resolves an omitted name to the generic built-in row.
        return {
            "type": "object",
            "properties": props,
            "required": ["task", "agent"] if names else ["task"],
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
        task: str,
        label: str | None = None,
        agent: str | None = None,
        instance: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        if (refusal := self._reject_useless_instance(agent, instance)) is not None:
            return refusal
        # Minted rather than left empty so every run of a resumable sub-agent is
        # addressable afterwards. Filling the field the model would have filled
        # is what keeps the rest of the dispatch path unchanged.
        minted = not instance and self._is_stateful(agent)
        if minted:
            instance = mint_handle(label or task, fallback=agent or "raven")
        org = self._cur()
        # Cleared before the call rather than only on the stateless path: an earlier
        # stateful call whose metadata went uncollected (no tool-event sink on this
        # channel) must not have its handle popped and reported as this call's own,
        # and that is just as true when this call is refused below.
        self._pending.pop(org.session_key, None)
        result = await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=org.channel,
            origin_chat_id=org.chat_id,
            session_key=org.session_key,
            agent=agent,
            instance=instance,
            instance_auto=minted,
            workspace=workdir.current(),
        )
        # Published only once the manager has taken the spawn. A refusal (delegation
        # paused, hourly cap) comes back as the result rather than as an exception,
        # and a handle announced for one would draw an instance row and a `new` badge
        # for a run that never started -- for every refused call, now that an unnamed
        # one carries a handle too.
        if instance and not result.startswith(SPAWN_REFUSED_PREFIX):
            self._pending[org.session_key] = {"instance": instance, "instance_auto": minted}
        return result
