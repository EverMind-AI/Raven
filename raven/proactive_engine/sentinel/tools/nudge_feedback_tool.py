"""``nudge_feedback`` tool: lets the main LLM classify the user's
intent toward a recent Sentinel-dispatched proactive nudge.

Why this exists: a regex-only path recording any non-``/dismiss``
reply as ACCEPTED would silently inflate the topic's acceptance_rate
on natural-language dismissals like "stop reminding me" / "so annoying" — the
adaptive-quota multiplier would then push *more* nudges, not fewer.
This tool defers the call to the main LLM's semantic judgement of the
same user message at zero extra LLM cost (the LLM runs anyway).

The tool is always registered. ``SentinelRunner.consume_feedback_via_tool``
gracefully reports ``recorded=False, reason=no_awaiting_nudge`` when
the LLM calls it spuriously — so over-eager calls are harmless.

The current session is published into the
``sentinel_current_session_key`` contextvar by ``on_user_inbound``;
asyncio context propagation carries it through to ``execute()``.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.agent.tools.base import Tool
from raven.proactive_engine.sentinel.executor.runner import (
    SentinelRunner,
    current_session_key,
)


_SENTIMENTS = ("accepted", "dismissed", "snoozed", "irrelevant")


class NudgeFeedbackTool(Tool):
    """Surface a structured sentiment label on the user's reply to a
    recent proactive nudge."""

    def __init__(self, runner: SentinelRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "nudge_feedback"

    @property
    def description(self) -> str:
        return (
            "Call this ONLY when the user's most recent reply is reacting "
            "to a proactive notification/reminder/nudge that the assistant "
            "(not the user) initiated earlier. Classify the user's "
            "intent toward that nudge: "
            "'accepted' = positive engagement (acknowledgment, thanks, "
            "follow-up question, took the suggested action); "
            "'dismissed' = negative engagement (e.g. '不要提醒了', "
            "'stop reminding me', 'leave me alone', 'don't bother'); "
            "'snoozed' = postpone (e.g. 'later', '稍后', 'not now', "
            "'remind me tomorrow'); "
            "'irrelevant' = the reply is unrelated to the nudge (user "
            "switched topic or asked something else). "
            "Do NOT call this for ordinary user-initiated messages — "
            "only when the prior turn carried a proactive nudge from the "
            "assistant. When in doubt, do not call. Calling with no "
            "pending nudge is a no-op."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sentiment": {
                    "type": "string",
                    "enum": list(_SENTIMENTS),
                    "description": (
                        "User intent toward the recent proactive nudge."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Optional brief justification (≤120 chars). E.g. "
                        "the dismissive phrase the user used, or the "
                        "topic they switched to."
                    ),
                },
            },
            "required": ["sentiment"],
        }

    async def execute(
        self,
        sentiment: str,
        reason: str | None = None,
        **kwargs: Any,
    ) -> str:
        session_key = current_session_key.get()
        if not session_key:
            # No active session context — the runner can't locate a
            # pending nudge without it. Report cleanly so the LLM
            # doesn't loop trying again.
            return "no_session_context: nudge_feedback recorded nothing"
        try:
            outcome = self._runner.consume_feedback_via_tool(
                session_key,
                sentiment=sentiment,
                reason=(reason or None) and reason[:120],
            )
        except Exception as exc:  # noqa: BLE001 — tool must not raise
            logger.warning(
                "nudge_feedback consume_feedback_via_tool failed: {}: {}",
                type(exc).__name__, exc,
            )
            return f"error: {type(exc).__name__}"
        if not outcome.get("recorded"):
            return f"no_pending_nudge: {outcome.get('reason', '')}"
        return (
            f"recorded {outcome.get('signal')} for nudge "
            f"{outcome.get('nudge_id')}"
        )


__all__ = ["NudgeFeedbackTool"]
