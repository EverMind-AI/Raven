"""``## Currently focused on`` — pure-algorithm summary of recent
session activity + topic/project distribution.

Reads SessionManager's per-session JSONL files (last activity timestamp)
and the recent tail of episodes.md (last ``window_hours`` worth of tag
distribution). Emits a 3-bullet summary:

- active sessions (channel:chat_id, message count, last activity)
- top topics (#tag counters within window)
- top projects (#project-X counters within window)

No LLM call. Cheap enough to run every Sentinel tick.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from raven.memory_engine.consolidate.consolidator import _parse_episode_line
from raven.proactive_engine.sentinel.attention_producers._base import (
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.session.manager import SessionManager


class CurrentlyFocusedProducer(AttentionProducer):
    """Snapshot of "what the user has been doing in the last N hours" —
    active sessions + top topics + top projects within the window.

    ``window_hours`` default 6h aligns with a typical work block; raise
    for longer-horizon snapshots (e.g. 12h end-of-day summary).
    """

    SECTION_HEADER = "## Currently focused on"

    def __init__(
        self,
        memory_store: "MemoryStore",
        session_manager: "SessionManager",
        *,
        window_hours: int = 6,
        top_tags: int = 6,
        top_projects: int = 5,
    ) -> None:
        self._memory_store = memory_store
        self._session_manager = session_manager
        self._window = timedelta(hours=window_hours)
        self._top_tags = top_tags
        self._top_projects = top_projects

    async def compute_body(self, now: datetime) -> str:
        active = self._collect_active_sessions(now)
        topics, projects = self._collect_tag_distribution(now)
        if not active and not topics and not projects:
            return ""
        lines: list[str] = []
        if active:
            lines.append("**Active sessions:**")
            for entry in active:
                lines.append(f"- `{entry['key']}` — {entry['msg_count']} msgs, last activity {entry['last_activity']}")
        if topics:
            top_pairs = sorted(
                topics.items(),
                key=lambda kv: -kv[1],
            )[: self._top_tags]
            lines.append("")
            lines.append("**Top topics:**")
            lines.append(
                "- " + ", ".join(f"`#{tag}` ({count})" for tag, count in top_pairs),
            )
        if projects:
            top_pairs = sorted(
                projects.items(),
                key=lambda kv: -kv[1],
            )[: self._top_projects]
            lines.append("")
            lines.append("**Top projects:**")
            lines.append(
                "- " + ", ".join(f"`{name}` ({count})" for name, count in top_pairs),
            )
        return "\n".join(lines)

    # ── Internals ───────────────────────────────────────────────────

    def _collect_active_sessions(
        self,
        now: datetime,
    ) -> list[dict[str, str | int]]:
        """Return sessions with at least one message in the window,
        sorted by last activity descending."""
        cutoff = now - self._window
        out: list[dict[str, str | int]] = []
        sessions_dir = self._session_manager.sessions_dir
        if not sessions_dir.is_dir():
            return []
        for path in sessions_dir.rglob("*.jsonl"):
            last_ts: datetime | None = None
            msg_count = 0
            key = self._session_manager.key_from_path(path)
            try:
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("_type") == "metadata":
                            mk = data.get("key")
                            if isinstance(mk, str) and mk:
                                key = mk
                            continue
                        ts_raw = data.get("timestamp")
                        if not isinstance(ts_raw, str):
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_raw)
                        except ValueError:
                            continue
                        if ts.tzinfo is not None:
                            ts = ts.replace(tzinfo=None)
                        if ts < cutoff:
                            continue
                        msg_count += 1
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
            except OSError:
                continue
            if msg_count == 0 or last_ts is None:
                continue
            out.append(
                {
                    "key": key,
                    "msg_count": msg_count,
                    "last_activity": last_ts.strftime("%Y-%m-%d %H:%M"),
                    "_sort_ts": last_ts.isoformat(),
                }
            )
        out.sort(key=lambda d: d["_sort_ts"], reverse=True)
        for d in out:
            d.pop("_sort_ts", None)
        return out

    def _collect_tag_distribution(
        self,
        now: datetime,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Return ``(topics, projects)`` counters over episodes.md within
        the window. ``topics`` excludes ``project-X`` tags; ``projects``
        strips the ``project-`` prefix from the key."""
        history_file = self._memory_store.history_file
        if not history_file.exists():
            return {}, {}
        cutoff = now - self._window
        topics: dict[str, int] = {}
        projects: dict[str, int] = {}
        try:
            text = history_file.read_text(encoding="utf-8")
        except OSError:
            return {}, {}
        for line in text.splitlines():
            parsed = _parse_episode_line(line)
            if not parsed:
                continue
            ts, _, tags = parsed
            try:
                dt = datetime.strptime(
                    ts.replace("T", " "),
                    "%Y-%m-%d %H:%M",
                )
            except ValueError:
                continue
            if dt < cutoff:
                continue
            for tag in tags:
                if tag.startswith("project-"):
                    name = tag[len("project-") :]
                    projects[name] = projects.get(name, 0) + 1
                else:
                    topics[tag] = topics.get(tag, 0) + 1
        return topics, projects


__all__ = ["CurrentlyFocusedProducer"]
