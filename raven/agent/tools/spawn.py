"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from raven.agent import workdir
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

    def _cur(self) -> _SpawnOrigin:
        return self._origin.get(None) or self._default

    def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set the origin context for subagent announcements (turn-local)."""
        self._origin.set(replace(self._cur(), channel=channel, chat_id=chat_id, session_key=session_key))

    @property
    def name(self) -> str:
        return "spawn"

    def _third_party_agents(self) -> list["AgentMeta"]:
        lister = getattr(self._manager, "list_third_party_agents", None)
        return lister() if callable(lister) else []

    @property
    def description(self) -> str:
        base = (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "Multiple spawns run concurrently."
        )
        agents = self._third_party_agents()
        if agents:
            from raven.agent.subagent.backends import format_agent_listing

            listing = format_agent_listing(agents)
            base += (
                " By default the subagent is a Raven agent; to delegate to a specialized "
                f"third-party agent instead, pass its name as `agent`. Available: {listing}."
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
        agents = self._third_party_agents()
        names = [a.name for a in agents]
        if names:
            props["agent"] = {
                "type": "string",
                "enum": names,
                "description": (
                    "Optional: delegate to this specialized third-party agent instead of a default Raven subagent."
                ),
            }
        stateful_names = [a.name for a in agents if a.stateful]
        if stateful_names:
            props["instance"] = {
                "type": "string",
                "description": (
                    "Optional: a short semantic handle (e.g. 'refactor-auth') naming a "
                    "conversation with a stateful third-party agent. Reuse the same handle "
                    "to continue that session; omit it to start a fresh one. Only for "
                    f"`agent` in {stateful_names} -- elsewhere a handle continues nothing "
                    "and the call is rejected."
                ),
            }
        return {
            "type": "object",
            "properties": props,
            "required": ["task"],
        }

    def _reject_useless_instance(self, agent: str | None, instance: str | None) -> str | None:
        """Why this handle cannot work, or ``None`` when it can.

        Same gate ``run_subagent_dag`` applies to a shared ``instance``, on the
        one-call surface: passing a handle to an agent that cannot resume it is
        not a no-op the caller can see. The backend silently ignores it and
        returns a reply written as if the earlier turns never happened, which
        reads as the sub-agent forgetting rather than as a rejected argument.
        Refused before the spawn so nothing runs under the false expectation.
        """
        if not instance:
            return None
        roster = {a.name: a for a in self._third_party_agents()}
        if agent is None:
            names = sorted(name for name, a in roster.items() if a.stateful)
            hint = f" Pass `agent` as one of {names} to reuse a session." if names else ""
            return (
                f"Error: `instance` was set to {instance!r} but no `agent` was named, so this is a "
                f"default Raven subagent -- it has no resumable session for a handle to continue."
                f"{hint} Call spawn again without `instance`."
            )
        meta = roster.get(agent)
        if meta is None or meta.stateful:
            return None
        names = sorted(name for name, a in roster.items() if a.stateful)
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
        org = self._cur()
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=org.channel,
            origin_chat_id=org.chat_id,
            session_key=org.session_key,
            agent=agent,
            instance=instance,
            workspace=workdir.current(),
        )
