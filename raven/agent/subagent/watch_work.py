"""Is this request work to run and watch, and does this roster hold the agent for it?

The judgement and its delivery were measured in on the on-call agent. The
finding that decides
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
import re
from dataclasses import dataclass, field
from pathlib import Path

_PROMPT = """\
Decide whether this request is work to run and watch, and name the subjects it gives.

Work to run and watch: the owner wants something executed and reported back, and
getting an answer takes more than one go -- a solver case, a training run, a
parameter sweep. It usually carries a budget, a limit, a "tell me when it's
done", or a note that the machine is shared. Where the work sits is irrelevant:
it may be a directory on this very computer.

Not that: reading a file, answering a question, writing or fixing code, running
one command and looking at the output, ordinary conversation.

Reply with JSON only: {"watched": true|false, "subjects": ["..."], "code_work": true|false}
"subjects": every concrete thing the request names to watch or act on -- a
filesystem path (absolute where it gave one), a URL, a repository or project
identifier, an account handle. Empty list if it names none.
"code_work": whether getting there requires writing or changing code as part of
the loop -- model or algorithm variants, scripts to build, "only change X.py"
rules -- as opposed to only running and tuning what already exists.

The request:
---
%s
---"""

_MAX_CHARS = 4000


@dataclass
class Verdict:
    """One turn's answer, plus the subjects it named.

    ``code_work``: the loop is driven by code changes, not only by tuning what
    exists. It decides what counts as a complete hand-off -- a bare watcher
    spawn on such a request drops the coding half (see ``solo_dispatch_note``).
    """

    watched: bool = False
    subjects: list[str] = field(default_factory=list)
    code_work: bool = False

    def claims(self, subject: str) -> bool:
        """Whether ``subject`` is one of the named subjects, or reaches one.

        Every named subject first gets the containment test the path-only
        version applied unconditionally -- relative paths included, since the
        review measured `subjects=["run"]` losing its claim on `run/job.sh`
        under a leading-slash dispatch -- and only then the anchor test.
        Anchors are what URLs, project identifiers and handles match by,
        because a look rarely repeats the subject verbatim: the owner names a
        pipeline by its web URL and the look arrives as a CLI call carrying
        the project slug percent-encoded (measured 2026-08-28, glab api
        projects/npc-work%2Faic%2F...). Matching generously is the affordable
        direction here: a false claim adds one inapplicable line and refuses
        nothing.
        """
        if not self.watched or not subject:
            return False
        try:
            target = Path(subject).expanduser()
        except (OSError, ValueError):
            target = None
        norm_target = _normalize(subject)
        for named in self.subjects:
            s = str(named)
            if target is not None:
                try:
                    root = Path(s).expanduser()
                except (OSError, ValueError):
                    root = None
                if root is not None and (target == root or root in target.parents):
                    return True
            if any(_hit(a, norm_target) for a in _anchors(s)):
                return True
        return False


def _hit(anchor: str, text: str) -> bool:
    """Whether ``anchor`` occurs in ``text`` on word boundaries.

    Bare substring containment reads ``/srv/case`` into ``/srv/case-old`` --
    a sibling with a longer name is not under the subject, and the fork's own
    path tests pin exactly that. A hit must not continue into an identifier
    character on either side; a separator or the end of the string is what
    says the object named is the object reached.
    """
    import re

    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(anchor)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text) is not None


def _normalize(text: str) -> str:
    """One comparable form for both sides: percent-decoded, schemeless, lowered.

    GitLab's ``/-/`` separator is dropped because it appears in web URLs and in
    nothing the API or CLI side says about the same object.
    """
    from urllib.parse import unquote

    out = unquote(str(text or "")).strip().lower()
    for scheme in ("https://", "http://"):
        if out.startswith(scheme):
            out = out[len(scheme) :]
    return out.replace("/-/", "/").rstrip("/")


def _anchors(named: str) -> list[str]:
    """What a look that reaches this subject must carry, in normalized form.

    The whole subject, its path without the host, and its last two segments --
    the pair like ``pipelines/2798916676`` that survives every rephrasing of
    the same object. Derived anchors shorter than 6 characters are dropped
    rather than allowed to match half the world; the whole subject only needs
    4, so a short handle like ``@bob`` -- which the prompt explicitly asks
    for -- stays claimable (the review caught 6 excluding it).
    """
    whole = _normalize(named)
    if len(whole) < 4:
        return []
    derived = []
    parts = whole.split("/")
    if len(parts) > 1:
        derived.append("/".join(parts[1:]))
        derived.append("/".join(parts[-2:]))
    return [whole] + [a for a in dict.fromkeys(derived) if a != whole and len(a) >= 6]


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
    subjects = data.get("subjects")
    if not isinstance(subjects, list):
        subjects = data.get("paths")

    # Strictly the boolean, or the word. bool() would read any non-empty string
    # as yes, so a reply of {"watched": "unsure"} would come back as a firm yes.
    def _said_yes(key: str) -> bool:
        flag = data.get(key)
        return flag is True or (isinstance(flag, str) and flag.strip().lower() == "true")

    return Verdict(
        watched=_said_yes("watched"),
        subjects=[str(s) for s in subjects if str(s).strip()] if isinstance(subjects, list) else [],
        code_work=_said_yes("code_work"),
    )


def reinjected(origin) -> bool:
    """Whether a turn of this origin is the runtime re-entering the conversation.

    A sub-agent's result relay and a sentinel notice carry no request of the
    owner's: the "last user message" of such a turn is the report the runtime
    wrote. Judging it for run-and-watch work answers a question nobody asked,
    and on 2026-09-08 it cost one relay turn 1229s -- the judgement's model call
    ran away on a 30k-char report and held the conversation for the whole of
    it. Cron and heartbeat are deliberately not here: their text is the owner's
    own instruction, written in advance.
    """
    from raven.spine import Origin

    return origin in (Origin.SENTINEL, Origin.SUBAGENT)


def asked_for(messages: list[dict], origin=None) -> str:
    """The last thing the owner said, without the runtime metadata glued to it.

    The metadata block is prepended to the user content by the assembler, and it
    is separated from the message by a blank line -- so a request that begins
    with it is split there and the rest kept.

    ``origin`` is the turn's. A re-injected turn (``reinjected``) has no request
    of the owner's to read, so it reads as none and no judgement is paid for it.
    """
    if origin is not None and reinjected(origin):
        return ""
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


def oncall_agent(agents: list) -> str | None:
    """The first row on this roster that owns run-and-watch work, or None.

    Read off the dispensed agent table, never re-derived. The declaration is
    settled once at admission, where the schema honours every spelling a roster
    row or a folder manifest has ever carried; a second reader opening config
    and a manifest for itself answers differently the moment those two disagree,
    and a legacy manifest is exactly where they did.

    Answered by the declaration rather than by anything about where the work
    would run: which machine a job lands on is the sub-agent's own business, and
    this question is only which agent the request belongs with. None means no
    such agent, which must change nothing.
    """
    for agent in agents:
        if getattr(agent, "owns_watched_work", False):
            return str(getattr(agent, "name", "") or "") or None
    return None


def preempt_owner_ask(state: "TurnWatch", args: dict) -> str:
    """The answer an owner-question earns instead of the owner, or "".

    On a turn judged to be watched work, with the on-call agent resolved and
    the hand-off not yet made, no question goes to the owner: the specialist
    reads the case itself (parameters, load forms, entry scripts live in it),
    the registry answers where things live, and anything genuinely the
    owner's travels through the specialist's own escalation door -- which
    prices a question before sending it. A pre-dispatch ask is dispatch
    avoidance wearing a question mark.

    One exemption stays open on purpose: a turn never judged watched is none
    of this function's business.

    Why so wide: the first cut matched the question against the subjects
    verbatim, and the very next headless run paraphrased ("this path" for the
    path itself) and bundled two case-answerable questions alongside --
    measured 2026-08-28, runs 2, 3 and 6 of nine. Below a certain model
    strength the ask-first instinct rephrases around any narrow filter, so
    the gate keys on the turn's judged state, not on the wording.
    """
    verdict, agent = state.verdict, state.agent
    if verdict is None or not verdict.watched or state.dispatched or not agent:
        return ""
    state.nudges += 1
    return (
        "\n\nThis question was not sent to the owner. This request is work for the "
        "on-call specialist, and every question here is answered on the way there: "
        "the case's own files carry its parameters and entry points, the owner's "
        "registry says where things live, and a question genuinely for the owner "
        "travels through the specialist's own escalation door, which prices it "
        f"first.\nThe next call is spawn `{agent}` (or `run_subagent_dag` with a coding "
        f"node feeding it, when the request includes code-level work -- one graph per "
        f"round, and you drive the rounds), handing over the owner's words whole."
    )


def nudge(agent: str, *, repeat: bool = False) -> str:
    """What a tool result gains when a look landed on a subject the owner asked about.

    Both work-shapes are named for the reason the fork's line names them (measured
    2026-08-21: a watch task steered with run-only words built its own monitor
    out of write_file and cron -- the mechanism was fine, the loop did not
    recognise itself in the sign).

    ``repeat`` switches to the hard form: the polite line was read and stepped
    past on every 2026-08-28 run --
    ten times in one of them -- and what those runs spent the reprieve on was
    local scavenging, two questions to the owner, and a read of the owner's
    ssh config. Saying the same polite thing an eleventh time is not a plan.

    The DAG door names code work "between rounds", not just "built first", and
    says the caller drives the rounds. Measured 2026-08-31, twice in a row: on a
    search task whose code changes recur every round, "built first" read as a
    one-time precondition that was already satisfied, and the loop reasoned its
    way to a single-node spawn -- honestly, since one acyclic graph cannot hold
    a loop. The sentence now matches the mechanism: one graph per round,
    instance handles carry each side's memory across rounds.
    """
    if repeat:
        return (
            "\n\nSTOP. This look landed on the owner's watched subject again, and the "
            "dispatch has already been called for once this turn. Do not probe further, "
            "do not read local config or history hunting for a way in, and do not ask "
            "the owner what the specialist can answer on its own."
            f"\nThe next call is spawn `{agent}` (or `run_subagent_dag` with a coding "
            f"node feeding it, when the request includes code-level work -- one graph "
            f"per round, and you drive the rounds), with the owner's words handed over "
            f"whole. Anything else spends the owner's budget outside the ledger."
        )
    return (
        "\n\nThis came from what the owner asked for, and what they asked for is work to "
        "stay with over time rather than a look you take once. That belongs with the "
        f"on-call specialist: spawn `{agent}` now, before running anything by hand, and "
        "hand it the owner's words -- the goal, the budget, every constraint, and this "
        "path -- rather than your summary of them. It keeps a ledger, meters the budget, "
        "wakes itself when results land, and judges each round; trials you run here "
        "leave no record it can use. This applies whether the work is something to RUN "
        "(a solver case, a training run, a sweep) or something to WATCH that you do not "
        "run (a disk, a queue, somebody else's job). When the owner's request includes "
        "code-level work -- a rig to build first, or code that changes between rounds "
        "-- do not do that work here either: dispatch `run_subagent_dag` with a coding "
        f"node feeding an `{agent}` node. One graph runs one round; the loop is yours "
        "to drive: the graph's results come back to you, and the next round's graph "
        "reuses each node's instance handle, so both sides keep what they learned."
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
    nudges: int = 0
    # The roster's on-call agent, cached by the first judgement that resolved
    # it, so a later interception can name the same door without re-probing.
    agent: str = ""
    # A bare watcher spawn landed on a code-driven request: dispatched, but
    # with the coding half of the owner's plan owned by nobody. Later looks
    # in the turn repeat the correction instead of going silent.
    solo: bool = False


def handed_over(name: str, args: dict, result: str, agent: str) -> bool:
    """Whether this call really put the work with ``agent``.

    Only a real hand-off may silence the turn's nudges. The first cut treated
    every spawn as one: an unrelated spawn of another agent silenced the next
    path nudge, and every refusal shape -- delegation paused, a DAG validation
    error, a declined graph -- read as success. Judged
    on the acceptance line the manager actually returns, and on the on-call
    agent being the one dispatched; anything unrecognised keeps the nudges
    alive, which is the affordable direction.
    """
    from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX

    text = str(result)
    if text.startswith(SPAWN_REFUSED_PREFIX) or text.lstrip().startswith("Error"):
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


_CODE_LINE = re.compile(
    r"^\s*(def |class |import |from \S+ import |function |const |let |var |#include|@|if __name__)",
    re.MULTILINE,
)


def hoarded_code_note(result: str, *, threshold: int = 6000) -> str:
    """The line a bulk-of-source result earns on its way into the main context, or "".

    Measured 2026-09-01 on the autoresearch run: the main agent, campaign
    already dispatched, sent sub-agents to fetch train.py and the tooling
    "for strategy design" -- whole files, several times, re-fetching what the
    context curator had just evicted. Thirty context rebuilds in ninety
    minutes, no experiment progress. The code detail it was hoarding belongs
    in the workspace of the node that edits it; what the main context needed
    was conclusions.

    Content-conditioned, not state-conditioned: a sub-agent return that is
    mostly source is worth this line in any turn, and the announce path that
    carries it into the context has no view of a turn's watch state anyway.
    Advisory, one paragraph, system voice -- placed with the announce's own
    instructions, outside the untrusted fence.
    """
    text = str(result)
    if len(text) < threshold:
        return ""
    sample = text[:20000]
    if len(_CODE_LINE.findall(sample)) < 8:
        return ""
    return (
        "\n\nNote: that result is mostly source code, quoted whole. Code belongs in the "
        "workspace of the node that edits it -- holding it here spends this context on "
        "detail that is not yours to act on, and re-fetching what compaction evicts "
        "grinds the session. If the code needs changing, hand a coding node the file's "
        "PATH and what to change; if you need facts about it, ask for the facts. Do not "
        "pull more file contents into this conversation."
    )


def solo_dispatch_note(agent: str, *, repeat: bool = False) -> str:
    """The line a bare watcher spawn earns on a code-driven request.

    Measured 2026-09-01, third occurrence of the shape: the whole search task
    -- goal, budget, every constraint, and an owner's rule that code changes
    and experiment runs are separate roles in a loop -- handed to the on-call
    agent as one spawn. ``handed_over`` said dispatched and every nudge went
    silent, so nothing in the system ever said the coding half now existed
    nowhere. The same build chose the graph correctly on other runs: the door
    works when taken, and which door gets taken is sampling. This line makes
    the shape mismatch cost one correction instead of the whole run.

    The spawn is not refused: the watcher half is right, and a refusal would
    throw it away. The correction names what is missing and the move that
    adds it, in the channel measured to be read (the spawn's own result).
    """
    if repeat:
        return (
            f"\n\nSTOP: this belongs to the work you handed to `{agent}` -- and the "
            "coding half of the owner's request still has no owner. Do not run or "
            "edit it here. Dispatch `run_subagent_dag` with a coding node feeding "
            f"an `{agent}` node; the running spawn's results fold in as round one."
        )
    return (
        "\n\nThe owner's request is driven by code changes between rounds, and this "
        f"spawn carries only the watcher: `{agent}` runs and judges trials, it does "
        "not redesign the code -- the owner split those roles on purpose, and the "
        "coding half now exists nowhere. Drive the loop as graphs instead: "
        f"`run_subagent_dag` with a coding node feeding an `{agent}` node, one graph "
        "per round, you drive the rounds. Fold this spawn's results in as round one "
        "rather than abandoning it."
    )
