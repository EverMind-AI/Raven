"""End-of-turn draft review gate (DR flow).

The candidate final answer is reviewed by an independent context — a
fresh ``[system, user]`` conversation that shares nothing with the
drafting context except the task, the draft, and a fenced evidence
pack. A failed review sends the turn back to solve exactly once: the
draft and the reviewer's feedback are injected into history (persisted,
so the draft -> feedback -> revision shape enters the training
distribution) and the loop re-samples.

Fail-open by contract: reviewer infra dying, timing out, or emitting
unparseable output must never silence the turn — the draft passes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from raven.agent.context import is_elided_tool_output
from raven.agent.harness_text import harness_body_kind
from raven.agent.evidence_round import EvidenceRound
from raven.agent.flow.answer_text import visible_answer
from raven.agent.flow.turn_task import task_for
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision
from raven.agent.ledger import ledger_append
from raven.security.trust import wrap_untrusted

logger = logging.getLogger(__name__)

# Why this gate writes to the ledger at all.
#
# Every counter below already existed and was already maintained per turn; none of them
# ever left the process. The only trace was a logger.warning, and the launcher keeps just
# the last 1500 bytes of stderr, so the record was both partial and unparseable. Measured
# consequence: `rejected_on_elided` was cited as "identically zero across the whole tree"
# in the evidence that retracted the dr@2.x elision attribution, while the warning that
# accompanies its increment is present on 8-12.5% of the questions in every DR arm of six
# separate batches. A counter that dies with the process reads exactly like an event that
# never happened - the `dr16-salvage-seam-zeroes-itself` shape, one layer earlier.
#
# Three things this deliberately does NOT do:
#
# 1. It does not aggregate into a scalar such as `verify_pass`. By construction the
#    accepted draft nearly always passed - it was accepted either because it passed,
#    because the revision budget ran out, or because a carve-out degraded a reject into a
#    pass. A near-constant field carries no signal. What varies is the FRICTION it took
#    to get there, so each review is recorded on its own line and any aggregate is
#    computed offline, where the definition can change without a rerun.
# 2. It does not reuse the word "verdict". Downstream `traj_scored.jsonl` already has a
#    `verdict` key meaning "the judge marked this answer correct". Two different verdicts
#    under one name is how a label comes to describe something nobody measured.
# 3. It does not let "no records" mean "no rejections". A flow-off anchor never builds
#    this gate at all, so it writes nothing - indistinguishable from a gate that ran and
#    never fired. The `installed` record is the presence flag that separates them; a
#    reader that finds no `installed` line must report "not applicable", not zero.
_LEDGER_OP = "verify"
_LEDGER_OP_GATE = "verify_gate"

_REVIEWER_SYSTEM = (
    "You are a strict draft reviewer for a deep-research task. Judge the "
    "draft ONLY against the evidence provided; do not use outside knowledge "
    "to fill gaps. Check: (1) is every decisive claim supported by the "
    "evidence, (2) are sources cited for the key facts, (3) does the draft "
    "actually answer the task. Keep any internal reasoning brief. Your "
    "reply must be EXACTLY one bare JSON object and nothing else - no prose "
    "before or after, no markdown code fences: "
    '{"pass": true|false, "unresolved_claims": <int>, '
    '"unsupported_claims": ["..."], "issues": ["..."]}. '
    '"pass" must be a JSON boolean literal (true or false), not a string. '
    "Fail the draft only for substantive problems - unsupported decisive "
    "claims, wrong entities, missing answer - not for style."
)

# The gate was rejecting correct answers because their evidence had aged out of
# the window, not because the answer was wrong. Both recoverable items in one
# 120-question batch died here: a draft committing "Dr. Martens" (gold: Dr
# Martens) and one committing "Last Christmas" (gold: Last Christmas) were each
# failed on every bullet as unsupported, with 87% and 100% of the tool results
# in context already replaced by the elision placeholder. A reviewer that reads
# "the supporting text is gone" as "the claim is unsupported" is testing whether
# evidence is still resident, not whether the answer is true - which biases it
# against exactly the long runs that most need a verdict.
_ELISION_RUBRIC = (
    " An `[earlier tool output elided to fit the context window]` placeholder "
    "means the supporting text was dropped to fit the window - NOT that the "
    "claim is unsupported. Never put a claim in unsupported_claims because its "
    "evidence was elided: if the draft cites a URL and the body is no longer in "
    "view, the citation is sufficient, and you may record it in "
    '"unverifiable_elided" instead. Reject only for a claim that contradicts '
    "evidence still in view, or that cites nothing at all. This overrides every "
    "rule above, including the per-constraint check: a constraint you cannot "
    "check because its evidence was elided is not a failed constraint."
)

_CONSTRAINT_RUBRIC = (
    " When the task pins multiple constraints (dates, places, names, "
    "quantities, relationships), check the drafted answer against EACH "
    "constraint one by one against the evidence, and treat any failed "
    "constraint as an unsupported claim."
)

_STRICT_REJECT_ONLY = (
    " Reject ONLY when the draft fails to answer the task, or a decisive "
    "claim is contradicted by or absent from the evidence - and name the "
    "offending claim(s) in unsupported_claims. Do not reject for style, "
    "breadth, uncited background detail, or claims you merely cannot "
    "verify either way."
)

_REVISION_PROMPT = (
    "A reviewer rejected the draft above. Fix exactly the listed issues and "
    "produce the corrected final answer. Do not restart the research; reuse "
    "the evidence already gathered.\n\nReviewer feedback:\n{feedback}"
)

# The prohibition above is correct only when the draft is wrong about evidence it
# holds. The reviewer fails a draft for unsupported decisive claims, wrong entities,
# or a missing answer - all three are evidence problems, and the largest measured
# one is that the supporting document was never retrieved at all. Told to reuse what
# it has, a turn in that state can recover nothing. This variant grants retrieval
# while keeping the narrowness the prohibition was protecting: the named claims
# only, no re-opening of questions the reviewer did not raise.
_EVIDENCE_ROUND_PROMPT = (
    "A reviewer rejected the draft above. The listed claims are not supported by "
    "the evidence gathered so far.\n\nYou may now run additional searches and open "
    "pages, targeted at exactly those claims. Search deeper rather than broader: "
    "results beyond the first few are available to you in this round. Do not "
    "restart the research, and do not re-open questions the reviewer did not "
    "raise. Once the gap is closed - or once it is clear it cannot be - produce "
    "the corrected final answer.\n\nReviewer feedback:\n{feedback}"
)


class DraftReviewerGate(AgentHook):
    """Verify-gate: review the candidate final, bounce it back once."""

    def __init__(
        self,
        provider,
        model: str | None = None,
        timeout_seconds: float = 180.0,
        attempt_timeout_seconds: float = 60.0,
        max_revisions: int = 1,
        review_final_draft: bool = False,
        evidence_items: int = 8,
        evidence_item_chars: int = 2000,
        max_tokens: int = 8192,
        constraint_rubric: bool = False,
        strict_reject_only: bool = False,
        fail_open_on_elided_evidence: bool = True,
        evidence_round: "EvidenceRound | None" = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._max_revisions = max_revisions
        self._review_final_draft = review_final_draft
        self._evidence_items = evidence_items
        self._evidence_item_chars = evidence_item_chars
        self._max_tokens = max_tokens
        self._strict_reject_only = strict_reject_only
        self._fail_open_on_elided_evidence = fail_open_on_elided_evidence
        self._evidence_round = evidence_round
        # Order is load-bearing. _CONSTRAINT_RUBRIC says "treat any failed
        # constraint as an unsupported claim"; the elision carve-out says a claim
        # whose evidence was elided is NOT unsupported. Those collide exactly when
        # a constraint fails because its evidence is gone, so the carve-out has to
        # be the last word rather than the first.
        ledger_append({
            "ts": time.time(),
            "op": _LEDGER_OP_GATE,
            "event": "installed",
            "max_revisions": max_revisions,
            "strict_reject_only": strict_reject_only,
            "fail_open_on_elided_evidence": fail_open_on_elided_evidence,
            "constraint_rubric": constraint_rubric,
            "evidence_round": evidence_round is not None,
        })
        self._reviewer_system = _REVIEWER_SYSTEM
        if constraint_rubric:
            self._reviewer_system += _CONSTRAINT_RUBRIC
        if strict_reject_only:
            self._reviewer_system += _STRICT_REJECT_ONLY
        self._reviewer_system += _ELISION_RUBRIC

    @property
    def name(self) -> str:
        return "DraftReviewerGate"

    @staticmethod
    def _record(
        state: dict,
        outcome: str,
        *,
        verdict: dict | None = None,
        draft: str | None = None,
        reviewed: bool = True,
    ) -> None:
        """One ledger line per review outcome. Write-only; nothing here is read back.

        Fields that a given outcome did not measure are written as null rather than 0.
        ``budget_spent`` returns before any review runs, so its counts are unknown, and
        the elision snapshot still on ``state`` belongs to the previous review - carrying
        it forward would let a stale number read as a fresh measurement.
        """
        claims = (verdict or {}).get("unsupported_claims") or []
        issues = (verdict or {}).get("issues") or []
        ledger_append({
            "ts": time.time(),
            "op": _LEDGER_OP,
            "outcome": outcome,
            "reviewed": reviewed,
            "review": state.get("reviews", 0) if reviewed else None,
            "reviewer_pass": None if verdict is None else bool(verdict.get("pass", True)),
            "n_unsupported_claims": len(claims) if verdict is not None else None,
            "n_issues": len(issues) if verdict is not None else None,
            "elided_in_context": state.get("evidence_elided_in_context") if reviewed else None,
            "elided_skipped_total": state.get("evidence_elided_skipped", 0) if reviewed else None,
            "revisions": state.get("revisions", 0),
            # Null, not 0, when the feature is off: "no round was granted here" and
            # "rounds do not exist on this arm" are different claims, and only the
            # second one makes a zero in an aggregate mean anything.
            "evidence_rounds": state.get("evidence_rounds"),
            "draft_chars": len(draft) if draft is not None else None,
        })

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        content = getattr(ctx.response, "content", None) or ""
        draft = visible_answer(content)
        if not draft:
            return HookDecision()

        state = ctx.metadata.setdefault(
            "verify_gate",
            {"revisions": 0, "reviews": 0, "fail_open": 0, "passes": 0, "rejects": 0},
        )
        if state["revisions"] >= self._max_revisions:
            state["accepted_after_revision"] = True
            # dr@3.0. One counter was doing two jobs: capping how often the turn may be
            # sent back, and deciding whether a review happens at all. So the draft that
            # SHIPS is the one draft nobody reads - 42 of 47 rejects on one batch, 28 of
            # 28 on another. Reviewing it changes no decision (it is accepted either
            # way, and nothing enters history), so the score budget is zero by
            # construction; what it produces is a verdict on the delivered answer, which
            # is the missing input for an in-run verify selector.
            if self._review_final_draft:
                verdict = await self._review(ctx, draft)
                state["verdict_final"] = verdict
                if verdict is not None:
                    state["reviews"] += 1
                self._record(
                    state, "budget_spent_reviewed", verdict=verdict, draft=draft,
                    reviewed=verdict is not None,
                )
                return HookDecision(
                    notes=["verify_gate: revision budget spent; reviewed for the record, accepting"]
                )
            self._record(state, "budget_spent", draft=draft, reviewed=False)
            return HookDecision(notes=["verify_gate: revision budget spent; accepting"])

        verdict = await self._review(ctx, draft)
        state["verdict"] = verdict
        state["reviews"] += 1
        if verdict is None:
            state["fail_open"] += 1
            self._record(state, "unavailable", draft=draft)
            return HookDecision(notes=["verify_gate: reviewer unavailable; fail-open"])
        if verdict.get("pass", True):
            state["passes"] += 1
            self._record(state, "pass", verdict=verdict, draft=draft)
            return HookDecision(notes=["verify_gate: pass"])

        # dr@2.0's first measurement found this rubric had landed as prompt text
        # only: rejected_on_elided never appeared anywhere in a batch, because
        # nothing on the code side ever read the elision count the evidence pack
        # already computes. A rubric the code cannot honour is inert - the reviewer
        # is free to ignore it, and in the two recoverable items of the previous
        # batch it did: both drafts named the gold answer and were failed on every
        # bullet while 87% and 100% of the tool results in context had already been
        # replaced by the elision placeholder. When the pack had to skip elided
        # bodies, a rejection cannot be told apart from "the evidence was not shown
        # to me", so it is not evidence of an unsupported claim.
        if self._fail_open_on_elided_evidence and state.get("evidence_elided_in_context"):
            state["rejected_on_elided"] = state.get("rejected_on_elided", 0) + 1
            logger.warning(
                "verify-gate: reject arrived with %d elided tool results in context; "
                "degraded to pass (rejected_on_elided=%d)",
                state["evidence_elided_in_context"],
                state["rejected_on_elided"],
            )
            self._record(state, "degraded_elided", verdict=verdict, draft=draft)
            return HookDecision(notes=["verify_gate: reject degraded to pass (evidence elided)"])

        if self._strict_reject_only and not (verdict.get("unsupported_claims") or []):
            state["strict_overrides"] = state.get("strict_overrides", 0) + 1
            logger.warning(
                "verify-gate: reject named no unsupported claim; degraded to pass (strict_reject_only)"
            )
            self._record(state, "degraded_no_claim", verdict=verdict, draft=draft)
            return HookDecision(notes=["verify_gate: reject degraded to pass (no named claim)"])

        state["rejects"] += 1
        state["revisions"] += 1
        self._record(state, "reject", verdict=verdict, draft=draft)
        feedback = self._format_feedback(verdict)
        # The round is opened before the bounce, not after the model asks for it:
        # the tool has to be deep by the time the re-sample issues its first search,
        # and there is no seam between the re-sample and that call.
        evidence_round = self._evidence_round is not None
        if evidence_round:
            self._evidence_round.open()
            state["evidence_rounds"] = self._evidence_round.opened
        logger.warning(
            "verify-gate: draft rejected (%d unsupported claim(s)); bouncing back for "
            "revision %d/%d (evidence_round=%s)",
            len(verdict.get("unsupported_claims") or []),
            state["revisions"],
            self._max_revisions,
            evidence_round,
        )
        prompt = _EVIDENCE_ROUND_PROMPT if evidence_round else _REVISION_PROMPT
        return HookDecision(
            rollback=True,
            rollback_inject=[
                {"role": "assistant", "content": draft},
                {"role": "user", "content": prompt.format(feedback=feedback)},
            ],
            notes=[f"verify_gate: rejected, revision {state['revisions']}/{self._max_revisions}"],
        )

    async def _review(self, ctx: AgentHookContext, draft: str) -> dict | None:
        task = task_for(ctx)
        messages = ctx.messages or []
        evidence, elided_skipped = self._evidence_pack(messages)
        # Two different quantities, and only the second is a valid trigger.
        # elided_skipped counts bodies the pack walked PAST while filling its slots,
        # so a turn that kept working after an elision reports 0: measured on one
        # 120-item arm, 26 items carried the placeholder while only 6 had
        # elided_skipped>0, i.e. the carve-out reached 31-57% of the population it
        # was written for. elided_in_context counts the whole context, and it is a
        # per-review SNAPSHOT rather than a running total - the accumulating form is
        # a latch that degrades every later reject in the turn once any review has
        # skipped one body.
        elided_in_context = sum(
            1
            for m in messages
            if m.get("role") == "tool" and is_elided_tool_output(str(m.get("content") or ""))
        )
        if ctx.metadata is not None:
            state = ctx.metadata.setdefault("verify_gate", {})
            state["evidence_elided_in_context"] = elided_in_context
            if elided_skipped:
                state["evidence_elided_skipped"] = state.get("evidence_elided_skipped", 0) + elided_skipped
        user = f"Task:\n{task}\n\nDraft answer:\n{draft}\n\nEvidence:\n{evidence or '(no tool evidence was gathered)'}"
        # The budget is spent as short attempts, not one long wait: a call
        # stalled on a dead pooled connection never errors and never
        # returns, while a fresh attempt completes in seconds.
        deadline = asyncio.get_event_loop().time() + self._timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning(
                    "verify-gate: reviewer timed out after %.0fs budget; fail-open", self._timeout_seconds
                )
                return None
            try:
                response = await asyncio.wait_for(
                    self._provider.chat_with_retry(
                        messages=[
                            {"role": "system", "content": self._reviewer_system},
                            {"role": "user", "content": user},
                        ],
                        model=self._model,
                        max_tokens=self._max_tokens,
                        temperature=0.0,
                    ),
                    timeout=min(self._attempt_timeout_seconds, remaining),
                )
                break
            except asyncio.TimeoutError:
                logger.warning(
                    "verify-gate: reviewer attempt stalled past %.0fs; retrying on a fresh call",
                    self._attempt_timeout_seconds,
                )
                continue
            except Exception as exc:
                logger.warning("verify-gate: reviewer call failed (%s: %s); fail-open", type(exc).__name__, exc)
                return None
        if getattr(response, "finish_reason", "") == "length":
            logger.warning(
                "verify-gate: reviewer generation truncated at max_tokens=%d (thinking overrun?); fail-open",
                self._max_tokens,
            )
            return None
        text = visible_answer(getattr(response, "content", None) or "")
        if not text or getattr(response, "finish_reason", "") == "error":
            logger.warning("verify-gate: reviewer returned no usable content; fail-open")
            return None
        return self._parse_verdict(text)

    @staticmethod
    def _parse_verdict(text: str) -> dict | None:
        candidates = [text]
        brace_start, brace_end = text.find("{"), text.rfind("}")
        if 0 <= brace_start < brace_end:
            candidates.append(text[brace_start : brace_end + 1])
        for candidate in candidates:
            verdict = None
            try:
                import json_repair

                verdict = json_repair.loads(candidate)
            except Exception:
                try:
                    verdict = json.loads(candidate)
                except Exception:
                    continue
            if not isinstance(verdict, dict):
                continue
            gate = verdict.get("pass")
            if isinstance(gate, str) and gate.strip().lower() in ("true", "false"):
                gate = gate.strip().lower() == "true"
                verdict["pass"] = gate
            if isinstance(gate, bool):
                # The list fields are iterated without a type check downstream, so a
                # reviewer answering `"unsupported_claims": 3` makes the gate raise
                # mid-rejection. The hook chain treats a raised hook as a no-op, so the
                # rejection then disappears with nothing marking it as lost: the draft
                # ships and the reject counter never moves. Normalise here, where the
                # verdict shape is already being fixed up.
                #
                # A count is not a named claim, so it collapses to empty rather than to
                # a fake entry -- that keeps ``strict_reject_only`` honest, since its
                # whole rule is that an unnamed reject degrades to a pass.
                for key in ("unsupported_claims", "issues"):
                    value = verdict.get(key)
                    if value is None or isinstance(value, list):
                        continue
                    verdict[key] = [value] if isinstance(value, str) and value.strip() else []
                    logger.warning(
                        "verify-gate: reviewer returned %s as %s, not a list; normalised to %d entries",
                        key,
                        type(value).__name__,
                        len(verdict[key]),
                    )
                return verdict
        logger.warning("verify-gate: reviewer output missing boolean 'pass'; fail-open")
        return None

    def _evidence_pack(self, messages: list[dict]) -> tuple[str, int]:
        """Last ``evidence_items`` tool results that still carry a body.

        Elided bodies are skipped rather than counted. The loop keeps only the
        most recent few tool results verbatim when it has to shrink a turn to
        fit, which is strictly fewer than the number of items wanted here — so
        taking a blind tail meant that on exactly the long trajectories where
        review matters most, the reviewer was fact-checking placeholders and
        the tail-truncation below could not repair it. Returns the pack and how
        many elided bodies were passed over, so the skip is observable instead
        of looking like a short trajectory.
        """
        results = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        items: list[str] = []
        skipped = 0
        for m in reversed(results):
            if len(items) >= self._evidence_items:
                break
            content = str(m.get("content") or "")
            # dr@3.2: skip every harness-authored body here, not just elisions.
            # ⚠️ ``skipped`` feeds the published ``rejected_on_elided`` observer, so
            # widening what it counts would silently redefine a metric that already
            # has readings on disk. Count elisions separately from what is skipped.
            kind = harness_body_kind(content)
            if kind is not None:
                if kind == "tool_output_elided":
                    skipped += 1
                continue
            if len(content) > self._evidence_item_chars:
                content = content[-self._evidence_item_chars :]
            items.append(wrap_untrusted(content, source=str(m.get("name") or "tool")))
        items.reverse()
        return "\n\n".join(items), skipped

    @staticmethod
    def _format_feedback(verdict: dict) -> str:
        lines = []
        for claim in verdict.get("unsupported_claims") or []:
            lines.append(f"- unsupported claim: {claim}")
        for issue in verdict.get("issues") or []:
            lines.append(f"- {issue}")
        return "\n".join(lines) or "- the draft does not adequately answer the task"


__all__ = ["DraftReviewerGate"]
