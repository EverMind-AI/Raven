"""The read-only view of the machines this instance can reach."""

from __future__ import annotations

from typing import Any

from raven.agent.tools.base import Tool


class OpsConnectionsTool(Tool):
    """List the connections the owner has set up, with what each machine is."""

    @property
    def name(self) -> str:
        return "ops_connections"

    @property
    def description(self) -> str:
        return (
            "List the machines this instance can run work on: the owner's own name for "
            "each one, what it is (CPU or GPU, its device, its cores), the unit its "
            "budget is counted in, and how many jobs it will run at once. "
            "Call this FIRST whenever a task says to run something on a machine -- "
            "including when the task names the machine in the owner's words ('on my CPU "
            "box', 'on the GPU machine') or gives an address. This is the only way in: "
            "credentials belong to the connection and never to you, so there is nothing "
            "to work out with exec or ssh, and an address in a task statement does not "
            "identify a machine by itself (two of these can share one). "
            "When more than one could serve, pick the one the work calls for -- a "
            "CPU-only solver on the CPU box, a run that needs device memory on the GPU "
            "machine -- and say which you picked and why. When none of them fits, ask "
            "the owner to add one; do not go looking for a way in. Read-only."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        from raven.ops.connections import describe

        return describe()
