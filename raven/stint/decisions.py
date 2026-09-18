"""What is waiting on a person, and how their answer gets back into the project.

``.stint/HUMAN_DECISIONS.md`` is the one file no agent writes. A role that hits
something only a person can settle asks for it, the Planner carries the question
here, and the run goes on around it -- which is what keeps one open question from
stalling a round.

Two things this module adds to that arrangement:

* **an id and a state per question**, so an answer can be recorded rather than
  guessed at from a file's prose, and so a question can name the tasks it holds
  up. That link is worth its weight three times: a page can say "this is holding
  up three tasks", answering unblocks them without the Planner having to
  remember, and the share of work blocked on a person is the honest measure of
  whether the project can run unattended at all.
* **a queue.** An answer submitted from a page arrives while a role may be
  mid-turn in the same checkout, so it is parked in the run's state directory and
  merged at the start of the next round, before the Planner reads. Writing
  straight into the project would race the role's own writes and could be undone
  by the enforcement pass; and changing a settled matter halfway through a round
  should wait for the boundary anyway.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from raven.stint.backlog import DECISIONS_FILE, STINT_DIR

PENDING_HEADING = "## Awaiting a person"
QUEUE_FILE = "pending-answers.json"

#: How much of the decisions file a role's round prompt carries. It grows by a
#: ruling a round, so a report-sized budget silently drops the newest ones --
#: the ones the round was planned around -- while keeping round 00's.
MAX_CHARS = 60_000

#: ``- [ ] Q03 (round 02) the duel's scoring rule`` with an optional
#: ``blocks: 7, 12`` tail. The checkbox is the state; the id is how an answer
#: finds its way back to the right line.
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
    answer: str = ""
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
            "answer": self.answer,
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


def pending(project: Path) -> list[Question]:
    return [question for question in read(project) if not question.answered]


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


# ---------------------------------------------------------------- the queue


def queue_path(state_dir: Path) -> Path:
    return Path(state_dir).expanduser() / QUEUE_FILE


def enqueue(state_dir: Path, question_id: str, answer: str) -> dict[str, Any]:
    """Park one answer for the next round to merge. Last write for an id wins."""
    if not question_id.strip():
        raise ValueError("an answer needs the id of the question it answers")
    if not answer.strip():
        raise ValueError("an answer needs text: it becomes the ruling the roles read")
    path = queue_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    queued = _read_queue(path)
    entry = {
        "id": question_id.strip(),
        "answer": answer.strip(),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    queued = [item for item in queued if item.get("id") != entry["id"]] + [entry]
    path.write_text(json.dumps(queued, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return entry


def requests_path(state_dir: Path) -> Path:
    return Path(state_dir) / "planner-requests.json"


def enqueue_request(state_dir: Path, text: str) -> dict[str, Any]:
    """Park what a person wants the next round to be about, for the Planner to read.

    Not an answer to a question and not a task: it is the instruction a person
    would have given out loud if they were in the room when the round was
    planned. Queued for the round boundary for the same reason answers are --
    the Planner may be mid-turn in this checkout right now.
    """
    if not text.strip():
        raise ValueError("a request needs text: it is what the next Planner is told to do")
    path = requests_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    queued_now = _read_queue(path)
    entry = {"text": text.strip(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(queued_now + [entry], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return entry


LEDGER_NAME = "planner-requests.log.jsonl"
REQUESTS_HEADING = "## Requests from the person running this"


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER_NAME


def take_requests(state_dir: Path) -> list[str]:
    """Every queued request, and the queue emptied: each one is carried once."""
    path = requests_path(state_dir)
    found = [str(item.get("text") or "").strip() for item in _read_queue(path)]
    found = [text for text in found if text]
    if found:
        path.write_text("[]\n", encoding="utf-8")
    return found


def record_requests(project: Path, state_dir: Path, texts: Sequence[str], round_index: int) -> None:
    """Keep what a person asked of a round, in both places it has to survive.

    The project's decisions file is the one every role reads, so a request that
    turns out to be a standing instruction is found there by the round after
    next. The ledger beside the run is the one that cannot be lost: the project
    file is a tracked file in a tree the ownership guard reverts writes to, and
    a record that can be reverted is not a record.

    The queue itself is emptied when the request is carried, so neither of these
    is what makes it act -- the round's own prompt does. These are the memory.
    """
    wanted = [" ".join(str(text).split()) for text in texts]
    wanted = [text for text in wanted if text]
    if not wanted:
        return
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger = ledger_path(state_dir)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        for text in wanted:
            handle.write(json.dumps({"round": round_index, "text": text, "at": at}, ensure_ascii=False) + "\n")

    document = decisions_path(project)
    if not document.is_file():
        return
    text = document.read_text(encoding="utf-8", errors="replace").rstrip("\n")
    if REQUESTS_HEADING not in text:
        text += (
            f"\n\n{REQUESTS_HEADING}\n\n"
            "What was asked of one round, as it was asked. Each entry acted on the round it names;\n"
            "it is kept here because an instruction given once is often meant to hold.\n"
        )
    entry = "\n".join(f"- {item}" for item in wanted)
    text += f"\n### Round {round_index:02d} ({at[:10]})\n\n{entry}\n"
    document.write_text(text + "\n", encoding="utf-8")


def queued(state_dir: Path) -> list[dict[str, Any]]:
    return _read_queue(queue_path(state_dir))


def merge(project: Path, state_dir: Path, round_index: int) -> list[str]:
    """Apply every queued answer and unblock what it was holding up.

    Returns the ids applied. Called at the top of a round, before the Planner
    runs, so an answer is always in the file the Planner is about to read.
    """
    path = queue_path(state_dir)
    entries = _read_queue(path)
    if not entries:
        return []
    document = decisions_path(project)
    if not document.is_file():
        return []
    text = document.read_text(encoding="utf-8", errors="replace")
    applied: list[str] = []
    unblock: list[int] = []
    for entry in entries:
        question_id = str(entry.get("id") or "")
        answer = " ".join(str(entry.get("answer") or "").split())
        updated, question = _record(text, question_id, answer, round_index)
        if question is None:
            continue
        text = updated
        applied.append(question_id)
        unblock.extend(question.blocks)
    if applied:
        document.write_text(text, encoding="utf-8")
        _unblock(project, unblock, round_index)
    path.unlink(missing_ok=True)
    return applied


def _record(text: str, question_id: str, answer: str, round_index: int) -> tuple[str, Question | None]:
    """Tick one entry's box and write the ruling under it, in place."""
    for match in _ENTRY.finditer(text):
        if match.group("id") != question_id or match.group("mark").lower() == "x":
            continue
        blocks = [int(part) for part in re.findall(r"\d+", match.group("blocks") or "")]
        line = match.group(0)
        ticked = line.replace("- [ ]", "- [x]", 1)
        ruling = f"\n      **Ruling** (round {round_index:02d}): {answer}"
        return text[: match.start()] + ticked + ruling + text[match.end() :], Question(
            id=question_id,
            text=match.group("text").strip(),
            round=int(match.group("round") or 0),
            answered=True,
            blocks=blocks,
            answer=answer,
            provisional=(match.group("provisional") or "").strip(),
        )
    return text, None


def _unblock(project: Path, task_ids: list[int], round_index: int) -> None:
    if not task_ids:
        return
    from raven.stint import backlog as backlog_mod

    try:
        backlog = backlog_mod.load(project)
    except backlog_mod.BacklogError:
        return
    changed = False
    for task_id in dict.fromkeys(task_ids):
        try:
            task = backlog.get(task_id)
        except backlog_mod.BacklogError:
            continue
        if task.state != backlog_mod.BLOCKED:
            continue
        # Only the human blockers lift: a task also waiting on another task is
        # still waiting on it, and clearing that here would hide the dependency.
        for blocker in [item for item in task.blocked_by if item.startswith("human:")]:
            backlog_mod.apply(
                backlog,
                "unblock",
                role=backlog_mod.HUMAN,
                task_id=task_id,
                round_index=round_index,
                blocker=blocker,
                reason="the question it waited on was answered",
            )
        changed = True
    if changed:
        backlog_mod.save(project, backlog)


def _read_queue(path: Path) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def answer_now(project: Path, question_id: str, answer: str, *, round_index: int = 0) -> dict[str, Any]:
    """Record a ruling straight into the project, for a project with no run.

    The queue exists because a role may be mid-turn in the same checkout. With no
    run there is no such turn, and parking the answer for a round boundary that
    may never arrive would leave a person's decision invisible to the next run
    that starts.
    """
    if not question_id.strip():
        raise ValueError("an answer needs the id of the question it answers")
    if not answer.strip():
        raise ValueError("an answer needs text: it becomes the ruling the roles read")
    document = decisions_path(project)
    if not document.is_file():
        raise ValueError(f"no {document} to answer in")
    text = document.read_text(encoding="utf-8", errors="replace")
    updated, question = _record(text, question_id.strip(), " ".join(answer.split()), round_index)
    if question is None:
        raise ValueError(f"no open question {question_id} in {document}")
    document.write_text(updated, encoding="utf-8")
    _unblock(project, question.blocks, round_index)
    return {
        "id": question.id,
        "answer": question.answer,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
