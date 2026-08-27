"""Parser / renderer for ``user_memory/behaviors.md``.

behaviors.md is the append-only event log produced by the idle-triggered
LLM extractor over session JSONL files. Pure markdown — no HTML comment
metadata — because the file is never mutated in place; new events just
get appended under a day header.

Format::

    ## 2026-05-29 (Fri)

    ### evt_a1b2c3 — 14:00–14:30
    - session: `cli:default` · turns: 8
    - intent: debug · outcome: resolved
    - topic: memory-engine · project: raven
    - source: user-asked · owner: user · tools: Bash, Edit
    - summary: debugged memory_engine session split — found extra MemoryStore()

H2 = ISO date (with optional weekday tag). H3 = event id + time range.
Five field bullets follow each H3. Same H2 may appear multiple times
when the extractor runs more than once per day; the parser handles this
by concatenating events from all H2s with the matching date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable


@dataclass
class BehaviorEvent:
    """Single behavior event — 12 canonical fields + a summary line."""

    id: str
    day: str  # ISO date, e.g. "2026-05-29"
    start: str  # "HH:MM"
    end: str  # "HH:MM"
    session: str  # session_key like "cli:default"
    turns: int
    intent: str
    outcome: str
    topic: str
    project: str
    source: str
    owner: str
    tools: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "day": self.day,
            "start": self.start,
            "end": self.end,
            "session": self.session,
            "turns": self.turns,
            "intent": self.intent,
            "outcome": self.outcome,
            "topic": self.topic,
            "project": self.project,
            "source": self.source,
            "owner": self.owner,
            "tools": list(self.tools),
            "summary": self.summary,
        }


_DAY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})(?:\s+\([^)]*\))?\s*$")
_EVENT_HEADER_RE = re.compile(r"^### (evt_[A-Za-z0-9_]+) — (\d{1,2}:\d{2})–(\d{1,2}:\d{2})\s*$")
_FIELD_LINE_RE = re.compile(r"^- ([A-Za-z_]+): (.+?)\s*$")


def _split_fields(body: str) -> dict[str, str]:
    """Parse one bullet's body containing 'key: val · key: val' segments."""
    out: dict[str, str] = {}
    # Strip leading backtick formatting on values so 'session: `cli:default`'
    # round-trips to 'cli:default'.
    for segment in body.split(" · "):
        segment = segment.strip()
        if ":" not in segment:
            continue
        k, _, v = segment.partition(":")
        out[k.strip()] = v.strip().strip("`")
    return out


def slice_after_day(text: str, since_day: str) -> str:
    """Return the suffix of ``text`` starting at the first H2 day-block
    whose day ≥ ``since_day`` (ISO ``YYYY-MM-DD``). Empty string if no
    H2 reaches the window.

    Soundness rests on the writer invariant that each
    ``render_append_block`` emits its day H2s in ascending order: so
    the first H2 ≥ ``since_day`` always precedes any in-window event,
    even when later blocks revisit earlier days. The slice may
    over-include — out-of-window events tagging along past the cutoff
    H2 — and callers still date-filter parsed events for exact
    membership. Hand-edited files that violate the writer invariant
    can under-include.

    Lets readers bound parse cost on append-only behaviors.md as it
    grows without rotating the file on disk.
    """
    if not text or not since_day:
        return text
    pos = 0
    for line in text.splitlines(keepends=True):
        m = _DAY_RE.match(line.rstrip("\n"))
        if m and m.group(1) >= since_day:
            return text[pos:]
        pos += len(line)
    return ""


def parse_behaviors(text: str) -> list[BehaviorEvent]:
    """Parse ``text`` into a flat list of ``BehaviorEvent``.

    Tolerant of duplicate H2 days (the append-only writer creates a new
    H2 block per extraction even when the day is already present), of
    unknown bullets (skipped), and of out-of-order events (preserved in
    document order; caller sorts if needed).
    """
    if not text:
        return []
    events: list[BehaviorEvent] = []
    current_day = ""
    current_event: dict[str, object] | None = None

    def _flush():
        nonlocal current_event
        if current_event is not None:
            try:
                events.append(_event_from_partial(current_event, current_day))
            except (KeyError, ValueError):
                pass
            current_event = None

    for line in text.splitlines():
        m_day = _DAY_RE.match(line)
        if m_day:
            _flush()
            current_day = m_day.group(1)
            continue
        m_event = _EVENT_HEADER_RE.match(line)
        if m_event:
            _flush()
            current_event = {
                "id": m_event.group(1),
                "start": m_event.group(2),
                "end": m_event.group(3),
                "_fields": {},
                "_tools": [],
                "_summary": "",
            }
            continue
        if current_event is None:
            continue
        m_field = _FIELD_LINE_RE.match(line)
        if not m_field:
            continue
        key = m_field.group(1).lower()
        raw = m_field.group(2)
        if key == "summary":
            current_event["_summary"] = raw.strip()
        elif key == "tools":
            current_event["_tools"] = [t.strip() for t in raw.split(",") if t.strip()]
        else:
            current_event["_fields"].update(_split_fields(f"{key}: {raw}"))
    _flush()
    return events


def _event_from_partial(partial: dict, day: str) -> BehaviorEvent:
    fields = partial["_fields"]
    # Tools normally arrive in the combined ``source · owner · tools``
    # bullet, so they're picked up by ``_split_fields`` as a comma-joined
    # string. Convert to list here so the dataclass invariant holds.
    tools = list(partial["_tools"])
    if not tools:
        tools_raw = fields.get("tools", "")
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    return BehaviorEvent(
        id=partial["id"],
        day=day,
        start=partial["start"],
        end=partial["end"],
        session=fields.get("session", ""),
        turns=int(fields.get("turns", "0") or "0"),
        intent=fields.get("intent", ""),
        outcome=fields.get("outcome", ""),
        topic=fields.get("topic", ""),
        project=fields.get("project", ""),
        source=fields.get("source", ""),
        owner=fields.get("owner", ""),
        tools=tools,
        summary=partial["_summary"],
    )


def _weekday_tag(d: str) -> str:
    try:
        return date.fromisoformat(d).strftime("(%a)")
    except ValueError:
        return ""


def render_event(event: BehaviorEvent) -> str:
    """Render one event as an H3 block (no enclosing H2 — caller groups
    multiple events under a single H2 day header)."""
    tools = ", ".join(event.tools) if event.tools else ""
    lines = [
        f"### {event.id} — {event.start}–{event.end}",
        f"- session: `{event.session}` · turns: {event.turns}",
        f"- intent: {event.intent} · outcome: {event.outcome}",
        f"- topic: {event.topic} · project: {event.project}",
        f"- source: {event.source} · owner: {event.owner} · tools: {tools}",
    ]
    if event.summary:
        lines.append(f"- summary: {event.summary}")
    return "\n".join(lines)


def render_append_block(events: Iterable[BehaviorEvent]) -> str:
    """Render a contiguous append-ready block — groups events by their
    ``day`` field, emits one ``## DATE (DOW)`` H2 per day group, then
    the H3 blocks underneath.

    Same-day events from a single extraction get one H2. If the caller
    wants multiple H2s for the same day (e.g. a second idle extraction
    later), they call this function again — each call emits its own
    H2(s); the parser is tolerant of duplicates.
    """
    by_day: dict[str, list[BehaviorEvent]] = {}
    for ev in events:
        by_day.setdefault(ev.day, []).append(ev)
    parts: list[str] = []
    for day in sorted(by_day.keys()):
        weekday = _weekday_tag(day)
        h2 = f"## {day} {weekday}".rstrip()
        parts.append(h2)
        parts.append("")
        for ev in by_day[day]:
            parts.append(render_event(ev))
            parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n" if parts else ""


def render_folded_line(event: BehaviorEvent) -> str:
    """Compact one-line summary of an event for PlannerContext injection.

    Drops fields Planner doesn't decide on (id / tools / source / owner)
    and the H3 header. Format::

        - [MM-DD HH:MM-HH:MM Nt] intent→outcome topic[ #project]: summary

    ~50-80 tokens per line; an 80-event window fits in ~5k tokens.
    """
    # Trim ``day`` to MM-DD (year omitted — Planner cares about recency,
    # not the year; the time window enforced by caller bounds the range).
    short_day = event.day[5:] if len(event.day) >= 10 else event.day
    parts: list[str] = [
        f"[{short_day} {event.start}-{event.end} {event.turns}t]",
    ]
    if event.intent and event.outcome:
        parts.append(f"{event.intent}→{event.outcome}")
    elif event.intent:
        parts.append(event.intent)
    elif event.outcome:
        parts.append(f"→{event.outcome}")
    if event.topic:
        parts.append(event.topic)
    if event.project:
        parts.append(f"#{event.project}")
    header = " ".join(parts)
    if event.summary:
        return f"- {header}: {event.summary}"
    return f"- {header}"


def render_folded_block(
    events: list[BehaviorEvent],
    *,
    max_events: int = 100,
) -> str:
    """Folded-single-line block, newest event last (matches read order).

    ``max_events`` caps the output — when exceeded, keeps the most-recent
    ``max_events`` and drops the rest from the front."""
    if not events:
        return ""
    # Sort by (day, end) ascending so the most recent line is at the
    # bottom — matches how Planner reads "recent events".
    ordered = sorted(events, key=lambda e: (e.day, e.end))
    if len(ordered) > max_events:
        ordered = ordered[-max_events:]
    return "\n".join(render_folded_line(e) for e in ordered)


__all__ = [
    "BehaviorEvent",
    "parse_behaviors",
    "slice_after_day",
    "render_event",
    "render_append_block",
    "render_folded_line",
    "render_folded_block",
]
