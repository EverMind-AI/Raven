"""Plain-first reply (DR flow, product experiment): answer before searching, or ask to.

On the first model call of a plain turn the web tools are withheld and one tool is
added in their place, ``request_research``. A plain turn is a session's first research
turn, or a later turn the conversation gate (``PlainTurnGate``) classed as a new topic
that is settled general knowledge - a third verdict beside research and
answer-from-context, emitted only when this feature is on. The model is told why: answer
now if the question is settled general knowledge, or call the tool. Three things can then
happen to that first response.

* It calls ``request_research`` (or, for models that ignore the tool, writes the research
  marker), or calls something other than the web (a clarify, a file read): the turn goes
  on as an ordinary research turn - the request is popped and a note says the tools are
  back. A tool call is the escalation channel because nothing of it is streamed as text;
  the marker was, and the loop has no way to retract text a client already displayed.
* It is a plain answer and the judge accepts it: the answer stands and reaches the
  terminal gates like any draft. The evidence reviewer has nothing to check it against;
  ``PlainScopedReview`` routes the judged draft past it.
* It is a plain answer the judge refuses, or the judge fails: the draft is kept in
  history as a hypothesis and the turn is sent into research.

The judge is an independent context asked two questions in one call about the question
and the draft: is this settled, time-invariant knowledge that a source could not
change, and is the draft sound - right, consistent, free of claims only a source could
supply? Both are recorded separately and both must hold; one call because each was
measured alone first (5/5 and 3/3 on the common-knowledge set) and the reasoning
model's per-call cost made a second round trip the larger failure surface. Its
failure direction is research, the same as every other judge here. This is the
"plain-first arm" the sufficiency gate's notes record as rejected for the measured
arms, because the decision rests on a prior nobody can check against evidence; it is
built here, off by default and product-only, so that prior can be measured instead of
argued about.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextvars import ContextVar

from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.security.trust import unwrap_untrusted
from research_flow.gates.ask_user import is_first_turn
from research_flow.gates.conversation import ConversationGate, TurnMode
from research_flow.support._verdict import coerce_bool, parse_bool_verdict
from research_flow.support.answer_text import closing_tag_bar, visible_answer
from research_flow.support.harness_text import plain_first_notice
from research_flow.support.ledger import ledger_append
from research_flow.support.turn_task import task_for
from research_flow.tools.web import fetch_result_ok

logger = logging.getLogger(__name__)

_LEDGER_OP = "plain_first"
_LEDGER_OP_REVIEW = "plain_review"
_WEB_TOOLS = frozenset({"web_search", "web_fetch"})
RESEARCH_MARKER = "[research needed]"
REQUEST_RESEARCH_TOOL = "request_research"
_ACCEPTED = frozenset({"accepted", "accepted_unjudged"})
PLAIN_TURN_SOURCE = "gate_plain"

# Whether the turn running in this context was classed a plain turn by the gate.
# A ContextVar for ``is_research_turn``'s reasons: one runtime, many concurrent
# turns. Default False, reset at every turn entry, so a turn can only be plain
# because THIS turn's gate said so.
_PLAIN_TURN: ContextVar[bool] = ContextVar("dr_plain_turn", default=False)


def set_plain_turn(value: bool) -> None:
    _PLAIN_TURN.set(bool(value))


def is_plain_turn() -> bool:
    return _PLAIN_TURN.get()


REQUEST_RESEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": REQUEST_RESEARCH_TOOL,
        "description": (
            "Ask for the web tools. Call this instead of answering when the answer depends on "
            "anything recent, on current figures, on a specific document, or when you are not "
            "certain. The question is then researched as the contract requires."
        ),
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Why research is needed, one clause."}},
            "required": [],
        },
    },
}

# Defined in ``harness_text`` so the strict echo recogniser can name it; the
# interpolated ``_ESCALATE_NOTE`` below stays out of that recogniser for the reason
# ``sufficiency_notice`` gives against interpolation.
FIRST_REPLY_NOTE = plain_first_notice(REQUEST_RESEARCH_TOOL)

_ESCALATE_NOTE = (
    "[plain-first] Research is required for this question ({why}). Web tools are "
    "available now: research it as the contract requires and write the grounded final "
    "answer.{hypothesis}"
)
_HYPOTHESIS_TAIL = " Treat your draft above as a hypothesis to verify, not as evidence."
_LATE_REQUEST_NOTE = (
    f"[plain-first] {REQUEST_RESEARCH_TOOL} is no longer offered: the web tools are "
    "available now. Research the question with them and write the grounded final answer."
)

_JUDGE_SYSTEM = """You judge a question and a draft answer that was written without
consulting any source. Decide two things independently.

plain_ok: the question is settled, time-invariant general knowledge - a definition, a
physical constant, a standard formula or concept, a well-established scientific or
historical fact - and nothing its answer turns on depends on recent events, on current
prices, rates, office-holders or software versions, on a specific document or dataset,
or on precise figures a source would have to supply. Anything recent, contested,
numerically precise or document-specific is plain_ok false.

sound: the draft actually answers the question asked, every claim it turns on is
correct and not overstated, it does not contradict itself, and it does not lean on a
claim only a source could supply. There is no evidence pack; do not fault the draft for
citing nothing. Background detail the answer does not turn on is not a reason to fail it.

When in doubt on either, false: a wrong true ships an answer nobody checked, a wrong
false costs one round of research.

Reply with one JSON object and nothing else:
{"plain_ok": true|false, "sound": true|false, "reason": "<one short clause>",
 "issues": ["<one short clause per problem with the draft>"]}"""

_GATE_SYSTEM_PLAIN = """You decide what a follow-up message in an ongoing research \
conversation needs: a new round of web research, an answer from what is already in \
the conversation, or a plain answer from general knowledge.

Answer with JSON only: {"turn": "research"|"context"|"plain", "why": "<one short clause>"}

Choose "context" when the message only asks to reformat, shorten, expand, translate, \
explain or summarise material already present, asks about the assistant's own \
reasoning, or is conversational (thanks, acknowledgement, a correction of tone or scope).

Choose "plain" when the message opens a topic the conversation does not contain AND \
that topic is settled, time-invariant general knowledge - a definition, a physical \
constant, a standard formula or concept, a well-established scientific or historical \
fact - whose answer does not depend on recent events, current figures, office-holders, \
software versions, a specific document, or precise numbers a source would have to supply.

Choose "research" for anything else: any fact, figure, source, date, name or \
development the conversation does not contain and general knowledge does not settle, \
any request to re-check or confirm something, anything after the period the existing \
evidence covers.

When two readings are close, choose "research"."""


class PlainFirstGate(AgentHook):
    """Withhold the web tools for the first reply; accept, or escalate to research."""

    def __init__(
        self,
        provider=None,
        judge_model: str | None = None,
        judge_timeout_seconds: float = 90.0,
        judge_max_tokens: int = 8192,
        judge_reasoning_effort: str | None = "low",
        closing_tag_required: bool = False,
    ) -> None:
        self._provider = provider
        self._judge_model = judge_model
        self._judge_timeout_seconds = judge_timeout_seconds
        self._judge_max_tokens = judge_max_tokens
        self._judge_reasoning_effort = judge_reasoning_effort
        self._closing_tag_required = closing_tag_required
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP,
                "event": "installed",
                "judge": provider is not None,
                "judge_model": judge_model,
            }
        )

    @property
    def name(self) -> str:
        return "PlainFirstGate"

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if not (is_first_turn() or is_plain_turn()) or (ctx.iteration or 0) != 1:
            return HookDecision()
        state = ctx.metadata.setdefault("plain_first", {})
        if state.get("outcome"):
            return HookDecision()
        state["entry"] = "plain_turn" if is_plain_turn() else "first_turn"
        tools = [t for t in (ctx.tools or []) if (t.get("function") or {}).get("name") not in _WEB_TOOLS]
        if len(tools) == len(ctx.tools or []):
            state["web_tools_absent"] = True
            return HookDecision()
        state["withheld"] = True
        return HookDecision(
            modified_tools=[*tools, REQUEST_RESEARCH_SCHEMA],
            append_note=FIRST_REPLY_NOTE,
            notes=["plain_first: web tools withheld"],
        )

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        state = ctx.metadata.get("plain_first") or {}
        if not state.get("withheld"):
            return HookDecision()
        proposed = list(getattr(ctx.response, "tool_calls", None) or [])
        mine = [c for c in proposed if getattr(c, "name", "") == REQUEST_RESEARCH_TOOL]
        if not mine:
            return HookDecision()
        if state.get("outcome"):
            # The first-reply note persists in history and names the tool, so a model
            # may call it again on a later iteration when it is no longer offered.
            # Executing an unregistered tool would hand back an error; once per turn
            # the proposal is popped and the model told the web tools are already
            # there. A second late call goes through to the registry's own error,
            # so the rollback budget is not spent on a model that keeps asking.
            if state.get("late_request_answered"):
                return HookDecision()
            state["late_request_answered"] = True
            ledger_append({"ts": time.time(), "op": _LEDGER_OP, "outcome": "late_request", "iteration": ctx.iteration})
            return HookDecision(
                rollback=True,
                rollback_inject=[{"role": "user", "content": _LATE_REQUEST_NOTE}],
                notes=["plain_first: late request_research call popped"],
            )
        # The rollback pops the whole tool-call response, so sibling calls are lost
        # with it - counted, because the model wrote them and never saw a result.
        state["mixed_call"] = len(proposed) > 1
        state["request_reason"] = _reason_from(getattr(mine[0], "arguments", None))
        self._finish(ctx, state, "escalated_tool")
        return self._escalate(state["request_reason"] or "the model asked for it", draft=None)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        state = ctx.metadata.get("plain_first") or {}
        if not state.get("withheld") or state.get("outcome"):
            return HookDecision()
        if getattr(ctx.response, "has_tool_calls", False):
            # A clarify or a local-file read on the first call: not a plain answer,
            # not a request for research either. The next iteration has every tool.
            return self._finish(ctx, state, "bypassed_tool_call")
        content = getattr(ctx.response, "content", None) or ""
        draft = visible_answer(
            content,
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required,
                getattr(ctx.response, "reasoning_content", None),
            ),
        )
        if not draft:
            # Answerless first reply: the salvage gate's business, not this one's.
            return self._finish(ctx, state, "answerless")
        if RESEARCH_MARKER in draft.lower():
            # Kept for models that write the marker instead of calling the tool. The
            # marker has streamed by now; the tool path exists so it need not.
            self._finish(ctx, state, "escalated_marker")
            return self._escalate("the model asked for it", draft=None)

        state["draft_chars"] = len(draft)
        if self._provider is None:
            return self._finish(ctx, state, "accepted_unjudged")
        started = time.monotonic()
        verdict = await self._judge(ctx, draft)
        state["judge_latency_s"] = round(time.monotonic() - started, 3)
        state["judge_reason"] = (verdict or {}).get("reason")
        if verdict is None:
            self._finish(ctx, state, "escalated_judge_failed")
            return self._escalate("the answer could not be checked", draft=draft)
        state["judge_plain_ok"] = verdict["plain_ok"]
        state["judge_sound"] = verdict["sound"]
        state["judge_issues"] = verdict["issues"]
        if not verdict["plain_ok"]:
            self._finish(ctx, state, "escalated_judge")
            return self._escalate(state["judge_reason"] or "an independent check found it needs sources", draft=draft)
        if not verdict["sound"]:
            # Common knowledge, wrongly stated: the same exit, a different reason,
            # and the issues travel so the research round knows what to check.
            self._finish(ctx, state, "escalated_review")
            why = "; ".join(verdict["issues"]) or state["judge_reason"] or "the draft did not hold up"
            return self._escalate(why, draft=draft)
        return self._finish(ctx, state, "accepted")

    def _finish(self, ctx: AgentHookContext, state: dict, outcome: str) -> HookDecision:
        state["outcome"] = outcome
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP,
                "outcome": outcome,
                "iteration": ctx.iteration,
                "draft_chars": state.get("draft_chars"),
                "judge_latency_s": state.get("judge_latency_s"),
                "reason": state.get("judge_reason") or state.get("request_reason"),
            }
        )
        logger.info("plain-first: %s", outcome)
        return HookDecision(notes=[f"plain_first: {outcome}"])

    @staticmethod
    def _escalate(why: str, draft: str | None) -> HookDecision:
        inject: list[dict] = []
        if draft:
            inject.append({"role": "assistant", "content": draft})
        inject.append(
            {
                "role": "user",
                "content": _ESCALATE_NOTE.format(why=why, hypothesis=_HYPOTHESIS_TAIL if draft else ""),
            }
        )
        return HookDecision(rollback=True, rollback_inject=inject, notes=[f"plain_first: escalated ({why})"])

    async def _judge(self, ctx: AgentHookContext, draft: str) -> dict | None:
        user = f"Question:\n{task_for(ctx)}\n\nDraft answer (written without sources):\n{draft}"
        verdict = await _ask_once(
            self._provider,
            system=_JUDGE_SYSTEM,
            user=user,
            key="plain_ok",
            model=self._judge_model,
            max_tokens=self._judge_max_tokens,
            timeout_seconds=self._judge_timeout_seconds,
            reasoning_effort=self._judge_reasoning_effort,
            who="plain-first judge",
        )
        if verdict is None:
            return None
        sound = coerce_bool(verdict.get("sound"))
        if sound is None:
            # Half a verdict is no verdict: a judge that answered the class but not
            # the soundness question has not checked the draft.
            logger.warning("plain-first judge: output missing boolean 'sound'")
            return None
        verdict["sound"] = sound
        reason = verdict.get("reason")
        verdict["reason"] = reason.strip()[:200] if isinstance(reason, str) else None
        raw_issues = verdict.get("issues")
        verdict["issues"] = (
            [str(i).strip()[:200] for i in raw_issues if str(i).strip()][:8] if isinstance(raw_issues, list) else []
        )
        return verdict


class PlainTurnGate(ConversationGate):
    """The conversation gate with a third verdict: a new common-knowledge topic.

    Built in place of ``ConversationGate`` only when plain-first is on, so the arms
    that never enable it keep the gate's two-key prompt byte for byte. The plain
    verdict is a RESEARCH turn - every observer runs, the plain-first gate withholds
    the web tools for one call, the judge reads the draft, an escalation lands in
    ordinary research. What it is not is a context turn: those have no judge, no
    reviewer and no record, and a misjudged common-knowledge answer must not ship
    through the one path that checks nothing. Every failure of the call itself is
    the parent's: a research turn that names its cause.
    """

    def __init__(self, provider, **kw) -> None:
        super().__init__(provider, **kw)
        self._system = _GATE_SYSTEM_PLAIN

    def _verdict(self, text: str) -> TurnMode | None:
        data = _first_json_object(text)
        if data is None:
            return None
        turn = str(data.get("turn") or "").strip().lower()
        why = data.get("why")
        why = str(why)[:200] if isinstance(why, str) else ""
        if turn == "plain":
            return TurnMode(True, PLAIN_TURN_SOURCE, why)
        if turn == "context":
            return TurnMode(False, "gate", why)
        if turn == "research":
            return TurnMode(True, "gate", why)
        # A model that answered in the parent's two-key shape is still read.
        legacy = coerce_bool(data.get("research"))
        if legacy is None:
            return None
        return TurnMode(legacy, "gate", why)


def _first_json_object(text: str) -> dict | None:
    if not text:
        return None
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            import json_repair

            data = json_repair.loads(candidate)
        except Exception:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
        if isinstance(data, dict):
            return data
    return None


class PlainScopedReview(AgentHook):
    """Route a judged, evidence-free plain answer past the evidence reviewer.

    The reviewer's rubric is claims against evidence; on a turn that opened no page it
    judges against an empty pack, and measured on six common-knowledge questions its
    verdicts on such drafts were pass 2 / reject 2 / timeout 1 - noise that costs 40-120s
    and, on a reject, sends a settled answer into research. The judge has already read
    the draft for soundness, so on exactly those responses - the judge accepted the
    draft AND this turn has no readable page - the reviewer is not consulted.

    A wrapper, for ``GatedHook``'s reasons: the reviewer is the measured surface, and
    with plain-first off (or ``review="full"``) this class is never built, so the chain
    is unchanged. Every other response - a draft written after research, a re-sample
    after an escalation - reaches the inner reviewer untouched, because the predicate
    reads this turn's transcript, not the configuration.
    """

    def __init__(self, inner: AgentHook, closing_tag_required: bool = False) -> None:
        self._inner = inner
        self._closing_tag_required = closing_tag_required

    @property
    def name(self) -> str:
        return f"PlainScoped({self._inner.name})"

    @property
    def inner(self) -> AgentHook:
        return self._inner

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.before_user_inbound(ctx)

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.before_iteration(ctx)

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.before_execute_tools(ctx)

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.terminal_answerless(ctx)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.after_send(ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if not self._plain_unsourced(ctx):
            return await self._inner.after_iteration(ctx)
        plain = ctx.metadata["plain_first"]
        # Stamped beside the reviewer's own counters so ``reviews == 0`` here reads as
        # "scoped out", not as a reviewer that timed out.
        verify = ctx.metadata.setdefault(
            "verify_gate", {"revisions": 0, "reviews": 0, "fail_open": 0, "passes": 0, "rejects": 0}
        )
        verify["scoped"] = "plain"
        plain["review"] = "skipped_judged"
        ledger_append(
            {
                "ts": time.time(),
                "op": _LEDGER_OP_REVIEW,
                "review": "skipped_judged",
                "iteration": ctx.iteration,
                "judge_sound": plain.get("judge_sound"),
            }
        )
        logger.info("plain-review: skipped, the judge read the draft")
        return HookDecision(notes=["plain_review: skipped_judged"])

    def _plain_unsourced(self, ctx: AgentHookContext) -> bool:
        plain = ctx.metadata.get("plain_first") or {}
        if plain.get("outcome") not in _ACCEPTED or plain.get("review"):
            return False
        if getattr(ctx.response, "has_tool_calls", False):
            return False
        draft = visible_answer(
            getattr(ctx.response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required, getattr(ctx.response, "reasoning_content", None)
            ),
        )
        if not draft:
            return False
        # The belt under the judge's verdict: a turn that opened a page has evidence,
        # and evidence is the inner reviewer's job whatever the judge said.
        for m in (ctx.messages or [])[ctx.turn_base or 0 :]:
            if isinstance(m, dict) and m.get("role") == "tool" and m.get("name") == "web_fetch":
                if fetch_result_ok(unwrap_untrusted(m.get("content"))):
                    return False
        return True


def _reason_from(arguments) -> str | None:
    if isinstance(arguments, dict):
        reason = arguments.get("reason")
    elif isinstance(arguments, str) and arguments.strip():
        try:
            reason = (json.loads(arguments) or {}).get("reason")
        except (ValueError, AttributeError):
            reason = None
    else:
        reason = None
    return reason.strip()[:200] if isinstance(reason, str) and reason.strip() else None


async def _ask_once(
    provider, *, system, user, key, model, max_tokens, timeout_seconds, who, reasoning_effort=None
) -> dict | None:
    """One judge call, one attempt: the reviewer measured 44-303s per call on this
    model with no gain from a fresh call, so a restart only forfeits progress."""
    # Passed only when configured, the distinction ``sufficiency`` documents: an
    # absent kwarg means the provider's default, an explicit ``None`` suppresses it.
    effort_kwargs = {"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}
    try:
        response = await asyncio.wait_for(
            provider.chat_with_retry(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
                **effort_kwargs,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("%s: stalled past %.0fs; failing toward research", who, timeout_seconds)
        return None
    except Exception as exc:
        logger.warning("%s: call failed (%s: %s); failing toward research", who, type(exc).__name__, exc)
        return None
    if getattr(response, "finish_reason", "") in ("length", "error"):
        logger.warning("%s: no verdict (finish_reason=%s)", who, response.finish_reason)
        return None
    text = visible_answer(getattr(response, "content", None) or "")
    parsed = parse_bool_verdict(text, key) if text else None
    if parsed is None:
        logger.warning("%s: output missing boolean %r", who, key)
        return None
    verdict, _ = parsed
    return verdict


__all__ = [
    "FIRST_REPLY_NOTE",
    "PLAIN_TURN_SOURCE",
    "REQUEST_RESEARCH_SCHEMA",
    "REQUEST_RESEARCH_TOOL",
    "RESEARCH_MARKER",
    "PlainFirstGate",
    "PlainScopedReview",
    "PlainTurnGate",
    "is_plain_turn",
    "set_plain_turn",
]
