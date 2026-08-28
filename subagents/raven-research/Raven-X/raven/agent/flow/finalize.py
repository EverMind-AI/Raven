"""Forced-termination backstop (DR flow).

A deep-research turn can end on an assistant message that carries
reasoning but no answer — the visible text after the think block is
empty — or on a degenerate spin dump. Left alone, that message persists
as the final answer and the task scores as unanswered, even though the
working memory usually contains the right candidate.

Two seams reach it. ``after_iteration`` catches turns that reached a text
response, and ``terminal_answerless`` catches the exits no iteration hook
sees — a provider error, an exhausted iteration budget — which is where
most answerless turns actually come from.

Recovery is two-staged and bounded:

1. **Commit nudge** (preferred): pop the answerless response and inject
   a persisted user note telling the model to commit to the
   best-supported answer from the evidence already gathered. The retry
   produces a normally-shaped final turn (reasoning + answer), so the
   trajectory stays training-clean.
2. **Salvage synthesis** (backstop): if the retry still carries no
   answer, one independent-context call — task + reasoning excerpt +
   evidence excerpts — picks the candidate best supported by the
   evidence. The instruction weighs recurrence and evidential support,
   not recency: the last hypothesis a stalled search visited is often a
   drift, not a decision.

Fail-open by contract: the salvage call dying must never silence the
turn — the original message stands and the failure lands in the
observer state.
"""

from __future__ import annotations

import asyncio
import logging
import time

from raven.agent.flow.answer_text import closing_tag_bar, visible_answer
from raven.agent.flow.turn_task import task_for
from raven.agent.harness_text import harness_body_kind, is_harness_echo
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.agent.hook.observers.loopscan import is_spin, spin_stats
from raven.agent.ledger import ledger_append
from raven.security.trust import wrap_untrusted

logger = logging.getLogger(__name__)

_COMMIT_NUDGE_TAIL = (
    "Do not restart the research. Commit to the best-supported answer from the "
    "evidence still readable above - some older tool results may have been "
    "replaced by a placeholder to fit the context window, so rely on what you "
    "can still see: state the answer first, then the key evidence with source "
    "URLs, then any remaining uncertainty."
)

_COMMIT_NUDGE_OPENERS = {
    "empty_visible_answer": "The last response contained reasoning but no final answer.",
    "spin_answer": "The last response repeated itself instead of settling on an answer.",
}


def _commit_nudge(reason: str) -> str:
    """The nudge has to name the right defect.

    The gate fires on two disjoint conditions and used one sentence for both, so
    a turn that HAD produced an answer judged degenerate was told it had produced
    none - and then asked to produce the thing it had just produced. ``reason`` is
    already computed one line above the injection site.
    """
    opener = _COMMIT_NUDGE_OPENERS.get(reason, _COMMIT_NUDGE_OPENERS["empty_visible_answer"])
    return f"[finalize] {opener} {_COMMIT_NUDGE_TAIL}"

_SALVAGE_SYSTEM = (
    "You finalize a deep-research task whose research phase ended without a "
    "committed answer. From the task, the researcher's notes and the "
    "evidence excerpts, choose the single answer best supported by the "
    "evidence. Weigh how often a candidate recurs in the notes and how "
    "directly the evidence backs it - NOT which candidate appears last; "
    "late hypotheses from a stalled search are often drift, not decisions. "
    "Never invent facts absent from the notes and evidence. Write the final "
    "answer directly: first line is the answer itself and nothing else, then "
    "the key evidence with source URLs. Do NOT label the answer unconfirmed, "
    "tentative or undetermined - a reply that declines to name a candidate is "
    "scored wrong, so naming your best-supported candidate is always the "
    "better option; put any doubt in one short line after the evidence. Never "
    "emit a list of searches or tool calls as your answer."
)
# The previous tail invited the hedge it then measured: 11 of 25 committed
# salvages carried "unconfirmed"-family wording and 0 of those 11 were judged
# correct, against 9 of 14 (64%) for the unhedged ones. Two of the 25 committed a
# JSON array of searches instead of an answer, which is why the plan ban is
# explicit.


class ForcedFinalizeGate(AgentHook):
    """Refuse to let an answerless terminal turn stand: nudge, then salvage."""

    def __init__(
        self,
        provider,
        model: str | None = None,
        max_nudges: int = 1,
        timeout_seconds: float = 240.0,
        attempt_timeout_seconds: float = 240.0,
        max_tokens: int = 4096,
        evidence_items: int = 8,
        evidence_item_chars: int = 2000,
        reasoning_excerpt_chars: int = 8000,
        closing_tag_required: bool = False,
        reasoning_effort: str | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_nudges = max_nudges
        self._timeout_seconds = timeout_seconds
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._max_tokens = max_tokens
        self._evidence_items = evidence_items
        self._evidence_item_chars = evidence_item_chars
        self._reasoning_excerpt_chars = reasoning_excerpt_chars
        self._closing_tag_required = closing_tag_required
        self._reasoning_effort = reasoning_effort

    @property
    def name(self) -> str:
        return "ForcedFinalizeGate"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        content = getattr(ctx.response, "content", None) or ""
        # Same bar as the terminal seam and as the scorer. Judged with the
        # permissive default, a turn cut mid-reasoning looks like it produced an
        # answer, so the cheap recovery — pop the response, inject a commit note,
        # re-sample — never runs and the turn falls straight through to terminal
        # salvage. Measured on one batch: nudges fired 11 of 110 and 20 of 120
        # against 36 and 25 answerless turns.
        answer = visible_answer(
            content,
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required,
                getattr(ctx.response, "reasoning_content", None),
            ),
        )
        if answer and not is_spin(spin_stats(answer)):
            return HookDecision()

        reason = "empty_visible_answer" if not answer else "spin_answer"
        state = ctx.metadata.setdefault(
            "force_finalize",
            {"empty_hits": 0, "nudges": 0, "synth_failed": 0},
        )
        state["empty_hits"] += 1
        state["last_reason"] = reason

        if state["nudges"] < self._max_nudges:
            state["nudges"] += 1
            logger.warning(
                "force-finalize: %s at iteration %s; commit nudge %d/%d",
                reason,
                ctx.iteration,
                state["nudges"],
                self._max_nudges,
            )
            return HookDecision(
                rollback=True,
                rollback_inject=[{"role": "user", "content": _commit_nudge(reason)}],
                notes=[f"force_finalize_nudge {reason}"],
            )

        salvage = await self._salvage(ctx, content)
        if salvage:
            state["synthesized"] = True
            self._mark_committed(state, salvage, "iteration")
            # The trail must say the shipped answer bypassed the reviewer
            # (observer order salvages answerless terminals, never reviews them).
            ledger_append({"ts": time.time(), "op": "force_finalize", "event": "salvage", "seam": "iteration", "reason": reason})
            logger.warning("force-finalize: salvage synthesis replaced an answerless final (%s)", reason)
            return HookDecision(
                short_circuit_result=salvage,
                notes=[f"force_finalize_salvage {reason}"],
            )
        state["synth_failed"] += 1
        logger.warning("force-finalize: salvage unavailable; passing the answerless final through")
        return HookDecision(notes=["force_finalize_failopen"])

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        """Last seam: the turn is over and carries no answer — salvage or fail open.

        Reached by the exits ``after_iteration`` never sees (provider error,
        exhausted budget, reasoning-only wrap-up). There is no iteration left to
        re-sample, so the commit nudge is not an option here and salvage runs
        immediately. Empirically this is the dominant answerless shape: the
        commit-nudge path fires on a handful of turns, while roughly one turn in
        seven dies on a context-window error whose response is never persisted.
        """
        state = ctx.metadata.setdefault(
            "force_finalize",
            {"empty_hits": 0, "nudges": 0, "synth_failed": 0},
        )
        state["terminal_hits"] = state.get("terminal_hits", 0) + 1
        content = self._last_assistant_text(ctx.messages or [])
        salvage = await self._salvage(ctx, content)
        if salvage:
            state["synthesized"] = True
            self._mark_committed(state, salvage, "terminal")
            ledger_append({"ts": time.time(), "op": "force_finalize", "event": "salvage", "seam": "terminal"})
            logger.warning("force-finalize: terminal salvage committed an answer for an answerless turn")
            return HookDecision(
                short_circuit_result=salvage,
                notes=["force_finalize_terminal_salvage"],
            )
        state["synth_failed"] += 1
        logger.warning("force-finalize: terminal salvage unavailable; the turn stays answerless")
        return HookDecision(notes=["force_finalize_terminal_failopen"])

    @staticmethod
    def _last_assistant_text(messages: list[dict]) -> str:
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "assistant":
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    return content
        return ""

    async def _salvage(self, ctx: AgentHookContext, content: str) -> str | None:
        task = task_for(ctx)
        notes = self._excerpt(content, self._reasoning_excerpt_chars)
        evidence = self._evidence_pack(ctx.messages or [])
        user = (
            f"Task:\n{task}\n\n"
            f"Researcher notes (reasoning that ended without an answer; may contain candidates):\n{notes}\n\n"
            f"Evidence excerpts:\n{evidence or '(no tool evidence was gathered)'}"
        )
        deadline = asyncio.get_event_loop().time() + self._timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning(
                    "force-finalize: salvage timed out after %.0fs budget; fail-open", self._timeout_seconds
                )
                return self._fail(ctx, "budget_exhausted")
            attempt_window = min(self._attempt_timeout_seconds, remaining)
            try:
                response = await asyncio.wait_for(
                    self._provider.chat_with_retry(
                        messages=[
                            {"role": "system", "content": _SALVAGE_SYSTEM},
                            {"role": "user", "content": user},
                        ],
                        model=self._model,
                        max_tokens=self._max_tokens,
                        temperature=0.0,
                        reasoning_effort=self._reasoning_effort,
                    ),
                    timeout=attempt_window,
                )
                break
            except asyncio.TimeoutError:
                # A restart throws away a generation that was merely slow, and
                # the fresh call is no faster: measured over one 440-question
                # batch, every salvage that stalled once went on to burn its
                # whole budget in identical stalls and failed open. Only retry
                # when the budget still affords a full attempt.
                if deadline - asyncio.get_event_loop().time() < self._attempt_timeout_seconds:
                    logger.warning("force-finalize: salvage stalled past %.0fs; fail-open", attempt_window)
                    return self._fail(ctx, "stalled")
                logger.warning(
                    "force-finalize: salvage attempt stalled past %.0fs; retrying on a fresh call",
                    attempt_window,
                )
                continue
            except Exception as exc:
                logger.warning("force-finalize: salvage call failed (%s: %s); fail-open", type(exc).__name__, exc)
                return self._fail(ctx, "call_failed")
        if getattr(response, "finish_reason", "") == "error":
            return self._fail(ctx, "error_response")
        # Truncation and "the model produced only reasoning" are different
        # diagnoses with different fixes — a bigger budget versus a better
        # prompt — and collapsing them is what the ``_fail`` reasons exist to
        # prevent. The reviewer gate already distinguishes them; this one did
        # not, so four versions of budget starvation were filed as
        # ``no_visible_answer``.
        if getattr(response, "finish_reason", "") == "length":
            return self._fail(ctx, "length")
        # The committed salvage must clear the same bar as the answer it
        # replaces. Judged with the permissive default, a salvage call that was
        # itself cut mid-think passes its whole chain of thought through as the
        # final answer -- the shape the closing-tag rule exists to reject, and
        # the seam is invisible downstream because the tag is already stripped.
        answer = visible_answer(
            getattr(response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required,
                getattr(response, "reasoning_content", None),
            ),
        )
        # dr@3.2: the harness must not be able to answer its own question. When
        # the evidence pack is all placeholders and refusals, the salvage model
        # sometimes returns one of them verbatim -- ``hle-256`` on the dr@3.0
        # live-web batch shipped the search-closed notice as its ``final_answer``,
        # scored zero, and still counted as ``closed_with_answer`` on the
        # answer-rate endpoint. So this is not only a lost point; it is a false
        # positive on a co-primary endpoint, and fixing it moves that endpoint
        # DOWN (~0.33pp on the dr arm). Doing it anyway is the whole point: an
        # endpoint that counts the harness's own sentence as "answered" is
        # measuring the harness, not the agent.
        #
        # Strict matching (whole answer == a harness sentence), never substring.
        # Blanking an answer that merely mentions the refusal would recreate the
        # exact failure this module exists to prevent -- MiroFlow's boxed
        # extraction dropped 10.83% of its own correct answers that way, and
        # dr@1.6's salvage seam blanked 15. Shaping may improve an answer; it may
        # never empty one.
        if is_harness_echo(answer):
            return self._fail(ctx, "harness_echo")
        return answer or self._fail(ctx, "no_visible_answer")

    @staticmethod
    def _mark_committed(state: dict, answer: str, seam: str) -> None:
        """Stamp a salvage that actually replaced the final answer.

        A committed salvage carries no closing tag -- ``visible_answer`` has
        already folded the reasoning away -- so it is indistinguishable by
        inspection from a turn that never reached its answer, and a consumer
        applying the closing-tag rule blanks both. This counter is the only
        thing that separates them, so it must be written wherever the answer
        is replaced, not derived downstream."""
        state["salvage_committed"] = state.get("salvage_committed", 0) + 1
        state["salvage_seam"] = seam
        state["salvage_answer_chars"] = len(answer)

    @staticmethod
    def _fail(ctx: AgentHookContext, reason: str) -> None:
        """Record why a salvage produced nothing, on the trajectory.

        Which failure mode dominates decides the fix (a stall wants a longer
        window, a think-only response wants more output budget), and stderr
        tails are truncated long before a 440-question batch is attributed."""
        ctx.metadata.setdefault("force_finalize", {})["synth_fail_reason"] = reason
        return None

    @staticmethod
    def _excerpt(text: str, cap: int) -> str:
        if len(text) <= cap:
            return text
        head = cap * 3 // 5
        tail = cap - head
        return f"{text[:head]}\n[... notes truncated ...]\n{text[-tail:]}"

    def _evidence_pack(self, messages: list[dict]) -> str:
        """Last ``evidence_items`` tool results that still carry a body.

        This gate fires precisely on the long, overflowing turns where the loop
        has already replaced older tool bodies with an elision placeholder, so a
        blind tail would ask the salvage model to commit an answer out of
        placeholders. Skip them and reach further back instead.

        dr@3.2: skip every harness-authored body, not just the elision marker.
        The elision placeholder was the only one recognised, and the gap was not
        theoretical - on the dr@3.0 live-web batch 34 of 108 salvage-committed
        turns (31.5%) had a pack that was entirely harness text or empty, and one
        of them (``hle-256``) returned the harness sentence verbatim as the run's
        final answer. A false positive here costs one item of look-back, which is
        why this side may be permissive; see ``harness_text`` for why the answer
        channel gets a stricter predicate instead of this one.
        """
        results = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        items: list[str] = []
        for m in reversed(results):
            if len(items) >= self._evidence_items:
                break
            content = str(m.get("content") or "")
            if harness_body_kind(content) is not None:
                continue
            if len(content) > self._evidence_item_chars:
                content = content[-self._evidence_item_chars :]
            items.append(wrap_untrusted(content, source=str(m.get("name") or "tool")))
        items.reverse()
        return "\n\n".join(items)


__all__ = ["ForcedFinalizeGate"]
