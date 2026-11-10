"""What the benchmark guard did, written where no model can read it. Off by default.

The guard's most common action is invisible by construction: a Hugging Face row is
dropped from a result list and the model is shown the list without it, which is
indistinguishable from a list that never had one. On the smoke run that prompted this
file the only way to establish the guard had fired at all was to re-issue the model's
own queries against the search vendor from outside the run and compare -- 17 of 21
organic rows turned out to be benchmark copies, every one of them removed. That is not
a check anyone can repeat over a 2,400-item evaluation.

So each action appends a row here. The existing ``support.ledger`` was the obvious
home and is the wrong one: it writes only between ``open_product_ledger`` and
``close_product_ledger``, which the evaluation path never calls, so every guard row in
an eval run would have gone nowhere -- which is exactly what happened.

This file is written and never read during a turn. Nothing here is fed back to the
model, by this module or any other: an instrument that can change what it measures is
not an instrument, and a model that can see which pages were refused has been told
where the answer key is.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

__all__ = ["audit", "audit_path", "set_audit_dir"]

_AUDIT_NAME = "benchmark_guard.jsonl"
_audit_dir: Path | None = None


def set_audit_dir(path: Path | str | None) -> None:
    """Point the audit at a directory, or ``None`` to turn it off.

    Called once by the plugin at construction, like ``ledger.set_ledger_dir``. A
    module global rather than a context variable on purpose: one process serves many
    sessions, and they all belong to the same run.
    """
    global _audit_dir
    _audit_dir = Path(path) if path is not None else None


def audit_path() -> str | None:
    """The file rows are appended to, or ``None`` when the audit is off."""
    return None if _audit_dir is None else str(_audit_dir / _AUDIT_NAME)


# Sixteen agents share a process and several processes share this file, so every row
# is clamped to one buffered write: ``open(..., "a")`` is ``O_APPEND``, under which a
# single write syscall cannot interleave with another writer's, and a line that stays
# under the 8 KiB default buffer is a single write. An uncapped list of URLs is the one
# field that could cross that and tear a row in half.
_MAX_STR = 400
_MAX_LIST = 10


def _clamp(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, (list, tuple)):
        return [_clamp(v) for v in value[:_MAX_LIST]]
    return value


def audit(event: str, **fields: Any) -> None:
    """Append one row. Never raises: an audit failure must not fail the tool call.

    The row carries a session key only if the caller passes one; this module does not
    reach for context of its own.
    """
    if _audit_dir is None:
        return
    row = {"ts": round(time.time(), 3), "pid": os.getpid(), "event": event}
    row.update({k: _clamp(v) for k, v in fields.items()})
    try:
        _audit_dir.mkdir(parents=True, exist_ok=True)
        with open(_audit_dir / _AUDIT_NAME, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return
