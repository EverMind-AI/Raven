"""Verdict sidecar — task-outcome labels for attempts, outside tracing.

Tracing's ``status.code`` answers "did the code crash"; a verdict answers
"did the task succeed", and additionally separates agent failure from
infrastructure failure (the evolver's SOP distinction: an infra failure must
never be diagnosed as an agent failure). Tracing cannot know any of this, so
verdicts are written by whoever can judge — a user command, an eval judge, a
replay harness — into an append-only ``verdicts.jsonl`` next to the trace
logs, keyed by ``attempt.id``.

Append-only on purpose: verdicts from different sources coexist (a user's
"fail" and a judge's "fail" are two records), and re-judging appends rather
than rewrites. :func:`latest_verdict` gives the last word per attempt.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection

from raven.tracing import config as tracing_config
from raven.utils.atomic_io import locked_append

VERDICT_STATUSES = ("pass", "fail", "infra")

_VERDICTS_FILE = "verdicts.jsonl"


@dataclass(frozen=True)
class Verdict:
    """One outcome label for one attempt.

    ``status``: ``pass`` / ``fail`` (agent failure) / ``infra`` (environment
    or harness crash — not the agent's doing, excluded from diagnosis).
    ``source``: who judged (``user`` / ``judge`` / ``eval`` / ...).
    ``why``: optional failure-cause class, free-form until a taxonomy binds it.
    """

    attempt_id: str
    status: str
    source: str
    ts: str
    why: str | None = None
    notes: str | None = None
    session_key: str | None = None


def _verdicts_path(state_dir: Path | None = None) -> Path:
    return (state_dir or tracing_config.state_dir()) / _VERDICTS_FILE


def record_verdict(
    attempt_id: str,
    status: str,
    *,
    source: str,
    why: str | None = None,
    notes: str | None = None,
    session_key: str | None = None,
    state_dir: Path | None = None,
) -> Verdict:
    """Append one verdict for ``attempt_id``. Raises ValueError on bad input."""
    if not attempt_id:
        raise ValueError("attempt_id is required")
    if status not in VERDICT_STATUSES:
        raise ValueError(f"status must be one of {VERDICT_STATUSES}, got {status!r}")
    if not source:
        raise ValueError("source is required")
    verdict = Verdict(
        attempt_id=attempt_id,
        status=status,
        source=source,
        ts=datetime.now(timezone.utc).isoformat(),
        why=why,
        notes=notes,
        session_key=session_key,
    )
    path = _verdicts_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    locked_append(path, [json.dumps(asdict(verdict), ensure_ascii=False)])
    return verdict


def read_verdicts(
    state_dir: Path | None = None,
    *,
    attempt_id: str | None = None,
    attempt_ids: Collection[str] | None = None,
) -> list[Verdict]:
    """All verdicts in file order (oldest first), optionally filtered.

    ``attempt_id`` matches one exact id; ``attempt_ids`` matches any of a set —
    pass an attempt's alias ids so verdicts recorded under absorbed or member
    ids stay visible for a merged attempt. The two filters are mutually
    exclusive. Defect-tolerant: unparseable or incomplete lines are skipped —
    one bad line must not make the whole label store unreadable.
    """
    if attempt_id is not None and attempt_ids is not None:
        raise ValueError("attempt_id and attempt_ids are mutually exclusive")
    path = _verdicts_path(state_dir)
    if not path.exists():
        return []
    wanted = set(attempt_ids) if attempt_ids is not None else None
    out: list[Verdict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            v = Verdict(
                attempt_id=d["attempt_id"],
                status=d["status"],
                source=d["source"],
                ts=d["ts"],
                why=d.get("why"),
                notes=d.get("notes"),
                session_key=d.get("session_key"),
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if attempt_id is not None and v.attempt_id != attempt_id:
            continue
        if wanted is not None and v.attempt_id not in wanted:
            continue
        out.append(v)
    return out


def latest_verdict(attempt_id: str, state_dir: Path | None = None) -> Verdict | None:
    """The most recently appended verdict for ``attempt_id``, or None."""
    matches = read_verdicts(state_dir, attempt_id=attempt_id)
    return matches[-1] if matches else None
