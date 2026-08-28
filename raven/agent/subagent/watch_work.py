"""Is this request work to run and watch, and does this roster hold the agent for it?

The judgement and its delivery are ported from the on-call agent's own
``raven/ops/watched.py``, where both were measured in. The finding that decides
the shape (2026-08-19, three runs, same task): a request naming a solver on this
very computer, a budget, and a shared machine was read correctly every time --
and iteration 1 still went straight to a local shell. The fact placed at the top
of the turn changed nothing; the roster entry saying the on-call agent covers
local work changed nothing (re-measured 2026-08-27 on this host's loop). The one
channel measured to change the next move is a line arriving *in a tool result*
at the moment of looking.

The host's version of the question is one step earlier than the fork's. There it
steers a loop that already is the on-call agent toward ``ops_declare``; here it
steers the main agent, which is one ``spawn`` away from an agent that keeps a
ledger, toward that spawn. What stays identical is the split: something other
than the acting model decides (a one-call judgement with the session's own
model), and the decision lands where a failure would.

Errors stay asymmetric on purpose. Judged not-watched when it was: no line, and
the loop behaves as it does today. Judged watched when it was not: one line that
does not apply, refusing nothing. That is what makes a wrong answer affordable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_PROMPT = """\
Decide whether this request is work to run and watch, and name the paths it gives.

Work to run and watch: the owner wants something executed and reported back, and
getting an answer takes more than one go -- a solver case, a training run, a
parameter sweep. It usually carries a budget, a limit, a "tell me when it's
done", or a note that the machine is shared. Where the work sits is irrelevant:
it may be a directory on this very computer.

Not that: reading a file, answering a question, writing or fixing code, running
one command and looking at the output, ordinary conversation.

Reply with JSON only: {"watched": true|false, "paths": ["..."]}
"paths": every filesystem path the request names, absolute where it gave one.
Empty list if it names none.

The request:
---
%s
---"""

_MAX_CHARS = 4000


@dataclass
class Verdict:
    """One turn's answer, plus the paths it named."""

    watched: bool = False
    paths: list[str] = field(default_factory=list)

    def claims(self, path: str) -> bool:
        """Whether ``path`` is one of the named paths, or sits under one."""
        if not self.watched or not path:
            return False
        try:
            target = Path(path).expanduser()
        except (OSError, ValueError):
            return False
        for named in self.paths:
            try:
                root = Path(str(named)).expanduser()
            except (OSError, ValueError):
                continue
            if target == root or root in target.parents:
                return True
        return False


def build_prompt(message: str) -> list[dict[str, str]]:
    """Messages for the one call this makes, with the session's own model."""
    return [{"role": "user", "content": _PROMPT % (message or "")[:_MAX_CHARS]}]


def read_verdict(text: str | None) -> Verdict:
    """Parse the reply. Anything unreadable is "not watched", never an exception.

    A judgement that cannot be read has to leave the turn exactly as it would
    have been without it -- this sits in front of every path-touching tool call,
    and a parse error there would break looking at files.
    """
    raw = (text or "").strip()
    if not raw:
        return Verdict()
    if "{" in raw:
        raw = raw[raw.index("{") : raw.rindex("}") + 1] if "}" in raw else raw
    try:
        data = json.loads(raw)
    except ValueError:
        return Verdict()
    if not isinstance(data, dict):
        return Verdict()
    paths = data.get("paths")
    # Strictly the boolean, or the word. bool() would read any non-empty string
    # as yes, so a reply of {"watched": "unsure"} would come back as a firm yes.
    flag = data.get("watched")
    said_yes = flag is True or (isinstance(flag, str) and flag.strip().lower() == "true")
    return Verdict(
        watched=said_yes,
        paths=[str(p) for p in paths if str(p).strip()] if isinstance(paths, list) else [],
    )


def asked_for(messages: list[dict]) -> str:
    """The last thing the owner said, without the runtime metadata glued to it.

    The metadata block is prepended to the user content by the assembler, and it
    is separated from the message by a blank line -- so a request that begins
    with it is split there and the rest kept.
    """
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        text = str(content or "")
        if text.startswith("[Runtime Context") and "\n\n" in text:
            text = text.split("\n\n", 1)[1]
        return text.strip()
    return ""


async def oncall_agent(names: list[str]) -> str | None:
    """The first agent on this roster that runs work on machines, or None.

    Answered by the manifest flag the DAG check keys on (``runsOnMachines``),
    not by the registry's contents: an empty registry still names the agent --
    the spawn-side check is what refuses it, and that refusal is how the owner
    gets asked for a machine at all. None means no such agent, or nothing could
    be established, which must change nothing.
    """
    from raven.agent.subagent.dag_machines import runs_on_machines

    for name in dict.fromkeys(names):
        if runs_on_machines(name):
            return name
    return None


def nudge(agent: str) -> str:
    """What a tool result gains when a look landed on a path the owner asked about.

    Both shapes are named for the reason the fork's line names them (measured
    2026-08-21: a watch task steered with run-only words built its own monitor
    out of write_file and cron -- the mechanism was fine, the loop did not
    recognise itself in the sign).
    """
    return (
        "\n\nThis came from what the owner asked for, and what they asked for is work to "
        "stay with over time rather than a look you take once. That belongs with the "
        f"on-call specialist: spawn `{agent}` now, before running anything by hand, and "
        "hand it the owner's words -- the goal, the budget, every constraint, and this "
        "path -- rather than your summary of them. It keeps a ledger, meters the budget, "
        "wakes itself when results land, and judges each round; trials you run here "
        "leave no record it can use. This applies whether the work is something to RUN "
        "(a solver case, a training run, a sweep) or something to WATCH that you do not "
        "run (a disk, a queue, somebody else's job)."
    )


def machineless_nudge(agent: str) -> str:
    """The line after a spawn was refused for having no machine.

    Sharper than :func:`nudge` on purpose, and it exists because the plain one
    went quiet at the wrong moment: the first live run (2026-08-27) spawned, was
    refused, and went straight to running trials by hand -- reading the owner's
    "I may be slow to answer, don't sit idle" as licence to skip the asking
    altogether. Slow-to-answer is about waiting, not about whether to ask: the
    question costs one message, and running anyway spends the budget on trials
    that leave no record and pre-empts the machine choice the refusal exists to
    put in front of the owner.
    """
    return (
        "\n\nSTOP running this by hand. Your spawn of the on-call specialist was refused "
        "because no machine is registered -- that refusal is the owner's own rule, and "
        "it applies to hand-run trials exactly as much as to dispatched ones. The next "
        "step is a message to the owner, now, asking for the machine: what they call it, "
        "ssh or this very computer, address/port/user/key if ssh, what is installed with "
        "paths, budget unit, and how many jobs at once. Then "
        "`raven ops connection add` records it and the spawn goes through. Send that "
        f"message and end your turn; do not run trials while `{agent}` has nowhere to "
        'record them. "The owner may be slow to answer" is about waiting, not asking '
        "-- ask first, report what you are blocked on, and stop."
    )


@dataclass
class TurnWatch:
    """One turn's watch state, owned by the turn and never by the loop object.

    The AgentLoop is a singleton and run_turn lets turns from other sessions run
    concurrently, so anything stored on the loop is shared: session B's dispatch
    would silence session A's nudge, and B's verdict would answer for A's
    request. Created as a local in _run_agent_loop and passed in, this dies with
    its turn.
    """

    verdict: Verdict | None = None
    dispatched: bool = False
    machineless: bool = False


def handed_over(name: str, args: dict, result: str, agent: str) -> bool:
    """Whether this call really put the work with ``agent``.

    Only a real hand-off may silence the turn's nudges. The first cut treated
    every spawn as one: an unrelated spawn of another agent silenced the next
    path nudge, and every refusal shape except the machineless one -- delegation
    paused, a DAG validation error, a declined graph -- read as success. Judged
    on the acceptance line the manager actually returns, and on the on-call
    agent being the one dispatched; anything unrecognised keeps the nudges
    alive, which is the affordable direction.
    """
    from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX

    text = str(result)
    if "Nothing was dispatched" in text or text.startswith(SPAWN_REFUSED_PREFIX) or text.lstrip().startswith("Error"):
        return False
    if name == "spawn":
        return str(args.get("subagent") or "") == agent and "started (id:" in text
    # The DAG leg reads the graph's own node fields, not the serialized call: a
    # prompt that merely mentions the agent's name is not a dispatch to it. And
    # it requires the tool's acceptance shape -- a declined graph ("The user did
    # not approve this graph, so nothing was run") carries no error prefix, so
    # the generic checks above let it through (reproduced in review).
    nodes = args.get("nodes")
    if not isinstance(nodes, list) or not any(
        isinstance(n, dict) and str(n.get("subagent") or "") == agent for n in nodes
    ):
        return False
    return "started in the background" in text or "-- outputs in " in text
