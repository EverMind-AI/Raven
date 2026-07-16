"""Durable run state: atomic JSON writes + the run_meta.json lifecycle.

Two invariants the resume story depends on:

- Every JSON this layer persists goes through :func:`atomic_write_json`
  (write tmp + rename), so a crash can never leave a half-written file that
  poisons the next resume.
- ``run_meta.json`` carries the config snapshot (a run's configuration must
  not drift mid-run — SOP §0 discipline, enforced here rather than by memory)
  and the one-way ``unsealed_at`` stamp (once test numbers are revealed,
  continuing the run would leak them into decisions).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

META_FILENAME = "run_meta.json"


def atomic_write_json(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json_or(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def config_fingerprint(snapshot: dict) -> str:
    """Stable hash of the effective run configuration (order-insensitive)."""
    canon = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunMeta:
    """The per-run lifecycle record under ``work_dir/run_meta.json``."""

    work_dir: Path
    created_at: str = ""
    config_snapshot: dict = field(default_factory=dict)
    config_hash: str = ""
    unsealed_at: Optional[str] = None
    finalize_reason: Optional[str] = None

    @property
    def path(self) -> Path:
        return Path(self.work_dir) / META_FILENAME

    @classmethod
    def load(cls, work_dir: Path) -> Optional["RunMeta"]:
        data = load_json_or(Path(work_dir) / META_FILENAME, None)
        if not isinstance(data, dict):
            return None
        return cls(
            work_dir=Path(work_dir),
            created_at=data.get("created_at", ""),
            config_snapshot=data.get("config_snapshot", {}),
            config_hash=data.get("config_hash", ""),
            unsealed_at=data.get("unsealed_at"),
            finalize_reason=data.get("finalize_reason"),
        )

    @classmethod
    def create(cls, work_dir: Path, snapshot: dict) -> "RunMeta":
        meta = cls(
            work_dir=Path(work_dir),
            created_at=_utcnow(),
            config_snapshot=snapshot,
            config_hash=config_fingerprint(snapshot),
        )
        meta.save()
        return meta

    def save(self) -> None:
        atomic_write_json(self.path, {
            "created_at": self.created_at,
            "config_snapshot": self.config_snapshot,
            "config_hash": self.config_hash,
            "unsealed_at": self.unsealed_at,
            "finalize_reason": self.finalize_reason,
        })

    def check_config(self, snapshot: dict) -> bool:
        """True iff ``snapshot`` matches the run's recorded configuration."""
        return config_fingerprint(snapshot) == self.config_hash

    def stamp_unsealed(self, reason: str) -> None:
        """One-way: after this, ``run`` refuses to resume without --force."""
        self.unsealed_at = _utcnow()
        self.finalize_reason = reason
        self.save()


__all__ = [
    "RunMeta",
    "atomic_write_json",
    "config_fingerprint",
    "load_json_or",
    "META_FILENAME",
]
