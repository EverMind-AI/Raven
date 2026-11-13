"""What is waiting on a person, written where every role reads it.

``.stint/HUMAN_DECISIONS.md`` is the one file no agent writes. A role that hits
something only a person can settle asks for it, the Planner carries the question
here, and the run goes on around it -- which is what keeps one open question from
stalling a round.

What this module adds to that arrangement is an id and a state per question, so
a question can name the tasks it holds up and a task can only be blocked on a
question that was asked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from raven.stint.backlog import DECISIONS_FILE, STINT_DIR

PENDING_HEADING = "## Awaiting a person"

#: ``- [ ] Q03 (round 02) the duel's scoring rule`` with an optional
#: ``blocks: 7, 12`` tail. The checkbox is the state; the id is what a blocked
#: task names.
_ENTRY = re.compile(
    r"^- \[(?P<mark>[ xX])\]\s*(?P<id>Q\d+)\s*"
    r"(?:\(round (?P<round>\d+)\))?\s*"
    r"(?P<text>.*?)\s*"
    r"(?:\|\s*blocks:\s*(?P<blocks>[\d,\s]+))?\s*"
    r"(?:\|\s*provisional:\s*(?P<provisional>.*?))?$",
    re.MULTILINE,
)


@dataclass
class Question:
    id: str
    text: str
    round: int = 0
    answered: bool = False
    blocks: list[int] = field(default_factory=list)
    #: The Planner's own ruling, standing until a person's replaces it. A run in
    #: soft mode decides and tells rather than blocks and waits.
    provisional: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "round": self.round,
            "answered": self.answered,
            "blocks": self.blocks,
            "provisional": self.provisional,
        }


def decisions_path(project: Path) -> Path:
    return Path(project).expanduser() / STINT_DIR / DECISIONS_FILE


def read(project: Path) -> list[Question]:
    path = decisions_path(project)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse(text)


def parse(text: str) -> list[Question]:
    found: list[Question] = []
    for match in _ENTRY.finditer(text):
        blocks = [int(part) for part in re.findall(r"\d+", match.group("blocks") or "")]
        found.append(
            Question(
                id=match.group("id"),
                text=match.group("text").strip(),
                round=int(match.group("round") or 0),
                answered=match.group("mark").lower() == "x",
                blocks=blocks,
                provisional=(match.group("provisional") or "").strip(),
            )
        )
    return found


def next_id(project: Path) -> str:
    known = [int(question.id[1:]) for question in read(project) if question.id[1:].isdigit()]
    return f"Q{max(known, default=0) + 1:02d}"


def ask(
    project: Path, text: str, *, round_index: int = 0, blocks: list[int] | None = None, provisional: str = ""
) -> Question:
    """Add one open question. Used by the Planner, and by the runtime for a role's ask.

    ``provisional`` is the asking role's own ruling, recorded on the same line so
    the person reads the question and the answer that stands in for theirs.
    """
    path = decisions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    question = Question(
        id=next_id(project),
        text=" ".join(text.split()).replace("|", "/"),
        round=round_index,
        blocks=blocks or [],
        provisional=" ".join(provisional.split()).replace("|", "/"),
    )
    line = f"- [ ] {question.id} (round {question.round:02d}) {question.text}"
    if question.blocks:
        line += " | blocks: " + ", ".join(str(item) for item in question.blocks)
    if question.provisional:
        line += f" | provisional: {question.provisional}"
    body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else f"# {DECISIONS_FILE}\n"
    if PENDING_HEADING in body:
        head, _, tail = body.partition(PENDING_HEADING)
        sections = tail.split("\n## ", 1)
        rest = "\n## " + sections[1] if len(sections) > 1 else ""
        body = head + PENDING_HEADING + sections[0].rstrip("\n") + "\n" + line + "\n" + rest
    else:
        body = body.rstrip("\n") + f"\n\n{PENDING_HEADING}\n\n{line}\n"
    path.write_text(body, encoding="utf-8")
    return question
