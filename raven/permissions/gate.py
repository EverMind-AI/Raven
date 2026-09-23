"""The permission gate: one decision waterfall over every tool call.

``check`` is the pure decision -- builtin rulings, user rules, then the mode's
reading of the ask tier -- and ``enforce`` is its turn-side half: the per-turn
dedup, the approval round-trip through whatever responder the entrance bound,
and the mapping onto a ``ToolResult`` the registry can answer the call with.
Every refusal continues the turn (``Continuation.CONTINUE``); the one path that
ends it is a human choosing "deny and stop" in the approval prompt.

Two grants outlast the click. "For this session" remembers the action's keys
on the conversation (``raven.permissions.session``), and a later call whose
every still-asking part was granted runs without a prompt. "Don't ask again"
writes the prefix rule the human confirmed into ``permissions.tools.exec``,
after the same validation the prompt's suggestion went through; the gate reads
config live, so the rule holds from the next call, and no session grant is
kept beside it -- taking the rule back means being asked again. A pattern that
fails validation still grants this once and this session -- the human did say
yes -- and the reason it was not written is logged.

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
from raven.config.update import allow_exec_pattern
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
from raven.contracts.tool import PARSE_RETRY_INSTRUCTION, STOP_RETRY_INSTRUCTION, Continuation, Tool, ToolResult
from raven.permissions.builtin import BuiltinRulings, action_digest, action_line, session_keys
from raven.permissions.judge import review
from raven.permissions.rules import default_tier, exec_approval_shape, user_tier, validate_exec_pattern
from raven.permissions.session import remember_allowed, session_allows, session_mode
from raven.permissions.turn import current_tool_call_id, current_turn, note_refusal
from raven.tracing import trace

#: What one evidence value may carry to a prompt. A person reads a prompt; past
#: a few screens nothing more is read, and the frame is built, serialised and
#: drawn on the event loop. The filesystem tools cap what they read off disk,
#: but a brand-new file's whole text, an MCP call's arguments and the fallback's
#: raw params reach here uncapped, and an approval now waits a day rather than
#: half a minute -- so the ceiling belongs where every tool's account passes,
#: not in each tool that remembers to have one.
_EVIDENCE_MAX_CHARS = 16 * 1024


def _clamped(evidence: dict[str, Any]) -> dict[str, Any]:
    """Cut every oversized string in one tool's account down to what is read.

    Marks what it cut rather than dropping it silently: a prompt showing half a
    diff has to say so, or the person answers about a change they cannot see.
    """
    out: dict[str, Any] = {}
    for key, value in evidence.items():
        if isinstance(value, str) and len(value) > _EVIDENCE_MAX_CHARS:
            out[key] = value[:_EVIDENCE_MAX_CHARS]
            out["truncated"] = True
        elif isinstance(value, dict | list | tuple):
            text = repr(value)
            if len(text) > _EVIDENCE_MAX_CHARS:
                out[key] = text[:_EVIDENCE_MAX_CHARS]
                out["truncated"] = True
            else:
                out[key] = value
        else:
            out[key] = value
    return out


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
            tier = default_tier(tool_name, params)
            if tier is Tier.ALLOW:
                return Allow(source=DecisionSource.DEFAULT)
        if mode is PermissionMode.FULL:
            return Allow(source=DecisionSource.MODE)
        suggested_pattern = ""
        ask_segments: tuple[str, ...] = ()
        if tool_name == "exec" and isinstance(params.get("command"), str):
            exec_rules = cfg.tools.get("exec")
            shape = exec_approval_shape(params["command"], exec_rules if isinstance(exec_rules, dict) else {})
            ask_segments, suggested_pattern = shape.ask_segments, shape.suggested_pattern
        keys = session_keys(tool_name, params, ask_segments)
        if session_allows(current_turn().conversation_id, keys):
            return Allow(source=DecisionSource.SESSION)
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
                    session_keys=keys,
                    suggested_pattern=suggested_pattern,
                )
        return NeedsApproval(
            reason=described.reason if described else "This call requires user approval (ask tier)",
            description=description,
            digest=digest,
            family=described.family if described else "",
            session_keys=keys,
            suggested_pattern=suggested_pattern,
        )

    async def enforce(self, tool_name: str, params: dict[str, Any], tool: Tool | None = None) -> ToolResult | None:
        """None waves the call through; a ``ToolResult`` replaces it.

        ``tool`` is the registered object behind ``tool_name``, consulted only
        for what a prompt should show (``Tool.approval_kind`` and
        ``approval_evidence``); the decision never reads it.
        """
        turn = current_turn()
        digest = action_digest(tool_name, params)
        if digest in turn.lapsed_digests:
            self._annotate({"permission.decision": "deny", "permission.source": "lapsed_earlier"})
            return self._refuse(
                tool_name,
                params,
                "This action was already sent for approval in this turn and the request expired "
                "with no answer. Asking again would expire the same way. Tell the user the "
                "approval lapsed and let them decide.",
                source="lapsed_earlier",
            )
        if digest in turn.denied_digests:
            self._annotate({"permission.decision": "deny", "permission.source": "denied_earlier"})
            return self._refuse(
                tool_name,
                params,
                "User denied this action earlier in the current turn",
                source="denied_earlier",
            )
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
            return self._refuse(tool_name, params, decision.reason, source=decision.source.value)
        if not self._allow_ask or turn.responder is None or not turn.conversation_id:
            self._annotate({"permission.decision": "deny", "permission.source": DecisionSource.UNATTENDED.value})
            return self._refuse(
                tool_name,
                params,
                f"{decision.reason}, but this turn is not interactive",
                source=DecisionSource.UNATTENDED.value,
            )
        kind, evidence = self._prompt_view(tool, params)
        try:
            outcome = await turn.responder.await_approval(
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                tool_call_id=current_tool_call_id(),
                command=action_line(tool_name, params),
                description=decision.description,
                suggested_pattern=decision.suggested_pattern,
                kind=kind,
                family=decision.family,
                origin=turn.origin,
                origin_name=turn.origin_name,
                evidence=evidence,
            )
        except Exception as exc:  # noqa: BLE001 - a broken transport must refuse, not execute
            logger.exception("permissions: approval transport failed for {}", tool_name)
            self._annotate({"permission.decision": "deny", "permission.source": "approval_transport_error"})
            return self._refuse(
                tool_name,
                params,
                f"The approval request could not be delivered ({exc})",
                source="approval_transport_error",
            )
        self._annotate(
            {
                "permission.decision": "allow" if outcome.approved else "deny",
                "permission.source": DecisionSource.APPROVAL.value,
                "permission.approval.choice": outcome.choice.value,
                "permission.approval.answered": outcome.answered,
            }
        )
        if outcome.approved:
            # A rule that reached the config file is read live from the next
            # call on, so it needs no session grant beside it -- and taking the
            # rule back then really does mean being asked again. The session
            # grant stays as the fallback whenever the rule does not carry this
            # call on its own: a pattern that could not be written, or one the
            # user's own stricter rule outranks (``git *: ask`` over the
            # ``git push *`` just saved -- the strictest matching rule wins).
            on_disk = added = False
            if outcome.choice is ApprovalChoice.ALLOW_ALWAYS:
                on_disk, added = self._persist(tool_name, outcome.pattern)
                # What the write actually did, back to whoever asked, so a later
                # undo is about this rule rather than about any rule wearing the
                # same text. The answer reached the client before this line ran,
                # so the transport is holding an undo open on it; a transport
                # with no undo (the ACP wire) has no such method and is skipped.
                report = getattr(turn.responder, "record_grant", None)
                if callable(report) and outcome.approval_id:
                    report(outcome.approval_id, outcome.pattern.strip(), added)
            covered = on_disk and user_tier(tool_name, params, self._config_source().tools) is Tier.ALLOW
            if outcome.choice is ApprovalChoice.ALLOW_SESSION or (
                outcome.choice is ApprovalChoice.ALLOW_ALWAYS and not covered
            ):
                remember_allowed(turn.conversation_id, decision.session_keys)
            return None
        turn.denied_digests.add(digest)
        if not outcome.answered:
            turn.lapsed_digests.add(digest)
        feedback = f' The user said: "{outcome.feedback}"' if outcome.feedback else ""
        if outcome.choice is ApprovalChoice.DENY_STOP:
            return self._refuse(
                tool_name,
                params,
                "User denied this action and asked to stop here." + feedback,
                source="approval_deny_stop",
                continuation=Continuation.ABORT_TURN,
            )
        if not outcome.answered:
            # Not a refusal. The request went out and nobody answered it -- the
            # connection went, the turn was torn down, a host's ceiling fired --
            # and the two shared one sentence: a model told it had been denied
            # stops asking and goes around, which is how a run whose approvals had
            # merely lapsed reported a system error and delivered something else
            # instead. Said plainly, the next move is to tell the reader, not to
            # find another way.
            return self._refuse(
                tool_name,
                params,
                "This action needed the user's approval, and the request expired with no "
                "answer. Nobody refused it. Tell the user the approval lapsed and ask whether to "
                "retry; do not repeat this call in this turn, and do not look for another way "
                "around it.",
                source="approval_lapsed",
            )
        return self._refuse(
            tool_name,
            params,
            "User denied this action." + feedback,
            source="approval_denied",
        )

    def _refuse(
        self,
        tool_name: str,
        params: dict[str, Any],
        reason: str,
        *,
        source: str = "",
        continuation: Continuation = Continuation.CONTINUE,
    ) -> ToolResult:
        """Answer the call with a refusal, and leave the turn a record of it.

        Every refusal goes through here so the record cannot miss a path: the
        one-shot ``-m`` surface has no human watching the run, and its summary
        is the only place a caller learns that the mutations it asked for were
        turned down.
        """
        note_refusal(tool_name, action_line(tool_name, params), reason, source)
        return self._refusal(f"Error: {reason}", continuation=continuation)

    def _persist(self, tool_name: str, pattern: str) -> tuple[bool, bool]:
        """Write the confirmed prefix as an allow rule; the grant already stands.

        Two answers, and they are not the same question: whether the rule is on
        disk (which decides if a session grant is still needed), and whether
        THIS call is the reason it is there (which decides whether an undo may
        take it away -- a rule the person already had is not this prompt's).
        """
        pattern = pattern.strip()
        why = "only exec patterns can be persisted" if tool_name != "exec" else validate_exec_pattern(pattern)
        if why is not None:
            logger.warning("permissions: not persisting {!r}: {}", pattern, why)
            self._annotate({"permission.persisted": False, "permission.persist.refused": why})
            return False, False
        try:
            added = allow_exec_pattern(pattern)
        except ValueError as exc:
            logger.warning("permissions: not persisting {!r}: {}", pattern, exc)
            self._annotate({"permission.persisted": False, "permission.persist.refused": str(exc)})
            return False, False
        except Exception:  # noqa: BLE001 - the config file is the user's; a failed write is theirs to hear about
            logger.exception("permissions: could not write allow rule {!r}", pattern)
            self._annotate({"permission.persisted": False})
            return False, False
        logger.info("permissions: {} exec allow rule {!r}", "added" if added else "kept", pattern)
        self._annotate(
            {"permission.persisted": True, "permission.persist.added": added, "permission.persist.pattern": pattern}
        )
        return True, added

    @staticmethod
    def _prompt_view(tool: Tool | None, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """What the prompt shows: the tool's own account of the call, or its arguments.

        A failing evidence hook falls back to the arguments rather than refusing
        or running the call -- it only decides what a person sees, never
        whether they are asked."""
        kind = str(getattr(tool, "approval_kind", "") or "") or "unknown"
        evidence: Any = None
        if tool is not None:
            try:
                evidence = tool.approval_evidence(params)
            except Exception:  # noqa: BLE001 - display only; the decision is made regardless
                logger.debug("permissions: approval evidence failed for {}", kind, exc_info=True)
        if not isinstance(evidence, dict):
            # The arguments are the evidence now, and a layout keyed to the
            # kind would draw them as blanks -- a file write with no path -- so
            # the kind follows them.
            return "unknown", _clamped({"input": params})
        return kind, _clamped(evidence)

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
