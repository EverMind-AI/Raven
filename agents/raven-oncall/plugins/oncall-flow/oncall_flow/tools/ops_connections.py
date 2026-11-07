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
            "to work out with exec or ssh. "
            'When the owner named the machine -- by an address or part of one ("100.27", '
            '"27"), a port ("64106"), a nickname, or its name -- pass those words as '
            "'hint': the answer says which machines they can mean, without addresses. "
            "Several machines can share one address, so a hint can match more than one; "
            "the answer then says how to settle it. "
            "When more than one could serve, pick the one the work calls for -- a "
            "CPU-only solver on the CPU box, a run that needs device memory on the GPU "
            "machine -- and say which you picked and why. When none of them fits, ask "
            "the owner to add one; do not go looking for a way in. Read-only."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "hint": {
                    "type": "string",
                    "description": (
                        "The owner's own words for the machine, as written: an address or "
                        "its last part, a port, a nickname, a name. Leave out when they "
                        "named none."
                    ),
                }
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        from oncall_flow.connections import describe

        hint = str(kwargs.get("hint") or "").strip()
        return describe(hint) if hint else describe()
