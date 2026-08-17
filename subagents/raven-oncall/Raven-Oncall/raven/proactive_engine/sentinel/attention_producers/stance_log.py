"""``## Recent stance log (30d)`` — user-expressed preference statements.

View backed by ``DailyAnalysisService``: the LLM call detects stance
entries from the inbound window; this producer just renders them. When
the LLM is disabled / fails the service may fall back to a prefix
heuristic — same render path either way.

Stateful via sidecar JSON (``user_memory/.stance_log.json``), not via
attention.md. Each tick reads-merges-writes the sidecar under its own
fcntl lock, then renders the section from the post-merge entries. The
older design (parse existing bullets out of attention.md during the
unlocked Phase 1 of AttentionUpdater) had a lost-update race: a
concurrent writer could FIFO-trim an entry that this producer would
then resurrect on its next Phase 2 write.

Append-style behavior: each daily run can add new entries on top of
the persisted log. To keep the section scannable we cap with
``stance_max_keep`` (FIFO) — oldest entries drop off the front.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)

_LEGACY_BULLET_RE = re.compile(
    r"^-\s+\[(?P<ts>[0-9T:\-.+]+)\]\s+(?P<text>.+)$",
)

if TYPE_CHECKING:
    from raven.config.raven import DailyAnalysisConfig
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.proactive_engine.sentinel.predictor.daily_analysis import (
        DailyAnalysisService,
    )


@dataclass
class _Entry:
    ts: str
    text: str


def _load_entries(path) -> list[_Entry]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("entries", []) if isinstance(data, dict) else []
    out: list[_Entry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ts = item.get("ts")
        text = item.get("text")
        if isinstance(ts, str) and isinstance(text, str):
            out.append(_Entry(ts=ts, text=text))
    return out


def _bootstrap_from_attention(
    memory_store,
    section_header: str,
) -> list[_Entry]:
    """One-time migration: pull stance bullets out of attention.md's
    legacy storage when the sidecar JSON doesn't exist yet.

    Workspaces created before the sidecar split kept stance entries
    inline in attention.md. Without this, upgrading those workspaces
    silently empties the section until 30d of fresh entries accumulate.
    Runs once per workspace; the next compute_body persists the result
    to the sidecar and this legacy path is never re-read.

    Holds the attention.md read lock during the snapshot so a
    concurrent AttentionUpdater Phase 2 write can't deliver a
    half-flushed view that permanently truncates the migrated history
    — bootstrap is one-shot, there's no "next chance" to recover.
    """
    attention_path = memory_store.attention_file
    if not attention_path.exists():
        return []
    try:
        with memory_store.locked_attention():
            text = attention_path.read_text(encoding="utf-8")
    except OSError:
        return []
    from raven.memory_engine.consolidate.attention import parse_attention

    body = parse_attention(text).get(section_header, "")
    out: list[_Entry] = []
    for line in body.splitlines():
        m = _LEGACY_BULLET_RE.match(line.strip())
        if m:
            out.append(_Entry(ts=m.group("ts"), text=m.group("text")))
    return out


def _save_entries(path, entries: list[_Entry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"entries": [{"ts": e.ts, "text": e.text} for e in entries]}
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


class StanceLogProducer(AttentionProducer):
    """Renders ``## Recent stance log (30d)`` from DailyAnalysisService."""

    SECTION_HEADER = "## Recent stance log (30d)"

    def __init__(
        self,
        *,
        analysis: "DailyAnalysisService",
        memory_store: "MemoryStore",
        config: "DailyAnalysisConfig",
    ) -> None:
        self._analysis = analysis
        self._memory_store = memory_store
        self._config = config

    async def compute_body(self, now: datetime) -> str:
        result = await self._analysis.get(now)
        if result is None:
            return ""
        cutoff = now - timedelta(days=30)
        cap = self._config.stance_max_keep
        # Read-merge-write under the sidecar lock so two concurrent
        # producers can't both reload the same pre-trim state and lose
        # each other's updates.
        with self._memory_store.locked_stance_log():
            if self._memory_store.stance_log_path.exists():
                existing = _load_entries(self._memory_store.stance_log_path)
            else:
                existing = _bootstrap_from_attention(
                    self._memory_store,
                    self.SECTION_HEADER,
                )
            merged: list[_Entry] = []
            seen: set[tuple[str, str]] = set()
            for ev in result.stance_entries:
                key = (ev.ts, ev.text)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(_Entry(ts=ev.ts, text=ev.text))
            for e in existing:
                key = (e.ts, e.text)
                if key in seen:
                    continue
                try:
                    dt = datetime.fromisoformat(e.ts)
                    if dt.tzinfo is not None:
                        dt = dt.replace(tzinfo=None)
                    if dt < cutoff:
                        continue
                except ValueError:
                    continue
                seen.add(key)
                merged.append(e)
            merged.sort(key=lambda e: e.ts, reverse=True)
            if len(merged) > cap:
                merged = merged[:cap]
            _save_entries(self._memory_store.stance_log_path, merged)
        if not merged:
            return ""
        return "\n".join(f"- [{e.ts}] {e.text}" for e in merged)


__all__ = ["StanceLogProducer"]
