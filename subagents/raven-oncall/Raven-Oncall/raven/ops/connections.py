"""The machines this instance can reach, named by their owner.

A campaign used to carry its own ``host``/``port``/``key``, which put the way in
inside the thing being run and left the agent to work the connection out for
itself. Measured 2026-08-14 on two FEA tasks whose statement did not spell the
port out: the loop tried port 22, then 2222, 8022, 10022, 443, read
``~/.ssh/config``, pulled a stale port out of ``known_hosts`` and believed it,
then read raven's own campaign directory to find the number -- a dozen rounds
without submitting a single job, and one window ended up asking the owner
whether a bastion was needed.

None of that is the agent being slow. It had no way to reach a machine except to
guess at one, so guessing is what it did.

Here a connection is something the owner sets up once and names -- "my CPU box",
"the GPU machine" -- and the agent only ever sees the name and what the machine
is. Three things follow:

  * a task statement stops carrying infrastructure. "run it on my CPU box" is
    what a person types, and there is no address in it to guess at;
  * a name is the only thing that can tell two machines apart when they share an
    address. 14.103.100.27:58717 and 14.103.100.27:64101 are different machines,
    and "on 14.103.100.27" names neither of them;
  * properties that belong to a machine live on the machine. Which unit its
    budget is counted in, and how many jobs it will run at once, were being
    restated in every campaign's meta and in the task statement itself.

Credentials stay a reference -- a path to a key the owner already has. Passwords
wait until there is a platform that needs them (2026-08-17, deliberate): storing
one means a keychain or a master password, and neither is worth building before
something asks for it.

Read-only by design for now. The file is written by hand; ``raven connection
add`` is product work that can wait until the shape is confirmed against a real
task.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STORE = "connections.json"

# What the agent may see. The key path is deliberately not in it: the agent never
# needs to authenticate, and a field it cannot use is one more thing to reason
# about wrongly.
#
# ``software`` is the deciding one and was not obvious. Measured 2026-08-17 on
# these two machines: CalculiX runs only on the box with the A800s, because the
# binary needs a glibc the 32-core box does not have. A rule like "a CPU-only
# solver belongs on the CPU box" would therefore pick the one machine that
# cannot run it. What a machine has installed decides; what it is made of only
# narrows.
_SHOWN = ("kind", "device", "cores", "memory", "software", "budget_unit",
          "concurrency", "note")

# What the backend needs and the agent does not. ``transport`` is in here rather
# than in _SHOWN on purpose: whether a machine is reached over ssh or is simply
# this one changes nothing about which machine the work calls for, and a field
# the agent can see is a field it can reason about wrongly -- here, by deciding
# it may skip naming the machine at all, which loses the record of where a
# command ran.
_TRANSPORT = ("host", "port", "user", "key", "transport")


def store_path() -> Path:
    """Where this instance keeps its connections, next to its config file."""
    from raven.config.paths import get_config_path

    try:
        return Path(get_config_path()).expanduser().parent / STORE
    except Exception:  # noqa: BLE001 -- a missing config path is not a failure here
        return Path.home() / ".raven" / STORE


def load() -> list[dict[str, Any]]:
    """Every connection, in file order. An unreadable or absent file is none."""
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("connections") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "").strip():
            out.append(row)
    return out


def get(conn_id: str) -> dict[str, Any] | None:
    """One connection by its id, or None. The id is internal and never renamed.

    Names are the owner's and change; a campaign that stored the name would have
    to be rewritten every time someone tidied their list.
    """
    if not conn_id:
        return None
    for row in load():
        if str(row.get("id")) == str(conn_id):
            return row
    return None


def display_name(conn_id: str) -> str:
    """The owner's own word for this machine, or the id when it is unknown."""
    row = get(conn_id)
    return str(row.get("display_name") or conn_id) if row else str(conn_id)


def resolve_into(meta: dict[str, Any]) -> dict[str, Any]:
    """``meta`` with its connection's transport filled in, or unchanged.

    A campaign that names a connection does not repeat its address. One that
    does not -- every campaign written before this existed -- is left exactly as
    it is, so nothing has to be migrated to keep running.

    Values already in the meta win, so a campaign can still override one field
    of a connection without describing the whole machine again.
    """
    conn_id = str(meta.get("connection") or "").strip()
    if not conn_id:
        return meta
    row = get(conn_id)
    if row is None:
        return meta
    merged = dict(meta)
    for k in _TRANSPORT:
        if k in row and not merged.get(k):
            merged[k] = row[k]
    return merged


def describe() -> str:
    """The list as the agent sees it: names and what each machine is, no secrets."""
    rows = load()
    if not rows:
        return (
            f"No connection is set up in this instance ({store_path()}). "
            "There is no machine to run on until the owner adds one -- ask them "
            "rather than looking for a way in yourself."
        )
    lines = []
    for row in rows:
        bits = [f"{row.get('display_name') or row['id']}   (id {row['id']})"]
        bits += [f"{k} {row[k]}" for k in _SHOWN if row.get(k) not in (None, "")]
        lines.append("  " + "   ".join(bits))
    return (
        f"{len(rows)} connection(s) this instance can run on:\n" + "\n".join(lines) +
        # The id has to be told where to go. Measured 2026-08-19: a loop reached
        # this listing, picked the right machine, wrote "that one is this laptop, I
        # will just run it here", and hand-ran five trials. It held an id with no
        # destination, so it fell back to what it already knew how to do. Naming
        # the next call is the whole difference: the run before this one skipped
        # the listing, was handed ops_declare directly, and used it.
        "\nPick the one the task calls for and hand its id to ops_declare as "
        "'connection' -- that is the one thing a campaign cannot be given later, "
        "and declaring costs nothing and runs nothing. Say which you picked and "
        "why. A machine that happens to be the computer you are on is still that "
        "machine's work: what you would be giving up by running it by hand is the "
        "ledger, the budget and the per-round directory, not distance. If none of "
        "them fits, ask the owner rather than looking for a way in yourself."
    )

def machine_for_path(path: str) -> tuple[str, str] | None:
    """The machine a path belongs to, as ``(id, display_name)``, or None.

    Answers one question and not the other: *whose* directory this is, never
    *where the work should run*. A case can sit on the owner's laptop and belong
    on the 32-core box; only the connection list, read whole, settles that.

    Why this exists. A task statement gives a path, and the first thing anyone
    does with a path is look at it. When the path is on a different machine that
    look fails, and the failure is what makes the loop go and read the machine
    list -- measured 2026-08-19 14:13, three local probes, "the path does not
    exist", then ops_connections eight seconds later. When the path is on THIS
    machine the look succeeds, and success carries no hint that the directory has
    an owner: three runs, three times straight to a local shell, twice writing
    into the owner's case. So the fact travels back the same way the failure does
    -- in the tool's own result, at the moment of looking.

    Claims come from ``paths`` when a connection lists them, and otherwise from
    the absolute paths already written into ``software`` ("CalculiX 2.17
    (/Evermind/...)"). Parsing a free-text field is loose on purpose: a claim
    that is missed costs one line of provenance, and a claim that is wrong costs
    one line that does not apply. Neither refuses anything, which is what makes a
    stale list affordable here.
    """
    import re

    try:
        target = Path(path).expanduser()
        rows = load()
    except Exception:  # noqa: BLE001 -- a look must not depend on this file
        return None
    for row in rows:
        claims = row.get("paths")
        if not isinstance(claims, list) or not claims:
            claims = re.findall(r"(/[^\s(),;:]+)", str(row.get("software") or ""))
        for claim in claims:
            root = Path(str(claim)).expanduser()
            if not _is_specific_enough(root):
                continue
            if target == root or root in target.parents:
                return str(row.get("id") or ""), str(row.get("display_name") or row.get("id") or "")
    return None


# A claim shallower than this is a whole filesystem, not a case: "/", "/opt",
# "/Users/admin". Honouring one would put a line about machines on every ordinary
# look, and a note that fires everywhere is read as noise and then not read at
# all. Three components is the shallowest thing worth claiming
# ("/Evermind/bj_share/lxt" is four; "/srv/arena" is two and is allowed by the
# root list below rather than by depth).
_MIN_CLAIM_DEPTH = 2
_NEVER_CLAIMED = frozenset({
    "/", "/usr", "/opt", "/etc", "/var", "/tmp", "/bin", "/sbin", "/lib",
    "/home", "/Users", "/Applications", "/System", "/Library", "/private",
})


def _is_specific_enough(root: Path) -> bool:
    """Whether a claimed root names a place rather than a filesystem."""
    text = str(root).rstrip("/") or "/"
    if text in _NEVER_CLAIMED:
        return False
    parts = [p for p in root.parts if p not in ("/", "")]
    if len(parts) < _MIN_CLAIM_DEPTH:
        return False
    # A home directory itself: /Users/admin, /home/me. Two components, and every
    # ordinary look happens under it.
    return not (len(parts) == 2 and f"/{parts[0]}" in ("/Users", "/home"))


def provenance_line(path: str) -> str:
    """One line naming the machine a path is on, or "" when nothing claims it."""
    found = machine_for_path(path)
    if not found:
        return ""
    conn_id, name = found
    return (f"\n\nThis is on {name} (machine={conn_id}), one of the machines you have. "
            f"Work that runs there and is worth watching goes through ops_declare and "
            f"ops_submit; ops_connections shows what else is available and what each one has.")
