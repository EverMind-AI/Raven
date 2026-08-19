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

# What the backend needs and the agent does not.
_TRANSPORT = ("host", "port", "user", "key")


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
        "\nPick the one the task calls for and pass its id as 'connection'. "
        "Say which you picked and why. If none of them fits, ask the owner; "
        "do not look for a way in yourself."
    )
