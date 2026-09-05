"""NudgeDispatcher — executes `action=nudge` decisions via the spine DeliveryHub.

A plain nudge is posted as a Text to each resolved ``(channel, chat_id)``
target, with ``source.extras._sentinel_origin=True``. It goes through the hub's
non-turn ``post`` (not back through a turn) so the user receives it as a
standalone proactive message — it never re-enters the tool-enabled agent loop,
so the agent cannot "act on" a reminder (e.g. fabricate deliverables or mark a
deadline done). Same delivery model as ``dispatch_options``. Real channel
outlets deliver the content verbatim (they ignore extras); the interactive CLI
outlet reads ``_sentinel_origin`` to prefix a proactive marker.

Callers own both rate limiting (NudgePolicy.check) and target resolution —
the dispatcher is handed an already-resolved target list. After a successful
dispatch, call ``policy.record_fired(...)`` to update state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from raven.proactive_engine.sentinel.types import PendingDecision, PlannerDecision, TaskOption
from raven.spine import ChatType, Source, Text


@dataclass
class ExecutionResult:
    """Outcome of dispatching one PlannerDecision. Returned by every executor."""

    delivered: bool
    reason: str  # short code for logs
    delivery_time: datetime | None = None
    defer_id: str | None = None  # set only by DeferManager for deferred decisions
    details: dict[str, Any] | None = None


def split_session_key(session_key: str) -> tuple[str, str]:
    """Split Planner's ``target_session`` ('channel:chat_id') into components.

    Falls back to sentinel defaults for malformed keys so the dispatcher
    never crashes on a Planner quirk.
    """
    if ":" in session_key:
        channel, chat_id = session_key.split(":", 1)
        return channel, chat_id
    # Planner may emit a bare session key or alias ('cli:direct', 'telegram:home').
    # Default to a synthetic channel when parsing fails.
    return "sentinel", session_key or "direct"


class NudgeDispatcher:
    """Dispatch `action=nudge` decisions to the spine DeliveryHub.

    Kept stateless — injects no time or IDs of its own. All rate-limiting
    state lives in NudgePolicy.

    The hub's ``post`` is late-bound via ``set_post`` (the hub is built inside
    the running loop, after the dispatcher): until then ``dispatch`` warns and
    skips rather than crashing.
    """

    def __init__(
        self,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._post: Callable[[Any], Awaitable[None]] | None = None
        self._now_fn = now_fn or datetime.now

    def set_post(self, post: Callable[[Any], Awaitable[None]]) -> None:
        """Late-bind the spine hub's ``post`` callable."""
        self._post = post

    async def dispatch(
        self,
        decision: PlannerDecision,
        targets: list[tuple[str, str]],
    ) -> ExecutionResult:
        """Post a plain nudge as a Text to each ``(channel, chat_id)`` in
        ``targets`` via the hub. Caller owns policy.check and target resolution
        (see module docstring for why this rides the non-turn ``post``)."""
        if decision.action != "nudge":
            return ExecutionResult(
                delivered=False,
                reason=f"wrong_action:{decision.action}",
            )
        if not decision.nudge_message:
            # Planner should have downgraded to skip already, but defend anyway.
            return ExecutionResult(delivered=False, reason="empty_message")
        if not targets:
            return ExecutionResult(delivered=False, reason="no_targets")
        if self._post is None:
            logger.warning("nudge_dispatch skipped: hub post not wired (set_post)")
            return ExecutionResult(delivered=False, reason="no_post")

        extras = {
            "_sentinel_origin": True,
            "_sentinel_action": "nudge",
            "_sentinel_priority": decision.priority,
            "_sentinel_proactivity_score": decision.proactivity_score,
            "_sentinel_reason": decision.reason,
        }
        delivered: list[str] = []
        for channel, chat_id in targets:
            await self._post(
                Text(
                    content=decision.nudge_message,
                    source=Source(
                        channel=channel,
                        chat_id=chat_id,
                        sender_id="sentinel",
                        chat_type=ChatType.DM,
                        extras=dict(extras),
                    ),
                )
            )
            delivered.append(f"{channel}:{chat_id}")

        now = self._now_fn()
        logger.info(
            "nudge_dispatched action=nudge targets={} priority={} score={:.2f} content={!r}",
            delivered,
            decision.priority,
            decision.proactivity_score,
            decision.nudge_message[:80],
        )

        return ExecutionResult(
            delivered=True,
            reason="ok",
            delivery_time=now,
            details={"targets": delivered, "priority": decision.priority},
        )

    async def dispatch_options(self, decision: PendingDecision) -> ExecutionResult:
        """Render a PendingDecision as a markdown menu and post it to the
        spine DeliveryHub (self._post / hub.post). The user replies with a
        number (or '/pick N' / a
        natural-language phrase); DecisionRouter consumes that reply
        next time around.

        Like ``dispatch``, this is stateless — caller has already
        passed the NudgePolicy gate and is responsible for adapter-side
        side effects (RoutineStore upgrades, FeedbackTracker dispatched
        signal etc.).
        """
        if not decision.options:
            return ExecutionResult(delivered=False, reason="empty_options")
        if self._post is None:
            logger.warning("discovery_menu skipped: hub post not wired (set_post)")
            return ExecutionResult(delivered=False, reason="no_post")

        menu_text = render_menu_markdown(decision)
        # Discovery menus are posted (not turned) so the user sees the raw
        # markdown verbatim (no agent paraphrase). User replies are then
        # caught by DecisionConsumerAdapter in the AgentLoop hook chain —
        # which short-circuits the LLM call when the reply matches a
        # PendingDecision option (agent/loop/main.py:264-265). So skipping
        # the agent here is safe: the pick path doesn't depend on the
        # menu being in agent conversation history.
        msg = Text(
            content=menu_text,
            source=Source(
                channel=decision.channel,
                chat_id=decision.to,
                sender_id="sentinel",
                chat_type=ChatType.DM,
                extras={
                    "_sentinel_origin": True,
                    "_sentinel_action": "discovery_menu",
                    "_sentinel_decision_id": decision.decision_id,
                    "_sentinel_option_count": len(decision.options),
                },
            ),
        )

        now = self._now_fn()
        await self._post(msg)

        logger.info(
            "discovery_menu dispatched decision_id={} channel={} to={} options={}",
            decision.decision_id,
            decision.channel,
            decision.to,
            [o.id for o in decision.options],
        )

        return ExecutionResult(
            delivered=True,
            reason="ok",
            delivery_time=now,
            details={
                "decision_id": decision.decision_id,
                "option_count": len(decision.options),
            },
        )


def render_menu_markdown(decision: PendingDecision) -> str:
    """Encode a PendingDecision as the user-facing markdown menu.

    Format intentionally minimal so any channel renders it cleanly:

        📋 [Today's suggestions]
        1. (new task) Draft reply to X about Y
           — noticed X messaged you yesterday and you haven't replied
        2. (recurring ✓) Review PR before Tuesday standup
           — noticed you've done this the past 3 weeks

        Reply with a number to choose, or reply "skip".
    """
    lines = ["📋 [今日建议]", ""]
    for idx, opt in enumerate(decision.options, start=1):
        marker = _option_type_marker(opt)
        lines.append(f"{idx}. {marker} {opt.title}")
        if opt.why:
            lines.append(f"   — {opt.why}")
    lines.append("")
    lines.append('回复数字选择，或回复 "跳过"。')
    return "\n".join(lines)


def _option_type_marker(opt: TaskOption) -> str:
    if opt.type == "routine_confirm":
        return "(持续模式 ✓)"
    return "(新任务)"


__all__ = [
    "NudgeDispatcher",
    "ExecutionResult",
    "split_session_key",
    "render_menu_markdown",
]
