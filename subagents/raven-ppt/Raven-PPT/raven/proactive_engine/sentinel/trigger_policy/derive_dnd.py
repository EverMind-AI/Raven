"""Parse ``attention.md`` ``## User overrides`` text into structured DndWindow.

The ``## User overrides`` H2 in attention.md is the canonical slot for
user-authored proactivity overrides — it's the first section in
``ATTENTION_SECTIONS``, has Chinese/English aliases (``## 用户指令``),
and is preserved across Sentinel ticks (no producer overwrites it).

This module reads that section's body and extracts structured DND
windows so :class:`NudgePolicy` can enforce them. Lines that don't
match the DSL are silently ignored — they still reach the Planner LLM
through ``attention_md`` as natural-language hints.

DSL format (one rule per line, bullet optional):
    - dnd: HH:MM-HH:MM [weekdays=Mon-Fri|Sat-Sun|0-6|0,2,4] reason=<snake_tag>
    - quiet_hours: HH:MM-HH:MM
Examples that parse::

    - dnd: 22:30-06:00 reason=nighttime
    - dnd: 11:00-15:00 weekdays=Mon-Fri reason=translation_focus
    - dnd: 00:00-09:00 weekdays=Sat-Sun reason=weekend_sleep_in
    - quiet_hours: 23:00-07:00
"""

from __future__ import annotations

import re

from raven.config.raven import DndWindow
from raven.memory_engine.consolidate.attention import parse_attention

# ``- dnd: 22:30-06:00 [weekdays=...] [reason=...]``
_DND_RE = re.compile(
    r"^\s*[-*]?\s*(?:dnd|quiet[_\s]?hours?)\s*[:：]\s*"
    r"(?P<sh>\d{1,2})[:：](?P<sm>\d{2})"
    r"\s*[-—~]\s*"
    r"(?P<eh>\d{1,2})[:：](?P<em>\d{2})"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)
_WEEKDAYS_RE = re.compile(
    r"weekdays\s*=\s*(?P<spec>[A-Za-z0-9,\-]+)",
    re.IGNORECASE,
)
_REASON_RE = re.compile(r"reason\s*=\s*(?P<tag>\S+)", re.IGNORECASE)

_DAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_weekdays(spec: str) -> list[int] | None:
    """Accept ``Mon-Fri``, ``Sat-Sun``, ``0-4``, ``0,2,4``, ``mon,wed,fri``.

    Returns sorted unique day indices in 0..6 (Mon..Sun), or ``None`` on
    parse failure so the rule still applies to every day rather than
    being silently dropped.
    """
    raw = spec.strip().lower()
    days: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            a, b = a.strip(), b.strip()
            ai = _DAY_NAMES.get(a, _to_int(a))
            bi = _DAY_NAMES.get(b, _to_int(b))
            if ai is None or bi is None:
                return None
            for d in range(min(ai, bi), max(ai, bi) + 1):
                days.add(d)
        else:
            di = _DAY_NAMES.get(chunk, _to_int(chunk))
            if di is None:
                return None
            days.add(di)
    return sorted(days) if days else None


def _to_int(s: str) -> int | None:
    try:
        v = int(s)
        return v if 0 <= v <= 6 else None
    except ValueError:
        return None


def parse_user_overrides_dnd(attention_md: str) -> list[DndWindow]:
    """Pick the ``## User overrides`` body out of ``attention_md`` and
    return its structured DND windows. Empty list on missing section or
    no parseable lines."""
    if not attention_md:
        return []
    sections = parse_attention(attention_md)
    body = sections.get("## User overrides", "")
    if not body.strip():
        return []
    out: list[DndWindow] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _DND_RE.match(line)
        if not m:
            continue
        try:
            sh = int(m.group("sh"))
            sm = int(m.group("sm"))
            eh = int(m.group("eh"))
            em = int(m.group("em"))
        except ValueError:
            continue
        # eh == 24 is allowed only as 24:00 (end-of-day); 24:30 etc. is
        # invalid. Unlike the scorer-injection path, the user-handwritten
        # DSL has no eh>=24 normalization, so reject malformed minutes here.
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 24 and 0 <= em <= 59):
            continue
        if eh == 24 and em != 0:
            continue
        rest = m.group("rest") or ""
        weekdays: list[int] | None = None
        wm = _WEEKDAYS_RE.search(rest)
        if wm:
            weekdays = _parse_weekdays(wm.group("spec"))
        reason = ""
        rm = _REASON_RE.search(rest)
        if rm:
            reason = rm.group("tag")
        out.append(
            DndWindow(
                start_hour=sh,
                start_minute=sm,
                end_hour=eh,
                end_minute=em,
                weekdays=weekdays,
                why=reason or "user_override",
            )
        )
    return out


_PLAN_HEAD_RE = re.compile(
    r"^\s*[-*]\s*"
    r"(?P<h>\d{1,2})[:：](?P<m>\d{2})\s+"
    r"(?P<tag>[A-Za-z0-9_]+)"
    r"(?P<rest>.*)$",
)
_PLAN_PRIORITY_RE = re.compile(
    r"priority\s*=\s*(?P<pri>low|medium|high)",
    re.IGNORECASE,
)
_PLAN_MSG_RE = re.compile(
    r"msg\s*=\s*(?P<msg>.*)",
    re.IGNORECASE | re.DOTALL,
)


def parse_daily_plan(attention_md: str) -> list[dict]:
    """Read attention.md ``## 今日 fire 计划`` body, return list of
    ``{"time_hhmm","topic_tag","priority","user_message","rationale"}`` entries.

    Lines that don't match the DSL are ignored (HTML comments,
    blank lines). Returns empty list when the section is absent or
    contains no parseable entries.

    DSL: ``- HH:MM topic_tag [| priority=...] [| msg=...] [| <rationale>]``.
    Split on ``|`` rather than packing into one giant regex so the
    keyed fields (priority / msg) vs the trailing rationale stay
    unambiguous. ``msg`` is the user-facing nudge text; ``rationale`` is
    the evidence-citing note. ``user_message`` is "" for legacy entries
    written before the field existed.
    """
    if not attention_md:
        return []
    sections = parse_attention(attention_md)
    body = sections.get("## 今日 fire 计划", "")
    if not body.strip():
        return []
    out: list[dict] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("<!--"):
            continue
        m = _PLAN_HEAD_RE.match(line)
        if not m:
            continue
        try:
            h = int(m.group("h"))
            mi = int(m.group("m"))
        except ValueError:
            continue
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            continue
        priority = "low"
        user_message = ""
        rationale_parts: list[str] = []
        for seg in (m.group("rest") or "").split("|"):
            seg = seg.strip()
            if not seg:
                continue
            pm = _PLAN_PRIORITY_RE.match(seg)
            if pm:
                priority = pm.group("pri").lower()
                continue
            mm = _PLAN_MSG_RE.match(seg)
            if mm:
                user_message = mm.group("msg").strip()
                continue
            rationale_parts.append(seg)
        out.append(
            {
                "time_hhmm": f"{h:02d}:{mi:02d}",
                "topic_tag": m.group("tag").strip().lower(),
                "priority": priority,
                "user_message": user_message,
                "rationale": " ".join(rationale_parts),
            }
        )
    return out


__all__ = ["parse_user_overrides_dnd", "parse_daily_plan"]
