"""Is this request work to run and watch, and which subjects does it name?

The question a loop cannot answer for itself. Measured three times on 2026-08-19
with the same task -- a solver on this computer, with a budget, a "tell me when
you're done", and a machine the owner said they were also using -- and three
times iteration 1 went straight to a local shell. The reasoning showed the
statement read correctly each time, budget and caveats and all; what never
appeared was the thought that this belonged on a machine and in a ledger. Once,
after the machine list was put in front of it, it even wrote "It's on
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
Decide whether this request is work to run and watch, and name the subjects it gives.

Work to run and watch: the owner wants something executed and reported back, and
getting an answer takes more than one go -- a solver case, a training run, a
parameter sweep. It usually carries a budget, a limit, a "tell me when it's
done", or a note that the machine is shared. Where the work sits is irrelevant:
it may be a directory on this very computer.

Not that: reading a file, answering a question, writing or fixing code, running
one command and looking at the output, ordinary conversation.

Reply with JSON only: {"watched": true|false, "subjects": ["..."]}
"subjects": every concrete thing the request names to watch or act on -- a
filesystem path (absolute where it gave one), a URL, a repository or project
identifier, an account handle. Empty list if it names none.

The request:
---
%s
---"""

_MAX_CHARS = 4000


@dataclass
class Verdict:
    """One turn's answer, plus the subjects it named."""

    watched: bool = False
    subjects: list[str] = field(default_factory=list)

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

    A judgement that cannot be read has to leave the turn exactly as it would have
    been without it -- this sits in front of every path-touching tool call, and a
    parse error there would break looking at files.
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
    # Strictly the boolean, or the word. bool() would read any non-empty string as
    # yes, so a reply of {"watched": "unsure"} would come back as a firm yes.
    flag = data.get("watched")
    said_yes = flag is True or (isinstance(flag, str) and flag.strip().lower() == "true")
    return Verdict(
        watched=said_yes,
        subjects=[str(s) for s in subjects if str(s).strip()] if isinstance(subjects, list) else [],
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
