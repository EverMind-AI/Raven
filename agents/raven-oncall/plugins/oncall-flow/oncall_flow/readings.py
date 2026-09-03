"""The numbers a campaign watches: what to read, when to read it, and the record.

A campaign could say what it was optimising and nothing about what it was
watching. The consequence was measured on 2026-08-17: an FEA campaign declared
``max_penetration`` as its target, and the ledger held only ``gpu_minutes_used``
-- the number the whole campaign existed to move was obtained by the loop sending
74 ad-hoc commands at the job directory, and then existed only inside its own
prose. Nothing could rank the rounds by it, nothing could check the parse, and a
reader had to go through the log to find out what the campaign had found.

So a declaration carries a readings table: a name, one command that prints the
value, and when to take it. Four times, and the third is the one nothing had:

  at_declare     once, when the campaign is declared -- the starting point.
                 A cold-started wake cannot remember what the price was when it
                 was asked to watch; "down 10% from where it started" is only
                 answerable against a record.
  after_trial    when a trial finishes -- the result of that round
  during_trial   while a trial is running -- "it has been going 40 minutes and
                 the timestep is collapsing" cannot be seen after the fact,
                 which is how one CFD leg burned 152 core-minutes
  each_wake      every time the campaign is read -- the current state of
                 whatever is being watched

A reading is taken again and again, so it must not change anything: that is
checked, narrowly, where the table is declared.

What is stored is the series, not the latest value. F2's four rounds read -70.88,
-0.44, -10.47 and 9.9e-07; the fact worth having is that the sequence is not
monotone, and no single point carries it.

This layer never judges. It runs the command the campaign wrote, records what
came back, and hands the series to whoever is reading.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

AT_DECLARE = "at_declare"
AFTER_TRIAL = "after_trial"
DURING_TRIAL = "during_trial"
EACH_WAKE = "each_wake"
WHENS = (AT_DECLARE, AFTER_TRIAL, DURING_TRIAL, EACH_WAKE)

# The two that are taken inside one trial's directory, and so are the only two
# that may say {job_dir}.
_PER_TRIAL = (AFTER_TRIAL, DURING_TRIAL)

READINGS_FILE = "readings.jsonl"

# Nothing longer than this is kept from one reading. A command that prints a
# whole table is fine and the design expects it ("all the new job postings" is
# one value); a command that prints a log is a mistake, and truncating it here
# keeps that mistake from filling the campaign's record.
_MAX_VALUE_CHARS = 4000

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,39}$")

# First words whose job is to change something. Narrow on purpose, in the same
# discipline as the copy net in ops_declare: it can only add refusals.
_MUTATORS = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "dd",
        "kill",
        "pkill",
        "killall",
        "chmod",
        "chown",
        "truncate",
        "mkfs",
        "reboot",
        "shutdown",
        "mkdir",
        "touch",
        "tee",
    }
)

# A redirect that writes a file. ``2>/dev/null`` and ``&>/dev/null`` are not
# writes to anything that matters, and the lookbehind is what keeps them out.
_REDIRECT = re.compile(r"(?<![0-9&>])>>?\s*(?P<target>[^\s|&;)]+)")


@dataclass(frozen=True)
class Reading:
    """One number this campaign watches, and how to get it."""

    name: str
    command: str
    when: str


def declared(meta: dict[str, Any]) -> list[Reading]:
    """The campaign's readings table, or empty. Never raises on a bad row."""
    rows = meta.get("readings")
    out: list[Reading] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, command = str(row.get("name") or ""), str(row.get("command") or "")
        when = str(row.get("when") or "")
        if name and command and when in WHENS:
            out.append(Reading(name=name, command=command, when=when))
    return out


def table_problem(rows: Any, existing: list[Reading] | None = None) -> str | None:
    """Why this readings table cannot be accepted, or None.

    ``existing`` is the table already on disk. A reading whose name is already in
    use may be re-declared only if the command is identical: changing how a
    number is obtained, while keeping its name, puts two different quantities in
    one column and there is nothing in the record to tell them apart afterwards.
    A new way of reading it is a new name.
    """
    if rows in (None, []):
        return None
    if not isinstance(rows, list):
        return "REFUSED: readings must be a list of {name, command, when}. Nothing was written."
    seen: dict[str, str] = {r.name: r.command for r in (existing or [])}
    for row in rows:
        if not isinstance(row, dict):
            return f"REFUSED: a reading must be {{name, command, when}}, not {row!r}. Nothing was written."
        name = str(row.get("name") or "").strip()
        command = str(row.get("command") or "").strip()
        when = str(row.get("when") or "").strip()
        if not _NAME.match(name):
            return (
                f"REFUSED: {name!r} is not a usable reading name. Use a short identifier -- "
                f"letters, digits, underscores -- the way a column is named. Nothing was written."
            )
        if not command:
            return (
                f"REFUSED: reading {name!r} has no command, so there is no way to get it.\n"
                f"Give one line that PRINTS the value and nothing else, as typed on the machine.\n"
                f"Nothing was written."
            )
        if when not in WHENS:
            return (
                f"REFUSED: reading {name!r} says when={when!r}. It has to be one of:\n"
                f"  at_declare    once, now -- the starting point a later round is compared against\n"
                f"  after_trial   when a trial finishes -- that round's result\n"
                f"  during_trial  while a trial runs -- progress, which cannot be seen afterwards\n"
                f"  each_wake     every time you read the campaign -- the current state\n"
                f"Nothing was written."
            )
        if "{job_dir}" in command and when not in _PER_TRIAL:
            return (
                f"REFUSED: reading {name!r} reads {{job_dir}}, and a {when} reading is not taken "
                f"inside any trial's directory, so there is nothing to fill in.\n"
                f"Use when='after_trial' or 'during_trial', or read a path that does not depend "
                f"on a round. Nothing was written."
            )
        changing = _changes_something(command)
        if changing:
            return (
                f"REFUSED: reading {name!r} changes something.\n"
                f"  the change  {changing}\n"
                f"A reading is taken again and again -- every wake, or every round -- so anything "
                f"it changes, it changes that many times, and the record of what was read stops "
                f"being a record of what was there. Read it and print it; do the changing with an "
                f"action. Nothing was written."
            )
        if name in [str(r.get("name") or "").strip() for r in rows if r is not row]:
            # Declaring the same quantity twice -- once at_declare for the
            # starting point and once each_wake for the current value -- is the
            # obvious way to ask for a baseline, and it does not work: the table
            # is keyed by name, so the second row replaces the first and the
            # starting point is silently never taken. It is not needed either:
            # every each_wake reading is taken once while declaring, and that
            # take IS the starting point.
            return (
                f"REFUSED: {name!r} is declared twice. One row per name -- the same name at two "
                f"times replaces itself.\n"
                f"A starting point needs no second row: an each_wake reading is taken once while "
                f"the campaign is declared, and that reading is what later values are compared "
                f"against. Use at_declare only for something you want read once and never again. "
                f"Nothing was written."
            )
        if name in seen and seen[name] != command:
            return (
                f"REFUSED: {name!r} is already being read, and this reads it a different way.\n"
                f"  now  {seen[name]}\n"
                f"  new  {command}\n"
                f"Values already recorded under that name came from the old command, and nothing "
                f"in the record would separate them from the new ones. Give the new way its own "
                f"name. Nothing was written."
            )
        seen[name] = command
    return None


def merge(existing: list[Reading], rows: Any) -> list[dict[str, str]]:
    """The table to store: what was there, plus what is being added.

    Additive by construction. A declaration that omits the table keeps the one on
    disk -- dropping a reading silently would end a series that a later round is
    still comparing against.
    """
    out = {r.name: {"name": r.name, "command": r.command, "when": r.when} for r in existing}
    for row in rows or []:
        if isinstance(row, dict) and row.get("name"):
            out[str(row["name"])] = {
                "name": str(row["name"]),
                "command": str(row.get("command") or "").strip(),
                "when": str(row.get("when") or "").strip(),
            }
    return list(out.values())


def _changes_something(command: str) -> str | None:
    """The step that writes rather than reads, or None. Narrow by design."""
    import shlex

    for step in re.split(r"&&|\|\||[;\n|]", command):
        step = step.strip()
        if not step:
            continue
        try:
            words = shlex.split(step)
        except ValueError:
            words = step.split()
        if words and Path(words[0]).name in _MUTATORS:
            return step
        for hit in _REDIRECT.finditer(step):
            if hit.group("target") not in ("/dev/null", "/dev/stderr", "/dev/stdout"):
                return step
    return None


async def take(
    meta: dict[str, Any],
    cdir: str | Path,
    when: str,
    *,
    job_dir: str = "",
    trial: str = "",
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Take every reading due at ``when`` and append each to the record.

    Best-effort throughout: a machine that cannot be reached, a command that
    fails, a value that does not parse -- each is recorded as what happened and
    none of them raises. A reading is taken on the way to doing something else
    (declaring, looking at the campaign), and losing that because a grep found
    nothing would be a poor trade.
    """
    import asyncio

    due = [r for r in declared(meta) if r.when == when]
    if not due:
        return []
    cdir = Path(cdir).expanduser()
    if runner is None:
        runner = _runner(meta)
    if runner is None:
        return [_record(cdir, r, when, trial, error="the campaign's machine could not be reached") for r in due]

    taken: list[dict[str, Any]] = []
    for reading in due:
        command = _fill(reading.command, meta, job_dir)
        try:
            rc, out = await asyncio.to_thread(runner, command)
        except Exception as exc:  # noqa: BLE001 -- a reading never costs the caller
            taken.append(_record(cdir, reading, when, trial, error=f"{type(exc).__name__}: {exc}"[:200]))
            continue
        if rc != 0:
            taken.append(_record(cdir, reading, when, trial, error=f"exit {rc}: {str(out).strip()[:200]}"))
            continue
        taken.append(_record(cdir, reading, when, trial, value=parse(out)))
    return taken


def parse(out: str) -> Any:
    """What the command printed, as the most specific thing it could be.

    A number stays a number, so it can be ranked and compared. Anything else is
    kept as it came -- a JSON document of every open posting is one value, and
    judging it is the loop's job, not this one's.
    """
    text = str(out or "").strip()
    if not text:
        return ""
    try:
        return float(text)
    except ValueError:
        pass
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text[:_MAX_VALUE_CHARS]


def numeric(value: Any) -> float | None:
    """The value as a number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def taken_for(cdir: str | Path, trial: str) -> set[str]:
    """After-trial reading names already attempted for this trial, or not obtained.

    Attempted, not obtained: a job directory that has been cleaned up will fail
    the same way every time, and retrying it on every look would fill the record
    with one error per wake.

    Only the after-trial ones. A during-trial reading is meant to be taken again
    on every look -- that repetition is the series -- so it must not be filtered
    out by having been taken once.
    """
    return {str(row.get("name")) for row in read(cdir) if row.get("trial") == trial and row.get("when") == AFTER_TRIAL}


def read(cdir: str | Path) -> list[dict[str, Any]]:
    path = Path(cdir).expanduser() / READINGS_FILE
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def series(cdir: str | Path, *, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
    """Every reading's record, oldest first, capped at the last ``limit`` each.

    The series is the point. A tool that printed the latest value would be
    handing over the one thing that cannot answer "has it moved" -- measured on
    the CFD leg, where the timestep collapsing over an hour is the whole signal
    and every individual sample looks like a small number.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for row in read(cdir):
        out.setdefault(str(row.get("name")), []).append(row)
    return {name: rows[-limit:] for name, rows in out.items()}


def baseline(cdir: str | Path) -> dict[str, Any]:
    """The at_declare values: what each reading was when the campaign opened."""
    out: dict[str, Any] = {}
    for row in read(cdir):
        if row.get("when") == AT_DECLARE and "value" in row:
            out.setdefault(str(row.get("name")), row["value"])
    return out


def _fill(command: str, meta: dict[str, Any], job_dir: str) -> str:
    return (
        command.replace("{job_dir}", job_dir)
        .replace("{staged_case}", str(meta.get("staged_case") or ""))
        .replace("{remote_dir}", str(meta.get("remote_dir") or ""))
    )


def _runner(meta: dict[str, Any]):
    from oncall_flow.connections import resolve_into
    from oncall_flow.transport import runner_from

    try:
        return runner_from(resolve_into(dict(meta)), what="campaign", cap_seconds=60.0)
    except Exception:  # noqa: BLE001 -- an unreachable machine is a recorded fact
        return None


def _record(
    cdir: Path,
    reading: Reading,
    when: str,
    trial: str,
    *,
    value: Any = None,
    error: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "name": reading.name,
        "when": when,
    }
    if trial:
        row["trial"] = trial
    if error:
        row["error"] = error
    else:
        row["value"] = value
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        with open(cdir / READINGS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return row
