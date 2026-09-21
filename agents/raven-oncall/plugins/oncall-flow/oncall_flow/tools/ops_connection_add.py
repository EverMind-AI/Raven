"""Add one machine to the registry, from what the owner answered.

The counterpart of ``ops_connections``' empty-registry reply: that reply asks
the owner what only they know, and this face takes the answer. Until 2026-09-06
the host ran ``raven ops connection add --non-interactive`` on the owner's
behalf before dispatching a spawn; that step went with the pre-dispatch registry
gate, so an owner who answered in conversation had nothing consuming the answer. Here the write belongs to the instance that needs the machine.

Two things keep the ask short. What a machine can say about itself -- cores,
memory, devices -- is read off it once reached, never typed. And what the owner
leaves out of the way in -- port, username, key path -- is resolved the way
their own terminal would resolve it (``ssh -G``, config blocks included), then
tried: the probe decides, and only what connected is written. Nothing is written
until the machine itself answers; an unreachable row in the registry is a fact
every later turn acts on, an absent one is a question the loop knows to ask.
"""

from __future__ import annotations

import asyncio
from typing import Any

from raven.contracts.tool import Tool

_BUDGET_UNITS = ("minute", "core-minute", "gpu-minute")


class OpsConnectionAddTool(Tool):
    """Write one machine the owner described, after reaching it."""

    @property
    def name(self) -> str:
        return "ops_connection_add"

    @property
    def description(self) -> str:
        return (
            "Add a machine to the registry from the owner's own answers, when ops_connections "
            "lists none that fits. Needed: what they call it, and whether it is this very computer "
            "or another one reached over ssh; for ssh, its address. Port, username and private-key "
            "path only when the owner gave them -- left out, ssh's own config is consulted and "
            "whatever connects is kept, so never invent one: a guess that lands in the registry "
            "stops being a guess. What is installed, which directories hold their work and how the "
            "budget is counted are optional; cores, memory and devices are read off the machine. "
            "Nothing is written until the machine answers. Then hand the new id to ops_declare."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "What the owner calls this machine."},
                "transport": {
                    "type": "string",
                    "enum": ["ssh", "local"],
                    "description": "'ssh' for another computer, 'local' for this very one.",
                },
                "host": {"type": "string", "description": "ssh only: the address."},
                "port": {"type": "integer", "description": "ssh only, if the owner said; else ssh's config."},
                "user": {"type": "string", "description": "ssh only, if the owner said; else ssh's config."},
                "key": {
                    "type": "string",
                    "description": "ssh only, if the owner said: path to the private key (the path is stored, "
                    "not the key). Left out, the keys ssh itself would try are probed in turn.",
                },
                "software": {"type": "string", "description": "Optional: what is installed, with paths."},
                "budget_unit": {
                    "type": "string",
                    "enum": list(_BUDGET_UNITS),
                    "description": "Optional: gpu-minute for a GPU machine, core-minute otherwise.",
                },
                "concurrency": {
                    "type": "integer",
                    "description": "Optional: jobs at once, read only when the machine reports no cores or devices.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: absolute directories on it that hold the owner's work.",
                },
                "id": {
                    "type": "string",
                    "description": "Short id; minted from the name when omitted. Never renamed, campaigns store it.",
                },
                "note": {"type": "string"},
            },
            "required": ["name", "transport"],
        }

    async def execute(
        self,
        name: str,
        transport: str,
        host: str = "",
        port: int = 0,
        user: str = "",
        key: str = "",
        software: str = "",
        budget_unit: str = "",
        concurrency: int = 0,
        paths: list | None = None,
        id: str = "",  # noqa: A002 -- the registry's own field name, kept so the model sees one spelling
        note: str = "",
        **kwargs: Any,
    ) -> str:
        from oncall_flow import connections
        from raven.utils.paths import mint_slug

        label = str(name).strip()
        if not label:
            return "REFUSED: the machine needs the owner's name for it. Nothing was written."
        kind = connections.LOCAL if str(transport).strip().lower() == connections.LOCAL else connections.SSH
        conn_id = str(id).strip() or mint_slug(label)
        row: dict[str, Any] = {"id": conn_id, "display_name": label, "transport": kind}
        if kind == connections.LOCAL:
            reached, message, found = await asyncio.to_thread(connections.probe, row)
        else:
            host = str(host).strip()
            if not host:
                return "REFUSED: an ssh machine needs its address; ask the owner rather than guessing. Nothing was written."
            # What the owner left out, ssh's own config supplies -- the same
            # answer their terminal would give -- and the probe decides. Asked
            # only when something is actually missing: with all three given there
            # is nothing for it to answer.
            given = str(key).strip()
            resolved: dict[str, Any] = {}
            if not (port and str(user).strip() and given):
                resolved = await asyncio.to_thread(connections.ssh_defaults, host, port=int(port or 0), user=str(user))
            row.update(
                {
                    "host": host,
                    "port": int(port or resolved.get("port") or 22),
                    "user": str(user).strip() or str(resolved.get("user") or "root"),
                }
            )
            keys = [given] if given else list(resolved.get("keys") or [])
            if not keys:
                return (
                    f"REFUSED: no private key to reach {row['user']}@{host}:{row['port']} with -- the owner gave "
                    "no key path and none of ssh's default keys exists here. Ask them for the path. Nothing was written."
                )
            reached, message, found = False, "", {}
            for candidate in keys:
                row["key"] = candidate
                # A path the owner named is their assertion, and is probed the way a
                # job will reach the machine -- an agent doing the authenticating is
                # their setup working. A path this tool picked is a claim of its own,
                # so it is probed with that key alone: credited otherwise, the row
                # would hold a key that only looked like the one that opened the
                # session, and stop working the day the agent or the config changes.
                reached, message, found = await asyncio.to_thread(connections.probe, row, isolate_key=not given)
                if reached:
                    break
            if not reached and not given:
                # Attribution costs the owner's ssh config: a candidate is tried
                # with no config read, so an alias or a jump host that makes this
                # machine reachable is not in play here. Naming the key puts the
                # probe back on the path a job will take.
                message = (
                    f"{message} (tried, each on its own, the key(s) ssh named: {', '.join(keys)}; "
                    "a candidate is probed with no ssh config read, so a machine reached through "
                    "an alias or a jump host needs its key path named)"
                )
        if not reached:
            return f"REFUSED: {message}. Nothing was written. Check the address, port, user and key with the owner."
        row.update(found)
        if str(software).strip():
            row["software"] = str(software).strip()
        row["budget_unit"] = str(budget_unit).strip() or ("gpu-minute" if row.get("kind") == "gpu" else "core-minute")
        # Not read on a row that reports cores or devices (capacity decides);
        # kept only where it is the one thing admission has to go on.
        if int(concurrency or 0) > 0 and not connections.resource_unit(row):
            row["concurrency"] = int(concurrency)
        if paths:
            row["paths"] = [str(p) for p in paths]
        if note:
            row["note"] = str(note)

        faults = connections.row_problems(row)
        blocking = [f for f in faults if f.blocking]
        if blocking:
            return "REFUSED: " + "; ".join(str(f) for f in blocking) + ". Nothing was written."
        try:
            written = connections.write(row)
        except ValueError as exc:
            return f"REFUSED: {exc} Nothing was written."

        how = f" with key {row['key']}" if kind == connections.SSH else ""
        lines = [f"{message}{how}; wrote {conn_id} to {written}."]
        if found:
            lines.append("Read off the machine: " + "   ".join(f"{k} {v}" for k, v in found.items()))
        for fault in faults:
            lines.append(f"Worth telling the owner: {fault}")
        try:
            alias = connections.write_ssh_alias(row)
        except OSError as exc:
            lines.append(f"ssh alias not written ({exc}); transfers will need the address by hand.")
        else:
            if alias:
                lines.append(f"ssh alias '{alias}' written to ~/.ssh/config, so rsync/scp reach it by id.")
        lines.append(f"Now hand '{conn_id}' to ops_declare as 'connection'.")
        return "\n".join(lines)
