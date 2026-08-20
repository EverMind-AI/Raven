"""Pin registry + rotation-transparent span reading over the tracing store.

Retention contract
------------------

The tracing store keeps logs for troubleshooting: the active log rotates into
``logs/archive/<date>/`` by size or day, and nothing deletes data today. A
trajectory referenced as corpus (a bug report, a regression case, evolver
evidence) needs a stronger promise: **pinned ids are never purged**. The pin
registry (``pins.json`` in the trace state dir) records that promise; any
future purge tooling MUST drop only spans whose ``attempt.id`` and ``traceId``
are both unpinned, and must keep every artifact such a span references.

Reading
-------

:func:`iter_spans` yields spans across archived + active logs in write order,
so callers address trajectories without knowing where rotation put them. Pin
ids and filter ids match against both ``attempt.id`` and ``traceId`` — the
two names a trajectory is known by (they coincide for single-turn attempts).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from raven.tracing import config as tracing_config
from raven.utils.atomic_io import atomic_replace

_PINS_FILE = "pins.json"


def _pins_path(state_dir: Path | None = None) -> Path:
    return (state_dir or tracing_config.state_dir()) / _PINS_FILE


def pins(state_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """The pin registry: id -> {reason, ts}. Empty on missing/corrupt file."""
    path = _pins_path(state_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def pin(id_: str, *, reason: str = "", state_dir: Path | None = None) -> None:
    """Protect an attempt/trace id from any future purge. Idempotent."""
    if not id_:
        raise ValueError("id is required")
    current = pins(state_dir)
    current[id_] = {"reason": reason, "ts": datetime.now(timezone.utc).isoformat()}
    path = _pins_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")


def unpin(id_: str, state_dir: Path | None = None) -> bool:
    """Remove a pin. Returns whether it existed."""
    current = pins(state_dir)
    if id_ not in current:
        return False
    del current[id_]
    atomic_replace(_pins_path(state_dir), json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    return True


def is_pinned(span: dict[str, Any], state_dir: Path | None = None, *, _pins: dict | None = None) -> bool:
    """Whether a span record is protected (by attempt.id or traceId)."""
    registry = pins(state_dir) if _pins is None else _pins
    if not registry:
        return False
    attrs = span.get("attributes") or {}
    return span.get("traceId") in registry or attrs.get("attempt.id") in registry


def span_log_paths(state_dir: Path | None = None) -> list[Path]:
    """All span log files in write order: archived (oldest first), then active."""
    base = state_dir or tracing_config.state_dir()
    logs_dir = base / "logs"
    archived = sorted((logs_dir / "archive").glob("*/audit-spans-*.log"))
    active = logs_dir / "audit-spans.log"
    return archived + ([active] if active.exists() else [])


def iter_spans(
    state_dir: Path | None = None,
    *,
    attempt_id: str | None = None,
    trace_id: str | None = None,
    session_key: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield span records across all logs in write order, filtered.

    ``attempt_id`` matches spans whose ``attempt.id`` OR ``traceId`` equals it
    (single-turn attempts are addressed by their trace id). Unparseable lines
    are skipped — one bad line must not hide a whole trajectory.
    """
    for path in span_log_paths(state_dir):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(span, dict):
                continue
            attrs = span.get("attributes") or {}
            if attempt_id is not None and attrs.get("attempt.id") != attempt_id and span.get("traceId") != attempt_id:
                continue
            if trace_id is not None and span.get("traceId") != trace_id:
                continue
            if session_key is not None and attrs.get("session.key") != session_key:
                continue
            yield span
