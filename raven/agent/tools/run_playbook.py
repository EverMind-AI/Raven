"""``run_playbook`` — the agent's own way into a playbook.

The passive path (L1 vocabulary, then the gate) decides before the turn starts,
which makes it deaf to two things the agent is not: a request that means a
playbook without naming any of its trigger words, and a parameter the user only
supplies after being asked for it. In both cases the conversation carries what
the funnel lacks, so the agent gets the same entry the funnel uses -- the same
library, the same executor, the same dispatch -- rather than a second path that
could drift from it.

Names only, never a spec: the tool takes a playbook id and parameters, so a
model (or text injected into one) cannot assemble a graph here that the library
does not already hold and validation has not already passed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.agent.tools.base import Tool

if TYPE_CHECKING:
    from raven.playbook.runtime import PlaybookRuntime


class RunPlaybookTool(Tool):
    """Run a stored playbook by name, with parameters read off the conversation."""

    # A dag hit dispatches in the background and announces its own result, so
    # the call itself returns as soon as the graph is accepted.
    timeout_seconds = 120.0

    def __init__(self, runtime: "PlaybookRuntime") -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "run_playbook"

    @property
    def description(self) -> str:
        listing = self._runtime.listing()
        if not listing:
            return "Run a stored playbook. No playbooks are installed."
        lines = "\n".join(f"- {pid}: {desc}" for pid, desc in listing)
        return (
            "Run one of the user's stored playbooks -- a saved multi-step procedure with its own "
            "agents and parameters. Use it when the request matches one of the playbooks below but "
            "was not picked up automatically (the passive matcher keys off specific trigger words), "
            "or when the user has just supplied a parameter a previous run asked for. Pass the "
            "parameters you can read off the conversation; anything still missing comes back as a "
            "question to relay.\n"
            f"Available playbooks:\n{lines}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Playbook id, exactly as listed in this tool's description.",
                    "enum": [pid for pid, _ in self._runtime.listing()] or None,
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Parameter values read off the conversation, keyed by the playbook's own "
                        "parameter names. Omit what the user has not said -- do not invent values."
                    ),
                },
            },
            "required": ["name"],
        }

    async def execute(self, name: str, params: dict[str, Any] | None = None, **kwargs: Any) -> str:
        plan = await self._runtime.run_named(name, params or {})
        if plan is None:
            known = ", ".join(pid for pid, _ in self._runtime.listing()) or "(none installed)"
            return f"Error: no playbook named {name!r}. Available: {known}"
        # Both outcomes are text for the model to relay: a dispatch receipt, or
        # the questions that stopped it. The distinction matters to the caller
        # only in tone, so it is not encoded in a separate field.
        notes = "".join(f"\n[note] {n}" for n in plan.notes)
        return f"{plan.reply}{notes}"
