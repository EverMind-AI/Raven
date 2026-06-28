"""Idle-triggered LLM extractor — sessions/*.jsonl → behaviors.md.

Reads each ``{ws}/sessions/{channel}/{chat_id}.jsonl`` from the
per-session cursor recorded in ``{ws}/user_memory/.behaviors_offsets.json``,
asks an LLM to emit structured BehaviorEvent records for the unprocessed
tail, and appends them under a day H2 in ``user_memory/behaviors.md``.

Append-only — never mutates already-written events. Idempotency is best
effort: on crash between (a) append-to-behaviors.md and (b) offset save,
the next run re-extracts the same tail and may produce near-duplicate
events. There is no automated dedup path — operators clean up by editing
behaviors.md manually if duplicates accumulate.

Triggering (idle / cooldown / min-segment) is enforced by the caller —
this module only implements the extraction primitive. ``SentinelRunner``
calls :meth:`BehaviorsExtractor.tick` on each timer tick; ``CLI rebuild``
calls :meth:`BehaviorsExtractor.run_all` bypassing the gates.

Multi-process caveat: SentinelRunner's idle signal (``_last_inbound_ts``)
is process-local. In split deployments where channel adapters and
Sentinel run in separate processes, the idle gate falls back to the
12h per-session cooldown alone.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, TYPE_CHECKING

from loguru import logger

from raven.memory_engine.consolidate.behaviors import (
    BehaviorEvent,
    render_append_block,
)
from raven.session.manager import SessionManager

if TYPE_CHECKING:
    from raven.config.raven import BehaviorsExtractConfig
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.providers.base import LLMProvider
    from raven.session.manager import Session


_EXTRACT_TOOL_NAME = "emit_behavior_events"


def build_extract_tool() -> list[dict[str, Any]]:
    """LLM tool schema for behavior extraction.

    Asks for structured event records. The LLM is told the canonical
    12-field shape; we parse strict and drop malformed entries rather
    than reprompt — cost beats completeness for a once-a-day pipeline.
    """
    return [{
        "type": "function",
        "function": {
            "name": _EXTRACT_TOOL_NAME,
            "description": (
                "Extract distinct user-agent interactions from the recent "
                "session messages into structured BehaviorEvent records. "
                "Emit ONE event per substantive interaction (debug session, "
                "design discussion, deferred decision, etc.). Skip "
                "small-talk and short clarifications. Empty array is "
                "acceptable when the chunk produced nothing memorable."
            ),
            "parameters": {
                "type": "object",
                "required": ["events"],
                "properties": {
                    "events": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "start", "end", "intent", "outcome",
                                "topic", "summary",
                            ],
                            "properties": {
                                "start": {
                                    "type": "string",
                                    "description": "HH:MM start (local).",
                                },
                                "end": {
                                    "type": "string",
                                    "description": "HH:MM end (local).",
                                },
                                "intent": {
                                    "type": "string",
                                    "description": (
                                        "What user was trying to do. "
                                        "Examples: debug, design, ask, "
                                        "plan, refactor, review."
                                    ),
                                },
                                "outcome": {
                                    "type": "string",
                                    "description": (
                                        "How it ended. Examples: "
                                        "resolved, open, deferred, "
                                        "blocked, abandoned, followup."
                                    ),
                                },
                                "topic": {
                                    "type": "string",
                                    "description": (
                                        "Short topic slug (kebab-case)."
                                    ),
                                },
                                "project": {
                                    "type": "string",
                                    "description": (
                                        "Project name or empty if N/A."
                                    ),
                                },
                                "source": {
                                    "type": "string",
                                    "description": (
                                        "Who initiated: 'user-asked', "
                                        "'agent-proposed', 'cron-fire', "
                                        "'nudge'."
                                    ),
                                },
                                "owner": {
                                    "type": "string",
                                    "description": "'user' or 'agent'.",
                                },
                                "tools": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Tools the agent used in this "
                                        "interaction (Bash / Edit / "
                                        "Read / WebFetch / etc.)."
                                    ),
                                },
                                "turns": {
                                    "type": "integer",
                                    "description": (
                                        "Round-trip count between user "
                                        "and agent in this interaction."
                                    ),
                                },
                                "summary": {
                                    "type": "string",
                                    "description": (
                                        "One-line summary, ≤120 chars, "
                                        "concrete identifiers preferred."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        },
    }]


_SYSTEM_PROMPT = """\
You read a recent slice of one conversation session and extract distinct
user-agent interactions into structured BehaviorEvent records via the
`emit_behavior_events` tool.

Rules:
1. ONE record per substantive interaction. Don't split a single debug
   session into 5 records just because the user asked 5 clarifying
   questions inside it.
2. Skip small-talk, clarifications < 3 turns, and pure tool-listing.
3. Use the session's first/last message timestamps inside the slice to
   compute start/end HH:MM. If timestamps are missing, estimate from
   message order with 5-min granularity.
4. ``summary`` must include concrete identifiers (file paths, function
   names, PR numbers, percentages). "User debugged auth" is too vague;
   "User debugged refresh_token() in auth/views.py — fixed TTL=300s
   typo" is good.
5. Empty events array is acceptable when the slice produced nothing
   memorable. Better to skip than fabricate.
"""


_OFFSETS_VERSION = 1


@dataclass
class _SessionOffset:
    processed_until_msg_idx: int = 0
    processed_until_ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed_until_msg_idx": self.processed_until_msg_idx,
            "processed_until_ts": self.processed_until_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_SessionOffset":
        return cls(
            processed_until_msg_idx=int(d.get("processed_until_msg_idx", 0)),
            processed_until_ts=str(d.get("processed_until_ts", "")),
        )


@dataclass
class BehaviorsOffsets:
    """Persistent per-session message cursor — survives crash + resume."""

    path: Path
    offsets: dict[str, _SessionOffset] = field(default_factory=dict)
    last_run_ts: str = ""  # ISO datetime of the most recent extraction tick

    @classmethod
    def load(cls, path: Path) -> "BehaviorsOffsets":
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        if not isinstance(raw, dict):
            return cls(path=path)
        offsets = {
            k: _SessionOffset.from_dict(v)
            for k, v in raw.get("offsets", {}).items()
            if isinstance(v, dict)
        }
        return cls(
            path=path,
            offsets=offsets,
            last_run_ts=str(raw.get("last_run_ts", "")),
        )

    def save(self) -> None:
        payload = {
            "version": _OFFSETS_VERSION,
            "last_run_ts": self.last_run_ts,
            "offsets": {k: v.to_dict() for k, v in self.offsets.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def get(self, session_key: str) -> _SessionOffset:
        return self.offsets.get(session_key, _SessionOffset())

    def set(self, session_key: str, offset: _SessionOffset) -> None:
        self.offsets[session_key] = offset

    def parsed_last_run(self) -> datetime | None:
        if not self.last_run_ts:
            return None
        try:
            return datetime.fromisoformat(self.last_run_ts)
        except ValueError:
            return None


class BehaviorsExtractor:
    """Per-tick orchestrator: walks sessions, calls LLM, appends events.

    Constructed once at sentinel-stack build; ``tick()`` is called on each
    runner tick (cheap-path gated by idle + cooldown), ``run_all()`` from
    the CLI bypasses both gates.
    """

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        session_manager: "SessionManager",
        provider: "LLMProvider",
        config: "BehaviorsExtractConfig",
        model: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.session_manager = session_manager
        self.provider = provider
        self.config = config
        self.model = model or config.model or ""
        self._now_fn = now_fn or datetime.now

    # ── Public entry points ─────────────────────────────────────────

    async def tick(self, *, idle_seconds_observed: int) -> int:
        """Gated extraction call. Honors ``idle_seconds`` and
        ``cooldown_hours``; returns count of new events appended.

        Returns 0 (no-op) when any gate fails. Caller (SentinelRunner)
        owns the idle measurement and passes it in.
        """
        if not self.config.enabled:
            return 0
        if idle_seconds_observed < self.config.idle_seconds:
            logger.debug(
                "BehaviorsExtractor: idle gate not passed ({}s < {}s)",
                idle_seconds_observed, self.config.idle_seconds,
            )
            return 0
        offsets = BehaviorsOffsets.load(self.memory_store.behaviors_offsets_path)
        last = offsets.parsed_last_run()
        if last is not None:
            elapsed = self._now_fn() - last
            if elapsed < timedelta(hours=self.config.cooldown_hours):
                remaining = timedelta(hours=self.config.cooldown_hours) - elapsed
                logger.debug(
                    "BehaviorsExtractor: cooldown active "
                    "(last_run={}, elapsed={}, remaining={})",
                    last.isoformat(timespec="seconds"),
                    str(elapsed).split(".")[0],
                    str(remaining).split(".")[0],
                )
                return 0
        return await self._extract_all_internal(offsets)

    async def run_all(self) -> int:
        """Bypass gates — extract every session's unprocessed tail.
        Used by the manual rebuild CLI command."""
        offsets = BehaviorsOffsets.load(self.memory_store.behaviors_offsets_path)
        return await self._extract_all_internal(offsets)

    # ── Internals ───────────────────────────────────────────────────

    async def _extract_all_internal(
        self, offsets: BehaviorsOffsets,
    ) -> int:
        sessions_dir = self.session_manager.sessions_dir
        if not sessions_dir.is_dir():
            return 0
        total_new = 0
        for session_path in sorted(sessions_dir.rglob("*.jsonl")):
            try:
                added = await self._extract_one_session(
                    session_path, offsets,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "BehaviorsExtractor: session {} failed: {}",
                    session_path.name, exc,
                )
                continue
            total_new += added
        offsets.last_run_ts = self._now_fn().isoformat(timespec="seconds")
        offsets.save()
        return total_new

    async def _extract_one_session(
        self, session_path: Path, offsets: BehaviorsOffsets,
    ) -> int:
        session_key = _session_key_from_path(session_path)
        messages = _load_session_messages(session_path)
        offset = offsets.get(session_key)
        tail = messages[offset.processed_until_msg_idx:]
        if len(tail) < self.config.min_segment_messages:
            return 0

        added = 0
        # Chunk to avoid single-call cost spikes; each chunk's events
        # appended + offset advanced independently so partial success
        # is preserved across crash. ``BehaviorsExtractConfig`` validates
        # max_messages_per_call ≥ min_segment_messages so this is a true
        # upper bound.
        cap = self.config.max_messages_per_call
        idx = 0
        while idx < len(tail):
            chunk = tail[idx:idx + cap]
            if len(chunk) < self.config.min_segment_messages:
                break
            events = await self._llm_extract(chunk, session_key)
            if events:
                self._append_to_behaviors(events)
                added += len(events)
            new_msg_idx = offset.processed_until_msg_idx + idx + len(chunk)
            last_ts = _last_ts(chunk) or offset.processed_until_ts
            offsets.set(session_key, _SessionOffset(
                processed_until_msg_idx=new_msg_idx,
                processed_until_ts=last_ts,
            ))
            offsets.save()  # Persist after each chunk (best-effort idempotency).
            idx += cap
        return added

    def _append_to_behaviors(self, events: list[BehaviorEvent]) -> None:
        text = render_append_block(events)
        if not text:
            return
        with self.memory_store.locked_behaviors():
            self.memory_store.behaviors_file.parent.mkdir(
                parents=True, exist_ok=True,
            )
            existing = (
                self.memory_store.behaviors_file.read_text(encoding="utf-8")
                if self.memory_store.behaviors_file.exists()
                else ""
            )
            sep = "" if not existing or existing.endswith("\n") else "\n"
            self.memory_store.behaviors_file.write_text(
                existing + sep + text, encoding="utf-8",
            )

    async def _llm_extract(
        self, chunk: list[dict[str, Any]], session_key: str,
    ) -> list[BehaviorEvent]:
        rendered = _render_chunk_for_llm(chunk)
        if not rendered:
            return []
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Session: {session_key}\n\n"
                    f"Messages:\n{rendered}"
                ),
            },
        ]
        response = await self.provider.chat_with_retry(
            messages=messages,
            tools=build_extract_tool(),
            model=self.model or None,
            tool_choice={
                "type": "function",
                "function": {"name": _EXTRACT_TOOL_NAME},
            },
        )
        if not response.has_tool_calls:
            return []
        args = response.tool_calls[0].arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return []
        if not isinstance(args, dict):
            return []
        raw_events = args.get("events", [])
        if not isinstance(raw_events, list):
            return []
        day = _chunk_day(chunk) or self._now_fn().date().isoformat()
        return _parse_events(raw_events, session_key, day)


# ── Helpers ────────────────────────────────────────────────────────


def _session_key_from_path(path: Path) -> str:
    """Recover the ``channel:chat_id`` key for a session file.

    SessionManager stores sessions under the nested layout
    ``sessions/{channel}/{chat_id}.jsonl``. The metadata line on disk
    carries the authoritative ``channel:chat_id`` key — we read it whenever
    possible. For metadata-less files the key is derived from the path via
    ``SessionManager.key_from_path``.
    """
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    meta = json.loads(line)
                except json.JSONDecodeError:
                    break
                if isinstance(meta, dict) and meta.get("_type") == "metadata":
                    key = meta.get("key")
                    if isinstance(key, str) and key:
                        return key
                break
    except OSError:
        pass
    return SessionManager.key_from_path(path)


def _load_session_messages(path: Path) -> list[dict[str, Any]]:
    """Return non-metadata message lines as dicts. Skips malformed lines."""
    messages: list[dict[str, Any]] = []
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
                    continue
                messages.append(data)
    except OSError:
        return []
    return messages


_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S")


def _parse_msg_ts(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _last_ts(chunk: list[dict[str, Any]]) -> str:
    for msg in reversed(chunk):
        ts = msg.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return ""


def _chunk_day(chunk: list[dict[str, Any]]) -> str:
    for msg in chunk:
        ts = _parse_msg_ts(msg.get("timestamp"))
        if ts is not None:
            return ts.date().isoformat()
    return ""


_TS_NOISE_RE = re.compile(r"[^\w\s\-/:.,()]")


def _render_chunk_for_llm(chunk: list[dict[str, Any]]) -> str:
    """Compact one-line-per-message render with timestamp prefix."""
    lines: list[str] = []
    for msg in chunk:
        role = str(msg.get("role", "?"))
        ts = msg.get("timestamp", "")
        ts_short = str(ts)[:16] if isinstance(ts, str) else ""
        content = msg.get("content")
        if isinstance(content, list):
            text_parts = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            content_text = " ".join(t for t in text_parts if t)
        else:
            content_text = str(content or "")
        content_text = _TS_NOISE_RE.sub(" ", content_text).strip()
        tools = msg.get("tools_used") or msg.get("tool_calls") or []
        tools_str = ""
        if isinstance(tools, list) and tools:
            names = []
            for t in tools:
                if isinstance(t, dict):
                    names.append(
                        t.get("name")
                        or (t.get("function") or {}).get("name")
                        or "",
                    )
                else:
                    names.append(str(t))
            names = [n for n in names if n]
            if names:
                tools_str = f" [tools: {', '.join(names)}]"
        lines.append(f"[{ts_short}] {role.upper()}{tools_str}: {content_text}")
    return "\n".join(lines)


def _parse_events(
    raw_events: list[Any], session_key: str, default_day: str,
) -> list[BehaviorEvent]:
    out: list[BehaviorEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        start = str(raw.get("start") or "").strip()
        end = str(raw.get("end") or "").strip()
        if not start or not end:
            continue
        # Normalize H:MM → HH:MM.
        start = start.zfill(5) if len(start) == 4 else start
        end = end.zfill(5) if len(end) == 4 else end
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            continue
        tools_raw = raw.get("tools") or []
        if not isinstance(tools_raw, list):
            tools_raw = []
        tools = [str(t).strip() for t in tools_raw if str(t).strip()]
        out.append(BehaviorEvent(
            id=f"evt_{uuid.uuid4().hex[:8]}",
            day=default_day,
            start=start,
            end=end,
            session=session_key,
            turns=int(raw.get("turns") or 0) or 0,
            intent=str(raw.get("intent") or "").strip(),
            outcome=str(raw.get("outcome") or "").strip(),
            topic=str(raw.get("topic") or "").strip(),
            project=str(raw.get("project") or "").strip(),
            source=str(raw.get("source") or "").strip(),
            owner=str(raw.get("owner") or "user").strip(),
            tools=tools,
            summary=summary,
        ))
    return out


__all__ = [
    "BehaviorsExtractor",
    "BehaviorsOffsets",
    "build_extract_tool",
]
