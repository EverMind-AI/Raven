"""Campaign directory layout and event trail: where a campaign lives on disk.

One JSON line per orchestration event, appended to ``<campaign>/events.jsonl``
next to the ledger. The eval plan's efficiency metrics read off this trail:
wake overhead (count of wakes per round), detection latency (job terminal ->
``trial_terminal_observed``; true remote finish timestamps are a later
refinement), and the submit/kill/conclude decision sequence. Best-effort by
design -- instrumentation must never break orchestration.

The fork rooted campaign directories at a host-config home
(``config.paths.get_ops_home``); the plugin owns its root instead: the
launcher-injected ``stateRoot`` reaches every reader as a
:class:`CampaignStore`, and ``campaign_dir`` takes the home explicitly.
There is deliberately no ambient default home here -- the home is a porting
rule, not a seam (verdict feature 3).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER_FILE = "ledger.json"
META_FILE = "meta.json"
EVENTS_FILE = "events.jsonl"
CONCLUDED_FILE = "concluded.json"


def campaign_slug(name: str, limit: int = 24) -> str:
    """Directory-safe form of a campaign name; the shared rule so every writer
    (tools, watcher, wake handler) lands in the same campaign directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:limit] or "campaign"


def campaign_dir(campaign: str, *, home: Path | str) -> Path:
    return Path(home) / campaign_slug(campaign)


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
        return campaign_dir(campaign, home=self.root)

    def campaign_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.iterdir() if p.is_dir())


# ── Declaration and conclusion ──────────────────────────────────────


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
    cancels the pending wake and stops probing, the scheduling tools refuse
    new wakes)."""
    d = Path(campaign_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (CONCLUDED_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / CONCLUDED_FILE)


# ── The event trail ─────────────────────────────────────────────────


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


__all__ = [
    "CONCLUDED_FILE",
    "EVENTS_FILE",
    "LEDGER_FILE",
    "META_FILE",
    "CampaignStore",
    "campaign_dir",
    "campaign_slug",
    "conclude",
    "is_concluded",
    "log_event",
    "read_events",
    "read_meta",
    "write_meta",
]
