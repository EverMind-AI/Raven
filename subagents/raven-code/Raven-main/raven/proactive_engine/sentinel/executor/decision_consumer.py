"""DecisionConsumer — orchestrates DecisionRouter + ActionExecutor +
state mutations into one ``async (req) -> MenuReply | None`` hook
that AgentLoop can call before its normal processing path.

Returns:
- ``None``     — the message did not consume a pending menu;
                 AgentLoop should process it as normal user input.
- ``MenuReply`` — the message DID consume a menu (pick or skip);
                 AgentLoop should short-circuit and emit this reply.

Side-effects on a successful pick (in order):
  1. ``PendingDecisionStore.mark_consumed(decision_id, picked_option_id)``
  2. ``NudgeFeedbackTracker.record_accepted(decision_id)`` (or
     ``record_dismissed`` for explicit skip)
  3. ``ActionExecutor.execute(option, decision)``  (only on pick)

``require_confirm=True`` (default) routes pick → confirm prompt →
yes/no → execute. The consumer parks the decision in
``awaiting_confirm`` state on first pick, asks the user "execute X?
yes / no", and only runs ActionExecutor when the second-leg reply
confirms. Cancel paths skip execution entirely.

``require_confirm=False`` shortcuts to immediate execution on the
first pick — fine for low-stakes deployments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from loguru import logger

from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.types import (
    ActionExecutionResult,
    PendingDecision,
    RouteResult,
    TaskOption,
)
from raven.spine.turn import TurnRequest

if TYPE_CHECKING:
    from raven.proactive_engine.sentinel.executor.action_executor import ActionExecutor
    from raven.proactive_engine.sentinel.executor.decision_router import DecisionRouter
    from raven.proactive_engine.sentinel.feedback.tracker import NudgeFeedbackTracker


@dataclass
class MenuReply:
    """A consumed-menu reply emitted by DecisionConsumer. AgentLoop's
    DecisionConsumerAdapter reads ``content`` / ``media`` to short-circuit
    the turn."""

    channel: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)


class DecisionConsumer:
    """Hook callable for AgentLoop: takes one inbound message, decides
    whether it consumes a pending menu, and returns the response."""

    def __init__(
        self,
        *,
        router: "DecisionRouter",
        executor: "ActionExecutor",
        pending_store: "PendingDecisionStore",
        feedback: "NudgeFeedbackTracker | None" = None,
        require_confirm: bool = True,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.router = router
        self.executor = executor
        self.pending_store = pending_store
        self.feedback = feedback
        self.require_confirm = require_confirm
        self._now_fn = now_fn or datetime.now

    async def __call__(self, req: TurnRequest) -> MenuReply | None:
        """Entry point. Defensive wrapper so AgentLoop never crashes
        on a consumer failure."""
        try:
            return await self._consume(req)
        except Exception as exc:
            logger.exception("DecisionConsumer crashed: {}", exc)
            return None

    # ------------------------------------------------------------------
    # Internals

    async def _consume(self, req: TurnRequest) -> MenuReply | None:
        # Nudge-injected user prompts (an exec_kind=reply payload we ourselves
        # injected) — don't recursively consume them as menu replies. A menu pick
        # is origin=USER, so the user-inbound chain runs and this typed guard is
        # the live check.
        if req.sentinel is not None and req.sentinel.action_origin:
            return None

        result = await self.router.maybe_consume(
            channel=req.source.channel,
            to=req.source.chat_id,
            content=req.text,
        )
        if not result.consumed:
            return None

        now_ms = int(self._now_fn().timestamp() * 1000)
        # We need the full PendingDecision to pass into ActionExecutor;
        # router already verified it exists, but re-fetch by id to be
        # safe (the store may have moved it).
        decision = self._lookup_decision(result.pending_decision_id, now_ms)
        if decision is None:
            logger.warning(
                "DecisionConsumer: router consumed but pending decision {} is no longer live; treating as no-op",
                result.pending_decision_id,
            )
            return None

        # ── Confirm-mode second leg ────────────────────────────────
        # Router only sets confirm_intent when pending was awaiting_confirm.
        if result.confirm_intent == "cancel":
            return await self._handle_cancel(decision, req, now_ms)
        if result.confirm_intent == "confirm":
            if result.option is None:
                logger.warning(
                    "DecisionConsumer: confirm result missing option for decision {}; cannot execute (mark cancelled)",
                    decision.decision_id,
                )
                return await self._handle_cancel(decision, req, now_ms)
            return await self._handle_confirmed_execute(
                decision=decision,
                option=result.option,
                inbound=req,
                route_result=result,
                now_ms=now_ms,
            )

        # ── First-leg flows (no awaiting_confirm) ──────────────────

        # Skip path — user said "no" / "skip" / etc.
        if result.option is None:
            return await self._handle_skip(decision, req, now_ms)

        # Pick path — user chose option N.
        if self.require_confirm:
            return await self._handle_pick_with_confirm(
                decision=decision,
                option=result.option,
                inbound=req,
                now_ms=now_ms,
            )
        return await self._handle_pick(
            decision=decision,
            option=result.option,
            inbound=req,
            route_result=result,
            now_ms=now_ms,
        )

    def _lookup_decision(self, decision_id: str | None, now_ms: int) -> PendingDecision | None:
        if decision_id is None:
            return None
        # Cheap search across active decisions; the store only holds a
        # handful at a time so a linear scan is fine.
        for d in self.pending_store.all_active(now_ms=now_ms):
            if d.decision_id == decision_id:
                return d
        return None

    async def _handle_skip(
        self,
        decision: PendingDecision,
        msg: TurnRequest,
        now_ms: int,
    ) -> MenuReply:
        # require_pending=True guards against a race: between the
        # router's read (no awaiting_confirm) and our write here, a
        # concurrent process could arm awaiting_confirm. Without the
        # guard, mark_consumed would clobber the user's earlier pick.
        ok = self.pending_store.mark_consumed(
            decision.decision_id,
            picked_option_id=None,
            consumed_at_ms=now_ms,
            require_pending=True,
        )
        if not ok:
            logger.info(
                "DecisionConsumer: skip no-op for {} (state shifted to awaiting_confirm or consumed)",
                decision.decision_id,
            )
            return MenuReply(
                channel=msg.source.channel,
                chat_id=msg.source.chat_id,
                content="这个 decision 已经处理过了。",
            )
        if self.feedback is not None:
            try:
                self.feedback.record_dismissed(
                    decision.decision_id,
                    reason="user_skip_via_menu",
                )
            except Exception as exc:
                logger.warning("FeedbackTracker.record_dismissed: {}", exc)
        return MenuReply(
            channel=msg.source.channel,
            chat_id=msg.source.chat_id,
            content="好的，已跳过今天的建议。",
        )

    async def _handle_pick(
        self,
        *,
        decision: PendingDecision,
        option: TaskOption,
        inbound: TurnRequest,
        route_result: RouteResult,
        now_ms: int,
    ) -> MenuReply:
        self.pending_store.mark_consumed(
            decision.decision_id,
            picked_option_id=option.id,
            consumed_at_ms=now_ms,
        )
        if self.feedback is not None:
            try:
                self.feedback.record_accepted(
                    decision.decision_id,
                    context=(f"option={option.id} kind={option.exec_kind} method={route_result.raw_match_method}"),
                )
            except Exception as exc:
                logger.warning("FeedbackTracker.record_accepted: {}", exc)

        result = await self.executor.execute(
            option,
            decision=decision,
            channel=inbound.source.channel,
            to=inbound.source.chat_id,
        )

        return MenuReply(
            channel=inbound.source.channel,
            chat_id=inbound.source.chat_id,
            content=_render_user_facing(result, option),
        )

    # ------------------------------------------------------------------
    # require_confirm=True paths

    async def _handle_pick_with_confirm(
        self,
        *,
        decision: PendingDecision,
        option: TaskOption,
        inbound: TurnRequest,
        now_ms: int,
    ) -> MenuReply:
        """First leg: park the decision in AWAITING_CONFIRM and ask the
        user to confirm. No execution and no feedback signal yet —
        we'll record_accepted only after the user actually confirms."""
        outcome = self.pending_store.mark_awaiting_confirm(
            decision.decision_id,
            picked_option_id=option.id,
            picked_at_ms=now_ms,
        )
        if outcome == PendingDecisionStore.AWAIT_OK:
            pass  # fall through to send confirm prompt
        elif outcome == PendingDecisionStore.AWAIT_NOT_FOUND:
            # Decision vanished between routing and confirm — extremely
            # unlikely under fcntl. Treat as "menu expired".
            logger.warning(
                "DecisionConsumer: decision {} not found at confirm time",
                decision.decision_id,
            )
            return None
        elif outcome == PendingDecisionStore.AWAIT_CONSUMED:
            # Already consumed by another path — don't re-execute.
            logger.info(
                "DecisionConsumer: decision {} already consumed; skipping confirm/execute",
                decision.decision_id,
            )
            return None
        elif outcome == PendingDecisionStore.AWAIT_ALREADY:
            # Another leg already armed awaiting_confirm; the user's
            # second-leg yes/no should land on that one. Idempotent —
            # treat as success and re-send the confirm prompt so the
            # user sees the same question.
            logger.info(
                "DecisionConsumer: decision {} already in awaiting_confirm (idempotent re-prompt)",
                decision.decision_id,
            )
            # fall through to send confirm prompt
        return MenuReply(
            channel=inbound.source.channel,
            chat_id=inbound.source.chat_id,
            content=(f"要执行：{option.title}？\n  · 回复 yes / 确认 → 执行\n  · 回复 no / 取消 → 跳过"),
        )

    async def _handle_confirmed_execute(
        self,
        *,
        decision: PendingDecision,
        option: TaskOption,
        inbound: TurnRequest,
        route_result: RouteResult,
        now_ms: int,
    ) -> MenuReply:
        """Second leg: user said yes. Mark fully consumed, record
        accepted, run executor, render user-facing reply."""
        self.pending_store.mark_consumed(
            decision.decision_id,
            picked_option_id=option.id,
            consumed_at_ms=now_ms,
        )
        if self.feedback is not None:
            try:
                self.feedback.record_accepted(
                    decision.decision_id,
                    context=(
                        f"option={option.id} kind={option.exec_kind} confirmed_via={route_result.raw_match_method}"
                    ),
                )
            except Exception as exc:
                logger.warning("FeedbackTracker.record_accepted: {}", exc)

        result = await self.executor.execute(
            option,
            decision=decision,
            channel=inbound.source.channel,
            to=inbound.source.chat_id,
        )
        return MenuReply(
            channel=inbound.source.channel,
            chat_id=inbound.source.chat_id,
            content=_render_user_facing(result, option),
        )

    async def _handle_cancel(
        self,
        decision: PendingDecision,
        msg: TurnRequest,
        now_ms: int,
    ) -> MenuReply:
        """Second leg: user said no. Mark cancelled (consumed with
        picked=None) and record dismissal.

        ``cancel_confirm`` only succeeds if the decision is still in
        awaiting_confirm (not consumed, not vanished). If the state
        already moved (e.g. concurrent confirm path or another process
        cancelled), we tell the user "already handled" rather than the
        misleading "cancelled" — silent no-op replies are worse than
        explicit ones."""
        cancelled = self.pending_store.cancel_confirm(
            decision.decision_id,
            cancelled_at_ms=now_ms,
        )
        if not cancelled:
            logger.info(
                "DecisionConsumer: cancel_confirm no-op for {} (state already shifted)",
                decision.decision_id,
            )
            return MenuReply(
                channel=msg.source.channel,
                chat_id=msg.source.chat_id,
                content="这个 decision 已经处理过了，无需取消。",
            )
        if self.feedback is not None:
            try:
                self.feedback.record_dismissed(
                    decision.decision_id,
                    reason="user_cancelled_after_pick",
                )
            except Exception as exc:
                logger.warning("FeedbackTracker.record_dismissed: {}", exc)
        return MenuReply(
            channel=msg.source.channel,
            chat_id=msg.source.chat_id,
            content="好的，已取消。",
        )


def _render_user_facing(result: ActionExecutionResult, option: TaskOption) -> str:
    """Compose a short user-facing message from the execution result."""
    if result.status == "ok":
        primary = result.output_text or f"已为您处理：{option.title}"
        if result.side_effects:
            # Keep side-effect log compact; user mostly cares about the
            # primary line. Show side effects only if explicit (one
            # short line, prefixed).
            secondary = "\n".join(f"  · {se}" for se in result.side_effects)
            return f"{primary}\n{secondary}" if len(secondary) < 240 else primary
        return primary
    if result.status == "deferred":
        return result.output_text or "已延后执行。"
    # error / unknown
    return f"无法执行：{option.title}\n原因：{result.error or '未知错误'}"


__all__ = ["DecisionConsumer"]
