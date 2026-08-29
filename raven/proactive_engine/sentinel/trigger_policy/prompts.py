"""Prompt templates and tool schema for ProactivePlanner."""

from __future__ import annotations

import calendar
import re
from datetime import datetime

from raven.i18n import t, zh_lexicon
from raven.proactive_engine.sentinel.types import PlannerContext
from raven.security.trust import wrap_untrusted

_DATE_RE = re.compile(
    r"(?P<iso>\d{4}-\d{1,2}-\d{1,2})"
    r"|"
    r"(?P<m>\d{1,2})(?:[/\-]|" + zh_lexicon.MONTH + r")(?P<d>\d{1,2})" + zh_lexicon.DAY + "?"
)


def _extract_upcoming_deadlines(
    text: str,
    now: datetime,
    horizon_days: int = 30,
) -> list[str]:
    """Pull date-line pairs from text and return ``- YYYY-MM-DD (N days left): <snippet>``.

    Spoon-feeds the Planner explicit timing so it doesn't have to parse
    Chinese dates from MEMORY.md text — qwen-27b was confabulating
    "approaching" on deadlines 11-24 days out. Past dates and dates
    beyond ``horizon_days`` are dropped.
    """
    if not text:
        return []
    seen: set[tuple] = set()
    out: list[tuple[int, datetime, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for m in _DATE_RE.finditer(line):
            try:
                if m.group("iso"):
                    dt = datetime.strptime(m.group("iso"), "%Y-%m-%d")
                else:
                    mo, da = int(m.group("m")), int(m.group("d"))
                    dt = datetime(now.year, mo, da)
                    if dt.date() < now.date():
                        dt = datetime(now.year + 1, mo, da)
            except (ValueError, TypeError):
                continue
            days = (dt.date() - now.date()).days
            if days < 0 or days > horizon_days:
                continue
            snippet = line[:80]
            key = (dt.date(), snippet)
            if key in seen:
                continue
            seen.add(key)
            out.append((days, dt, snippet))
    out.sort(key=lambda x: x[0])
    return [
        t("- {date} ({days} days left): {snippet}", date=dt.strftime("%Y-%m-%d"), days=days, snippet=snippet)
        for days, dt, snippet in out
    ]


PLANNER_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "planner_decision",
        "description": "Report your decision about whether to proactively act.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "skip",
                        "nudge",
                        "nudge_inject",
                        "nudge_defer",
                        "spawn_agent",
                    ],
                    "description": (
                        "skip = do nothing this tick. "
                        "nudge = send a standalone message NOW. "
                        "nudge_inject = append content to the agent's NEXT reply in "
                        "target_session (use when user is mid-conversation AND your "
                        "info naturally extends what the agent is about to say — "
                        "e.g. user asked about flights, you want to add a passport "
                        "expiry heads-up). "
                        "nudge_defer = wait until target_session's current thread "
                        "settles, then send as follow-up (use when interrupting "
                        "would hurt but you still have value to deliver after — "
                        "e.g. user is asking about a medical symptom, you have "
                        "refill reminders that shouldn't derail the current topic). "
                        "spawn_agent = dispatch a micro-agent for a multi-step task."
                    ),
                },
                "topic_tag": {
                    "type": "string",
                    "description": (
                        "Short stable identifier for the *topic* of this nudge "
                        "(snake_case, ASCII). Same logical topic must reuse the "
                        "same tag across ticks so NudgePolicy can suppress "
                        "rapid same-topic repeats even when wording differs. "
                        "Examples: 'deadline_clawtrack', 'birthday_xiaotang', "
                        "'weekly_running_goal', 'blog_writing'. For action=skip "
                        "you may use 'n_a'."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "One-sentence justification for this decision.",
                },
                "proactivity_score": {
                    "type": "number",
                    "description": (
                        "Confidence 0-1 that proactive action would benefit the user. "
                        "Below 0.5 should default to skip unless other evidence is strong."
                    ),
                    "minimum": 0,
                    "maximum": 1,
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "high = urgent / time-sensitive; "
                        "medium = scheduled routine or known deadline; "
                        "low = suggestion, informational"
                    ),
                },
                "target_session": {
                    "type": "string",
                    "description": "Which session key to deliver to (pick from active_sessions).",
                },
                "nudge_message": {
                    "type": "string",
                    "description": (
                        "The message (required when action in {nudge, nudge_inject, nudge_defer}). "
                        "Reflect the user's tone preferences and cite relevant details "
                        "from memory/sessions. Give a 'pass' exit (e.g. 'no rush, next time') "
                        "when appropriate. For nudge_inject, write it as a P.S.-style "
                        "addendum that piggybacks on the agent's main reply. For "
                        "nudge_defer, write it as a follow-up that opens with a bridge "
                        "back to what the user just was doing."
                    ),
                },
                "spawn_task": {
                    "type": "string",
                    "description": (
                        "Self-contained task description for the micro-agent (required when action=spawn_agent)."
                    ),
                },
                "defer_condition": {
                    "type": "string",
                    "description": (
                        "Natural-language wait condition (required when "
                        "action=nudge_defer). Describe what event in the target "
                        "session should unblock the follow-up, e.g. "
                        "'the back-pain conversation winds down' / 'user replies to flight search'."
                    ),
                },
            },
            "required": ["action", "topic_tag", "reason", "proactivity_score"],
        },
    },
}


def build_context_prompt(ctx: PlannerContext) -> str:
    """Assemble the user-role context block for one tick.

    Target < 2K tokens — the Planner is a fast-path decision, not a
    reasoning marathon.
    """
    parts: list[str] = []

    parts.append(
        t(
            "## Current time\n{now} ({weekday})",
            now=ctx.now.isoformat(),
            weekday=t(calendar.day_name[ctx.now.weekday()]),
        )
    )

    if ctx.user_profile:
        parts.append(t("## User profile\n{profile}", profile=ctx.user_profile.strip()))

    if ctx.memory_md:
        parts.append(t("## User MEMORY.md\n") + wrap_untrusted(ctx.memory_md.strip(), source="unverified memory"))

    # Pre-compute days_until for date-like patterns found in user_profile /
    # memory_md so the Planner doesn't have to parse dates itself and
    # confabulate "approaching" on deadlines 11-24 days out.
    upcoming = _extract_upcoming_deadlines(
        "\n".join([ctx.user_profile or "", ctx.memory_md or ""]),
        ctx.now,
    )
    if upcoming:
        parts.append(t("## Upcoming deadlines (days remaining computed)\n") + "\n".join(upcoming))

    if ctx.history_md_recent:
        parts.append(t("## Recent HISTORY excerpt\n{history}", history=ctx.history_md_recent.strip()))

    # Folded behaviors.md tail — one line per BehaviorEvent within the
    # window. Complements HISTORY by surfacing intent/outcome patterns
    # the Planner uses for "same topic recently failed → don't re-nudge".
    if ctx.behaviors_recent:
        parts.append(
            t(
                "## Recent behaviour summary\n"
                "(folded BehaviorEvents -- format `[date slot turns] intent->outcome "
                "topic #project: summary`, newest last)\n{behaviors}",
                behaviors=ctx.behaviors_recent.strip(),
            )
        )

    # Sentinel/cron-derived state. Sections selected via
    # SentinelConfig.attention_planner_sections; assembled by
    # AttentionUpdater and read back here.
    if ctx.attention_md:
        parts.append(
            t("## attention.md (sentinel-derived)\n")
            + wrap_untrusted(ctx.attention_md.strip(), source="unverified attention")
        )

    if ctx.active_sessions:
        lines = []
        for s in ctx.active_sessions:
            entry = (
                f"- **{s.key}** (last active {s.last_active_at.isoformat()})\n"
                f"  user: {s.last_user_message or '(none)'}\n"
                f"  assistant: {s.last_assistant_message or '(none)'}"
            )
            if s.status:
                entry += f"\n  status: {s.status}"
            lines.append(entry)
        parts.append(t("## Active sessions\n") + "\n".join(lines))

    if ctx.routines:
        lines = []
        for r in ctx.routines:
            dow = t("every day") if r.day_of_week is None else t(calendar.day_name[r.day_of_week])
            ts = t(" {start:02d}-{end:02d}h", start=r.time_slot[0], end=r.time_slot[1]) if r.time_slot else ""
            lines.append(
                t(
                    "- [{status}] {pattern} ({dow}{ts}, seen {count} times)",
                    status=r.status,
                    pattern=r.pattern,
                    dow=dow,
                    ts=ts,
                    count=r.occurrence_count,
                )
            )
        parts.append(t("## Learned routines\n") + "\n".join(lines))

    if ctx.calendar:
        parts.append(t("## Calendar\n") + "\n".join(f"- {c}" for c in ctx.calendar))

    nps = ctx.nudge_policy_state
    policy_lines = [
        t("## NudgePolicy state"),
        t("- nudges used this hour: {n}", n=nps.nudges_used_this_hour),
        t("- remaining today: {n}", n=nps.remaining_today),
        f"- Quiet hours: {'yes' if nps.in_quiet_hours else 'no'}",
    ]
    # Only render when multiplier deviates from 1.0 — otherwise the
    # default line just pollutes the prompt.
    if nps.hour_quota_multiplier < 0.99:
        policy_lines.append(
            t(
                "- **adaptive tightening**: hour_quota x {m:.2f} (acceptance over the last 7 days is low; raise the nudge value threshold)",
                m=nps.hour_quota_multiplier,
            )
        )
    elif nps.hour_quota_multiplier > 1.01:
        policy_lines.append(
            t(
                "- **adaptive loosening**: hour_quota x {m:.2f} (acceptance over the last 7 days is high; a borderline helpful follow-up may go out, the quality bar stays)",
                m=nps.hour_quota_multiplier,
            )
        )
    parts.append("\n".join(policy_lines))

    if ctx.last_decision:
        last = ctx.last_decision
        entry = t(
            "## Last tick's decision\naction={action}, priority={priority}\nreason: {reason}",
            action=last.action,
            priority=last.priority,
            reason=last.reason,
        )
        if last.nudge_message:
            entry += t("\nlast nudge: {message}", message=last.nudge_message[:200])
        parts.append(entry)

    fh = ctx.fire_history or {}
    if fh and (fh.get("topic_counts_24h") or fh.get("topic_counts_7d") or fh.get("recent_dismissals")):
        lines = [t("## Recent nudge history")]
        t24 = fh.get("topic_counts_24h") or {}
        t7d = fh.get("topic_counts_7d") or {}
        if t24:
            lines.append(
                t(
                    "- topic_tags pushed in the last 24h (**if this nudge is the same topic, reuse one of these tags verbatim; "
                    "no synonyms**, e.g. `anniversary_tom` exists -> do not create `anniversary_8year`): "
                )
                + ", ".join(f"`{k}`x{v}" for k, v in sorted(t24.items(), key=lambda x: -x[1])[:8])
            )
        if t7d:
            top7 = [(k, v) for k, v in t7d.items() if v >= 2]
            if top7:
                lines.append(
                    t("- topics pushed 2+ times in 7 days (**same rule: same topic, same tag**): ")
                    + ", ".join(f"`{k}`x{v}" for k, v in sorted(top7, key=lambda x: -x[1])[:8])
                )
        dismissals = fh.get("recent_dismissals") or []
        if dismissals:
            lines.append(
                t(
                    "- recent dismissals: {n} (the user said they knew / did not want to be disturbed)",
                    n=len(dismissals),
                )
            )
        lines.append(
            t(
                "  -> **respect these signals**: mute a session that was just dismissed; "
                "same topic pushed 1+ in 24h / 4+ in 7d -> skip or a different topic (**not a new tag or new wording**); "
                "but never miss a genuinely urgent deadline."
            )
        )
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


__all__ = ["PLANNER_TOOL", "build_context_prompt"]
