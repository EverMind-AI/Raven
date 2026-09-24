"""The round's own facts, written into a role's prompt before the round is dispatched.

A third class of placeholder, beside the two that already exist, and filled at a
third time. ``${params.x}`` is filled when the playbook loads; ``{{dep.output}}``
and ``{{ref:path}}`` are filled by the graph runner when the node actually runs;
``{{round.*}}`` is filled here, when a round is compiled into a graph.

**Filled here rather than added to the runtime grammar**, which would have been
the shorter patch. That grammar is read by every node of every graph and every
spawn in the process; a fourth placeholder kind in it is a change to all of
them, to serve a caller that already knows every one of these answers before it
submits anything. An unknown ``{{...}}`` body is ordinary text to the runtime
grammar, so the two never collide.

What deliberately stays a runtime placeholder is ``{{ref:...}}``: a role's
standing orders are a file in the project, and the graph should carry the
reference rather than the prose. A round that pasted every rule into every
prompt is how the reply ceiling was hit the first time.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from raven.i18n import prompt, t
from raven.playbook.stint_spec import MemoryEntry, RoleEntry
from raven.stint.journal import FIRST_ROUND, journal_tail
from raven.stint.ownership import expand
from raven.stint.verify import CheckResult, render_checks

__all__ = [
    "ROUND_SLOTS",
    "fill_round_slots",
    "finish_section",
    "guard_section",
    "where_section",
    "journal_window",
    "questions_section",
    "render",
    "round_slots",
    "verify_table",
]

ROUND_SLOTS = ("index", "journal", "verify", "guard", "finish")

_ROUND_SLOT = re.compile(r"\{\{round\.(\w+)\}\}")
_TEMPLATE_SLOT = re.compile(r"\{\{(\w+)\}\}")

NOTHING_OWNED = "- (nothing: you write nothing this round, and that is the instruction)"
NOTHING_APPENDED = "- (nothing)"
NOTHING_READ = "- (nothing named: read what the paths above and your task point you at)"

ENFORCE_WRITE_HARD = (
    "This is checked, not requested. When you stop, what you touched is compared with the list "
    "above and anything outside it is undone -- a copy is kept where a person can read it, and "
    "the round records that you went outside."
)
ENFORCE_WRITE_SOFT = (
    "This is stated, not checked. Nothing undoes a write outside the list above, which is exactly "
    "why staying inside it is on you."
)
ENFORCE_READ_HARD = " Reading outside the paths above is refused at the tool gate."

READS_LEAD_SOFT = (
    "Where to start reading. This is not a fence -- you may open anything in the tree -- it is what "
    "the round expects you to need, and it says nothing about what you may write:"
)
READS_LEAD_HARD = "The only paths you may read. Reaching outside them is refused before the read happens:"

WHERE_CHECKOUT = (
    "You are working in {workdir}. That directory is a checkout of the project made for this "
    "stint, and for this round it *is* the project -- another copy of it elsewhere on this "
    "machine is not where your work goes, and not where the state you are reading lives. Your "
    "commits are collected on a branch of its own and handed back when the stint ends."
)
WHERE_IN_PLACE = "You are working in {workdir}, which is the project itself rather than a checkout of it."

NO_VERIFY_YET = "(nothing yet: no checks have run)"
STILL_UNANSWERED = "still unanswered"

ENDING_EARLY = (
    "You are the last role of this round, so what you write is what the stint reads when it decides "
    "whether to open another one. If nothing is left that is worth another round, write {marker} on "
    "a line of its own and the stint ends with this round. That is the only thing that ends it early: "
    "without those words it keeps going until its rounds are spent."
)


class UnknownRoundSlotError(KeyError):
    """A template asked for a ``{{round.x}}`` this module does not fill."""


def render(template_name: str, **slots: str) -> str:
    """One of this module's templates with its slots filled.

    Refuses a slot the caller did not fill rather than leaving ``{{name}}`` in
    the text: an unfilled slot reaches a model as literal braces, which reads as
    an instruction nobody finished writing.
    """
    text = prompt(template_name)
    missing = sorted({name for name in _TEMPLATE_SLOT.findall(text) if name not in slots})
    if missing:
        raise KeyError(f"{template_name}: unfilled slot(s) {', '.join(missing)}")
    return _TEMPLATE_SLOT.sub(lambda match: slots[match.group(1)], text).strip()


def questions_section(questions: Sequence[Mapping[str, Any]]) -> str:
    """What a person was asked and what came back, for the next round to read.

    Carried with the journal rather than in a section of its own because it is
    the same kind of thing -- something a previous round left for this one, in
    the place a role is already told to look. An unanswered one travels too: a
    role that asked and heard nothing should not ask again as if it never had.
    """
    if not questions:
        return ""
    lines: list[str] = []
    for question in questions:
        answer = str(question.get("answer") or "").strip()
        lines.append(f"- round {question.get('round')}, {question.get('role')}: {question.get('text')}")
        lines.append(f"  - answer: {answer}" if answer else f"  - {t(STILL_UNANSWERED)}")
    return render("stint_question", questions="\n".join(lines))


def _lines(patterns: Sequence[str], empty: str, index: int) -> str:
    """The patterns as the enforcing pass will read them, not as they were written.

    ``{NN}`` is expanded here for the same reason the section exists at all: a
    role told it owns ``reports/brief_{NN}.md`` has to guess the round's spelling,
    and ``brief_2.md`` is not ``brief_02.md`` -- it would be undone as a stray
    write, for obeying the only instruction it was given.
    """
    if not patterns:
        return t(empty)
    return "\n".join(f"- {expand(pattern, index)}" for pattern in patterns)


def where_section(workdir: str, checkout: bool) -> str:
    """Which directory the role is standing in, and that it is the real one.

    Said because a role that runs ``cat .git`` in a worktree reads
    ``gitdir: .../worktrees/tree5``, correctly concludes it is not in the
    original, and goes looking for it -- which is a round spent crawling the
    machine for a project it was already inside.
    """
    if not workdir:
        return ""
    return t(WHERE_CHECKOUT if checkout else WHERE_IN_PLACE).format(workdir=workdir)


def finish_section(marker: str, terminal: bool) -> str:
    """The word that ends the stint early, said to the role that can say it.

    A stint stops on ``stop.until`` appearing in the round's summary, and what
    reaches that summary is the output of the nodes nothing depends on. So the
    marker is worth telling exactly those roles: a playbook can declare one and
    have every round run to the budget anyway, because the word was declared to
    the machine and never to anybody who could write it.

    Not said to a role something else waits on, whose output the check never
    sees. Telling it the word would be asking it to write something nobody reads
    and then leaving it to conclude, round after round, that it was ignored.
    """
    if not marker or not terminal:
        return ""
    return t(ENDING_EARLY).format(marker=marker)


def guard_section(role: RoleEntry, index: int, where: str = "") -> str:
    """What this role owns, said to the role, from the same declaration that enforces it.

    Written once in the playbook and read twice -- here, and by the pass that
    undoes a stray write. A boundary a role was never told about is a trap, and
    one told in different words from the one enforced is worse.

    ``reads`` needs its own lead-in rather than a third sentence in the shape of
    the two above it. Owned, append-only and then a third list reads as a third
    grade of permission, and ``reads`` is not one: unless ``enforce.read`` is
    hard it points, and a role that took it for a fence would stop opening the
    file it needed.

    The hard branch is written and unreachable: nothing gates a read, so
    ``validate._read_fences_nobody_holds`` refuses the grade at load. The wording
    stays for whoever wires the gate, and that refusal is what keeps it from
    being read out to a role in the meantime.
    """
    enforcement = t(ENFORCE_WRITE_HARD if role.enforce.write == "hard" else ENFORCE_WRITE_SOFT)
    if role.enforce.read == "hard":
        enforcement += t(ENFORCE_READ_HARD)
    return render(
        "stint_guard",
        where=where,
        reads_lead=t(READS_LEAD_HARD if role.enforce.read == "hard" else READS_LEAD_SOFT),
        owns=_lines(role.owns, NOTHING_OWNED, index),
        appends=_lines(role.appends, NOTHING_APPENDED, index),
        reads=_lines(role.reads, NOTHING_READ, index),
        enforcement=enforcement,
    )


def journal_window(text: str, entry: MemoryEntry | None) -> str:
    """The part of a carried file that reaches the next prompt.

    Two gates, and both matter for different files: a journal of forty short
    rounds is cut by the round count, one of three enormous rounds by the
    character count.
    """
    if not (text or "").strip():
        return t(FIRST_ROUND)
    rounds = entry.recent_rounds if entry is not None else None
    limit = entry.max_chars if entry is not None else None
    windowed = journal_tail(text, rounds) if rounds is not None else journal_tail(text)
    if limit is not None and len(windowed) > limit:
        # Cut from the front: the rounds nearest this one are the ones the next
        # role needs, and a note says so rather than letting the cut read as the
        # journal having started there.
        windowed = t("(earlier rounds trimmed to fit)") + "\n\n" + windowed[-limit:]
    return windowed


def verify_table(results: Sequence[Mapping[str, object]]) -> str:
    """Last round's checks as the table a role reads, from what the stint recorded."""
    if not results:
        return t(NO_VERIFY_YET)
    return render_checks([CheckResult.from_dict(entry) for entry in results])


def round_slots(
    role: RoleEntry,
    *,
    index: int,
    journal: str = "",
    memory: MemoryEntry | None = None,
    verify: Sequence[Mapping[str, object]] = (),
    where: str = "",
    finish: str = "",
) -> dict[str, str]:
    """Every ``{{round.*}}`` value for one role's step of one round."""
    return {
        "index": str(index),
        "journal": journal_window(journal, memory),
        "verify": verify_table(verify),
        "guard": guard_section(role, index, where),
        "finish": finish,
    }


def fill_round_slots(template: str, slots: Mapping[str, str]) -> str:
    """Substitute ``{{round.x}}`` and leave every other placeholder alone.

    An unknown one is refused rather than left in place: every other kind of
    placeholder here belongs to a grammar that resolves later, but ``round.``
    is claimed by this module, so a body under it that nothing fills is a
    misspelling that would otherwise reach a model as literal text.
    """
    unknown = sorted({name for name in _ROUND_SLOT.findall(template) if name not in slots})
    if unknown:
        raise UnknownRoundSlotError(f"no such round slot(s): {', '.join('round.' + name for name in unknown)}")
    return _ROUND_SLOT.sub(lambda match: slots[match.group(1)], template)
