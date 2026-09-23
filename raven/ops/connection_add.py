"""The write half of the machine registry: reach a machine, then write it down.

:mod:`raven.ops.connections` reads the owner's machines and stays read-only on
purpose. This module is the one door that adds a row, and it is the same door
whichever surface knocks: the owner in a terminal (``raven ops connection
add``), or an agent that was told about a machine in conversation
(``ops_connection_add``). Until 2026-09-06 the host ran the command on the
owner's behalf before dispatching a sub-agent; that step went with the
pre-dispatch registry gate, and the write moved to the on-call product's own
plugin. It sits here now because the coding agent runs on the owner's machines
too -- it builds a case where the solver is and smoke-tests it there -- and a
second copy of the probe in a second plugin is the drift this tree already paid
for once (five byte-identical registries on one computer, 2026-08-25).

Two things keep what the owner is asked short. What a machine can say about
itself -- cores, memory, devices -- is read off it once reached, never typed.
And what the owner leaves out of the way in -- port, username, key path -- is
resolved the way their own terminal would resolve it (``ssh -G``, config
blocks included), then tried: the probe decides, and only what connected is
written. Nothing is written until the machine itself answers; an unreachable
row in the registry is a fact every later turn acts on, an absent one is a
question the loop knows to ask.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

# The reader is reached through its module at call time, not bound at import:
# where the store is gets decided per call (see ``connections.store_path``),
# and a test that stands a scratch registry in patches it there.
from raven.ops import connections
from raven.ops.connections import LOCAL, SSH, UNREADABLE, transport_of

# What the owner has to be asked for, when the registry lists nothing that
# fits. Shared text: the on-call agent's listing reply and the coding agent's
# guide both point at it, so the questions are spelled once. The loop cannot
# fill any of it in -- the address and the key are deliberately outside what it
# can see -- and it must not guess: a guess that lands in the registry stops
# being a guess and becomes what every later turn reads as fact.
ASK_OWNER = """\
Ask the owner, in one message, and stop there until they answer:
  - is the machine this very computer, or another one reached over ssh?
  - what do they call it?
  - if another one: its address -- and port, username and private-key path
    only if they know them; left out, ssh's own config is tried and whatever
    connects is kept
Optional, only if they care to say: what is installed on it (with paths),
which directories on it hold their work, and how its budget is counted.

With the answers, call ops_connection_add: it reaches the machine before
writing anything, and what the machine says about itself is read off it.
Until a machine is listed there is nothing to run on there. Do not look for a
way in yourself -- not with exec, not with an ssh typed from an address in the
task statement."""

# What the machine is asked about itself. Read once reached, never typed twice.
PROBE_SCRIPT = (
    "echo CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null); "
    "echo MEM=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}'); "
    "echo GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | paste -sd'|' -); "
    "echo LIBC=$(ldd --version 2>/dev/null | head -1)"
)


def ssh_defaults(host: str, *, port: int = 0, user: str = "") -> dict[str, Any]:
    """What ssh itself would use for this host: ``port``, ``user``, ``keys``.

    ``ssh -G`` prints the resolved configuration -- ``~/.ssh/config`` blocks
    included -- so an owner who left the port, the user or the key path out
    gets the values their own ssh would pick, not a guess. ``keys`` lists only
    identity files that exist here, in ssh's own order; the caller probes each
    and keeps the one that connects. Empty when ssh is not on this computer.
    """
    argv = ["ssh", "-G"] + (["-p", str(port)] if port else []) + [f"{user}@{host}" if user else host]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if done.returncode != 0:
        return {}
    out: dict[str, Any] = {"keys": []}
    for line in done.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "port" and value.strip().isdigit():
            out["port"] = int(value)
        elif key == "user" and value.strip():
            out["user"] = value.strip()
        elif key == "identityfile" and value.strip() and Path(os.path.expanduser(value.strip())).exists():
            out["keys"].append(value.strip())
    return out


def probe(row: dict[str, Any], *, timeout: float = 30.0, isolate_key: bool = False) -> tuple[bool, str, dict[str, Any]]:
    """Reach the machine and read what it is. ``(reached, message, detected)``.

    The reaching is the point and the reading is the bonus. A row that cannot be
    reached is not written, because an unreachable row in the registry is worse
    than an absent one: absent is a question the loop knows to ask, unreachable
    is a fact it acts on. Rides :mod:`raven.ops.transport`, the seam ``exec``'s
    machine channel reaches a machine through, so the way a machine is reached
    here is the way it will be reached afterwards.

    ``isolate_key`` is for the one caller that has to say WHICH key opened the
    session rather than merely that one opened: trying the keys ssh named when
    the owner gave no path. Without it ``-i`` is a preference and not a
    restriction -- the other configured identities and the agent are offered too
    -- so the first candidate could be credited with a session a different key
    authenticated, and the registry would then hold a path that stops working
    the day that agent or config changes.
    """
    from raven.ops.transport import TransportError, make_ssh_runner, runner_from

    where = "this computer" if transport_of(row) == LOCAL else f"{row.get('user')}@{row.get('host')}:{row.get('port')}"
    try:
        if isolate_key and transport_of(row) == SSH:
            runner = make_ssh_runner(
                str(row.get("host") or ""),
                int(row.get("port") or 22),
                os.path.expanduser(str(row.get("key") or "")),
                user=str(row.get("user") or "root"),
                identities_only=True,
            )
        else:
            runner = runner_from(row, cap_seconds=timeout)
        code, out = runner(PROBE_SCRIPT)
    except TransportError as exc:
        return False, f"could not reach {where}: {exc}", {}
    except FileNotFoundError:
        return False, f"could not reach {where}: ssh not found on this computer", {}
    if code != 0:
        return False, f"could not reach {where}: {out.strip() or f'exit {code}'}", {}
    return True, f"reached {where}", parse_probe(out)


def parse_probe(out: str) -> dict[str, Any]:
    """The row fields a probe's output names. Only what parsed cleanly."""
    seen: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if value.strip():
            seen[key.strip()] = value.strip()
    found: dict[str, Any] = {}
    if seen.get("CORES", "").isdigit():
        found["cores"] = int(seen["CORES"])
    if seen.get("MEM", "").isdigit() and int(seen["MEM"]) > 0:
        found["memory"] = f"{seen['MEM']} GB"
    gpu = seen.get("GPU", "")
    if gpu:
        cards = [c.strip() for c in gpu.split("|") if c.strip()]
        # The count is what admission reads (`gpus`), and the device line is
        # written as "N x <card>" when the cards match, which is the other
        # spelling the readers infer a count from. Left as a "+"-joined list,
        # the row a real two-card box probed to (2026-09-07) had no count at all,
        # and admission fell back to job count on a machine it could have gated
        # by device.
        found["gpus"] = len(cards)
        found["device"] = f"{len(cards)} x {cards[0]}" if len(set(cards)) == 1 else " + ".join(cards)
        found["kind"] = "gpu"
    elif "cores" in found:
        found["kind"] = "cpu"
    if seen.get("LIBC"):
        found["note"] = seen["LIBC"]
    return found


def write(row: dict[str, Any]) -> Path:
    """Append one machine, keeping whatever shape the file already had.

    Refuses rather than rewrites when the file holds entries the reader skips:
    serialising the survivors would erase rows the owner wrote by hand, and a
    recoverable typo is theirs to fix, not ours to delete on the way past. A
    file that does not exist yet is started here -- the first machine an agent
    adds for an owner who never ran the command is the common case.
    """
    path = connections.store_path()
    found = connections.read()
    if found.state == UNREADABLE:
        raise ValueError(f"{found.detail}\nFix or move that file before adding to it.")
    if found.detail:
        raise ValueError(
            f"{found.detail}\nAdding a machine here would rewrite the file without them. "
            "Give those entries an id (or remove them) first, then add this machine."
        )
    rows = list(found.rows)
    if any(str(r.get("id")).strip() == row["id"] for r in rows):
        raise ValueError(f"a machine with id {row['id']!r} is already listed in {path}")
    rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"connections": rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


ALIAS_MARK = "# raven connection {conn_id} (managed; rewritten on every add)"


def write_ssh_alias(row: dict[str, Any]) -> str | None:
    """Keep a ``Host <id>`` alias in ``~/.ssh/config`` for an ssh row.

    Transfers cannot ride the exec machine channel -- rsync and scp run their
    client HERE and only address the machine -- so without an alias every
    transfer carries the raw address, and the raw address then has to live in
    the model's context (measured 2026-09-03: a task statement shipped the
    ssh string because nothing else could address the machine). With the
    alias, ``rsync ... <id>:...`` resolves inside ssh's own config and the
    context carries only the registry name.

    One managed block per id, replaced in full on re-add; everything outside
    the markers is the owner's and is never touched. Returns the alias, or
    None when there is nothing to write (a local row) -- a failure is
    reported by the caller as a warning, never as a refusal: the alias is a
    convenience beside the registry, not part of it.
    """
    if transport_of(row) == LOCAL:
        return None
    conn_id = str(row["id"])
    mark = ALIAS_MARK.format(conn_id=conn_id)
    block = "\n".join(
        [
            mark,
            f"Host {conn_id}",
            f"  HostName {row.get('host')}",
            f"  Port {int(row.get('port') or 22)}",
            f"  User {row.get('user') or 'root'}",
            f"  IdentityFile {row.get('key') or '~/.ssh/id_rsa'}",
            mark,
        ]
    )
    path = Path(os.path.expanduser("~/.ssh/config"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if mark in text:
        head, _, rest = text.partition(mark)
        _, _, tail = rest.partition(mark)
        text = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
    text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return conn_id
