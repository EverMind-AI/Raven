"""Escalation mechanics: the interruption contract at rest, and the question trail.

The two tools that spend a person's attention (part 2b: ``ops_ask_owner`` asks,
``ops_finish`` reports and closes) sit on this layer; what lands here is
everything about escalation that is a fact on disk rather than a tool face:

  - **the guard's durability.** A wake turn starts cold from disk, so a guard
    rebuilt empty every turn would hand back the whole interruption budget on
    each wake. The contract itself comes from ``meta.json`` (the campaign's
    fixed setup, quiet hours and ask budget included); the counts come from
    ``interruptions.json`` (what has happened so far).
  - **the question trail.** Which question the owner has not come back on is
    read from the events and notes -- the same records a grader and a later
    turn read anyway, so there is no second place to keep in sync. A question
    carries an ``ask_id``; it is closed by the owner speaking (an answer to it,
    or anything typed after it), by the loop settling it on the default it
    named, or by the campaign withdrawing it. A note the loop writes about its
    own work does not close it.

Per the seam-one final ruling the fork's owner-window store machinery is not
rebuilt; the contract is enforced plugin-side, in front of whatever delivery
the host lends (D4: model-mediated) -- a denied ask never reaches the person
whichever way allowed ones travel.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from oncall_flow.interruption import ContractGuard, InterruptionContract

GUARD_FILE = "interruptions.json"
NOTES_FILE = "notes.jsonl"


def load_guard(cdir: Path) -> ContractGuard:
    """The campaign's guard, rebuilt from disk with its counts intact.

    The contract itself comes from ``meta.json`` (the campaign's fixed setup);
    the counts come from ``interruptions.json`` (what has happened so far). A
    campaign with no contract configured gets a permissive one, which is honest:
    no contract means no threshold, not a secret default.
    """
    state_path = cdir / GUARD_FILE
    if state_path.exists():
        try:
            return ContractGuard.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
    contract_cfg: dict[str, Any] = {}
    start_hour = datetime.now().hour
    meta_path = cdir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            contract_cfg = dict(meta.get("interruption_contract") or {})
            start_hour = int(meta.get("start_hour", start_hour))
        except (OSError, ValueError):
            pass
    quiet = contract_cfg.get("quiet_hours")
    if quiet is not None:
        contract_cfg["quiet_hours"] = tuple(quiet)
    return ContractGuard(InterruptionContract(**contract_cfg), start_hour=start_hour)


def save_guard(cdir: Path, guard: ContractGuard) -> None:
    cdir.mkdir(parents=True, exist_ok=True)
    tmp = cdir / (GUARD_FILE + ".tmp")
    tmp.write_text(json.dumps(guard.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(cdir / GUARD_FILE)


def append_note(campaign_dir: Path, note: str, *, source: str = "agent", ask_id: str = "") -> None:
    """Append one instruction to the campaign's notes.

    ``source`` says who it came from. A note the owner typed and a note the loop
    wrote about itself carry different weight on a later read, and a wake turn
    that cannot tell them apart has to guess.

    ``trials_at_the_time`` is stamped from the ledger so a later turn can tell
    whether an instruction has already been acted on: an instruction recorded at
    round 3, read again at round 5 with a round-4 submit that matches it, has
    been handled. Without it the same "add two more angles" gets acted on at
    every wake -- which is the one new failure the automatic capture introduces.
    """
    rnd = None
    try:
        led = json.loads((campaign_dir / "ledger.json").read_text(encoding="utf-8"))
        rnd = len(led.get("records") or {})
    except (OSError, ValueError):
        pass
    campaign_dir.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "note": note, "source": source}
    if ask_id:
        row["ask_id"] = ask_id
    if rnd is not None:
        row["trials_at_the_time"] = rnd
    with open(campaign_dir / NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_owner_answer(cdir: Path, answer: str) -> None:
    """An answer given inline is the same fact as one typed into the chat, and
    has to land in the same place -- the next wake reads notes, not this turn."""
    append_note(cdir, answer[:2000], source="owner")


# The events that end a question, each naming the ask it ends.
_CLOSING_EVENTS = frozenset({"owner_answered", "ask_settled", "ask_withdrawn"})


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def open_question(cdir: Path) -> dict[str, Any] | None:
    """The latest allowed question, while nobody has closed it; else None.

    ``{"ask_id", "question", "blocks_progress", "ts"}``. Only the latest ask
    counts: a new question supersedes the one before it.

    An ask with an id is closed by an event naming that id, or by the owner
    speaking after it -- a note of theirs that answers no other question.
    The loop's own notes do not close it: a progress note is not the owner's
    answer, and taking it for one let the loop end its own question without
    saying so. An ask recorded before ids existed keeps
    the old reading, where any later note closes it.
    """
    ask: dict[str, Any] | None = None
    events = _rows(cdir / "events.jsonl")
    for e in events:
        if e.get("kind") == "ask_owner" and e.get("allowed") is not False:
            ask = e
    if ask is None:
        return None
    asked_at = str(ask.get("ts") or "")
    ask_id = str(ask.get("ask_id") or "")
    for e in events:
        if ask_id and e.get("kind") in _CLOSING_EVENTS and e.get("ask_id") == ask_id:
            return None
    for n in _rows(cdir / NOTES_FILE):
        if str(n.get("ts") or "") < asked_at:
            continue
        if not ask_id:
            return None
        if n.get("source") == "owner" and n.get("ask_id") in (None, "", ask_id):
            return None
    return {
        "ask_id": ask_id,
        "question": str(ask.get("question") or "a question"),
        "blocks_progress": bool(ask.get("blocks_progress")),
        "ts": asked_at,
    }


def blocking_question_open(cdir: Path) -> str:
    """An unanswered question the loop itself marked as blocking, or "".

    Public because ops_submit needs it: the refusal there rests on the loop's own
    reading, not on ours -- it said nothing worth doing was left, so spending
    compute contradicts it.
    """
    q = open_question(cdir)
    return q["question"] if q and q["blocks_progress"] else ""


def unanswered_question(cdir: Path) -> str:
    """The question the owner has not come back on, or "" (see ``open_question``)."""
    q = open_question(cdir)
    return q["question"] if q else ""


def decided_without_owner(cdir: Path) -> list[dict[str, str]]:
    """Questions the loop settled on its default, with the owner never answering.

    ``[{"question", "reason"}]``, oldest first. A report reads these out, so a
    decision the owner was asked for and never made is on the page rather than
    only in the trail.
    """
    events = _rows(cdir / "events.jsonl")
    asked = {
        str(e.get("ask_id")): str(e.get("question") or "")
        for e in events
        if e.get("kind") == "ask_owner" and e.get("ask_id")
    }
    answered = {str(e.get("ask_id")) for e in events if e.get("kind") == "owner_answered" and e.get("ask_id")}
    out = []
    for e in events:
        ask_id = str(e.get("ask_id") or "")
        if e.get("kind") == "ask_settled" and ask_id in asked and ask_id not in answered:
            out.append({"question": asked[ask_id], "reason": str(e.get("reason") or "")})
    return out


def anything_running(cdir: Path) -> bool:
    """Whether a trial of this campaign is still burning machine time."""
    try:
        led = json.loads((cdir / "ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    terminal = {"succeeded", "failed", "cancelled"}
    return any(str(r.get("status")) not in terminal for r in (led.get("records") or {}).values())


__all__ = [
    "GUARD_FILE",
    "NOTES_FILE",
    "anything_running",
    "append_note",
    "append_owner_answer",
    "blocking_question_open",
    "decided_without_owner",
    "load_guard",
    "open_question",
    "save_guard",
    "unanswered_question",
]
