"""Campaign event log: the measurement trail for the on-call eval metrics.

One JSON line per orchestration event, appended to ``<campaign>/events.jsonl``
next to the ledger. The eval plan's efficiency metrics read off this trail:
wake overhead (count of wakes per round), detection latency (job terminal ->
``trial_terminal_observed``; true remote finish timestamps are a later
refinement), and the submit/kill/conclude decision sequence. Best-effort by
design -- instrumentation must never break orchestration.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

EVENTS_FILE = "events.jsonl"


def ops_home() -> Path:
    """The ops root for this instance, resolved per call.

    A function rather than the constant it replaced: ``--config`` is known at
    startup, after this module is imported, so a constant sends every default
    resolution back to ``~/.raven/ops``. See ``config.paths.get_ops_home``.
    """
    from raven.config.paths import get_ops_home

    return get_ops_home()


def campaign_slug(name: str, limit: int = 24) -> str:
    """Directory-safe form of a campaign name; the shared rule so every writer
    (tools, watcher, wake handler) lands in the same campaign directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:limit] or "campaign"


def campaign_dir(campaign: str, *, home: Path | None = None) -> Path:
    return (home or ops_home()) / campaign_slug(campaign)


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
