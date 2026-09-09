"""Chain assembly: the fork's ``build_dr_flow`` observers on the trunk's hook seams.

``build_chain`` is a faithful port of the fork's observer assembly
(``raven/agent/flow/dr.py``): same conditions, same constructor kwargs, same
order, ``GatedHook`` / ``ClarifyExemptHook`` wrapping preserved. What the fork's
loop did around the observers - the turn-mode decision, the memo / brief /
reminder injections, the memo update, the process appendix, the pending-clarify
persistence - is one extra hook here, :class:`TurnFrame`, built into the same
chain.

:class:`ResearchFlowHook` is the single contributed hook. The fork ran one loop
per session, so a flow assembly was per-session by construction; the trunk runs
one runtime for every session, so this hook keys a lazily built chain by
``(session_key, mode)``, resolving the knobs as the base :class:`FlowConfig`
deep-merged with the session mode's ``drFlow`` overlay, and dispatches each
phase to a :class:`~raven.agent.hook.composite.CompositeHook` over the chain so
short-circuit / rollback / note chaining semantics are the loop's own.
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent.hook.composite import CompositeHook
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from research_flow.config import FlowConfig
from research_flow.gates.ask_user import (
    AskUserGate,
    ClarifyExemptHook,
    PendingClarify,
    clarify_verdict,
    is_reply_to,
    render_brief,
    reply_overlap,
    set_chain_round,
    set_clarify_verdict,
    set_first_turn,
    set_turn_brief,
    take_pending_clarify,
    turn_brief,
)
from research_flow.gates.budget_note import BudgetNoteObserver
from research_flow.gates.conversation import (
    ConversationGate,
    GatedHook,
    ResearchMemo,
    TurnMode,
    is_research_turn,
    prior_sources,
    set_prior_sources,
    set_research_turn,
)
from research_flow.gates.evidence_floor import EvidenceFloorGate
from research_flow.gates.fetch_floor import FetchFloorObserver
from research_flow.gates.fetch_gate import FetchGateObserver
from research_flow.gates.final_shape import shape_final_answer
from research_flow.gates.finalize import ForcedFinalizeGate
from research_flow.gates.plain_first import (
    PLAIN_TURN_SOURCE,
    PlainFirstGate,
    PlainScopedReview,
    PlainTurnGate,
    set_plain_turn,
)
from research_flow.gates.report_shape import ReportShape, ReportShapeGate, render_reminder
from research_flow.gates.spin_breaker import SpinEntryBreaker
from research_flow.gates.sufficiency import SufficiencyGate
from research_flow.gates.verify import DraftReviewerGate
from research_flow.state import SessionStore
from research_flow.support.evidence_round import EvidenceRound
from research_flow.support.fetch_gate_core import FetchGate
from research_flow.support.ledger import close_product_ledger, ledger_path, open_product_ledger
from research_flow.support.process_appendix import build_appendix, read_ledger
from research_flow.support.search_saturation import SearchSaturation
from research_flow.support.turn_observers import turn_observers
from research_flow.tools.web import set_current_session

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider

# Per-turn ledger file token, joined with the pid: one runtime serves many
# concurrent turns, and the token in the filename is what joins a row back to
# its turn (same scheme as the fork's loop).
_TURN_SEQ = itertools.count()

# ``ctx.metadata`` is ONE dict per turn on this host: the loop seeds it at
# the inbound fire and hands the same dict to the iteration run (where it
# stamps ``mode`` / ``mode_overlay``) and to the outbound -- pinned by the
# trunk's ``test_metadata_is_one_dict_across_all_three_phase_groups`` -- and
# it is fresh every turn, whatever the origin. Per-turn state the plugin
# hands from one phase group to another rides that dict under the keys
# below, each read ONCE and popped, for the reason ``take_pending_clarify``
# states: ``before_user_inbound`` is skipped for some turn origins, and a
# value that survived by accident would classify the previous turn's
# question or stamp its verdict on this turn's record. (The gates keep their
# own cross-phase state in ContextVars -- gates/conversation.py -- under
# that file's rules.)

# What the USER wrote, before the memo / brief / reminder were folded in.
# ``ctx.turn_question`` is read after the inbound rewrite, so by then it
# carries our own blocks and the classifier would be reading them.
_USER_TEXT_KEY = "dr_user_text"

# The turn-mode decision, handed from the iteration phase that makes it to
# the turn-end stamp that records it.
_TURN_MODE_KEY = "dr_turn_mode"


@dataclass
class ToolHandles:
    """The plugin's own tool instances, handed to the chain that steers them."""

    web_search: Any | None = None
    web_fetch: Any | None = None
    ask_user: Any | None = None


@dataclass
class SessionGear:
    """One session's chain-owned tool companions.

    The fork built one ``EvidenceRound`` / ``SearchSaturation`` per flow
    assembly and shared the instance between the gates and the search tool
    ("one instance, three holders"). Here the chain owns them and registers
    them in a shared per-session map; the tool factories close over
    ``current_session()`` and look the instances up here, so the gate that
    opens a round and the tool that spends it still hold the same object.
    """

    evidence_round: EvidenceRound | None = None
    saturation: SearchSaturation | None = None
    # The digest knobs a mode may move. ``web_fetch`` is built once from the
    # base config while the chain is rebuilt per (session, mode), so the tool
    # reads these off the session's gear at call time; ``None`` for the model
    # means the provider's default, exactly as ``DigestConfig.model`` does.
    digest_model: str | None = None
    digest_verbatim_head_chars: int = 0


def evidence_round_for(config: FlowConfig) -> EvidenceRound | None:
    """The fork's evidence-round construction, verbatim conditions."""
    if config.verify.enabled and config.verify.evidence_round:
        return EvidenceRound(
            depth=config.verify.evidence_round_depth,
            searches=config.verify.evidence_round_searches,
        )
    return None


def saturation_for(config: FlowConfig) -> SearchSaturation | None:
    """The fork's saturation construction, verbatim conditions."""
    if config.search.saturation.enabled:
        return SearchSaturation(
            k=config.search.saturation.k,
            identity_key=config.search.saturation.identity,
            on_saturate=config.search.saturation.on_saturate,
            max_pages=config.search.saturation.max_pages,
        )
    return None


class TurnFrame(AgentHook):
    """The turn-boundary work the fork's loop did outside its observers.

    ``before_user_inbound`` resets the turn-scoped ContextVars, consumes an open
    clarify round from the session store, and rewrites the inbound text (memo
    prepended, brief prepended outermost, report reminder appended).
    ``before_iteration`` decides, once per turn, whether this is a research turn
    - the decision needs prior history, which ``before_user_inbound`` does not
    carry. ``after_send`` folds the turn's ledger rows into the memo, appends
    the process appendix to the outbound, persists a clarify round the gate
    opened, and saves the session record.

    First in the chain, never wrapped in ``GatedHook``: it is the hook that
    SETS the research flag every wrapped observer's predicate reads.
    """

    def __init__(
        self,
        cfg: FlowConfig,
        store: SessionStore,
        gate: ConversationGate | None,
        mode: str = "",
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._gate = gate
        self._mode = mode
        # Resolved once, the way the fork's assembly resolved them: a feature
        # switch read alone downstream cannot then act on a surface that is off.
        self._conversation = cfg.conversation.enabled
        self._research_memo = cfg.conversation.enabled and cfg.conversation.research_memo
        self._report_reminder = cfg.final_shape.report_structure and cfg.final_shape.report_reminder
        self._process_appendix = cfg.final_shape.process_appendix
        self._ask_user_brief = cfg.ask_user_on and cfg.ask_user.brief

    @property
    def name(self) -> str:
        return "TurnFrame"

    # -- turn entry -----------------------------------------------------

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        # Cleared first and unconditionally, before any early return: a turn
        # that inherited the previous one's chain count would be refused a
        # legitimate first question, and a turn that inherited a brief would
        # prepend a stale Q/A pair to an unrelated question. Both failures are
        # silent. ``prior_sources`` is cleared for the reason its own comment
        # states: a stale list would absolve a citation to a page nobody opened.
        set_chain_round(0)
        set_turn_brief("")
        set_clarify_verdict(None)
        set_first_turn(False)
        set_plain_turn(False)
        set_prior_sources(())
        ctx.metadata.pop(_TURN_MODE_KEY, None)
        text = ctx.inbound_content or ""
        ctx.metadata[_USER_TEXT_KEY] = text
        record = self._store.load(ctx.session_key)
        if self._conversation:
            self._consume_pending_clarify(ctx.session_key, record, text)
        content = text
        if self._research_memo:
            memo = ResearchMemo.from_metadata(record.research_memo)
            # Recorded before the render, and independently of whether the block
            # ends up injected: "did this conversation open that page" is a fact
            # about the conversation, not about whether we showed the list.
            set_prior_sources(tuple(memo.opened))
            block = memo.render(max_chars=self._cfg.conversation.memo_max_chars)
            if block:
                content = f"{block}\n\n{content}"
        # The brief is the OUTERMOST prefix - injected after the memo, exactly
        # as the fork ordered its three injection call sites.
        brief = turn_brief()
        if brief:
            content = f"{brief}\n\n{content}"
        if self._report_reminder:
            # The turn's own text, not the assembled content: the checklist reads the
            # request, and the memo and brief prefixed above are ours, not the user's.
            content = f"{content}\n\n{render_reminder(text)}"
        if content != text:
            return HookDecision(modified_content=content)
        return HookDecision()

    def _consume_pending_clarify(self, session_key: str, record: Any, text: str) -> None:
        """Take this session's open clarify round, if any, and decide what it means.

        Popped, not read: exactly one turn may consume a pending, and the pop is
        the only place that guarantee lives - the record is saved back without
        it before anything else happens.
        """
        raw = record.pending_clarify
        if raw is None:
            return
        record.pending_clarify = None
        record.chain_round = 0
        self._store.save(session_key, record)
        pending = PendingClarify.from_metadata(raw)
        if pending is None:
            return
        # The chain count passes THROUGH the consume rather than resetting -
        # zeroing here would make ``maxRounds > 1`` structurally unreachable.
        set_chain_round(pending.chain_round)
        answered = True
        if self._cfg.ask_user.brief_requires_answer_check:
            answered = is_reply_to(pending, text, threshold=self._cfg.ask_user.reply_overlap_threshold)
        # The overlap is recorded, not just used: ``pending_verdict`` must be
        # re-computable offline from the session record.
        set_clarify_verdict(
            "answered" if answered else "new_request",
            round(reply_overlap(pending, text), 4),
        )
        if not answered:
            set_chain_round(0)
            logger.info("ask_user: pending clarify abandoned - the message reads as a new request")
            return
        if self._ask_user_brief:
            set_turn_brief(render_brief(pending, text))

    # -- turn-mode decision ----------------------------------------------

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        """Set this turn's research flag. Turn one by config, later turns by gate.

        Runs at iteration 1 rather than in ``before_user_inbound`` because the
        gate needs the conversation so far, and only the iteration phases carry
        ``ctx.messages``; everything before ``ctx.turn_base`` is prior history.
        The flag it sets defaults to True, so with the feature off nothing
        downstream can tell this method exists.
        """
        if ctx.iteration != 1 or not self._conversation:
            return HookDecision()
        # The filed record, not the trimmed window: the fork's gate read the
        # session's own record, and judging on the post-compaction window makes
        # a long session's gate (and the first-turn detection below) see a
        # stump. The iteration context carries that record natively (hook
        # surface v3); the window slice stays the fallback for a host that
        # populates none.
        source = list(ctx.session_history) if ctx.session_history else (ctx.messages or [])[: ctx.turn_base]
        prior = [m for m in source if m.get("role") in ("user", "assistant")]
        # ``askUser.mode="first_turn"`` mandates a clarify round on exactly this
        # turn, and the gate cannot see it from a session_key alone.
        set_first_turn(not prior)
        if not prior:
            # The first message is the one the user opened the product to ask,
            # and there is no conversation for a classifier to read.
            mode = TurnMode(True, "first_turn")
        elif (_verdict := clarify_verdict()) is not None and _verdict[0] == "answered":
            # This message answers questions THIS flow asked before researching,
            # so the research it was gating has not happened yet. Consult the
            # commit marker rather than letting the classifier re-derive it.
            mode = TurnMode(True, "clarify_answer")
        elif self._gate is None:
            mode = TurnMode(True, "config_always")
        else:
            text = str(ctx.metadata.pop(_USER_TEXT_KEY, None) or "") or ctx.turn_question
            mode = await self._gate.decide(text, prior)
        set_research_turn(mode.research)
        # The gate's third verdict, read by the plain-first gate one hook later in
        # the same phase. Only ``PlainTurnGate`` ever emits the source.
        set_plain_turn(mode.source == PLAIN_TURN_SOURCE)
        ctx.metadata[_TURN_MODE_KEY] = mode
        if not mode.research:
            logger.info("conversation-gate: answering from context ({}) - {}", mode.source, mode.why)
        return HookDecision()

    # -- turn exit --------------------------------------------------------

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        record = self._store.load(ctx.session_key)
        # The plugin's own handoff keys come off before the counters are read:
        # they are phase-to-phase freight, not a gate's measurement.
        turn_mode = ctx.metadata.pop(_TURN_MODE_KEY, None)
        ctx.metadata.pop(_USER_TEXT_KEY, None)
        # The gates' own counters first, the turn-level ones over them - the
        # order the fork stamped them in, and the one that lets a turn-level key
        # win a name collision.
        observers: dict[str, Any] = turn_observers(ctx.metadata)
        final_content = ctx.outbound_content or ""
        changed = False

        if self._cfg.final_shape.record:
            # Read-only, recorded beside the raw answer, never instead of it.
            # The fork additionally waived the closing-tag bar when the final
            # response's reasoning arrived out-of-band; that per-response fact
            # never reaches ``after_send``, so the configured bar is used as is.
            shaped = shape_final_answer(
                final_content,
                closing_tag_required=self._cfg.think_closing_tag_required,
            )
            observers["final_shape"] = shaped.counters()
            if self._cfg.final_shape.report_structure or self._cfg.final_shape.record:
                observers["report_shape"] = {
                    "expected": bool(self._cfg.final_shape.report_structure),
                    **ReportShape(shaped.visible).counters(),
                }

        mode = turn_mode
        if isinstance(mode, TurnMode):
            observers["conversation_gate"] = mode.counters()

        # A turn the conversation gate answered from context did no research, so
        # an appendix would report an empty trail as though it were the record
        # behind the reply. The memo is NOT gated on the research flag: a
        # non-research turn opens no pages, so its rows are empty and the merge
        # is a no-op except for the turn counter - but the ledger file still has
        # to be read under the same condition it was opened.
        research = is_research_turn()
        wants_appendix = research and self._process_appendix
        wants_memo = self._research_memo
        if wants_appendix or wants_memo or self._process_appendix:
            path = ledger_path()
            rows = read_ledger(path) if path else []
            if wants_memo:
                memo = ResearchMemo.from_metadata(record.research_memo)
                memo.merge_ledger(
                    rows,
                    max_sources=self._cfg.conversation.memo_max_sources,
                    max_queries=self._cfg.conversation.memo_max_queries,
                    max_opened=self._cfg.conversation.memo_max_opened,
                )
                if not memo.empty:
                    record.research_memo = memo.to_metadata()
            if wants_appendix:
                appendix, trail = build_appendix(path, final_content, prior_sources())
                observers["process_appendix"] = trail
                if appendix:
                    # Rides on the outbound reply only: the persisted transcript
                    # never carries it, so no model reads it this turn or as
                    # history next turn.
                    final_content = final_content.rstrip() + "\n" + appendix
                    changed = True
                    # And the rendered trail parked where a HOST can reach it.
                    # The line above is the whole of the appendix's distribution
                    # neutrality, and it is also why a caller reading the session
                    # record - the only machine-readable channel this product has
                    # - could otherwise not see the trail at all: the turn's
                    # ledger is deleted moments later. Kept OUT of
                    # ``process_appendix``, which is counters a reader parses as
                    # a measurement payload; a multi-kilobyte string does not
                    # belong beside them.
                    observers["research_trail"] = appendix

        # ``take_*`` clears as it reads. Without that a later turn that asked
        # nothing would re-persist the previous turn's pending and answer it twice.
        pending = take_pending_clarify()
        if pending:
            pending["asked_at"] = datetime.now().isoformat()
            record.pending_clarify = pending
            try:
                record.chain_round = int(pending.get("chain_round") or 1)
            except (TypeError, ValueError):
                record.chain_round = 1
        # The chain's own mode, not the context's metadata: the chain IS what
        # governed the turn, and a host that hands a bare send context still
        # gets the right label.
        record.mode = self._mode
        record.observers = observers
        # The trunk stamps this dict onto the turn's last substantive assistant
        # message at persist -- the seam the fork's loop had natively -- so the
        # per-turn history survives beyond this record's latest-turn copy. The
        # stamp is copied after this phase returns, so it reads the dict as
        # finished here.
        if observers:
            ctx.metadata["observers"] = observers
        self._store.save(ctx.session_key, record)
        if changed:
            return HookDecision(modified_content=final_content)
        return HookDecision()


def build_chain(
    cfg: FlowConfig,
    provider: "LLMProvider | None",
    *,
    max_iterations: int,
    context_window_tokens: int,
    tools: ToolHandles,
    store: SessionStore,
    evidence_round: EvidenceRound | None = None,
    mode: str = "",
) -> list[AgentHook]:
    """The fork's ``build_dr_flow`` observer assembly, as one hook chain.

    ``max_iterations`` / ``context_window_tokens`` are the resolved loop values
    the fork's assembly received; ``cfg.max_iterations`` / ``cfg.context_window_tokens``
    override them for this chain only, exactly as the fork's ``effective_*`` did.
    ``evidence_round`` defaults to a fresh instance built from ``cfg`` (the
    fork's conditions); the caller that shares it with the search tool passes
    the shared instance in. The saturation rule has no chain-side holder - the
    hook builds it with :func:`saturation_for` straight into the session gear.
    An LLM-backed gate whose provider is absent is skipped with a warning,
    never built to crash. ``mode`` names the session profile this chain was
    resolved for; the chain records it, it does not read it.
    """
    effective_iterations = cfg.max_iterations or max_iterations
    effective_window = cfg.context_window_tokens or context_window_tokens

    if evidence_round is None:
        evidence_round = evidence_round_for(cfg)

    ask_user_on = cfg.ask_user_on
    if ask_user_on and cfg.prompt_section_override:
        logger.warning(
            "drFlow.askUser is on but promptSectionOverride replaces the contract, "
            "so the ask_user clause is not rendered; the tool is registered without "
            "it being asked for"
        )
    if (
        ask_user_on
        and cfg.ask_user.outline
        and cfg.final_shape.report_structure
        and not cfg.final_shape.report_reminder
    ):
        logger.warning(
            "drFlow.askUser.outline is on with finalShape.reportStructure on and "
            "finalShape.reportReminder off - the measured bad combination (2/9 "
            "well-formed on outline-carrying history); read the ask rate stratified "
            "by reportReminder"
        )
    if ask_user_on and tools.ask_user is None:
        logger.warning(
            "research-flow: askUser is on for this chain but no ask_user tool was "
            "contributed at activation (the base config had it off); the clause "
            "may name a tool the model cannot call"
        )

    def _skip(gate: str) -> None:
        logger.warning(
            "research-flow: {} is enabled but no provider was lent to the plugin; skipping the gate",
            gate,
        )

    gate = None
    if cfg.conversation.enabled and cfg.conversation.gate == "agentic":
        if provider is None:
            _skip("conversation.gate=agentic")
        else:
            # The plain verdict is offered only when there is a plain-first gate to
            # act on it; otherwise the two-key prompt stays byte for byte.
            gate_cls = PlainTurnGate if cfg.plain_first.enabled else ConversationGate
            gate = gate_cls(
                provider,
                model=cfg.conversation.gate_model,
                max_tokens=cfg.conversation.gate_max_tokens,
                timeout_seconds=cfg.conversation.gate_timeout_seconds,
                history_messages=cfg.conversation.gate_history_messages,
                history_chars=cfg.conversation.gate_history_chars,
                reasoning_effort=cfg.conversation.gate_reasoning_effort,
            )

    # Observer order is part of the flow contract: note appenders first, then
    # the spin breaker (a restart must be intercepted before the terminal gates
    # see it), then ForcedFinalizeGate ahead of the evidence floor ahead of the
    # reviewer - an answerless terminal is salvaged, never reviewed, and a draft
    # below the floor is bounced, never reviewed.
    observers: list[AgentHook] = []
    if cfg.plain_first.enabled:
        # First in the chain: it decides, on the first model call, whether there is a
        # research turn at all, and on the first response whether the plain answer
        # stands - before any terminal gate reads that response as a draft.
        if cfg.plain_first.judge and provider is None:
            _skip("plainFirst.judge")
        plain: AgentHook = PlainFirstGate(
            provider if cfg.plain_first.judge else None,
            judge_model=cfg.plain_first.judge_model,
            judge_timeout_seconds=cfg.plain_first.judge_timeout_seconds,
            judge_max_tokens=cfg.plain_first.judge_max_tokens,
            judge_reasoning_effort=cfg.plain_first.judge_reasoning_effort,
            closing_tag_required=cfg.think_closing_tag_required,
        )
        if ask_user_on:
            plain = ClarifyExemptHook(plain)
        observers.append(plain)
    if cfg.budget_note.enabled:
        observers.append(
            BudgetNoteObserver(
                max_iterations=effective_iterations,
                context_window_tokens=effective_window,
                warn_ratio=cfg.budget_note.warn_ratio,
            )
        )
    if cfg.fetch_floor.enabled:
        observers.append(
            FetchFloorObserver(
                min_searches=cfg.fetch_floor.min_searches,
                max_notes=cfg.fetch_floor.max_notes,
            )
        )
    if cfg.fetch_gate.enabled:
        observers.append(
            FetchGateObserver(
                FetchGate(
                    k=cfg.fetch_gate.k,
                    release_after_failed_fetches=cfg.fetch_gate.release_after_failed_fetches,
                    release_after_closed_iterations=cfg.fetch_gate.release_after_closed_iterations,
                )
            )
        )
    if cfg.sufficiency.enabled:
        if provider is None:
            _skip("sufficiency")
        else:
            observers.append(
                SufficiencyGate(
                    provider,
                    model=cfg.sufficiency.model,
                    min_searches=cfg.sufficiency.min_searches,
                    min_fetches=cfg.sufficiency.min_fetches,
                    judge_listing=cfg.sufficiency.judge_listing,
                    timeout_seconds=cfg.sufficiency.timeout_seconds,
                    attempt_timeout_seconds=cfg.sufficiency.attempt_timeout_seconds,
                    max_tokens=cfg.sufficiency.max_tokens,
                    reasoning_effort=cfg.sufficiency.reasoning_effort,
                    evidence_items=cfg.sufficiency.evidence_items,
                    evidence_item_chars=cfg.sufficiency.evidence_item_chars,
                )
            )
    if cfg.spin_breaker.enabled:
        observers.append(
            SpinEntryBreaker(
                max_iterations=effective_iterations,
                context_window_tokens=effective_window,
                phrase_hits=cfg.spin_breaker.phrase_hits,
                min_budget_ratio=cfg.spin_breaker.min_budget_ratio,
                min_entity_overlap=cfg.spin_breaker.min_entity_overlap,
                max_triggers=cfg.spin_breaker.max_triggers,
                evidence_round=evidence_round,
            )
        )
    if cfg.force_finalize.enabled:
        if provider is None:
            _skip("forceFinalize")
        else:
            finalizer: AgentHook = ForcedFinalizeGate(
                provider,
                model=cfg.force_finalize.model,
                max_nudges=cfg.force_finalize.max_nudges,
                timeout_seconds=cfg.force_finalize.timeout_seconds,
                attempt_timeout_seconds=cfg.force_finalize.attempt_timeout_seconds,
                max_tokens=cfg.force_finalize.max_tokens,
                evidence_items=cfg.force_finalize.evidence_items,
                evidence_item_chars=cfg.force_finalize.evidence_item_chars,
                reasoning_excerpt_chars=cfg.force_finalize.reasoning_excerpt_chars,
                closing_tag_required=cfg.think_closing_tag_required,
                reasoning_effort=cfg.force_finalize.reasoning_effort,
            )
            # The third terminal gate, and the one the commit marker cannot
            # reach: a prose clarify is "answerless" to it, so it is exempted
            # whenever the clarify round exists.
            if ask_user_on:
                finalizer = ClarifyExemptHook(finalizer)
            observers.append(finalizer)
    if cfg.evidence_floor.enabled:
        floor: AgentHook = EvidenceFloorGate(
            min_pages=cfg.evidence_floor.min_pages,
            min_domains=cfg.evidence_floor.min_domains,
            max_rollbacks=cfg.evidence_floor.max_rollbacks,
            closing_tag_required=cfg.think_closing_tag_required,
        )
        # A prose clarify reads as a draft here too, and more pages are not the
        # answer to "which sense of the term did you mean".
        if ask_user_on:
            floor = ClarifyExemptHook(floor)
        observers.append(floor)
    if cfg.verify.enabled:
        if provider is None:
            _skip("verify")
        else:
            reviewer: AgentHook = DraftReviewerGate(
                provider,
                model=cfg.verify.model,
                timeout_seconds=cfg.verify.timeout_seconds,
                attempt_timeout_seconds=cfg.verify.attempt_timeout_seconds,
                attempt_http_timeout_seconds=cfg.verify.attempt_http_timeout_seconds,
                max_revisions=cfg.verify.max_revisions,
                review_final_draft=cfg.verify.review_final_draft,
                max_tokens=cfg.verify.max_tokens,
                reasoning_effort=cfg.verify.reasoning_effort,
                constraint_rubric=cfg.verify.constraint_rubric,
                strict_reject_only=cfg.verify.strict_reject_only,
                fail_open_on_elided_evidence=cfg.verify.fail_open_on_elided_evidence,
                evidence_round=evidence_round,
                closing_tag_required=cfg.think_closing_tag_required,
            )
            # An accepted plain answer carries no evidence for this reviewer to
            # check and the judge has already read it; the scoped wrapper ships it
            # and hands every researched draft straight through.
            if cfg.plain_first.enabled and cfg.plain_first.review == "skip":
                reviewer = PlainScopedReview(reviewer, closing_tag_required=cfg.think_closing_tag_required)
            # A clarify the model wrote as prose instead of calling the tool
            # arrives here as an ordinary draft, and the reviewer rejects it
            # for being one.
            if ask_user_on:
                reviewer = ClarifyExemptHook(reviewer)
            observers.append(reviewer)

    # Wrapping happens here, after every observer is built, so the gate covers
    # the whole observer set by construction - an observer added later cannot
    # forget to opt in. Inert when the feature is off: the predicate reads a
    # ContextVar that defaults True and that nothing sets unless
    # ``conversation.enabled``.
    if cfg.conversation.enabled:
        observers = [GatedHook(o, is_research_turn) for o in observers]

    # Deliberately outside the wrap above: the stratum this bar exists for IS
    # the non-research turn, and wrapping it would gate it out of the only
    # place it was built for.
    if cfg.final_shape.report_structure and cfg.final_shape.report_bounce:
        bar: AgentHook = ReportShapeGate(closing_tag_required=cfg.think_closing_tag_required)
        # Same exemption as the reviewer's: a clarify has no report sections to
        # be missing, and demanding them turns the one turn that must not
        # answer into a rewrite.
        if ask_user_on:
            bar = ClarifyExemptHook(bar)
        observers.append(bar)

    # The second observer outside the wrap, for the opposite reason: it has to
    # run on a NON-research turn, because that is the turn where ``ask_user``
    # must be taken out of the schema.
    if ask_user_on:
        observers.append(
            AskUserGate(
                mode=cfg.ask_user.mode,
                max_rounds=cfg.ask_user.max_rounds,
                first_iteration_only=cfg.ask_user.first_iteration_only,
                max_questions=cfg.ask_user.max_questions,
                max_outline_items=cfg.ask_user.max_outline_items,
                outline=cfg.ask_user.outline,
                delivery=cfg.ask_user.delivery,
                tool=tools.ask_user,
            )
        )

    # First, never wrapped: it sets the research flag every GatedHook predicate
    # reads, and its ``after_send`` must see the outbound before the chain ends.
    return [TurnFrame(cfg, store, gate, mode), *observers]


@dataclass
class _ChainSlot:
    """One (session, mode) chain and the facts the hook needs beside it."""

    mode: str
    cfg: FlowConfig
    composite: CompositeHook
    opens_ledger: bool


@dataclass
class ResearchFlowHook(AgentHook):
    """The single contributed hook: per-(session, mode) chains, six phases.

    ``session_gear`` is the shared per-session map the web-tool factories read
    (see :class:`SessionGear`); the hook writes a session's instances into it
    when it builds that session's chain, and drops them - together with the
    tools' per-session slots - when the session switches mode, so the next
    turn's tool state is rebuilt against the new chain's instances.

    Only an ITERATION context is guaranteed to name the session's mode: the
    loop stamps ``mode`` / ``mode_overlay`` at its entry, after the inbound
    fire. A chain is therefore built from an iteration context alone, and the
    other two phases reuse the one the session's last iteration resolved -
    otherwise every turn built a second, base-config chain for the rewrite,
    threw the real one away, and ran the turn's exit under the wrong knobs.
    """

    #: Six gates in the chain this hook delegates to answer ``rollback``. The
    #: loop reads the flag off this class before any session chain exists, so it
    #: cannot be derived from the members that carry the behaviour.
    rolls_back_iterations = True

    cfg: FlowConfig
    provider: "LLMProvider | None"
    tools: ToolHandles
    store: SessionStore
    max_iterations: int = 40
    context_window_tokens: int = 0
    session_gear: dict[str, SessionGear] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._chains: dict[tuple[str, str], _ChainSlot] = {}
        self._session_mode: dict[str, str] = {}
        self._current: dict[str, _ChainSlot] = {}
        self._unmoded: _ChainSlot | None = None

    @property
    def name(self) -> str:
        return "ResearchFlowHook"

    def _resolve(self, ctx: AgentHookContext) -> _ChainSlot:
        """This turn's chain.

        ``"mode" in metadata`` is the test, not a truthy mode: a deployment
        that declares no modes reports ``""`` as its mode on the iteration
        contexts, and that is a real answer, while the absent key means the
        phase simply is not told.
        """
        if "mode" not in (ctx.metadata or {}):
            return self._reuse(ctx)
        key = ctx.session_key
        mode = str(ctx.metadata.get("mode") or "")
        prev = self._session_mode.get(key)
        if prev is not None and prev != mode:
            # The mode changed between turns: the old chain's knobs no longer
            # describe this session, and the tools' per-session slots hold the
            # old chain's evidence round / saturation instances.
            self._chains.pop((key, prev), None)
            self.session_gear.pop(key, None)
            for tool in (self.tools.web_search, self.tools.web_fetch):
                forget = getattr(tool, "forget_session", None)
                if callable(forget):
                    forget(key)
        self._session_mode[key] = mode
        slot = self._chains.get((key, mode))
        if slot is None:
            overlay = ctx.metadata.get("mode_overlay") or {}
            dr_diff = overlay.get("drFlow") or {}
            try:
                cfg = self.cfg.with_overlay(dr_diff)
            except Exception as e:  # noqa: BLE001 - a bad overlay must not kill the turn
                # Unreachable for a shipped overlay: the launcher validates every
                # mode at render time and refuses to start on this. What can still
                # arrive here is a hand-edited rendered config, and a turn that
                # runs is still better than one that dies -- but at error level,
                # because the session now runs a profile its mode label denies.
                logger.error(
                    "research-flow: mode {!r} overlay rejected ({}); running the base config",
                    mode,
                    e,
                )
                cfg = self.cfg
            # The loop's own enforced cap and resolved window (hook surface
            # v3), when a turn is driving; the activation values stand in for
            # a host that stamps neither. Explicit config wins over both
            # inside build_chain, so the shipped numbers do not move.
            max_iterations = self.max_iterations
            if isinstance(ctx.max_iterations, int) and ctx.max_iterations > 0:
                max_iterations = ctx.max_iterations
            window = self.context_window_tokens
            if isinstance(ctx.context_window_tokens, int) and ctx.context_window_tokens > 0:
                window = ctx.context_window_tokens
            er = evidence_round_for(cfg)
            sat = saturation_for(cfg)
            self.session_gear[key] = SessionGear(
                evidence_round=er,
                saturation=sat,
                digest_model=cfg.digest.model,
                digest_verbatim_head_chars=cfg.digest.verbatim_head_chars,
            )
            observers = build_chain(
                cfg,
                self.provider,
                max_iterations=max_iterations,
                context_window_tokens=window,
                tools=self.tools,
                store=self.store,
                evidence_round=er,
                mode=mode,
            )
            slot = _ChainSlot(
                mode=mode,
                cfg=cfg,
                composite=CompositeHook(observers),
                opens_ledger=(
                    cfg.final_shape.process_appendix or (cfg.conversation.enabled and cfg.conversation.research_memo)
                ),
            )
            self._chains[(key, mode)] = slot
        self._current[key] = slot
        return slot

    def _reuse(self, ctx: AgentHookContext) -> _ChainSlot:
        """The chain for a phase whose context does not name the mode.

        The session's last iteration resolved one, and within a turn that IS
        this turn's chain - so ``after_send`` runs the mode's gates and closes
        the ledger the mode's chain opened, and the next turn's inbound rewrite
        runs the mode the session is on.

        Before any turn of this process has reached an iteration there is
        nothing to reuse, and the base config stands in for that one rewrite.
        Kept out of ``_chains`` so it can never be served to an iteration phase
        as if it were a mode's chain, and it does not claim the session's mode:
        a slot built from an overlay nobody handed us is not that mode's chain.
        """
        slot = self._current.get(ctx.session_key)
        if slot is not None:
            return slot
        if self._unmoded is None:
            cfg = self.cfg
            self._unmoded = _ChainSlot(
                mode="",
                cfg=cfg,
                composite=CompositeHook(
                    build_chain(
                        cfg,
                        self.provider,
                        max_iterations=self.max_iterations,
                        context_window_tokens=self.context_window_tokens,
                        tools=self.tools,
                        store=self.store,
                    )
                ),
                opens_ledger=(
                    cfg.final_shape.process_appendix or (cfg.conversation.enabled and cfg.conversation.research_memo)
                ),
            )
        return self._unmoded

    def _start_turn(self, slot: _ChainSlot) -> None:
        """What the fork's loop did at the top of every turn, tool side."""
        cfg = slot.cfg
        if self.tools.web_search is not None:
            # Repeat detection is scoped to the turn; ``identityScope="topic"``
            # narrows that to the budgets for a product conversation. Forced
            # back to turn scope whenever the conversation surface is off,
            # exactly as the fork's assembly resolved it.
            keep = cfg.conversation.enabled and cfg.conversation.identity_scope == "topic"
            self.tools.web_search.start_turn(keep_identities=keep)
        if self.tools.web_fetch is not None:
            self.tools.web_fetch.start_turn()
        if slot.opens_ledger:
            open_product_ledger(f"{os.getpid()}-{next(_TURN_SEQ)}")

    # -- phases -----------------------------------------------------------

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        slot = self._resolve(ctx)
        return await slot.composite.before_user_inbound(ctx)

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        slot = self._resolve(ctx)
        # The tool coroutines run in this turn's task: selecting the session
        # here scopes every tool call of the turn to its own per-session state.
        set_current_session(ctx.session_key)
        if ctx.iteration == 1:
            self._start_turn(slot)
        return await slot.composite.before_iteration(ctx)

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return await self._resolve(ctx).composite.before_execute_tools(ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._resolve(ctx).composite.after_iteration(ctx)

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        return await self._resolve(ctx).composite.terminal_answerless(ctx)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        slot = self._resolve(ctx)
        try:
            return await slot.composite.after_send(ctx)
        finally:
            # In ``finally`` so a failed render still releases the per-turn
            # file - otherwise one raising turn leaves a trail on disk that no
            # later turn reads.
            if slot.opens_ledger:
                close_product_ledger()


__all__ = [
    "ResearchFlowHook",
    "SessionGear",
    "ToolHandles",
    "TurnFrame",
    "build_chain",
    "evidence_round_for",
    "saturation_for",
]
