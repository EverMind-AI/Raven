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
    read from the events and closed by a later note -- the same records a
    grader and a later turn read anyway, so there is no second place to keep
    in sync.

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


def append_note(campaign_dir: Path, note: str, *, source: str = "agent") -> None:
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
    if rnd is not None:
        row["trials_at_the_time"] = rnd
    with open(campaign_dir / NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_owner_answer(cdir: Path, answer: str) -> None:
    """An answer given inline is the same fact as one typed into the chat, and
    has to land in the same place -- the next wake reads notes, not this turn."""
    append_note(cdir, answer[:2000], source="owner")


def blocking_question_open(cdir: Path) -> str:
    """An unanswered question the loop itself marked as blocking, or "".

    Public because ops_submit needs it: the refusal there rests on the loop's own
    reading, not on ours -- it said nothing worth doing was left, so spending
    compute contradicts it.
    """
    path = cdir / "events.jsonl"
    if not path.exists():
        return ""
    asked_at, question = "", ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "ask_owner" and e.get("allowed") is not False:
            if e.get("blocks_progress"):
                asked_at, question = str(e.get("ts") or ""), str(e.get("question") or "a question")
            else:
                asked_at, question = "", ""
    if not question:
        return ""
    notes = cdir / NOTES_FILE
    if notes.exists():
        for line in notes.read_text(encoding="utf-8").splitlines():
            try:
                n = json.loads(line)
            except ValueError:
                continue
            if str(n.get("ts") or "") >= asked_at:
                return ""
    return question


def unanswered_question(cdir: Path) -> str:
    """The question the owner has not come back on, or "".

    Read from the events, which is what a grader and a later turn read anyway; a
    second place to keep this in sync is a second place for it to be wrong. An
    owner's note closes it, and so does one the loop writes to say the question
    no longer matters -- both are a statement on the record that the answer is no
    longer being waited for.
    """
    path = cdir / "events.jsonl"
    if not path.exists():
        return ""
    asked_at, question = "", ""
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "ask_owner" and e.get("allowed") is not False:
            asked_at, question = str(e.get("ts") or ""), str(e.get("question") or "a question")
    if not question:
        return ""
    # Any note written after the question closes it: the owner's answer arrives
    # as one (captured from the chat), and so does the loop saying the answer no
    # longer matters. Timestamps rather than a flag, because notes.jsonl is where
    # both already land.
    notes = cdir / NOTES_FILE
    if notes.exists():
        for line in notes.read_text(encoding="utf-8").splitlines():
            try:
                n = json.loads(line)
            except ValueError:
                continue
            if str(n.get("ts") or "") >= asked_at:
                return ""
    return question


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
    "load_guard",
    "save_guard",
    "unanswered_question",
]
