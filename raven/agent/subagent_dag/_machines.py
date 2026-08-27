"""Whether a node's agent has a machine to run on, asked before anything is spawned.

The graph the main agent writes for "train me a model and watch it" is
``coding -> on-call``, and the on-call half cannot start without a machine to run
on. Nothing checked that, so the shape of the failure was: the coding node runs
to completion, the on-call node is dispatched, and only then -- minutes in, with
the code already written -- does the registry turn out to be empty. This asks
first, next to the capability checks, for the reason ``_capabilities`` gives: a
rejected graph must cost zero sub-agent runs.

The machine also belongs *before* the coding node and not only in front of the
on-call one. What the training script may do is decided by the box it will run
on -- how much device memory there is decides the batch size and whether
gradient checkpointing has to be on, and one submission that left it off took
78 GiB and died. Code written before the machine is known is written for an
imagined machine. Refusing the graph is what puts that choice back in front of
the owner, who is still sitting there, having just typed the task.

Two decisions carry this file.

**The agent's own installation answers.** Not a copy of the schema kept here:
this repo has no ``raven/ops`` at all, so a second reader would be a second
definition of what a machine is, and the two would drift the way ``name`` and
``display_name`` already did. The sub-agent's checkout is asked instead, through
the same command an owner runs, and its answer is whatever its own reader says.
It is asked with no ``--config``, so it resolves the owner's registry in the
host's raven home -- the one file ``raven ops connection add`` writes, rather
than any per-install copy of it.

**Refuse on contradiction, never on uncertainty.** A refusal needs the doctor to
have run, returned the JSON it promises, and said no machine is usable. A venv
that is not built, a command that does not exist because that agent has nothing
to do with machines, a timeout, output that will not parse -- none of those are
evidence of anything, and each leaves the graph exactly as it was. The check can
only ever add a refusal it can name.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass

from loguru import logger

# Long enough for a cold interpreter on a laptop, short enough that a wedged
# venv does not hold up the turn the owner is waiting in.
_TIMEOUT_S = 20.0


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


def _raven_in(agent: str) -> str | None:
    """The ``raven`` this agent runs as, or None when it is not a vendored one."""
    try:
        from raven.agent.subagent.vendored_agents import checkout_of, vendored_folder

        folder = vendored_folder(agent)
        checkout = checkout_of(folder) if folder else None
    except Exception as exc:  # noqa: BLE001 -- a roster that cannot be read refuses nothing
        logger.debug("machines: cannot locate {}: {}", agent, exc)
        return None
    if checkout is None:
        return None
    binary = checkout / ".venv" / "bin" / "raven"
    return str(binary) if binary.is_file() else None


def ask(agent: str, *, timeout: float = _TIMEOUT_S) -> Verdict | None:
    """What this agent's own installation says about its machines, or None.

    None means nothing was learned -- no vendored checkout, no venv, no such
    command, a timeout, or output that would not parse. Every one of those is a
    reason to leave the graph alone rather than to refuse it.
    """
    binary = _raven_in(agent)
    if binary is None:
        return None
    # Deliberately without --config: that resolves the registry beside the host's
    # own config, which is the owner's list and the one `connection add` writes.
    # RAVEN_CONNECTIONS is cleared for the same reason -- a launcher may have
    # pointed this process at some other instance's file.
    env = {k: v for k, v in os.environ.items() if k != "RAVEN_CONNECTIONS"}
    try:
        done = subprocess.run(
            [binary, "ops", "connection", "doctor", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("machines: {} could not be asked: {}", agent, exc)
        return None
    try:
        payload = json.loads(done.stdout)
        return Verdict(
            agent=agent,
            usable=int(payload["usable"]),
            listed=int(payload["listed"]),
            blocking=tuple(str(line) for line in payload.get("blocking", [])),
            machines=tuple(m for m in payload.get("machines", []) if isinstance(m, dict)),
        )
    except (ValueError, KeyError, TypeError):
        logger.debug("machines: {} answered nothing this understands", agent)
        return None


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


def verdict_for(agents: list[str], *, timeout: float = _TIMEOUT_S) -> Verdict | None:
    """What the first machine-running agent in this graph reports, or None.

    One answer is enough. A graph with two such agents would be asking the owner
    the same question twice, and the registry they read is the same file.
    """
    for agent in dict.fromkeys(agents):
        if (verdict := ask(agent, timeout=timeout)) is not None:
            return verdict
    return None


async def ask_async(agent: str, *, timeout: float = _TIMEOUT_S) -> Verdict | None:
    """Async wrapper of :func:`ask` that runs the subprocess off the event loop."""
    return await asyncio.to_thread(ask, agent, timeout=timeout)


async def verdict_for_async(agents: list[str], *, timeout: float = _TIMEOUT_S) -> Verdict | None:
    """Async wrapper of :func:`verdict_for` so an async caller pays no stalls.

    ``ask`` shells out to a vendored agent's cold interpreter and can block for
    the full 20 s timeout. Running it in a thread matches the pattern
    ``login_shell_env`` uses in every other transport and keeps the event loop
    free for concurrent turns.
    """
    for agent in dict.fromkeys(agents):
        if (verdict := await ask_async(agent, timeout=timeout)) is not None:
            return verdict
    return None


def machineless(agents: list[str], *, timeout: float = _TIMEOUT_S) -> Verdict | None:
    """The first agent in the graph that has nowhere to run, or None."""
    verdict = verdict_for(agents, timeout=timeout)
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
