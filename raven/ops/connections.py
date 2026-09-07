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

Read-only here: the file is written by hand or by
``raven ops connection add`` (``raven/cli/ops_connection_commands.py``). What an
agent is *shown*, and how a campaign is given a way onto the machine it names,
belong to that agent's own installation and are not rendered here; what this
module offers a caller is the store behind the owner's command, the faults that
make a row unusable, and the projection (:func:`shown`) that keeps an address
out of whatever gets handed on.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STORE = "connections.json"

SSH, LOCAL = "ssh", "local"

# Without these there is no way onto the machine, so a row missing one cannot be
# used for anything and saying so is a refusal. The list is short on purpose:
# every row that existed before this file was written predates most of the fields
# below, and those rows work. A check that retroactively condemns a working
# registry is a check the owner turns off.
_BLOCKING = ("id", "display_name")
_BLOCKING_SSH = ("host", "port", "user", "key")

# Wanted, and reported, but never a refusal. ``software`` is the deciding one for
# picking a machine -- measured 2026-08-17, CalculiX runs only on the box with
# the A800s because the binary needs a glibc the 32-core box does not have, so a
# rule like "a CPU-only solver belongs on the CPU box" picks the one machine that
# cannot run it. ``budget_unit`` and ``concurrency`` belong to the machine and
# were being restated in every campaign's meta instead.
_WANTED = ("software", "budget_unit", "concurrency")

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Where a machine's budget is metered: a property of the machine, so it is not
# restated in the meta of every campaign run on it.
_BUDGET_UNITS = ("minute", "core-minute", "gpu-minute")

# Names that mean a field this file reads, spelled the way someone writing the
# file by hand reaches for first: without them a row written with ``name``
# lists as its bare id, and nothing says why.
_MISSPELLED = {
    "name": "display_name",
    "hostname": "host",
    "address": "host",
    "username": "user",
    "identity_file": "key",
    "ssh_key": "key",
    "gpu": "device",
    "cpus": "cores",
    "ram": "memory",
}


def transport_of(row: dict[str, Any]) -> str:
    """``ssh`` or ``local``. Absent means ssh, which is what the runner assumes."""
    return LOCAL if str(row.get("transport") or SSH).strip().lower() == LOCAL else SSH


# How many devices a ``device`` line written as "2 x NVIDIA A800..." names.
_DEVICE_COUNT = re.compile(r"^\s*(\d+)\s*[xX]\s")
# A memory line such as "463 GB" or "1.5 TiB", read only when admission asks.
_MEMORY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(TiB|TB|T|GiB|GB|G)\b", re.IGNORECASE)


def capacity(row: dict[str, Any]) -> dict[str, int]:
    """What a row can hand out, in whole units. Empty when it says nothing countable.

    ``gpus`` is read first; absent, a ``device`` written as "N x ..." names the
    count. ``cores`` is the row's own field. ``memory_gb`` is parsed from the
    memory text when it parses at all. A row that hands out nothing here is
    admitted by job count, the way every row was before capacity existed.
    """
    out: dict[str, int] = {}
    g = row.get("gpus")
    if isinstance(g, int) and not isinstance(g, bool) and g >= 1:
        out["gpus"] = g
    elif str(row.get("kind") or "").strip().lower() == "gpu":
        # The "N x ..." reading is for a GPU row only, as the admission design
        # says: a CPU box whose device line reads "2 x Intel Xeon" hands out
        # cores, and reading it as two devices would gate it on GPUs it has not.
        m = _DEVICE_COUNT.match(str(row.get("device") or ""))
        if m and int(m.group(1)) >= 1:
            out["gpus"] = int(m.group(1))
    c = row.get("cores")
    if isinstance(c, int) and not isinstance(c, bool) and c >= 1:
        out["cores"] = c
    m = _MEMORY.match(str(row.get("memory") or ""))
    if m:
        n = float(m.group(1))
        if m.group(2).lower().startswith("t"):
            n *= 1024
        if n >= 1:
            out["memory_gb"] = int(n)
    return out


def resource_unit(row: dict[str, Any]) -> str:
    """``gpus``, ``cores``, or "" for a row admitted by job count.

    A GPU machine's cores are not what its jobs contend for, so a row that hands
    out devices is gated on those alone -- and a GPU row whose device count is
    unknown is gated by job count, never by its cores (reviewed 2026-09-07: a
    probed two-card box with no count was admitted by its 128 cores, sixteen
    jobs deep). The doctor names such a row; `gpus: N` or a "N x ..." device
    line moves it onto device admission.
    """
    cap = capacity(row)
    if "gpus" in cap:
        return "gpus"
    if str(row.get("kind") or "").strip().lower() == "gpu":
        return ""
    if "cores" in cap:
        return "cores"
    return ""


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a row. ``blocking`` means the machine cannot be used.

    The two levels are not decoration. A blocking problem is the ground a caller
    refuses on -- an agent reading this list to pick a machine has nowhere to put
    the work without one -- while the rest is worth telling the owner and worth
    nobody's refusal. Folding them together would mean a registry that predates a field
    reads as broken, and a check that condemns working machines gets turned off.
    """

    text: str
    blocking: bool = False

    def __str__(self) -> str:
        return self.text


def row_problems(row: dict[str, Any]) -> list[Problem]:
    """Everything wrong with one row, in the owner's terms. Empty when it is fine.

    Judged here, next to the reader, rather than in whatever wrote the file. Every
    connections.json that has existed so far was written by hand, and a
    hand-written file has to be judged by what reads it or by nothing at all.
    """
    rid = str(row.get("id") or "").strip()
    label = rid or "<no id>"
    out: list[Problem] = []
    raw_id = str(row.get("id") or "")
    if not rid:
        out.append(Problem("a machine here has no id", blocking=True))
    elif raw_id != rid:
        # Consumers normalise differently: `shown` hands the raw id on, while
        # whoever asks for a machine by name has usually stripped it, and
        # `usable` collects its duplicate set on stripped ids, so two spellings
        # of one id are seen as one. A padded id would otherwise read as usable
        # and then be unselectable. Refused at the row instead of taught to
        # every reader.
        out.append(Problem(f"{label}: id has leading or trailing whitespace", blocking=True))
    elif not _ID.match(rid):
        out.append(Problem(f"{label}: id must be lowercase letters, digits, '-' or '_'", blocking=True))
    for wrong, right in _MISSPELLED.items():
        if wrong in row and right not in row:
            out.append(Problem(f"{label}: '{wrong}' is not a field this reads; it is spelled '{right}'"))
    kind = transport_of(row)
    blocking = _BLOCKING + (_BLOCKING_SSH if kind == SSH else ())
    for field in blocking:
        if field != "id" and row.get(field) in (None, ""):
            out.append(Problem(f"{label}: '{field}' is missing, so there is no way onto it", blocking=True))
    for field in _WANTED:
        if field == "concurrency" and resource_unit(row):
            continue
        if row.get(field) in (None, ""):
            out.append(Problem(f"{label}: '{field}' is not set"))
    port = row.get("port")
    if port not in (None, "") and (isinstance(port, bool) or not isinstance(port, int)):
        out.append(Problem(f"{label}: 'port' must be a number, not {port!r}", blocking=True))
    elif isinstance(port, int) and not 1 <= port <= 65535:
        out.append(Problem(f"{label}: 'port' {port} is not a port number", blocking=True))
    conc = row.get("concurrency")
    if conc not in (None, "") and (isinstance(conc, bool) or not isinstance(conc, int) or conc < 1):
        out.append(Problem(f"{label}: 'concurrency' must be a whole number of jobs, not {conc!r}"))
    gp = row.get("gpus")
    if gp not in (None, "") and (isinstance(gp, bool) or not isinstance(gp, int) or gp < 1):
        out.append(Problem(f"{label}: 'gpus' must be a whole number of devices, not {gp!r}"))
    held_unit = resource_unit(row)
    if str(row.get("kind") or "").strip().lower() == "gpu" and held_unit != "gpus":
        out.append(
            Problem(
                f"{label}: kind is gpu but neither 'gpus' nor a device written as 'N x ...' says how many "
                "devices it hands out; jobs are admitted by job count until it does"
            )
        )
    elif held_unit and conc not in (None, ""):
        out.append(
            Problem(
                f"{label}: 'concurrency' is not read on a row that says {held_unit}; capacity decides how many jobs fit"
            )
        )
    unit = row.get("budget_unit")
    if unit not in (None, "") and str(unit) not in _BUDGET_UNITS:
        out.append(Problem(f"{label}: 'budget_unit' should be one of {', '.join(_BUDGET_UNITS)}, not {unit!r}"))
    paths = row.get("paths")
    if paths is not None:
        if not isinstance(paths, list):
            out.append(Problem(f"{label}: 'paths' must be a list of absolute paths"))
        else:
            for claim in paths:
                if not str(claim).startswith("/"):
                    out.append(Problem(f"{label}: path {claim!r} is not absolute"))
                elif not _is_specific_enough(Path(str(claim))):
                    out.append(Problem(f"{label}: path {claim!r} names a filesystem rather than a place"))
    return out


def usable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The machines that can actually be run on. What a caller refuses over.

    A duplicated id takes every row that carries it out, not just the later
    one: the id is what a campaign stores and what anything asking for a
    machine by name looks up, so two rows behind one id make the reference
    ambiguous -- which row answers depends on file order, and "the first one"
    is an accident, not an answer.
    Judged here rather than only in :func:`problems` because this is the list
    the readers and the doctor's exit code key on; a blocking
    diagnostic that leaves the registry usable is a warning nobody refuses
    over (measured: two valid rows sharing an id kept usable=2 and doctor
    exit 0 while problems() reported a blocking defect).
    """
    ids = [str(r.get("id") or "").strip() for r in rows]
    duplicated = {i for i in ids if i and ids.count(i) > 1}
    return [
        r
        for r in rows
        if str(r.get("id") or "").strip() not in duplicated and not any(p.blocking for p in row_problems(r))
    ]


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
_SHOWN = ("kind", "device", "gpus", "cores", "memory", "software", "budget_unit", "concurrency", "note")

# Points this instance at a registry that is not beside its own config. Set by
# a launcher that hosts raven inside another raven: the machines belong to the
# owner, not to whichever sub-agent is asking, and every copy taken to keep an
# instance supplied is a copy that stops being true the day the owner adds a
# machine. Five byte-identical copies existed on this computer on 2026-08-25,
# and the launcher that made them copied once and never again.
CONNECTIONS_ENV = "RAVEN_CONNECTIONS"


def store_path() -> Path:
    """Where this instance reads its machines: the env var, else beside its config."""
    override = os.environ.get(CONNECTIONS_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from raven.config.paths import get_config_path

    try:
        return Path(get_config_path()).expanduser().parent / STORE
    except Exception:  # noqa: BLE001 -- a missing config path is not a failure here
        return Path.home() / ".raven" / STORE


MISSING, UNREADABLE, OK = "missing", "unreadable", "ok"


@dataclass(frozen=True)
class Read:
    """The registry as this instance found it, and why it looks that way.

    The state is separate from the rows because "there are no machines" and "the
    machine list could not be read" are different facts and were being reported
    as one. A hand-edited file with a stray comma parsed as ``ValueError``,
    became ``[]``, and the listing built on it then told the loop that the owner
    had set no machine up -- so a typo silently took the whole on-call surface
    out, and the one thing the loop was told about it was false.
    """

    rows: list[dict[str, Any]]
    state: str
    detail: str = ""


def read() -> Read:
    """The registry, with the reason behind an empty one. Never raises."""
    path = store_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Read([], MISSING)
    except OSError as exc:
        return Read([], UNREADABLE, f"{path} could not be opened: {exc}")
    try:
        data = json.loads(text)
    except ValueError as exc:
        return Read([], UNREADABLE, f"{path} is not valid JSON: {exc}")
    rows = data.get("connections") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return Read(
            [],
            UNREADABLE,
            f"{path} should hold a list of machines, either as the whole file or "
            "under a 'connections' key; it holds neither.",
        )
    out = [r for r in rows if isinstance(r, dict) and str(r.get("id") or "").strip()]
    dropped = len(rows) - len(out)
    detail = f"{dropped} entr{'y' if dropped == 1 else 'ies'} in {path} had no id and were skipped."
    return Read(out, OK, detail if dropped else "")


def load() -> list[dict[str, Any]]:
    """Every connection, in file order. An unreadable or absent file is none."""
    return read().rows


def problems() -> list[Problem]:
    """Everything wrong with the registry as a whole: the file, then each row."""
    found = read()
    if found.state == UNREADABLE:
        return [Problem(found.detail, blocking=True)]
    out = [Problem(found.detail)] if found.detail else []
    seen: dict[str, int] = {}
    for row in load():
        out.extend(row_problems(row))
        rid = str(row.get("id") or "").strip()
        seen[rid] = seen.get(rid, 0) + 1
    out.extend(
        Problem(f"{rid}: appears {n} times; an id has to name one machine", blocking=True)
        for rid, n in seen.items()
        if n > 1
    )
    return out


def shown(row: dict[str, Any]) -> dict[str, Any]:
    """One machine as anything outside may see it: no address, no credential.

    Handed over as data so a caller in another process can put it in front of
    whoever needs it. The split is the point of the function: what a machine
    *is* travels, and the way onto it does not -- a field the reader cannot use
    is one more thing to reason about wrongly, and the reader here is whatever
    an agent's own installation shows the model.
    """
    out = {"id": str(row.get("id") or ""), "display_name": str(row.get("display_name") or row.get("id") or "")}
    out.update({k: row[k] for k in _SHOWN if row.get(k) not in (None, "")})
    if isinstance(row.get("paths"), list):
        out["paths"] = [str(x) for x in row["paths"]]
    return out


# A claim shallower than this is a whole filesystem, not a case: "/", "/opt",
# "/Users/admin". Honouring one would put a line about machines on every ordinary
# look, and a note that fires everywhere is read as noise and then not read at
# all. Three components is the shallowest thing worth claiming
# ("/Evermind/bj_share/lxt" is four; "/srv/arena" is two and is allowed by the
# root list below rather than by depth).
_MIN_CLAIM_DEPTH = 2
_NEVER_CLAIMED = frozenset(
    {
        "/",
        "/usr",
        "/opt",
        "/etc",
        "/var",
        "/tmp",
        "/bin",
        "/sbin",
        "/lib",
        "/home",
        "/Users",
        "/Applications",
        "/System",
        "/Library",
        "/private",
    }
)


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
