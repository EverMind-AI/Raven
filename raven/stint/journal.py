"""The append-only record a round leaves for the rounds after it.

Every round is a fresh conversation for every role, so nothing a role worked
out survives in its own head. The journal is where it survives instead: one
section per round, one subsection per role, appended and never rewritten.

Two gates keep it from eating the prompt it is supposed to inform. Only the
last :data:`JOURNAL_ROUNDS_IN_PROMPT` sections travel, and what does travel is
clipped at :data:`MAX_JOURNAL_CHARS`. A journal that grows for forty rounds
would otherwise become most of every prompt, and the rounds before the window
are in the commit log anyway -- which is the point of committing each role's
work separately.

Being a file rather than a conversation is what makes it reviewable: a person
opens it, reads what the last round concluded, and edits it if it is wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

JOURNAL = "JOURNAL.md"

MAX_JOURNAL_CHARS = 16_000
JOURNAL_ROUNDS_IN_PROMPT = 2

FIRST_ROUND = "(nothing yet: this is the first round)"
"""What stands in the journal's place before any round has written one.

Said rather than left blank: an empty slot in a prompt reads as a section the
role was not given, and a role that thinks it was denied its own history looks
for it somewhere else.
"""

_ROUND_HEADING = re.compile(r"^##\s+Round\s+(\d+)\s*$", re.MULTILINE)


def round_heading(index: int) -> str:
    return f"## Round {index:02d}"


def journal_tail(journal: str, rounds: int = JOURNAL_ROUNDS_IN_PROMPT) -> str:
    """The last ``rounds`` round sections, which is what the next round needs.

    A journal that grows for forty rounds would otherwise become most of every
    prompt, and the older rounds are in the commit log anyway.
    """
    marks = list(_ROUND_HEADING.finditer(journal))
    if len(marks) <= rounds:
        return journal.strip()
    return journal[marks[-rounds].start() :].strip()


def round_entry(journal: str, index: int, subsection: str = "### Build") -> str:
    """One role's entry for one round, which is what the next role in it reads."""
    heading = round_heading(index)
    if heading not in journal:
        return ""
    body = journal.split(heading, 1)[1]
    body = _ROUND_HEADING.split(body)[0]
    if subsection not in body:
        return ""
    entry = body.split(subsection, 1)[1]
    for stop in ("\n### ", "\n## "):
        entry = entry.split(stop)[0]
    return entry.strip()


#: How much of a role's account travels into the file. The window that reaches a
#: prompt is clipped again by :data:`MAX_JOURNAL_CHARS`; this one bounds the file
#: itself, which is read by people and committed forty times.
MAX_ENTRY_CHARS = 4_000


def append_entry(path: Path, index: int, section: str, text: str, limit: int = MAX_ENTRY_CHARS) -> None:
    """One role's account of this round, under its own subsection.

    Appended, never rewritten: the section belongs to the role that just
    finished, and the rounds before it are the only record that they happened.
    A second call for the same role and round adds a second entry rather than
    replacing the first -- a role that ran twice did run twice.
    """
    body = (text or "").strip()
    if not body or not section.strip():
        return
    if len(body) > limit:
        body = body[: limit - 1].rstrip() + "\u2026"
    heading = round_heading(index)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if heading not in existing:
        existing = (existing.rstrip() + "\n\n" if existing.strip() else "") + heading + "\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip() + f"\n\n### {section.strip()}\n\n{body}\n", encoding="utf-8")


def begin_round(workspace: Path, index: int) -> None:
    """The round's own section in the journal, so both roles append under it."""
    path = workspace / JOURNAL
    heading = round_heading(index)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if heading in text:
        return
    body = text.rstrip() + "\n\n" if text.strip() else ""
    path.write_text(body + heading + "\n\n", encoding="utf-8")
