"""The machines this instance can reach, named by their owner.

A campaign that carries its own ``host``/``port``/``key`` puts the way in
inside the thing being run, and leaves the agent to work the connection out for
itself: a statement that does not spell the port out sends it guessing through
22, 2222, 8022, ``~/.ssh/config`` and a stale ``known_hosts`` entry.

None of that is the agent being slow. It had no way to reach a machine except to
guess at one, so guessing is what it did.

Here a machine is something the owner sets up once and names -- "my CPU box",
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

Trunk's :mod:`raven.ops.connections` is the reader and
:mod:`raven.ops.connection_add` the writer; both are imported here rather than
mirrored. This module kept a byte-aligned copy of the reader from the vendored
fork until 2026-09-23, on the ground that trunk's module was not a contracts
paper; the plugin already imported trunk's utilities and hooks, the copy had to
be re-aligned by hand on every change, and the coding agent came to need the
same functions -- so a third copy was the alternative. What stays here is what
only a campaign needs: looking a connection up by id, its owner-facing name,
filling a campaign's meta from its row, and the listing the on-call model reads.
"""

from __future__ import annotations

from typing import Any

# The reader, whole. Re-exported by name so every caller in this plugin keeps
# its ``connections.<name>`` spelling.
from raven.ops.connection_add import ASK_OWNER
from raven.ops.connections import (  # noqa: F401 -- re-exports
    LOCAL,
    MISSING,
    OK,
    SHOWN,
    SSH,
    STORE,
    UNREADABLE,
    Problem,
    Read,
    capacity,
    load,
    problems,
    read,
    resource_unit,
    row_problems,
    shown,
    store_path,
    transport_of,
    usable,
)

_SHOWN = SHOWN

# What the backend needs and the agent does not. ``transport`` is in here rather
# than in SHOWN on purpose: whether a machine is reached over ssh or is simply
# this one changes nothing about which machine the work calls for, and a field
# the agent can see is a field it can reason about wrongly -- here, by deciding
# it may skip naming the machine at all, which loses the record of where a
# command ran.
_TRANSPORT = ("host", "port", "user", "key", "transport")


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


# The ask is trunk's (the coding agent's guide points at the same text); what
# follows it is this product's: there is no campaign yet, so the turn ends on
# the question.
_REQUEST = (
    ASK_OWNER + "\nThere is no campaign yet, so end the turn with the question; whoever called you "
    "relays the answer. Do not submit anything until a machine is listed."
)


def describe() -> str:
    """The list as the agent sees it: names and what each machine is, no secrets."""
    found = read()
    if found.state == UNREADABLE:
        # Deliberately not folded into "there are none". There may well be
        # machines in that file, and saying otherwise sends the loop off to find
        # its own way onto a box the owner has already described.
        return (
            f"The machine registry for this instance cannot be read.\n{found.detail}\n\n"
            "This is NOT the same as having no machines: the file may well list "
            "several, and this instance cannot see any of them. Say exactly this "
            "to the owner -- the file needs fixing, or rewriting with "
            "`raven ops connection add`. Do not submit anything and do not look "
            "for a way in yourself."
        )
    rows = load()
    if not rows:
        where = "is empty" if found.state == OK else "does not exist"
        return f"No machine is set up in this instance ({store_path()} {where}).\n\n" + _REQUEST
    # Only what makes a machine unusable. The rest is the owner's to fix and
    # `raven ops connection doctor` is where they read it: a line that appears on
    # every listing is read as noise and then not read at all, which is the same
    # reason a shallow path claim is not honoured below.
    faults = [f for f in problems() if f.blocking]
    lines = []
    for row in rows:
        bits = [f"{row.get('display_name') or row['id']}   (id {row['id']})"]
        bits += [f"{k} {row[k]}" for k in SHOWN if row.get(k) not in (None, "")]
        lines.append("  " + "   ".join(bits))
    trouble = (
        "\n\nOne or more of these cannot be used as written, and picking it will "
        "fail:\n  "
        + "\n  ".join(str(f) for f in faults)
        + "\nSay this to the owner; `raven ops connection doctor` reports the same."
        if faults
        else ""
    )
    return (
        f"{len(rows)} connection(s) this instance can run on:\n" + "\n".join(lines) + trouble +
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
        # And what to declare it AS. This listing is the first stop for anything
        # that needs a machine, so the fork belongs here rather than in a document
        # -- measured twice on 2026-08-21, a watch task came through here, picked
        # the right machine off this very text, and then went and built its own
        # monitor out of write_file and cron. Nothing on the way had said that a
        # campaign can be a watch: every sign said case, trial, round.
         + "\nWhat kind of target it is goes with it, and there are three. "
        "objective_kind='optimize' when one number the run reports has to go as "
        "far as it will go. 'complete' when it has to run to its own end and the "
        "result has to hold up, with no number ranking the rounds. 'condition' "
        "when nothing is being run at all and something outside has to become "
        "true -- a price, a disk filling, a queue, somebody else's job. That last "
        "one is still a campaign: give it a readings table (what to read, and "
        "whether to read it every wake or after each round), and its starting "
        "value is taken as you declare it, which is the one thing a later wake "
        "cannot reconstruct. Its budget can be counted in looks rather than in "
        "machine time, and coming back is ops_check_later. A cron job and a file "
        "of your own do the same arithmetic with none of the record."
    )


# What a machine is asked about itself, the moment it is reached. Read off the
# box rather than typed by the owner, so the row cannot say 64 cores about a
# machine with 32. Mirrors the CLI's probe line.
_PROBE = (
    "echo CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null); "
    "echo MEM=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}'); "
    "echo GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | paste -sd'|' -); "
    "echo LIBC=$(ldd --version 2>/dev/null | head -1)"
)
