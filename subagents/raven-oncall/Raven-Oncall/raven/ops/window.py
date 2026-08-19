"""Which window is watching which campaign, and which task that campaign is.

An ops home holds every campaign of an instance, and with no ``--config`` that is
one home for every window. The campaign a call means was inferred by counting
directories in it: exactly one meant that one, anything else meant "say which
one". Two windows each watching an experiment is the ordinary two-window case, so
both of them got the error -- the whole of "you cannot watch two experiments at
once".

Two identities answer two different questions, and neither substitutes for the
other:

  - **the window** (session key, ``tui:<id>``, minted per window by
    ``session.create``) tells two open windows apart. It dies with its window.
  - **the task** (a fingerprint of the statement the operator handed over) is what
    a *new* window has when the old one is gone. The state is on disk precisely so
    another window can pick the watch up, and this is how it recognises which one
    to pick up without being told.

So the lookup is: what this window claimed, then what this task belongs to, then
ask. The last tier is the only one anybody has to act on, and it is reached once
per window at most.

Deliberately plain files, rewritten whole. This is an index, not a record anything
is judged on -- every campaign's own directory remains the truth, which is why a
lost or corrupt file falls back to the older rule rather than failing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

STORE = ".window-campaigns.json"
TASK_FILE = "task.sha"


def task_fingerprint(statement: str) -> str:
    """A stable name for the task statement a window was handed.

    Whitespace-normalised so a re-paste that rewraps is the same task, and short
    because it is an index key rather than evidence. Taken from the *first* user
    message of a session and nothing else: a wake turn's message is
    ``[Ops campaign 'X' round 2 due] ...``, which has nothing to do with the
    statement, so fingerprinting every message would change the answer each turn.
    """
    text = " ".join((statement or "").split())
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _path(ops_home: Path) -> Path:
    return Path(ops_home) / STORE


def _load(ops_home: Path) -> dict[str, dict]:
    try:
        data = json.loads(_path(ops_home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in data.items():
        if not k:
            continue
        # A bare string is the first shape this file had.
        if isinstance(v, str) and v:
            out[str(k)] = {"campaign": v, "pid": None}
        elif isinstance(v, dict) and v.get("campaign"):
            out[str(k)] = {"campaign": str(v["campaign"]), "pid": v.get("pid")}
    return out


def campaign_for_window(ops_home: Path, session_key: str) -> str | None:
    """The campaign this window claimed, or None if it has not said."""
    if not session_key:
        return None
    row = _load(ops_home).get(session_key)
    return row["campaign"] if row else None


def bind_window(ops_home: Path, session_key: str, campaign: str,
                pid: int | None = None) -> None:
    """Record that this window is working on this campaign.

    The pid is stored so a later question -- "is any live window watching this
    campaign?" -- can be answered, which is what keeps a deliberate second run of
    the same statement from being read as a takeover.

    A window that moves on to different work overwrites its row: this is about
    what is being watched now, not a history. An empty session key is not a
    window (a cron turn, the CLI, a test); giving them all one shared row would
    hand every session-less caller whatever the last one touched.
    """
    if not session_key or not campaign:
        return
    # A wake turn is not a window. It already knows its campaign -- the wake
    # message names it -- so it has nothing to claim, and a row per wake grows
    # without bound over a long watch while describing no window at all.
    if session_key.startswith("cron:"):
        return
    table = _load(ops_home)
    row = {"campaign": campaign, "pid": pid}
    if table.get(session_key) == row:
        return
    table[session_key] = row
    try:
        p = _path(ops_home)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    except OSError:
        # An index that cannot be written costs the fallback rule, not the turn.
        pass


def has_live_window(ops_home: Path, campaign: str, exclude_session: str = "") -> bool:
    """Whether some window whose process is still running claimed this campaign.

    ``exclude_session`` leaves the asking window out, so the question becomes
    "is somebody *else* watching this" -- which is what decides whether an
    unnamed call may be handed a campaign it never claimed.
    """
    from raven.proactive_engine.schedulers.cron.service import _pid_alive

    for sid, row in _load(ops_home).items():
        if row["campaign"] != campaign or (exclude_session and sid == exclude_session):
            continue
        pid = row.get("pid")
        if pid and _pid_alive(int(pid)):
            return True
    return False


def window_for_campaign(ops_home: Path, campaign: str) -> tuple[str, int | None] | None:
    """The window a campaign's wakes belong to, as ``(session_key, pid)``.

    A wake belongs to the window that is watching the campaign, not to whichever
    session happened to schedule it. Those differ from the second round on: the
    turn that reschedules is a wake turn, and taking its session as the owner
    hands the next wake to a one-shot session that no longer exists when it
    fires. Measured 2026-08-14 -- one campaign's wake carried
    ``owner=cron:650e19fa owner_pid=<the other window's process>``, and ran a
    full turn, ledger read and job submit included, inside the window holding a
    different experiment's conversation.

    A live window wins over a stale row, so a campaign taken over by a second
    window does not keep pointing at the first.
    """
    from raven.proactive_engine.schedulers.cron.service import _pid_alive

    stale: tuple[str, int | None] | None = None
    for sid, row in _load(ops_home).items():
        if row["campaign"] != campaign:
            continue
        pid = row.get("pid")
        if pid and _pid_alive(int(pid)):
            return sid, int(pid)
        if stale is None:
            stale = (sid, int(pid) if pid else None)
    return stale


def remember_task(campaign_dir: Path, fingerprint: str) -> None:
    """Record which task statement this campaign was started for.

    First writer wins. The statement is a property of the campaign's origin, and
    every later turn has a different first message -- a wake turn's is
    "[Ops campaign 'X' round 1 due] ...", which has nothing to do with the task.
    Measured on the two-window run 2026-08-13: rewriting on every submit replaced
    the statement's fingerprint with a wake message's, so a window opened later
    with the original statement no longer recognised the campaign.
    """
    if not fingerprint:
        return
    existing = Path(campaign_dir) / TASK_FILE
    try:
        if existing.read_text(encoding="utf-8").strip():
            return
    except OSError:
        pass
    try:
        d = Path(campaign_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / TASK_FILE).write_text(fingerprint + "\n", encoding="utf-8")
    except OSError:
        pass


def campaign_for_task(ops_home: Path, fingerprint: str, *,
                      exclude_live: bool = False) -> str | None:
    """The campaign started for this task statement, if one was.

    ``exclude_live`` skips a campaign some running window is already watching:
    handing the same statement to a second window on purpose is a second
    experiment, not a takeover. Same rule a wake's owner is judged by -- yield
    only to a window that is still there.
    """
    home = Path(ops_home)
    if not fingerprint or not home.exists():
        return None
    for d in sorted(p for p in home.iterdir() if p.is_dir()):
        try:
            if (d / TASK_FILE).read_text(encoding="utf-8").strip() != fingerprint:
                continue
        except OSError:
            continue
        if exclude_live and has_live_window(home, d.name):
            continue
        return d.name
    return None
