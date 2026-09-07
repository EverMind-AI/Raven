"""Machine-wide job occupancy, read across every campaign before a submit.

A machine's registry row can say ``concurrency: 1``, and until tonight nothing
read it at submit time: each campaign scheduled as if it were alone on the box.
Measured 2026-08-31 on the A800 machine -- two campaigns (baseline-a800,
baseline-train-a800) each submitted a trial within two minutes of each other,
both landed on device 0, and the second died of CUDA OOM inside the first one's
48 GiB. The registry knew the machine takes one job at a time; nobody asked it.
The resubmits repeated the collision, because the thing each campaign read --
its own ledger -- was never where the contention was.

The same cross-campaign read answers a second question: whether the exact trial
being submitted is already some other campaign's job. The two campaigns above
carried the same idem_key -- the same config against the same apparatus digest
-- so every unit of spend was being bought twice, and whichever result landed
second would have been a re-measurement nobody ordered.

Both reads live here rather than in the backend: the backend meters one job at
a time and one campaign's directory, so from where it stands every submit looks
alone. Only the ops home sees all the ledgers at once.

Refuse on contradiction, never on uncertainty (the rule the host-side machine
gate wrote down before it was retired): a campaign whose meta names no connection, a registry row with no
``concurrency``, a meta or ledger that cannot be read -- none of those are
evidence of anything, and each leaves the submit exactly as it was.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# How long a handle-less record counts as the machine's reservation. A submit
# takes seconds between `record()` and `set_handle()`; a record still without a
# handle ten minutes later is a crash orphan, and counting it would wedge the
# gate on a job that never started -- the failure the handle-less skip below
# was written against.
RESERVATION_GRACE_S = 600.0

_LOCK_NAME = ".submit.lock"

_NOUN = {"gpus": "device(s)", "cores": "core(s)"}


@dataclass(frozen=True)
class Occupant:
    """One non-terminal job somewhere under the ops home, and what it holds."""

    campaign: str
    idem_key: str
    status: str
    held: dict = field(default_factory=dict)

    def units(self, unit: str) -> int:
        """How many of ``unit`` this job holds. A record from before resources were
        written down holds one: that is what it was billed as."""
        try:
            n = int(self.held.get(unit) or 1)
        except (TypeError, ValueError):
            n = 1
        return max(1, n)


def _campaign_dirs(ops_home: Path) -> list[Path]:
    try:
        return sorted(d for d in ops_home.iterdir() if d.is_dir())
    except OSError:
        return []


def _meta_connection(cdir: Path) -> str:
    try:
        meta = json.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(meta.get("connection") or "")


@contextmanager
def reservation_lock(ops_home: Path) -> Iterator[None]:
    """Serialize check-and-reserve across every submit under this ops home.

    The gate reads every sibling ledger and then this campaign writes its own;
    two submits interleaving between the read and the write both see a free
    machine, both record, and the collision the gate exists for is back
    (reviewed 2026-09-04: a ``concurrency: 1`` machine admitted twice). Held
    only across the checks and the ledger writes, never across the awaited
    backend submit -- the fresh record left behind is what covers that window
    (see ``running_on``). An advisory file lock, so every process sharing the
    ops home takes the same one.
    """
    # The host's portable lock, not fcntl: that module is POSIX-only, and this
    # module is imported by every submit before a backend is chosen, so a bare
    # fcntl import would refuse even a remote job from a Windows-hosted agent.
    from raven.utils.portable_lock import file_lock

    with file_lock(ops_home / _LOCK_NAME):
        yield


def _is_reservation(rec: object, now: float) -> bool:
    reserved_at = getattr(rec, "reserved_at", None)
    return isinstance(reserved_at, (int, float)) and 0 <= now - reserved_at < RESERVATION_GRACE_S


def running_on(ops_home: Path, connection: str) -> list[Occupant]:
    """Non-terminal jobs across every campaign bound to ``connection``.

    A record with a handle is a job on the machine. A record without one is
    the machine's reservation while it is fresh (written seconds ago by a
    submit whose backend call is still in flight) and a crash orphan once it is
    not; reconciliation skips orphans, so counting them would wedge the gate on
    jobs that never started, and they are skipped here too. A concluded
    campaign's records still count while non-terminal: conclusion is a
    statement about the campaign, not a kill, and its job is on the machine
    either way.
    """
    from oncall_flow.ledger import Ledger, LedgerCorruptError

    out: list[Occupant] = []
    now = time.time()
    for cdir in _campaign_dirs(ops_home):
        if _meta_connection(cdir) != connection:
            continue
        try:
            pending = Ledger(cdir / "ledger.json").pending()
        except (OSError, ValueError, LedgerCorruptError):
            continue
        for rec in pending:
            if rec.handle is None and not _is_reservation(rec, now):
                continue
            held = getattr(rec, "resources_held", None)
            status = "reserved" if rec.handle is None else str(getattr(rec.status, "value", rec.status))
            out.append(Occupant(cdir.name, rec.idem_key, status, dict(held) if isinstance(held, dict) else {}))
    return out


def capacity_refusal(
    ops_home: Path,
    connection: str,
    *,
    incoming: int,
    concurrency: object,
    display: str = "",
) -> str | None:
    """Why this batch may not start now, or None.

    Counts every campaign's running jobs on the machine, not this one's: the
    collision this gate exists for was between two campaigns that were each
    correct about themselves.
    """
    if not connection or not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        return None
    occupants = running_on(ops_home, connection)
    free = concurrency - len(occupants)
    if incoming <= free:
        return None
    name = display or connection
    lines = "\n".join(f"  {o.campaign}: {o.idem_key} ({o.status})" for o in occupants)
    can_fit = f"submit at most {free} config(s) in this round, or " if free > 0 else ""
    return (
        f"REFUSED: {name} runs {concurrency} job(s) at a time -- its registry row says so -- "
        f"and {len(occupants)} is/are already on it:\n{lines}\n"
        f"Submitting {incoming} more now would stack them on the same device and the "
        f"newcomer dies of the incumbent's memory (measured 2026-08-31: CUDA OOM). "
        f"{can_fit}wait for the running job(s) -- your wake returns when they land. "
        f"If a listed job is dead, poll its campaign (ops_tune_status) so its record "
        f"turns terminal. Nothing was submitted."
    )


def admission_refusal(
    ops_home: Path,
    connection: str,
    *,
    unit: str,
    capacity: int,
    requests: list[int],
    memory_capacity_gb: int | None = None,
    memory_requests: list[int] | None = None,
    display: str = "",
) -> str | None:
    """Why this batch may not start now, or None -- admitted by what is free.

    The unit is neither the job nor the card (owner's rulings, 2026-09-03): a job
    declares how many devices or cores it holds, the machine declares how many it
    has, and the batch fits when the sum requested fits in what is not held. A
    request larger than the whole machine is refused as such rather than left to
    wait for a day that never comes. Memory is a second dimension, checked only
    when both sides declared it.
    """
    if not connection or unit not in _NOUN or capacity < 1 or not requests:
        return None
    name = display or connection
    noun = _NOUN[unit]
    over = [r for r in requests if r > capacity]
    if over:
        return (
            f"REFUSED: {name} has {capacity} {noun}, and a job holding {max(over)} can never start "
            f"here. Declare it on a machine with that many, or split the work. Nothing was submitted."
        )
    occupants = running_on(ops_home, connection)
    held = sum(o.units(unit) for o in occupants)
    free = capacity - held
    want = sum(requests)
    if want > free:
        lines = "\n".join(
            f"  {o.campaign}: {o.idem_key} ({o.status}, {o.units(unit)} {noun}"
            + (f", ids {','.join(map(str, o.held.get('device_ids') or []))}" if o.held.get("device_ids") else "")
            + ")"
            for o in occupants
        )
        fits = f"Submit what fits in {free}, or " if free > 0 else ""
        return (
            f"REFUSED: {name} has {capacity} {noun} and {held} is/are held:\n{lines}\n"
            f"This round asks for {want}. {fits}wait -- your wake returns when a job lands. "
            f"If a listed job is dead, poll its campaign (ops_tune_status) so its record turns "
            f"terminal. Nothing was submitted."
        )
    if memory_capacity_gb and memory_requests and any(memory_requests):
        held_mem = 0
        for o in occupants:
            try:
                held_mem += int(o.held.get("memory_gb") or 0)
            except (TypeError, ValueError):
                pass
        want_mem = sum(memory_requests)
        if held_mem + want_mem > memory_capacity_gb:
            return (
                f"REFUSED: {name} has {memory_capacity_gb} GB of memory and {held_mem} GB is/are held; "
                f"this round declares {want_mem} GB. Submit what fits in {memory_capacity_gb - held_mem} GB, "
                f"or wait. Nothing was submitted."
            )
    return None


def free_device_ids(ops_home: Path, connection: str, capacity: int) -> list[str]:
    """Device ids not held by any non-terminal job on the machine, lowest first.

    A job that wrote its ids down holds exactly those. A job from before ids
    were written down -- a live record across the upgrade, or a campaign
    declared before this shipped -- holds a device somewhere, and nothing says
    which. It is counted conservatively: it takes the lowest ids still free, as
    many as it is billed for. That is where such a job most likely is (the
    2026-08-31 collision was two campaigns both pinned to device 0), and the
    admission count already keeps the total honest; without this the allocator
    handed a newcomer device 0 while the legacy job sat on it.
    """
    taken: set[str] = set()
    unplaced = 0
    for o in running_on(ops_home, connection):
        ids = o.held.get("device_ids") or []
        if ids:
            taken.update(str(d) for d in ids)
        else:
            unplaced += o.units("gpus")
    free = [str(i) for i in range(max(0, capacity)) if str(i) not in taken]
    return free[unplaced:]


def duplicate_refusal(ops_home: Path, self_dir: Path, idem_key: str) -> str | None:
    """Why this exact trial should not be bought again, or None.

    Same idem_key means same config against the same apparatus digest, so a hit
    in another live campaign is the same measurement ordered twice. Only other
    campaigns are read -- a retry inside one campaign is the ledger's own
    idempotency to handle, so ``self_dir`` (this campaign's directory, compared
    as a path because the directory name is the slug of the campaign name, not
    the name) is skipped -- and a concluded campaign no longer claims its keys:
    re-running a finished experiment's trial under a new campaign is a
    reproduction, which is legitimate work.
    """
    from oncall_flow.ledger import Ledger, LedgerCorruptError

    try:
        self_resolved = self_dir.resolve()
    except OSError:
        self_resolved = self_dir
    for cdir in _campaign_dirs(ops_home):
        try:
            if cdir.resolve() == self_resolved:
                continue
        except OSError:
            pass
        if (cdir / "concluded.json").exists():
            continue
        try:
            rec = Ledger(cdir / "ledger.json").get(idem_key)
        except (OSError, ValueError, LedgerCorruptError):
            continue
        if rec is None:
            continue
        status = str(getattr(rec.status, "value", rec.status))
        return (
            f"{idem_key}: this exact trial (same config, same apparatus) is already "
            f"campaign '{cdir.name}''s job, currently {status}. Read that campaign "
            f"(ops_tune_status) instead of buying the same measurement twice. If this "
            f"campaign supersedes that one, conclude it first; if the two campaigns are "
            f"one piece of work under two names, carry on under one and conclude the other."
        )
    return None
