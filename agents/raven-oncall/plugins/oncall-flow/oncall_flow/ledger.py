"""Durable job ledger for the Ops orchestration loop.

The ledger is the on-disk source of truth for what the loop is doing: for each
job (keyed by its idempotency key) it records the backend handle, current
status, and terminal result. After a crash the loop reopens the ledger and
resumes from it — re-driving ``submit`` only for jobs that never got a handle,
and never re-running a job already recorded terminal. Every mutation is
persisted atomically (temp file + ``os.replace``), and both the temp file and
its directory are fsynced before the rename is considered done: the rename
alone survives ``kill -9`` but not a host that loses power, which can leave the
new name pointing at unflushed bytes.

If the file is unreadable anyway, opening raises ``LedgerCorruptError`` rather
than starting from an empty ledger. Starting empty would lose the record of
which jobs already finished, and the loop would resubmit them -- for an on-call
loop whose jobs are expensive and sometimes irreversible, refusing to start is
the safer failure, and it is loud.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oncall_flow.backend import JobHandle, JobResult, JobStatus

_VERSION = 1


class LedgerCorruptError(RuntimeError):
    """The ledger file exists but cannot be read back."""


@dataclass
class JobRecord:
    idem_key: str
    status: JobStatus = JobStatus.PENDING
    campaign: str | None = None
    handle: JobHandle | None = None
    result: JobResult | None = None
    attempts: int = 0
    escalated: bool = False
    # What was actually submitted for this trial. A wake turn is a cold start:
    # the only account of "what did round 0 run" it can read is this ledger, and
    # without the config it had to decode the folded idem_key. Measured
    # 2026-08-17: an arm reconstructed round 0 as "20x6x6 mesh, 50kN" -- neither
    # value was in the run -- by mixing the key with a free-text reference note,
    # and reasoned from that for the rest of the campaign.
    config: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records: dict[str, JobRecord] = {}
        if self._path.exists():
            self._load()

    def get(self, idem_key: str) -> JobRecord | None:
        return self._records.get(idem_key)

    def has(self, idem_key: str) -> bool:
        return idem_key in self._records

    def all(self) -> list[JobRecord]:
        return list(self._records.values())

    def pending(self) -> list[JobRecord]:
        return [r for r in self._records.values() if not r.is_terminal]

    def by_campaign(self, campaign: str) -> list[JobRecord]:
        return [r for r in self._records.values() if r.campaign == campaign]

    def record(self, idem_key: str, *, campaign: str | None = None, config: dict[str, Any] | None = None) -> JobRecord:
        rec = self._records.get(idem_key)
        if rec is None:
            rec = JobRecord(idem_key=idem_key, campaign=campaign, config=config)
            self._records[idem_key] = rec
            self._persist()
        elif config and rec.config is None:
            # A record written before configs were kept, met again on a resubmit.
            rec.config = config
            self._persist()
        return rec

    def set_handle(self, idem_key: str, handle: JobHandle) -> None:
        self._require(idem_key).handle = handle
        self._persist()

    def set_status(self, idem_key: str, status: JobStatus) -> None:
        self._require(idem_key).status = status
        self._persist()

    def set_result(self, idem_key: str, result: JobResult) -> None:
        rec = self._require(idem_key)
        rec.result = result
        rec.status = result.status
        self._persist()

    def bump_attempts(self, idem_key: str) -> int:
        rec = self._require(idem_key)
        rec.attempts += 1
        self._persist()
        return rec.attempts

    def mark_escalated(self, idem_key: str) -> None:
        self._require(idem_key).escalated = True
        self._persist()

    def _require(self, idem_key: str) -> JobRecord:
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


def _record_to_dict(r: JobRecord) -> dict:
    return {
        "idem_key": r.idem_key,
        "status": r.status.value,
        "campaign": r.campaign,
        "handle": {"backend": r.handle.backend, "job_id": r.handle.job_id} if r.handle else None,
        "result": _result_to_dict(r.result) if r.result else None,
        "attempts": r.attempts,
        "escalated": r.escalated,
        "config": r.config,
    }


def _result_to_dict(res: JobResult) -> dict:
    return {
        "status": res.status.value,
        "metrics": res.metrics,
        "output": res.output,
        "error": res.error,
        "deliverable": res.deliverable,
    }


def _record_from_dict(d: dict) -> JobRecord:
    h = d.get("handle")
    res = d.get("result")
    return JobRecord(
        idem_key=d["idem_key"],
        status=JobStatus(d["status"]),
        campaign=d.get("campaign"),
        handle=JobHandle(h["backend"], h["job_id"]) if h else None,
        result=(
            JobResult(
                JobStatus(res["status"]),
                res.get("metrics", {}),
                res.get("output", {}),
                res.get("error"),
                res.get("deliverable"),
            )
            if res
            else None
        ),
        attempts=d.get("attempts", 0),
        config=d.get("config"),
        escalated=d.get("escalated", False),
    )
