"""What a campaign is allowed to DO, written down before it does any of it.

A campaign could start a trial and nothing else. That is enough while the work is
a parameter sweep, where every round is the same command with different numbers.
It is not enough for a campaign that watches: when the condition it was waiting
for holds, something has to happen -- place the order, apply for the posting,
raise the alert -- and none of that is a trial.

Two properties make this a declared table rather than a command composed at the
moment of acting.

**It is a permission list.** The declaration is written once, before anything
runs, and read back to the owner. An action that is not in it cannot be taken. A
loop that assembled the order line each time it woke could put ``sell`` where it
had said ``buy`` and nothing would be standing between that and the broker.

**Repeating is not always harmless, and only the declaration knows which.**
Marking a thread read twice leaves it read; buying three shares twice leaves six.
So each action says which it is, and the harmful ones are keyed in the ledger by
what they were asked to do -- a wake that comes back after the acting turn died
finds the record and does not do it again. This is the same guarantee trials have
always had, and the reason an action does not go through ``exec``: exec keeps no
record, so the second wake cannot tell that the first one already acted.

What this module does not do is decide when. That is the loop's judgement, and it
is the whole thing being measured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Repeating it leaves the world where one call already put it.
SAFE = "safe"
# Repeating it happens twice: two orders, two emails, two applications.
HARMFUL = "harmful"
_REPEATS = (SAFE, HARMFUL)

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# The name a trial submit uses, so it cannot also name an action.
RUN = "run"
# Not an action: it says the look happened and nothing needed doing.
NONE = "none"
_RESERVED = frozenset({RUN, NONE})


@dataclass(frozen=True)
class Action:
    name: str
    command: str
    repeat: str = HARMFUL

    @property
    def repeat_is_harmful(self) -> bool:
        return self.repeat != SAFE


def declared(meta: dict[str, Any]) -> list[Action]:
    """The campaign's action table, or empty. Never raises on a bad row."""
    out: list[Action] = []
    rows = meta.get("actions")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, command = str(row.get("name") or ""), str(row.get("command") or "")
        if name and command:
            repeat = str(row.get("repeat") or HARMFUL)
            out.append(Action(name=name, command=command,
                              repeat=repeat if repeat in _REPEATS else HARMFUL))
    return out


def find(meta: dict[str, Any], name: str) -> Action | None:
    for action in declared(meta):
        if action.name == name:
            return action
    return None


def table_problem(rows: Any, existing: list[Action] | None = None) -> str | None:
    """Why this action table cannot be accepted, or None.

    An action already declared may be re-declared identically and not otherwise.
    The table is what stands between a loop and an order it never showed anyone;
    letting it be rewritten once results exist would take that away at exactly
    the point it starts to matter.
    """
    if rows in (None, []):
        return None
    if not isinstance(rows, list):
        return "REFUSED: actions must be a list of {name, command, repeat}. Nothing was written."
    seen = {a.name: a.command for a in (existing or [])}
    for row in rows:
        if not isinstance(row, dict):
            return f"REFUSED: an action must be {{name, command, repeat}}, not {row!r}. Nothing was written."
        name = str(row.get("name") or "").strip()
        command = str(row.get("command") or "").strip()
        repeat = str(row.get("repeat") or "").strip()
        if name in _RESERVED:
            return (
                f"REFUSED: {name!r} is not available as an action name -- 'run' means starting a "
                f"trial and 'none' means the look needed no action. Give it its own name, like "
                f"'buy' or 'apply'. Nothing was written."
            )
        if not _NAME.match(name):
            return (
                f"REFUSED: {name!r} is not a usable action name. Use a short lowercase verb -- "
                f"'buy', 'apply', 'mark_read' -- since this is the name you will pass to "
                f"ops_submit. Nothing was written."
            )
        if not command:
            return (
                f"REFUSED: action {name!r} has no command, so there is nothing for it to do.\n"
                f"Write the line as it would be typed on the machine, with {{placeholders}} for "
                f"whatever changes per call. Nothing was written."
            )
        if repeat not in _REPEATS:
            return (
                f"REFUSED: action {name!r} must say what repeating it does:\n"
                f"  repeat='harmful'  doing it twice happens twice -- an order, an email, an "
                f"application. It will be refused the second time.\n"
                f"  repeat='safe'     doing it twice leaves the world where once already put it "
                f"-- a like, a read mark.\n"
                f"Nothing here can work that out from the command, and a wake turn remembers "
                f"nothing, so an unguarded repeat is a real second order. Nothing was written."
            )
        if name in seen and seen[name] != command:
            return (
                f"REFUSED: action {name!r} is already declared as something else.\n"
                f"  now  {seen[name]}\n"
                f"  new  {command}\n"
                f"This table is the list of what may be done, read back before anything ran. "
                f"Rewriting an entry in it would make that reading worthless. Nothing was written."
            )
        seen[name] = command
    return None


def merge(existing: list[Action], rows: Any) -> list[dict[str, str]]:
    """The table to store: what was there, plus what is being added."""
    out = {a.name: {"name": a.name, "command": a.command, "repeat": a.repeat} for a in existing}
    for row in rows or []:
        if isinstance(row, dict) and row.get("name"):
            out[str(row["name"])] = {
                "name": str(row["name"]),
                "command": str(row.get("command") or "").strip(),
                "repeat": (str(row.get("repeat") or HARMFUL).strip()
                           if str(row.get("repeat") or "").strip() in _REPEATS else HARMFUL),
            }
    return list(out.values())


def fill(action: Action, values: dict[str, Any]) -> tuple[str, list[str]]:
    """The command with this call's values in it, and any placeholder left empty.

    Refusing on a missing value rather than substituting nothing: an order line
    that lost its quantity is not a smaller order, it is a command whose meaning
    depends on what the tool happens to do with an empty string.
    """
    import string

    missing: list[str] = []
    try:
        names = [n for _, n, _, _ in string.Formatter().parse(action.command) if n]
    except ValueError:
        names = []
    command = action.command
    for name in names:
        key = name.split(".")[0].split("[")[0]
        if key not in (values or {}):
            missing.append(key)
            continue
        command = command.replace("{" + name + "}", str(values[key]))
    return command, sorted(set(missing))


def idem_key(action: Action, values: dict[str, Any], *, already: int = 0) -> str:
    """The ledger key for this call.

    For a harmful action it is the action and its values and nothing else, so a
    second attempt at the same thing collides with the record of the first --
    which is the entire cold-start guarantee: the wake that comes back after the
    acting turn is gone reads the ledger and sees it was done.

    For a safe one the count is folded in, because doing it again is legitimate
    and each occurrence deserves its own line.
    """
    from raven.ops.proposer import config_key

    stem = f"{action.name}--{config_key(dict(values or {}))}"
    return stem if action.repeat_is_harmful else f"{stem}--{already}"
