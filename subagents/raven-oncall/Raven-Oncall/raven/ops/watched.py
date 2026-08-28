"""Is this request work to run and watch, and which paths does it name?

The question a loop cannot answer for itself. Measured three times on 2026-08-19
with the same task -- a solver on this computer, with a budget, a "tell me when
you're done", and a machine the owner said they were also using -- and three
times iteration 1 went straight to a local shell. The reasoning showed the
statement read correctly each time, budget and caveats and all; what never
appeared was the thought that this belonged on a machine and in a ledger. Once,
after the machine list was put in front of it, it even wrote "It's on 我的笔记本
(conn_this_mac) - this machine" and then ran the solver by hand anyway.

The same task with a path on a different box goes through the whole loop --
because looking at that path FAILS, and the failure arrives in a tool result at
the moment of looking. That is the only channel measured to change what it does
next: the same fact placed at the top of the turn changed nothing.

So two pieces. Something other than the acting model decides (here), and the
decision is delivered where a failure would be (in the tool result).

Errors are asymmetric on purpose. Judged not-watched when it was: no line, and
the loop behaves as it does today. Judged watched when it was not: one line that
does not apply. Neither refuses anything, which is what makes a wrong answer
affordable.
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

    A judgement that cannot be read has to leave the turn exactly as it would have
    been without it -- this sits in front of every path-touching tool call, and a
    parse error there would break looking at files.
    """
    raw = (text or "").strip()
    if not raw:
        return Verdict()
    if "{" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1] if "}" in raw else raw
    try:
        data = json.loads(raw)
    except ValueError:
        return Verdict()
    if not isinstance(data, dict):
        return Verdict()
    paths = data.get("paths")
    # Strictly the boolean, or the word. bool() would read any non-empty string as
    # yes, so a reply of {"watched": "unsure"} would come back as a firm yes.
    flag = data.get("watched")
    said_yes = flag is True or (isinstance(flag, str) and flag.strip().lower() == "true")
    return Verdict(
        watched=said_yes,
        paths=[str(p) for p in paths if str(p).strip()] if isinstance(paths, list) else [],
    )


def provenance_line() -> str:
    """What a tool adds to its result when it touched one of those paths.

    Two shapes, because the line used to name only one of them -- "ops_submit runs
    a round against it", "a working directory per round" -- and a campaign that
    watches something runs no rounds and needs no directory. Measured 2026-08-21
    on a watch task: everything the loop was told about the on-call path described
    an experiment, and it built its own monitor out of write_file and cron instead.
    Nothing was wrong with the mechanism; it did not recognise itself in the sign.
    """
    return (
        "\n\nThis came from what the owner asked for, and what they asked for is work to "
        "stay with over time rather than a look you take once. That belongs on a machine "
        "and in a ledger, and ops_declare records it once, costs nothing, and runs "
        "nothing:\n"
        "  something to RUN and watch -- ops_declare then ops_submit, which gives it a "
        "budget that is counted, a working directory per round, and a wake when the "
        "result lands.\n"
        "  something to WATCH that you do not run -- a price, a disk, a queue, somebody "
        "else's job -- ops_declare with objective_kind='condition' and a readings table "
        "saying what to read and when. The starting value is taken as you declare it, "
        "which is the one thing a later wake cannot reconstruct, and the budget can be "
        "counted in looks rather than machine time.\n"
        "Either way ops_connections shows the machines, and coming back is "
        "ops_check_later rather than a cron job you keep yourself: a wake through the "
        "campaign carries the record with it."
    )
