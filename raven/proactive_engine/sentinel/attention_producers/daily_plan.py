"""The daily fire plan section of attention.md, an LLM-driven daily fire schedule.

Runs once per day (first tick after 06:00 local time) to enumerate the
fires the Planner intends to deliver today: routines, in-window
deadlines, weekly habits, calendar-driven reminders. Output lands in
``attention.md`` and is read back by the tick-by-tick Planner so each
tick can defer to the daily plan instead of re-discovering candidates
from raw memory.

Bridges the "30-min reactive tick" architecture toward "daily planning
+ tick execution" — what mature scheduling systems (and humans) do.
Adds ~1 LLM call/day at the cost of higher Type-A coverage (routines
get explicit slots) and better Restraint (single LLM has full visibility
to spread fires + avoid DND windows).

Output DSL (one entry per line, parseable by ``parse_daily_plan``):

    ## Today's fire plan
    <!-- generated 2026-05-04T06:00:00 | model=qwen3.5-27b -->
    - 07:30 routine_morning_med | priority=high | "morning meds"
    - 11:30 routine_noon_med | priority=high | "pre-lunch meds"
    - 13:00 deadline_clawtrack | priority=medium | "release in 3 days"
    - 19:00 routine_emotion_log | priority=low | "evening mood log"
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable

from loguru import logger

from raven.i18n import prompt, t, zh_lexicon
from raven.memory_engine import DAILY_FIRE_PLAN_HEADER
from raven.proactive_engine.sentinel.attention_producers._base import (
    WEEKDAY,
    AttentionProducer,
)

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider
    from raven.memory_engine import MemoryStore


# Date patterns commonly written in MEMORY.md and persona text:
#   "5/15" / "5-15"   — M/D (current year assumed)
#   month/day with the Chinese counters (zh_lexicon.DATE_PATTERN)
#   "2026-05-15"      — ISO full date
# We do NOT use a single mega-regex: keeping these split keeps misfires
# (e.g. matching version "v1.5.10") tractable to debug.
_DATE_RE_ZH = re.compile(zh_lexicon.DATE_PATTERN)
_DATE_RE_MD = re.compile(r"(?<![\d.])\b(?P<m>\d{1,2})[/-](?P<d>\d{1,2})\b(?![\d.])")
_DATE_RE_ISO = re.compile(r"\b\d{4}-(?P<m>\d{2})-(?P<d>\d{2})\b")


def _extract_dates_from_memory(text: str, today: date) -> list[tuple[str, int, int, int]]:
    """Scan ``text`` for date-like patterns. Return tuples
    ``(snippet, month, day, days_until)`` sorted by ``days_until``.

    Dates that fall outside the [-30, +60] day window from ``today`` are
    dropped — older entries are historical, far-future entries are noise.
    """
    seen: set[tuple[int, int, str]] = set()
    out: list[tuple[str, int, int, int]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pat in (_DATE_RE_ZH, _DATE_RE_ISO, _DATE_RE_MD):
            for m in pat.finditer(line):
                try:
                    mn = int(m.group("m"))
                    dn = int(m.group("d"))
                except (ValueError, IndexError):
                    continue
                if not (1 <= mn <= 12 and 1 <= dn <= 31):
                    continue
                try:
                    target = date(today.year, mn, dn)
                except ValueError:
                    continue
                snippet = line[:80].replace("\t", " ")
                key = (mn, dn, snippet[:40])
                if key in seen:
                    continue
                seen.add(key)
                days_until = (target - today).days
                if days_until < -30 or days_until > 60:
                    continue
                out.append((snippet, mn, dn, days_until))
    out.sort(key=lambda x: x[3])
    return out


def _format_days_until_block(text: str, now: datetime) -> str:
    """Render the detected-key-dates markdown block listing every date
    found in ``text`` with its T-N offset from today. Returns empty
    string when nothing is found — LLM should then fall back to dates
    explicitly mentioned in MEMORY.md."""
    today = now.date()
    dates = _extract_dates_from_memory(text, today)
    if not dates:
        return ""
    lines = [t("## Detected key dates (today = {today}, T-N precomputed)", today=today.isoformat())]
    for snippet, mn, dn, days_until in dates:
        if days_until < 0:
            tag = t("T+{n} ({n} days ago)", n=-days_until)
        elif days_until == 0:
            tag = t("T-day (today)")
        else:
            tag = t("T-{n} ({n} days left)", n=days_until)
        lines.append(t("- {month}/{day} = {tag}  · context: {snippet}", month=mn, day=dn, tag=tag, snippet=snippet))
    return "\n".join(lines)


_PLAN_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "emit_daily_plan",
        "description": (
            "Emit today's proactive-fire schedule. List ONLY topics worth "
            "firing today; skip days where nothing routine/anticipatory "
            "is due. Return entries sorted by scheduled time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "time_hhmm": {
                                "type": "string",
                                "description": "24-hour time HH:MM when to fire.",
                            },
                            "topic_tag": {
                                "type": "string",
                                "description": (
                                    "snake_case stable topic key. Prefixes: "
                                    "routine_*, daily_*, weekly_*, monthly_*, "
                                    "deadline_*, birthday_*, anniversary_*."
                                ),
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "rationale": {
                                "type": "string",
                                "description": (
                                    "Short evidence-citing note (internal — for "
                                    "logs/scoring). Quote the specific USER.md "
                                    "sentence that justifies firing today."
                                ),
                            },
                            "user_message": {
                                "type": "string",
                                "description": (
                                    "The exact words shown to the user when this "
                                    "fires — natural, warm, in the user's language, "
                                    "one short line. NOT internal bookkeeping: no "
                                    "'USER.md records', no role labels. Must contain "
                                    "no '|' character."
                                ),
                            },
                        },
                        "required": [
                            "time_hhmm",
                            "topic_tag",
                            "priority",
                            "rationale",
                            "user_message",
                        ],
                    },
                },
            },
            "required": ["entries"],
        },
    },
}


class DailyPlanProducer(AttentionProducer):
    """Attention producer that calls an LLM once per day to emit a fire
    schedule. Cached for the rest of the day; next call after 06:00
    local time triggers a fresh plan."""

    SECTION_HEADER = DAILY_FIRE_PLAN_HEADER

    _PLAN_CADENCE = timedelta(hours=20)
    _PLAN_TEMPERATURE = 0.3
    _PLAN_MAX_TOKENS = 2048

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        provider: "LLMProvider",
        policy=None,
        model: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._provider = provider
        self._policy = policy
        self._model = model or ""
        self._now_fn = now_fn or datetime.now
        self._last_plan_at: datetime | None = None
        self._cached_body: str = ""
        self._inflight_lock = asyncio.Lock()

    def should_run(self, now: datetime) -> bool:
        # Honor cadence: skip if we already planned within last 20 hours.
        if self._last_plan_at is not None:
            if now - self._last_plan_at < self._PLAN_CADENCE:
                return False
        # Only kick off a new plan after the morning wake threshold to
        # ensure quiet-hours / sleep-end signals are stable.
        if now.hour < 6:
            return False
        return True

    async def compute_body(self, now: datetime) -> str:
        async with self._inflight_lock:
            if self._last_plan_at is not None and now - self._last_plan_at < self._PLAN_CADENCE:
                return self._cached_body
            try:
                body = await self._run_llm(now)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DailyPlanProducer LLM call failed: {}: {}",
                    type(exc).__name__,
                    exc,
                )
                body = self._cached_body  # keep yesterday's plan rather than blank
            self._cached_body = body
            self._last_plan_at = now
            return body

    async def _run_llm(self, now: datetime) -> str:
        weekday = WEEKDAY[now.weekday()]
        memory_md = self._memory_store.read_long_term()
        attention_md = self._read_attention_excerpt()
        used_tags = self._recent_topic_tags(now)

        used_tags_block = t("## Used topic_tags (reuse these; do not coin new names)\n") + (
            "\n".join(f"- `{tag}`" for tag in used_tags) if used_tags else t("(no history)")
        )
        dates_block = _format_days_until_block(memory_md + "\n" + (attention_md or ""), now)

        user_prompt = (
            t("## Current time\n{now} ({weekday})\n\n", now=now.isoformat(), weekday=t(weekday))
            + t("## User MEMORY.md (profile + preferences + deadlines)\n{memory}\n\n", memory=memory_md.strip())
            + t(
                "## attention.md excerpt (routines / project rhythm / pending)\n{attention}\n\n",
                attention=attention_md.strip() if attention_md else t("(empty)"),
            )
            + f"{dates_block}\n\n"
            + f"{used_tags_block}\n\n"
            + t(
                "Build today's fire plan from the above. **Emit only topics with concrete evidence in USER.md**; "
                "do not invent from prefix templates (such as routine_morning_med). Reuse the exact string of any "
                "historically used tag. **Follow the T-N window rules strictly** -- the T values are precomputed in the "
                "detected-key-dates block, read them off. Return through the emit_daily_plan tool."
            )
        )

        response = await self._provider.chat_with_retry(
            messages=[
                {"role": "system", "content": prompt("daily_planner")},
                {"role": "user", "content": user_prompt},
            ],
            tools=[_PLAN_TOOL],
            model=self._model or None,
            max_tokens=self._PLAN_MAX_TOKENS,
            temperature=self._PLAN_TEMPERATURE,
        )
        if not response.has_tool_calls:
            logger.warning(
                "DailyPlan: no tool call from LLM (content head: {})",
                (response.content or "")[:120],
            )
            return ""

        try:
            args = response.tool_calls[0].arguments
            entries = args.get("entries") or []
        except (KeyError, AttributeError, json.JSONDecodeError):
            return ""

        if not entries:
            return ""

        entries.sort(key=lambda e: str(e.get("time_hhmm", "99:99")))
        lines = [f"<!-- generated {now.isoformat()} | model={self._model or 'default'} | entries={len(entries)} -->"]
        for e in entries:
            hhmm = str(e.get("time_hhmm", "")).strip()
            tag = str(e.get("topic_tag", "")).strip()
            pri = str(e.get("priority", "low")).strip()
            # '|' is the field separator, so it must not leak into any value.
            why = str(e.get("rationale", "")).strip().replace("\n", " ").replace("|", "/")
            msg = str(e.get("user_message", "")).strip().replace("\n", " ").replace("|", "/")
            if not hhmm or not tag:
                continue
            head = f"- {hhmm} {tag} | priority={pri}"
            if msg:
                head += f" | msg={msg}"
            lines.append(f"{head} | {why}")
        return "\n".join(lines)

    def _recent_topic_tags(self, now: datetime, days: int = 14) -> list[str]:
        """Return list of topic_tags that fired in the last ``days`` —
        the planner is required to reuse these strings for matching
        logical topics so NudgePolicy's per-topic dedup engages."""
        if self._policy is None:
            return []
        try:
            return self._policy.recent_topic_tags(now - timedelta(days=days))
        except (AttributeError, TypeError):
            return []

    def _read_attention_excerpt(self) -> str:
        """Return attention.md sections relevant to planning (routines /
        rhythm / pending), or empty string when none are populated."""
        try:
            attention_file = self._memory_store.attention_file
            if not attention_file.exists():
                return ""
            from raven.memory_engine import parse_attention

            sections = parse_attention(attention_file.read_text(encoding="utf-8"))
            wanted = [
                "## User overrides",
                "## Pending proposals",
                "## Currently focused on",
                "## Project rhythm (last 7 days)",
            ]
            parts = []
            for h in wanted:
                body = sections.get(h, "").strip()
                if body:
                    parts.append(f"{h}\n{body}")
            return "\n\n".join(parts)
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["DailyPlanProducer"]
