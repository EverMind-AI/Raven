"""The read-only view of the machines this instance can reach."""

from __future__ import annotations

from typing import Any

from raven.contracts.tool import Tool


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
            "Call this FIRST whenever the owner wants something run and watched -- a solver "
            "case, a training run, a sweep, anything carrying a budget or a result worth "
            "waiting for. A path to a case or a script is the whole signal: nobody has to name "
            "a machine, a path does not say which one it is, and it may well be the computer "
            "you are already on. Measured 2026-08-19 -- two tasks of the same shape, one path "
            "under a shared mount and one under a home directory, and only the first was read "
            "as belonging on a machine. Where a path looks like it lives decides nothing. "
            "This is the only way in: "
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
        from oncall_flow.connections import describe

        return describe()
