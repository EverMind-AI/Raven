"""Campaign state for the oncall flow, file-backed under the plugin's stateRoot.

The fork rooted this at its own ops home (``~/.raven/ops``); the plugin owns a
store instead, rooted at the slice's ``stateRoot``. The on-disk shape is the
fork's, kept file for file so the part-two tool ports land on the same layout:
one directory per campaign holding ``ledger.json`` (the durable task ledger),
``meta.json`` (the declaration), ``events.jsonl`` (the trail), ``state_facts.json``
(the probe counter the full state-claims gate extends in part two) and
``concluded.json`` (the terminal marker).

Three fork disciplines are translated whole rather than approximated:

- **The ledger refuses to open corrupt.** It holds which jobs already
  finished; starting empty would resubmit them, and for jobs that are
  expensive and sometimes irreversible, refusing loudly is the safer failure.
- **Every ledger mutation is persisted atomically and fsynced** (temp file +
  ``os.replace`` + directory fsync): the rename alone survives ``kill -9``
  but not a host that loses power.
- **Attendance is counted from the trail, not from the model's word**: looks
  from the probe counter the tool layer writes, wakes from the events the
  scheduling path logs, wall-clock from the declaration timestamp.

Budgets carry the fork's three meters (compute / wall-clock / looks); only the
two off-machine meters are answerable here -- a compute budget's spend belongs
to the backends, which arrive in part two.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

LEDGER_FILE = "ledger.json"
META_FILE = "meta.json"
EVENTS_FILE = "events.jsonl"
FACTS_FILE = "state_facts.json"
CONCLUDED_FILE = "concluded.json"

_VERSION = 1


def campaign_slug(name: str, limit: int = 24) -> str:
    """Directory-safe form of a campaign name; the shared rule so every writer
    (tools, watcher, wake handler) lands in the same campaign directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:limit] or "campaign"


class CampaignStore:
    """The plugin's ops home: one directory per campaign under ``stateRoot``.

    Kept the fork's flat shape (campaign dirs directly under the root) so a
    reader that walks it -- the watcher, a footnote -- skips non-campaign
    entries by the same rule the fork used: no ``ledger.json`` + ``meta.json``
    pair, not a campaign.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def dir_for(self, campaign: str) -> Path:
        return self.root / campaign_slug(campaign)

    def campaign_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.iterdir() if p.is_dir())


# ── Task records and the durable ledger ────────────────────────────


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.SUCCEEDED, TaskStatus.FAILED)


@dataclass(frozen=True)
class TaskHandle:
    """Backend-issued identifier for a submitted job, used to poll and fetch."""

    backend: str
    job_id: str


@dataclass
class TaskRecord:
    """One trial of a campaign, keyed by its idempotency key.

    ``result`` stays a plain dict in part one (the fork's shape:
    status/metrics/output/error/deliverable); the part-two backend port owns
    firming it into a typed result.
    """

    idem_key: str
    status: TaskStatus = TaskStatus.PENDING
    campaign: str | None = None
    handle: TaskHandle | None = None
    result: dict[str, Any] | None = None
    attempts: int = 0
    escalated: bool = False
    config: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


class LedgerCorruptError(RuntimeError):
    """The ledger file exists but cannot be read back."""


class Ledger:
    """Durable task ledger: the on-disk source of truth for what the watch runs.

    After a crash the loop reopens the ledger and resumes from it -- never
    re-running a job already recorded terminal. Unreadable means refuse, not
    restart (see the module docstring).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records: dict[str, TaskRecord] = {}
        if self._path.exists():
            self._load()

    def get(self, idem_key: str) -> TaskRecord | None:
        return self._records.get(idem_key)

    def has(self, idem_key: str) -> bool:
        return idem_key in self._records

    def all(self) -> list[TaskRecord]:
        return list(self._records.values())

    def pending(self) -> list[TaskRecord]:
        return [r for r in self._records.values() if not r.is_terminal]

    def by_campaign(self, campaign: str) -> list[TaskRecord]:
        return [r for r in self._records.values() if r.campaign == campaign]

    def record(self, idem_key: str, *, campaign: str | None = None, config: dict[str, Any] | None = None) -> TaskRecord:
        rec = self._records.get(idem_key)
        if rec is None:
            rec = TaskRecord(idem_key=idem_key, campaign=campaign, config=config)
            self._records[idem_key] = rec
            self._persist()
        elif config and rec.config is None:
            # A record written before configs were kept, met again on a resubmit.
            rec.config = config
            self._persist()
        return rec

    def set_handle(self, idem_key: str, handle: TaskHandle) -> None:
        self._require(idem_key).handle = handle
        self._persist()

    def set_status(self, idem_key: str, status: TaskStatus) -> None:
        self._require(idem_key).status = status
        self._persist()

    def set_result(self, idem_key: str, status: TaskStatus, result: dict[str, Any] | None = None) -> None:
        rec = self._require(idem_key)
        rec.result = result
        rec.status = status
        self._persist()

    def bump_attempts(self, idem_key: str) -> int:
        rec = self._require(idem_key)
        rec.attempts += 1
        self._persist()
        return rec.attempts

    def mark_escalated(self, idem_key: str) -> None:
        self._require(idem_key).escalated = True
        self._persist()

    def _require(self, idem_key: str) -> TaskRecord:
        try:
            return self._records[idem_key]
        except KeyError:
            raise KeyError(f"no ledger record for {idem_key!r}; call record() first") from None

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _VERSION, "records": {k: _record_to_dict(r) for k, r in self._records.items()}}
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)
        # The rename itself needs flushing too, or a host crash can leave the
        # directory entry unwritten and the previous ledger in place -- silently
        # losing the last mutation instead of corrupting it.
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _load(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            self._records = {k: _record_from_dict(v) for k, v in payload.get("records", {}).items()}
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise LedgerCorruptError(
                f"ledger at {self._path} is unreadable ({exc}); "
                "it holds which jobs already finished, so starting fresh could "
                "resubmit them -- move it aside deliberately to start over"
            ) from exc


def _record_to_dict(r: TaskRecord) -> dict:
    return {
        "idem_key": r.idem_key,
        "status": r.status.value,
        "campaign": r.campaign,
        "handle": {"backend": r.handle.backend, "job_id": r.handle.job_id} if r.handle else None,
        "result": r.result,
        "attempts": r.attempts,
        "escalated": r.escalated,
        "config": r.config,
    }


def _record_from_dict(d: dict) -> TaskRecord:
    h = d.get("handle")
    result = d.get("result")
    return TaskRecord(
        idem_key=d["idem_key"],
        status=TaskStatus(d["status"]),
        campaign=d.get("campaign"),
        handle=TaskHandle(h["backend"], h["job_id"]) if h else None,
        result=result if isinstance(result, dict) else None,
        attempts=d.get("attempts", 0),
        escalated=d.get("escalated", False),
        config=d.get("config"),
    )


# ── Declaration, conclusion and the event trail ────────────────────


def read_meta(campaign_dir: str | Path) -> dict[str, Any]:
    """The campaign's declaration; raises to the caller (a watcher skips the
    campaign, a tool refuses the call) rather than inventing an empty one."""
    path = Path(campaign_dir) / META_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"meta at {path} is not an object")
    return data


def write_meta(campaign_dir: str | Path, meta: dict[str, Any]) -> None:
    d = Path(campaign_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (META_FILE + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / META_FILE)


def is_concluded(campaign_dir: str | Path) -> bool:
    return (Path(campaign_dir) / CONCLUDED_FILE).exists()


def conclude(campaign_dir: str | Path, payload: dict[str, Any]) -> None:
    """Mark the campaign over. Every reader honors the marker (the watcher
    skips, part two's scheduling tools refuse new wakes)."""
    d = Path(campaign_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (CONCLUDED_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / CONCLUDED_FILE)


def log_event(campaign_dir: str | Path, kind: str, **fields: Any) -> None:
    """Append one event; swallow all I/O errors (observability never blocks ops)."""
    try:
        d = Path(campaign_dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields}
        with open(d / EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(campaign_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(campaign_dir).expanduser() / EVENTS_FILE
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


# ── Attendance: how much of the watch has been kept ────────────────

# Events that mean "the loop arranged to come back". Both are the loop's own
# decision to keep the watch open; the wake that then fires is the scheduler's,
# and it is not recorded in the campaign's trail.
_WAKE_KINDS = frozenset({"wake_scheduled", "check_later"})


@dataclass(frozen=True)
class Attendance:
    """What the campaign's own record says about the watch being kept.

    ``looks`` is the number of times the tool layer read the world for this
    campaign -- the probe counter written by ``ops_tune_status`` (part two),
    not a count of turns. A wake that read nothing did not look.
    """

    looks: int = 0
    wakes: int = 0
    opened_at: datetime | None = None
    minutes_open: float | None = None


def attendance(campaign_dir: str | Path, *, now: datetime | None = None) -> Attendance:
    """Read the watch's own record. Never raises: a campaign directory that is
    missing, half-written or unreadable reports what could be read of it."""
    cdir = Path(campaign_dir).expanduser()
    looks = _read_probe_seq(cdir)

    wakes = 0
    first_ts: datetime | None = None
    try:
        for event in read_events(cdir):
            if event.get("kind") in _WAKE_KINDS:
                wakes += 1
            if first_ts is None:
                first_ts = _parse(event.get("ts"))
    except Exception:  # noqa: BLE001
        pass

    opened = _declared_at(cdir) or first_ts
    minutes = None
    if opened is not None:
        minutes = max(0.0, ((now or datetime.now()) - opened).total_seconds() / 60.0)
    return Attendance(looks=looks, wakes=wakes, opened_at=opened, minutes_open=minutes)


def _read_probe_seq(cdir: Path) -> int:
    """The probe counter from ``state_facts.json`` -- the one key of the fork's
    state-claims record part one reads; the full gate arrives in part two on
    the same file. An unreadable record is zero looks, not a crash."""
    path = cdir / FACTS_FILE
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("probe_seq") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _declared_at(cdir: Path) -> datetime | None:
    """When the campaign was declared, from its declaration.

    Preferred over the first event because an amended declaration is still the
    same watch: the clock a wall-clock budget runs against started when the
    owner asked for the watch."""
    path = cdir / META_FILE
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamped = _parse(meta.get("declared_at"))
    if stamped is not None:
        return stamped
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


# ── Budgets: how much of what, who measures it ─────────────────────

SHARED = "shared"
ADDITIVE = "additive"

# Who holds the reading for this budget's unit.
COMPUTE = "compute"  # the machine: core-minutes, gpu-minutes, licence-hours
WALL_CLOCK = "wall-clock"  # the clock: how long the watch has been open
LOOKS = "look"  # the campaign's own record: one per time it read the world

_METERS = frozenset({COMPUTE, WALL_CLOCK, LOOKS})


@dataclass(frozen=True)
class Budget:
    """A declared allowance: how much of what, who measures it, and how
    concurrent use adds up."""

    unit: str
    total: float
    overlap: str = SHARED
    meter: str = COMPUTE

    @property
    def is_additive(self) -> bool:
        return self.overlap == ADDITIVE

    @property
    def off_machine(self) -> bool:
        """Whether the spend has to be read from the campaign rather than the
        host: a host asked for the spend on a wall-clock budget answers with
        the machine time its jobs used, which for a watch that ran no jobs is
        zero however long it has been watching."""
        return self.meter in (WALL_CLOCK, LOOKS)


def budget_from_meta(meta: dict) -> Budget | None:
    """The campaign's declared budget, or ``None`` when it declared none.

    ``None`` is a first-class answer and not a zero: plenty of work has no
    budget, and inventing a default would put a number nobody chose in front
    of a loop that cannot tell it from one the operator set.
    """
    declared = meta.get("budget")
    if not (isinstance(declared, dict) and declared.get("total") is not None):
        return None
    try:
        total = float(declared["total"])
    except (TypeError, ValueError):
        return None
    overlap = str(declared.get("overlap") or SHARED)
    meter = str(declared.get("meter") or COMPUTE)
    return Budget(
        unit=str(declared.get("unit") or "unit"),
        total=total,
        overlap=overlap if overlap in (SHARED, ADDITIVE) else SHARED,
        # Never inferred from the unit. "minute" is a wall-clock minute on a
        # watch and a core-minute on a solver campaign, and guessing puts a
        # spend nobody measured against a total somebody set.
        meter=meter if meter in _METERS else COMPUTE,
    )


def off_machine_spend(campaign_dir: str | Path, declared: Budget, *, now: datetime | None = None) -> float | None:
    """What has been spent against a budget the host cannot measure, or None.

    ``None`` for a compute budget -- not zero. This function is not the one
    that knows about machine time, and answering zero would put a figure in
    front of a loop that reads as "nothing spent yet".
    """
    if not declared.off_machine:
        return None
    kept = attendance(campaign_dir, now=now)
    if declared.meter == LOOKS:
        return float(kept.looks)
    if declared.meter == WALL_CLOCK:
        return kept.minutes_open
    return None


__all__ = [
    "ADDITIVE",
    "Attendance",
    "Budget",
    "CampaignStore",
    "COMPUTE",
    "CONCLUDED_FILE",
    "EVENTS_FILE",
    "FACTS_FILE",
    "LEDGER_FILE",
    "LOOKS",
    "Ledger",
    "LedgerCorruptError",
    "META_FILE",
    "SHARED",
    "TaskHandle",
    "TaskRecord",
    "TaskStatus",
    "WALL_CLOCK",
    "attendance",
    "budget_from_meta",
    "campaign_slug",
    "conclude",
    "is_concluded",
    "log_event",
    "off_machine_spend",
    "read_events",
    "read_meta",
    "write_meta",
]
