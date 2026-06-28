"""Cron tool for scheduling reminders and tasks.

Two layers live here:

1. ``CronTool`` — the LLM-facing agent tool. Creates jobs with the
   request-time channel/chat_id verbatim; no forwarding decision here.
2. ``resolve_cron_delivery`` — delivery resolver consumed at TRIGGER
   time by ``raven.cli._cron_handler``. Pass-through for real
   channels; broadcast/forward for ephemeral ones (cli/tui).
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from raven.agent.tools.base import Tool
from raven.proactive_engine.schedulers.cron.service import CronService
from raven.proactive_engine.schedulers.cron.types import CronSchedule

if TYPE_CHECKING:
    from raven.session.manager import SessionManager


# ────────────────────────────────────────────────────────────────────
# Delivery resolution (consumed at TRIGGER time, not at job creation)
# ────────────────────────────────────────────────────────────────────


@dataclass
class DeliveryTarget:
    """One concrete (channel, chat_id) the cron handler should deliver to."""

    channel: str
    chat_id: str


def is_ephemeral_channel(channel: str, enabled_channels: set[str]) -> bool:
    """An ephemeral channel cannot deliver to itself after its host
    process exits (cli/tui REPL closes; webui session ends). Rule:
    anything not in ``ChannelManager.enabled_channels`` is ephemeral
    and needs forwarding.

    Today this matches ``cli`` + ``tui``. Future webui / desktop
    frontends are covered automatically with no code change.
    """
    return channel not in enabled_channels


def resolve_cron_delivery(
    *,
    channel: str,
    chat_id: str,
    forward_channels: list[str],
    enabled_channels: set[str],
    session_manager: "SessionManager | None" = None,
) -> tuple[list[DeliveryTarget], list[str]]:
    """Resolve final delivery targets for a cron job at trigger time.

    Returns ``(targets, warnings)``:
      - non-ephemeral channels pass through directly (per-job binding);
      - ephemeral channels (cli/tui) broadcast per ``forward_channels``:
        ``["*"]`` expands to ``enabled_channels``, specific names
        restrict; each target's chat_id comes from ``session_manager``
        (most-recent session). Channels with no recent session are
        skipped with a warning.

    Warnings are surfaced via log, never raised — one stale forward
    target won't break a fire that has other valid targets.
    """
    if not is_ephemeral_channel(channel, enabled_channels):
        return [DeliveryTarget(channel=channel, chat_id=chat_id)], []

    if not forward_channels:
        return [], [f"{channel}: no forward_channels configured"]

    if "*" in forward_channels:
        targets_channels = list(enabled_channels)
    else:
        targets_channels = [c for c in forward_channels if c in enabled_channels]

    if not targets_channels:
        return [], [f"{channel}: forward_channels has no overlap with enabled"]

    results: list[DeliveryTarget] = []
    warnings: list[str] = []
    for ch in targets_channels:
        cid = (
            session_manager.find_most_recent_chat_id(ch)
            if session_manager is not None else None
        )
        if cid:
            results.append(DeliveryTarget(channel=ch, chat_id=cid))
        else:
            warnings.append(f"{ch}: no recent session, skipped")
    return results, warnings


# ────────────────────────────────────────────────────────────────────
# CronTool (LLM-facing)
# ────────────────────────────────────────────────────────────────────


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove.\n"
            "\n"
            "BEFORE displaying any reminder list, table, or status summary (CRITICAL):\n"
            "ALWAYS call `cron(action='list')` first to fetch the live job set.\n"
            "Do NOT reconstruct the list from conversation history — fired one-shot\n"
            "reminders, expired schedules, and reminders created in cron-triggered\n"
            "callback sessions all change the job set behind your back. Conversation\n"
            "history will show 'I scheduled X at 14:33' but a job that fired at 14:35\n"
            "is GONE from jobs.json (delete_after_run=true for ``at`` schedules).\n"
            "Without a fresh `cron.list` you will hallucinate '⏳ pending' rows for\n"
            "jobs that already completed — this confuses the user about real state.\n"
            "\n"
            "When NOT to create a recurring cron (F-K.1 — explicit ask required):\n"
            "Recurring crons (`cron_expr` or `every_seconds`) require the user to "
            "EXPLICITLY ask for a repeating schedule. Phrases like '每天提醒...', "
            "'remind me weekly', '每小时...', 'every Monday' — these imply repetition. "
            "WITHOUT such phrasing, default to `at` (one-shot) OR no cron at all.\n"
            "\n"
            "When NOT to use cron at all (F-K.3 — complaints ≠ reminders):\n"
            "Users often vent about chronic problems ('脖子又僵了', '总忘了喝水', "
            "'I keep forgetting to stretch'). This is a COMPLAINT, not a reminder "
            "request. Replying with empathy + ad-hoc action is usually correct. "
            "Do NOT create a recurring cron unless the user follows up with "
            "an explicit '请帮我设个定时提醒' / 'please set up a recurring reminder'. "
            "Otherwise the cron will fire forever, including at night, weekends, "
            "and during focus time — the very situations the user was complaining "
            "about.\n"
            "\n"
            "When to use which schedule field (CRITICAL — choose carefully):\n"
            "- `at`: ONE-TIME reminders. Use this for any 'X minutes/hours/days from now' "
            "intent. Compute the absolute target time yourself (current_time + duration) "
            "and pass it as ISO datetime. ⚠️ DO NOT use `every_seconds` to express "
            "'X minutes later' — that creates an infinite repeating reminder.\n"
            "- `cron_expr`: Fixed-time recurring patterns ('每天 7:00', 'every Monday 9 AM'). "
            "User must explicitly want a repeating schedule on a CLOCK basis.\n"
            "- `every_seconds`: Periodic interval that should repeat indefinitely "
            "(e.g. 'every hour during work', 'check every 30 min'). Rarely correct — "
            "user usually means a one-shot reminder, not infinite repetition. Default to `at` "
            "unless user explicitly asks for ongoing repetition.\n"
            "\n"
            "Anti-pattern examples to avoid:\n"
            "- ❌ '50 分钟后提醒我休息' → every_seconds=3000 (recurring forever, fires at 3am)\n"
            "- ✅ '50 分钟后提醒我休息' → at=(now+50min) (fires once)\n"
            "- ❌ '每天提醒吃药' → at=tomorrow_07:00 (fires once, never repeats)\n"
            "- ✅ '每天提醒吃药' → cron_expr='0 7 * * *' (fires daily forever)\n"
            "- ❌ '脖子总是僵' → cron_expr='*/45 * * * *' (assumes recurring without ask, "
            "fires at midnight) — instead reply with empathy + suggest one-shot stretch\n"
            "- ✅ '脖子总是僵' → no cron; sympathize + offer concrete now-action\n"
            "- ❌ '每次写作我都忘记喝水' → recurring cron (no explicit ask) — instead "
            "acknowledge + suggest user-driven habit cue\n"
            "- ✅ '帮我每小时提醒喝水' → cron_expr='0 * * * *' (explicit ask)\n"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "message": {"type": "string", "description": "Reminder message (for add)"},
                "every_seconds": {
                    "type": "integer",
                    "description": (
                        "Interval in seconds for INFINITELY-recurring reminders. "
                        "⚠️ DO NOT use to express 'X minutes from now' — use `at` "
                        "(one-shot) for that. Only set when user explicitly wants "
                        "ongoing periodic repetition without an end."
                    ),
                },
                "cron_expr": {
                    "type": "string",
                    "description": (
                        "Cron expression like '0 9 * * *' for clock-based recurring "
                        "schedules ('every day at 9am', 'every Monday')."
                    ),
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron expressions (e.g. 'America/Vancouver')",
                },
                "at": {
                    "type": "string",
                    "description": (
                        "ISO datetime for ONE-TIME execution (e.g. '2026-02-12T10:30:00'). "
                        "Use for 'X minutes from now' / 'tomorrow at Y' / any non-repeating "
                        "reminder. Compute absolute time = current_time + duration."
                    ),
                },
                "topic_tag": {
                    "type": "string",
                    "description": (
                        "Short snake_case identifier for the subject of this "
                        "reminder (e.g. 'birthday_zhouxiaotang', "
                        "'medication_morning', 'anniversary_8year'). "
                        "STRONGLY RECOMMENDED for any recurring or topical "
                        "reminder. Two purposes: "
                        "(1) L3 Sentinel suppresses its own proactive nudges "
                        "on the same topic within 24h after this cron fires — "
                        "prevents double-reminding via both surfaces; "
                        "(2) cron_create dedups by topic_tag — if a cron for "
                        "'medication_morning' already exists, creating a new "
                        "one with the same topic_tag updates the existing job "
                        "instead of spawning a parallel reminder. Without "
                        "topic_tag the LLM tends to create N near-duplicate "
                        "crons across the month (different schedules, same "
                        "intent) → fires N times where 1 was wanted. "
                        "Omit only for one-off arbitrary reminders with no "
                        "Sentinel overlap and no risk of re-asking later."
                    ),
                },
                "job_id": {"type": "string", "description": "Job ID (for remove)"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        topic_tag: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(message, every_seconds, cron_expr, tz, at, topic_tag)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        topic_tag: str | None = None,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"
        # tz anchors a cron expression's wall-clock recurrence and a naive `at`
        # datetime; for an every schedule (a relative interval) it is meaningless
        # and is ignored rather than erroring (no needless agent retry).
        if tz and (cron_expr or at):
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(tz)
            except Exception:
                return f"Error: unknown timezone '{tz}'"

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at:
            from datetime import datetime

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            # A naive `at` + tz means "that wall-clock time in tz" — anchor it so
            # .timestamp() does not silently fall back to the host's local zone.
            # An offset-aware string already carries its zone, so tz is ignored.
            if dt.tzinfo is None and tz:
                from zoneinfo import ZoneInfo

                dt = dt.replace(tzinfo=ZoneInfo(tz))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        # Store request-time channel/chat_id verbatim. Delivery resolution
        # (per-job pass-through vs ephemeral forward) happens at trigger
        # time in ``raven.cli._cron_handler`` via ``resolve_cron_delivery``.
        try:
            job = self._cron.add_job(
                name=message[:30],
                schedule=schedule,
                message=message,
                deliver=True,
                channel=self._channel,
                to=self._chat_id,
                delete_after_run=delete_after,
                topic_tag=topic_tag,
            )
        except ValueError as exc:
            # A non-runnable schedule (at in the past, every_seconds <= 0, an
            # invalid cron expr) is rejected by the service rather than stored as
            # a job that silently never fires — surface it so the agent retries.
            return f"Error: {exc}"
        return f"Created job '{job.name}' (id: {job.id})"

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
