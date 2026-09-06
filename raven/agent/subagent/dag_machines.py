"""Whether a node's agent has a machine to run on, asked before anything is spawned.

The graph the main agent writes for "train me a model and watch it" is
``coding -> on-call``, and the on-call half cannot start without a machine to run
on. Nothing checked that, so the shape of the failure was: the coding node runs
to completion, the on-call node is dispatched, and only then -- minutes in, with
the code already written -- does the registry turn out to be empty. This asks
first, next to the capability checks, for the reason ``dag_capabilities`` gives: a
rejected graph must cost zero sub-agent runs.

The machine also belongs *before* the coding node and not only in front of the
on-call one. What the training script may do is decided by the box it will run
on -- how much device memory there is decides the batch size and whether
gradient checkpointing has to be on, and one submission that left it off took
78 GiB and died. Code written before the machine is known is written for an
imagined machine. Refusing the graph is what puts that choice back in front of
the owner, who is still sitting there, having just typed the task.

Two decisions carry this file.

**The host's own registry reader answers.** This used to shell out to the
sub-agent checkout's binary, because this repo had no ``raven/ops`` and a second
reader would have been a second definition of what a machine is. The registry
has since been lifted into the host (``raven.ops.connections``), which makes the
host the definition; the subprocess collapsed into one bit -- "does this agent
run work on the owner's machines" -- and that bit is now declared in the
manifest (``runsOnMachines``) rather than probed off a venv at 20 s a call.

**Refuse on contradiction, never on uncertainty.** A refusal needs a reader to
have run and said no machine is usable. An agent whose manifest does not claim
machines, a folder that cannot be found, a manifest that will not parse -- none
of those are evidence of anything, and each leaves the graph exactly as it was.
The check can only ever add a refusal it can name.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from loguru import logger


@dataclass(frozen=True)
class Verdict:
    """One agent's registry, as its own installation reports it."""

    agent: str
    usable: int
    listed: int
    blocking: tuple[str, ...] = ()
    machines: tuple[dict, ...] = ()
    """Each usable machine as its own installation projects it for an agent:
    what it is, what is installed, how its budget is metered. Never an address
    and never a credential -- that split is enforced on the far side, and the
    reader here may be a node writing a training script."""

    def machine(self, machine_id: str) -> dict | None:
        return next((m for m in self.machines if str(m.get("id")) == machine_id), None)


def _config_rows() -> list[dict]:
    """The editable roster rows; module-level so tests pin the roster."""
    from raven.config.update_subagents import get_agents

    return get_agents()


def runs_on_machines(agent: str) -> bool:
    """Whether this agent's roster row declares it runs work on the owner's machines.

    The applied config rows are the first truth -- the same rows a migrated
    product registers with, so the flag survives its vendored folder's
    retirement. The folder's own ``subagent.json`` stays the fallback for a
    discovered seed no config row names yet: the agents-door truth, config
    plus discovered seeds. A named row is authoritative either way, and a
    roster that cannot be read refuses nothing -- a flag misread as False
    only silences a check, never breaks a dispatch.
    """
    try:
        for row in _config_rows():
            if row.get("name") == agent:
                return bool(row.get("runsOnMachines"))
    except Exception as exc:  # noqa: BLE001 -- a roster that cannot be read refuses nothing
        logger.debug("machines: cannot read config roster for {}: {}", agent, exc)
    try:
        from raven.agent.subagent.vendored_agents import product_folder

        folder = product_folder(agent)
        if folder is None:
            return False
        entry = json.loads((folder / "subagent.json").read_text(encoding="utf-8"))
        return bool(entry.get("runsOnMachines") or entry.get("runs_on_machines"))
    except Exception as exc:  # noqa: BLE001 -- a roster that cannot be read refuses nothing
        logger.debug("machines: cannot read manifest for {}: {}", agent, exc)
        return False


def ask(agent: str) -> Verdict | None:
    """The owner's registry, for an agent that runs work on machines, or None.

    None means the question does not apply -- the agent's manifest does not
    claim machines, or could not be read. Per the module contract that must
    leave the graph exactly as it was.
    """
    if not runs_on_machines(agent):
        return None
    from raven.ops.connections import problems, read, shown, usable

    found = read()
    fit = usable(found.rows)
    return Verdict(
        agent=agent,
        usable=len(fit),
        listed=len(found.rows),
        blocking=tuple(str(f) for f in problems() if f.blocking),
        machines=tuple(shown(r) for r in fit),
    )


def refusal(verdict: Verdict) -> str:
    """What to say to a graph whose agent has nowhere to run its work.

    Addressed at the main agent, which is talking to the owner: the owner typed
    the task a moment ago and is the only one who can answer any of this. The
    fields are named because the loop cannot work them out -- the address and the
    key are deliberately outside what any of it can see.
    """
    head = f"'{verdict.agent}' runs work on the owner's machines, and this installation has none it can use"
    if verdict.listed:
        head += f" ({verdict.listed} listed, none usable)"
    lines = "".join(f"\n  - {line}" for line in verdict.blocking)
    return (
        f"{head}.{lines}\n\n"
        "Nothing was dispatched. Ask the owner for a machine before running any of "
        "this -- the work has to be built for the box it will run on, not fitted to "
        "one afterwards. They will need to say:\n"
        "  - what they call the machine\n"
        "  - whether it is reached over ssh, or is this very computer\n"
        "  - for ssh: address, port, username, and the path to the private key\n"
        "  - what is installed on it, with paths\n"
        "  - whether its budget is counted in minutes, core-minutes or gpu-minutes\n"
        "  - how many jobs it will run at once\n"
        "Then `raven ops connection add` writes it down, after connecting to it. "
        "Re-submit this graph once they have."
    )


def verdict_for(agents: list[str]) -> Verdict | None:
    """What the first machine-running agent in this graph reports, or None.

    One answer is enough. A graph with two such agents would be asking the owner
    the same question twice, and the registry they read is the same file.
    """
    for agent in dict.fromkeys(agents):
        if (verdict := ask(agent)) is not None:
            return verdict
    return None


async def ask_async(agent: str) -> Verdict | None:
    """Async wrapper of :func:`ask` that keeps the file reads off the event loop."""
    return await asyncio.to_thread(ask, agent)


async def verdict_for_async(agents: list[str]) -> Verdict | None:
    """Async wrapper of :func:`verdict_for` so an async caller pays no stalls."""
    return await asyncio.to_thread(verdict_for, agents)


def machineless(agents: list[str]) -> Verdict | None:
    """The first agent in the graph that has nowhere to run, or None."""
    verdict = verdict_for(agents)
    return verdict if verdict is not None and verdict.usable == 0 else None


MACHINE_KEY = "machine"


def named(node: object) -> str:
    """The machine this node was given, or "" when it names none."""
    inputs = getattr(node, "inputs", None) or {}
    return str(inputs.get(MACHINE_KEY) or "").strip()


def brief(machine: dict) -> str:
    """One machine written out for a node whose work will run on it.

    Appended to the prompt rather than left for the node to ask about, for the
    reason the on-call line already measured about facts a loop needs: the same
    sentence placed where the work happens changes what gets done, and placed
    anywhere else does not.

    Sizing is the sharpest use. How much device memory there is decides the batch
    size and whether gradient checkpointing has to be on -- one submission that
    left it off took 78 GiB and died -- and which solver is installed decides
    what may be called at all. A node that writes the code before the machine is
    known writes it for an imagined one. It is not the only use: a node that
    reports afterwards has to say which box the numbers came from.
    """
    skip = {"id", "display_name"}
    facts = "\n".join(f"  {k}: {v}" for k, v in machine.items() if k not in skip and v not in (None, ""))
    return (
        f"\n\n---\nThe work in this run happens on {machine.get('display_name') or machine.get('id')} "
        f"(machine={machine.get('id')}):\n{facts}\n"
        "Whatever your part is, it is about that machine. Code written here has to run there, so "
        "what is installed on it is what may be called and what it has is what the work has to fit "
        "in; anything said about results afterwards is said about that machine. It is not reachable "
        "from here and nothing here should try -- the agent that runs the work owns the way onto it."
    )


def unnamed_machine(nodes: list, verdict: Verdict) -> str:
    """Why this graph cannot be dispatched as written, or "".

    Refused rather than guessed even when only one machine would fit. The choice
    is the owner's and the graph is where it gets recorded; a default picked here
    would be a decision nobody made appearing in a ledger as one that was.
    """
    for node in nodes:
        if getattr(node, "subagent", None) != verdict.agent:
            continue
        chosen = named(node)
        listing = "\n".join(
            f"  {m.get('display_name') or m.get('id')}   (machine={m.get('id')})   "
            + "   ".join(f"{k} {v}" for k, v in m.items() if k not in ("id", "display_name") and v not in (None, ""))
            for m in verdict.machines
        )
        if not chosen:
            return (
                f"node '{getattr(node, 'id', '?')}' runs work on one of the owner's machines and "
                f"does not say which.\n{listing}\n\n"
                f"Put the one this work calls for in that node's inputs as "
                f"'{MACHINE_KEY}', and say why you picked it. It has to be settled "
                "here rather than later: every node this one depends on is told what the "
                "machine is and writes for it, and code written before the machine is known "
                "is written for an imagined one."
            )
        if verdict.machine(chosen) is None:
            return (
                f"node '{getattr(node, 'id', '?')}' names machine '{chosen}', which is not one "
                f"this installation can use.\n{listing}"
            )
    return ""


def with_facts(nodes: list, verdict: Verdict) -> list:
    """``nodes`` with the graph's machine written into every one of them.

    Everyone, not the nodes upstream of the work. Which node needs to know is not
    something this code gets to see: the one writing the training script needs
    the device memory, and the one writing the report afterwards needs to say
    which box the numbers came from. An earlier version handed it only to the
    transitive dependencies of the machine-using node, which is a guess about the
    shape of a graph, dressed up as a derivation.

    So the machine is treated as what it is -- a fact about this piece of work,
    settled once and told to everybody doing it. A graph that puts work on two
    machines says so, and every node gets both, since a node that reads the wrong
    one is the failure this is here to prevent and cannot be fixed by showing it
    less.
    """
    chosen: dict[str, dict] = {}
    for node in nodes:
        if getattr(node, "subagent", None) != verdict.agent:
            continue
        if machine := verdict.machine(named(node)):
            chosen[str(machine.get("id"))] = machine
    if not chosen:
        return nodes
    footer = "".join(brief(m) for m in chosen.values())
    return [node.model_copy(update={"prompt_template": node.prompt_template + footer}) for node in nodes]
