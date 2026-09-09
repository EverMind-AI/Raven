"""The permission gate: one decision waterfall over every tool call.

``check`` is the pure decision -- builtin rulings, user rules, then the mode's
reading of the ask tier -- and ``enforce`` is its turn-side half: the per-turn
dedup, the approval round-trip through whatever responder the entrance bound,
and the mapping onto a ``ToolResult`` the registry can answer the call with.
Every refusal continues the turn (``Continuation.CONTINUE``); the one path that
ends it is a human choosing "deny and stop" in the approval prompt.

``allow_ask`` is fixed per gate, not read from the turn: a sub-agent's task
inherits the parent turn's context by asyncio's own rule, so a gate built for
an unattended registry must refuse to ask even when a responder is visible in
its context.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from raven.config.schema import PermissionsConfig
from raven.contracts.permissions import (
    Allow,
    ApprovalChoice,
    Decision,
    DecisionSource,
    Deny,
    NeedsApproval,
    PermissionMode,
    Tier,
)
from raven.contracts.tool import PARSE_RETRY_INSTRUCTION, STOP_RETRY_INSTRUCTION, Continuation, ToolResult
from raven.permissions.builtin import BuiltinRulings, action_digest, action_line
from raven.permissions.judge import review
from raven.permissions.rules import default_tier, user_tier
from raven.permissions.session import session_mode
from raven.permissions.turn import current_tool_call_id, current_turn
from raven.tracing import trace


class PermissionGate:
    """Decide about one tool call before dispatch."""

    def __init__(
        self,
        *,
        config_source: Callable[[], PermissionsConfig],
        builtin: BuiltinRulings,
        judge_provider_for: Callable[[], Any] | None = None,
        allow_ask: bool = True,
    ) -> None:
        self._config_source = config_source
        self._builtin = builtin
        self._judge_provider_for = judge_provider_for
        self._allow_ask = allow_ask

    async def check(self, tool_name: str, params: dict[str, Any]) -> Decision:
        """The waterfall: builtin deny, user deny, user
        allow, then the mode's reading of the ask tier."""
        cfg = self._config_source()
        try:
            mode = PermissionMode(session_mode(current_turn().conversation_id) or cfg.mode)
        except ValueError:
            mode = PermissionMode.ASK
        ruling = self._builtin.ruling(tool_name, params)
        if isinstance(ruling, Deny):
            return ruling
        # A family the surface declared does not decide; it describes. The call
        # answers to the tiers like any other mutation, and when it lands on a
        # prompt the family's line is what the human reads. Recorded before any
        # tier decides so the audit keeps the classification whichever way it goes.
        described = ruling if isinstance(ruling, NeedsApproval) else None
        if described is not None:
            self._annotate({"permission.family": described.family})
        if user_tier(tool_name, params, cfg.tools) is Tier.DENY:
            return Deny(
                reason="This call is blocked by a deny rule in your permissions config",
                source=DecisionSource.USER_DENY,
            )
        tier = user_tier(tool_name, params, cfg.tools)
        if tier is Tier.ALLOW:
            return Allow(source=DecisionSource.USER_ALLOW)
        if tier is None:
            tier = default_tier(tool_name)
            if tier is Tier.ALLOW:
                return Allow(source=DecisionSource.DEFAULT)
        if mode is PermissionMode.FULL:
            return Allow(source=DecisionSource.MODE)
        # Deliberately NOT auto-allowing a sandboxed exec here: the Boxlite VM
        # mounts the real workspace at /workspace read-write (plus any
        # configured rw volumes), so "the sandbox holds it" is false for host
        # data -- rm -rf /workspace deletes real files. Auto-allow can return
        # only for an execution setup with no writable host mounts, which no
        # shipped executor provides today.
        digest = action_digest(tool_name, params)
        description = described.description if described else f"Approve this action: {action_line(tool_name, params)}"
        if mode is PermissionMode.SMART and self._judge_provider_for is not None:
            provider = self._judge_provider_for()
            if provider is not None:
                await self._notify_review("started", tool_name)
                try:
                    outcome = await review(
                        provider,
                        tool_name=tool_name,
                        params=params,
                        model=cfg.judge_model or None,
                        timeout_s=cfg.judge_timeout_seconds,
                    )
                finally:
                    await self._notify_review("ended", tool_name)
                self._annotate(
                    {
                        "permission.judge.decision": "allow" if outcome.allow else "escalate",
                        "permission.judge.reason": outcome.reason,
                        "permission.judge.failed": outcome.failed,
                    }
                )
                if outcome.allow:
                    return Allow(source=DecisionSource.JUDGE)
                return NeedsApproval(
                    reason=f"This call requires user approval ({outcome.reason or 'the reviewer escalated it'})",
                    description=description,
                    digest=digest,
                    family=described.family if described else "",
                )
        return NeedsApproval(
            reason=described.reason if described else "This call requires user approval (ask tier)",
            description=description,
            digest=digest,
            family=described.family if described else "",
        )

    async def enforce(self, tool_name: str, params: dict[str, Any]) -> ToolResult | None:
        """None waves the call through; a ``ToolResult`` replaces it."""
        turn = current_turn()
        digest = action_digest(tool_name, params)
        if digest in turn.lapsed_digests:
            self._annotate({"permission.decision": "deny", "permission.source": "lapsed_earlier"})
            return self._refusal(
                "Error: This action was already sent for approval in this turn and the request "
                "expired with no answer. Asking again would expire the same way. Tell the user the "
                "approval lapsed and let them decide."
            )
        if digest in turn.denied_digests:
            self._annotate({"permission.decision": "deny", "permission.source": "denied_earlier"})
            return self._refusal("Error: User denied this action earlier in the current turn")
        decision = await self.check(tool_name, params)
        if isinstance(decision, Allow):
            self._annotate({"permission.decision": "allow", "permission.source": decision.source.value})
            return None
        if isinstance(decision, Deny):
            self._annotate({"permission.decision": "deny", "permission.source": decision.source.value})
            if decision.source is DecisionSource.BUILTIN_PARSE_ERROR:
                # The model's own to fix, so it keeps the call: blocking its
                # siblings and telling it not to try another way is what ended
                # a job one step before delivery.
                return ToolResult(
                    model_text=f"Error: {decision.reason}{PARSE_RETRY_INSTRUCTION}",
                    retryable=True,
                    blocks_call=False,
                    continuation=Continuation.CONTINUE,
                    ok=False,
                )
            return self._refusal(f"Error: {decision.reason}")
        if not self._allow_ask or turn.responder is None or not turn.conversation_id:
            self._annotate({"permission.decision": "deny", "permission.source": DecisionSource.UNATTENDED.value})
            return self._refusal(f"Error: {decision.reason}, but this turn is not interactive")
        try:
            outcome = await turn.responder.await_approval(
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                tool_call_id=current_tool_call_id(),
                command=action_line(tool_name, params),
                description=decision.description,
            )
        except Exception as exc:  # noqa: BLE001 - a broken transport must refuse, not execute
            logger.exception("permissions: approval transport failed for {}", tool_name)
            self._annotate({"permission.decision": "deny", "permission.source": "approval_transport_error"})
            return self._refusal(f"Error: The approval request could not be delivered ({exc})")
        self._annotate(
            {
                "permission.decision": "allow" if outcome.approved else "deny",
                "permission.source": DecisionSource.APPROVAL.value,
                "permission.approval.choice": outcome.choice.value,
                "permission.approval.answered": outcome.answered,
            }
        )
        if outcome.choice is ApprovalChoice.ALLOW:
            return None
        turn.denied_digests.add(digest)
        if not outcome.answered:
            turn.lapsed_digests.add(digest)
        feedback = f' The user said: "{outcome.feedback}"' if outcome.feedback else ""
        if outcome.choice is ApprovalChoice.DENY_STOP:
            return self._refusal(
                "Error: User denied this action and asked to stop here." + feedback,
                continuation=Continuation.ABORT_TURN,
            )
        if not outcome.answered:
            # Not a refusal. The request went out and the deadline passed with
            # nobody having answered it, and the two shared one sentence: a model
            # told it had been denied stops asking and goes around, which is how a
            # run whose approvals had merely lapsed reported a system error and
            # delivered something else instead. Said plainly, the next move is to
            # tell the reader, not to find another way.
            return self._refusal(
                "Error: This action needed the user's approval, and the request expired with no "
                "answer. Nobody refused it. Tell the user the approval lapsed and ask whether to "
                "retry; do not repeat this call in this turn, and do not look for another way "
                "around it."
            )
        return self._refusal("Error: User denied this action." + feedback)

    @staticmethod
    def _annotate(attributes: dict) -> None:
        """Merge decision attributes onto the innermost open tool.call span.

        Direct span annotation rather than a hand-off slot: the registry's
        instrumentation keeps a properly nested current-span (a token per
        frame), so a forwarding tool's nested dispatch annotates its own span
        and the outer call keeps its own record. ``Span.set`` merges, so the
        reviewer's verdict and the final decision coexist. Best-effort
        telemetry: outside a traced turn there is no span and nothing to do.
        """
        span = trace.current_span()
        if span is not None:
            span.set(attributes)

    @staticmethod
    async def _notify_review(phase: str, tool_name: str) -> None:
        """Tell a watching surface the reviewer is running; never load-bearing."""
        hook = current_turn().on_review
        if hook is None:
            return
        try:
            await hook(phase, tool_name)
        except Exception:  # noqa: BLE001 - a display failure must not change a decision
            logger.debug("permissions: review notification failed", exc_info=True)

    @staticmethod
    def _refusal(message: str, *, continuation: Continuation = Continuation.CONTINUE) -> ToolResult:
        return ToolResult(
            model_text=message + STOP_RETRY_INSTRUCTION,
            retryable=False,
            blocks_call=True,
            continuation=continuation,
            ok=False,
        )


__all__ = ["PermissionGate"]
