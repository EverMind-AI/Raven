"""AgentHook ABC + AgentHookContext + HookDecision.

Factory-loop tier: Versioned with the factory loop, not frozen for every
loop — a replacement loop may ship its own hook vocabulary and version this
paper with it. Only the ``contract`` tier is a cross-loop promise.

The hook chain is the loop's one extension door: the host hands finished
hooks and the loop fires six phases -- one on the way in, three per ReAct
iteration, one when a turn ends with nothing to show, one on the way out.
eval_engine builds three concrete hooks on the iteration phases; a product
that needs to steer the loop (budget notes, forced finalization, spin
breaking) builds its own on the same six.

Design choices:

1. **All phases default to no-op.** Every method on ``AgentHook``
   returns a pass-through ``HookDecision()`` unless overridden. Subclasses
   only implement the phases they care about.

2. **Async everywhere.** Even phases that look synchronous today (e.g.
   ``response_modifier`` takes ``(str, str) -> str``) become ``async``
   here so an LLM-based hook (Eval judge, personalizer classifier) can
   be added without redoing the interface. Sync callers can simply
   ``return`` immediately.

3. **Four orthogonal return modes**, all expressed via
   ``HookDecision``:

   - *pass-through* — default. Let the next hook / main loop continue.
   - *short_circuit_result* — halt the chain and the AgentLoop returns
     this value as the outbound reply (or processes it as a final
     answer, depending on the phase). Used by the Sentinel decision
     consumer and the personalizer "ask a clarification question" branch.
   - *modified_content* — for ``after_send``: transform the outbound
     text. Used by Sentinel's nudge_inject (append nudge to reply).
   - *rollback* — for the iteration phases: discard what this iteration
     appended and re-sample the LLM call without consuming an iteration,
     optionally with injected messages the re-sample sees and generation
     overrides for that one call. How a gate bounces a draft back.

   ``modified_tools`` (``before_iteration``) is not a mode but a grant: the
   tool schemas the model is shown for this one iteration. ``append_note``
   (the three iteration phases) is the other grant: a short harness-authored
   note the loop appends to the last transcript message before the next
   model call -- a budget warning, a gate notice -- chained across hooks in
   order. The note is data the model reads, never a message the user sent;
   the transcript keeps it where it landed. On ``before_user_inbound``,
   ``modified_content`` rewrites the inbound text before the loop dispatches
   it (a memo prepended, a reminder appended), chained through
   ``ctx.inbound_content``.

4. **HookDecision is immutable from the hook's perspective.** Hooks
   build a fresh decision each call; ``CompositeHook`` is responsible
   for chaining modifications (next hook sees the previous hook's
   transformed content via ``ctx.outbound_content``).

5. **AgentHookContext fields are populated by phase.** Not every
   attribute is meaningful in every phase — e.g. ``turn_request`` is
   set during ``before_user_inbound`` but ``None`` during
   ``before_iteration``. Callers (AgentLoop) are responsible
   for setting the relevant fields before invoking a phase. Hooks
   should not panic on ``None`` for unused fields.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.spine.turn import TurnRequest


# ---------------------------------------------------------------------------
# Context & Decision
# ---------------------------------------------------------------------------


@dataclass
class AgentHookContext:
    """State carried through a hook chain for one AgentLoop turn.

    ``session_key`` is the only universally required field. The rest are
    populated by phase — see each hook method's docstring on
    :class:`AgentHook` for what's set when.

    The context is mutable: callers (AgentLoop / CompositeHook) update
    fields between phases as new information becomes available (e.g.
    ``response`` is filled after the LLM returns; ``outbound_content``
    is filled before ``after_send``). Hooks may also mutate fields
    they own — but the canonical channel for "I changed the outbound
    text" is the ``HookDecision.modified_content`` return value, which
    ``CompositeHook`` propagates into the context for the next hook.
    """

    session_key: str

    # ── before_user_inbound ──
    turn_request: "TurnRequest | None" = None
    #: The inbound text as the loop will dispatch it: the request's text at
    #: first, then whatever the hooks before this one rewrote it to. What a
    #: hook rewrites here shapes the model's view of THIS turn only -- the
    #: session record keeps the user's own words.
    inbound_content: str | None = None
    #: The session's persisted record so far, read-only, populated at every
    #: phase. "So far" is exact: the turn is filed only after the send fire,
    #: so even at ``after_send`` the record still ends with the PREVIOUS
    #: turn. A gate deciding whether a follow-up needs new work reads the
    #: conversation here, not the working window a trimmer may have
    #: shortened (the iteration phases carry that window in ``messages``).
    #: Two seams to know: the inbound fire reads the message's own session,
    #: before a caller-supplied key takes over and before token
    #: consolidation runs; from the iteration phases on this is the
    #: post-consolidation record of the session the turn actually runs and
    #: persists into. Never mutate it.
    session_history: "list[dict[str, Any]] | None" = None

    # ── before_iteration / before_execute_tools / after_iteration ──
    iteration: int | None = None
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    response: Any | None = None  # LLMResponse or dict; left as Any to avoid
    # an import cycle from this base module.
    #: This turn's question, read once at loop entry from the inbound user
    #: message -- later the history has grown and "the last user message" may
    #: be an injection.
    turn_question: str = ""
    #: Index in ``messages`` where this turn's own messages start; what a
    #: hook counts (searches this turn, fetches this turn) is ``messages[turn_base:]``.
    turn_base: int = 0
    #: The iteration cap the loop enforces THIS turn: the per-session mode
    #: policy's cap when one is set, else the loop default, read once at
    #: turn entry -- ``iteration`` counts toward this denominator. A hook
    #: rollback re-runs an iteration without moving either. Populated at the
    #: iteration phases and ``terminal_answerless``.
    max_iterations: int | None = None
    #: The context window of the turn's active model binding, resolved
    #: configured-first and never over the network. It is the budget the
    #: loop plans by, not a per-call guarantee: a router-picked model or a
    #: mid-turn provider fallback can run this turn on a window that
    #: differs. Populated with ``max_iterations``.
    context_window_tokens: int | None = None

    # ── after_send ──
    outbound_content: str | None = None

    # ── Free-form ──
    #: One dict per turn when the loop drives all phases: seeded at the
    #: inbound fire, carried through the iterations (where the loop adds
    #: ``mode`` / ``mode_overlay``), still present after the send. What a
    #: hook stashes under ``metadata["observers"]`` (a dict) is filed onto
    #: the turn's last substantive assistant message at persist time, after
    #: the ``after_send`` fire -- a stash from any phase, the send included,
    #: reaches the filed record. The entry ``observers["acp_meta"]`` (a dict)
    #: additionally travels to an ACP client as the prompt response's
    #: ``_meta`` (raven/acp/methods.py reads it back from the filed record).
    #: One key of it the ACP layer reads for itself rather than passing on:
    #: ``acp_meta["raven.holdTurn"]`` (``{"untilMs": int | None, "why": str}``)
    #: on a turn that ended normally says the agent's work goes on without it
    #: (a wake it armed runs a later turn on this session), so the layer keeps
    #: the ``session/prompt`` open until a later turn ends without the key.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookDecision:
    """Result of a hook invocation.

    Four states (mutually compatible only in this combinatorial sense):

    - ``pass_through=True``, no short-circuit, no modification — default,
      continue to next hook / main loop.
    - ``short_circuit_result`` is set — halt the chain; AgentLoop treats
      this value as the final answer for the current phase.
    - ``modified_content`` is set — only meaningful for the ``after_send``
      phase; the next hook in the chain sees the modified text as
      ``ctx.outbound_content``.
    - ``rollback=True`` — halt the chain; AgentLoop pops every message the
      current iteration appended and re-samples the LLM call without
      consuming an iteration. Honored in ``before_execute_tools`` (the
      proposed tool calls are discarded unexecuted) and ``after_iteration``
      (tool side effects, if any ran, stand -- only the history is popped).
      Bounded per turn by the loop's rollback cap; past the cap the decision
      degrades to pass-through and the refusal is counted in
      ``ctx.metadata["rollbacks_refused"]``. Every honoured rollback is
      counted in ``ctx.metadata["hook_rollbacks"]``, so a later hook can tell
      a re-sampled iteration from the first sampling of that number (the
      counter is what makes "this is the turn boundary" decidable after a
      rollback to iteration 1). ``rollback_overrides`` carries
      generation-parameter overrides for the re-sample call only (keys
      outside the loop's allowlist are dropped); ``rollback_inject`` carries
      messages appended after the pop, so the re-sample sees them -- they
      persist into history like any prompt the model was shown.

    ``modified_tools`` is a grant, not a halting state: in
    ``before_iteration`` it replaces the tool schemas the model is shown
    for this one iteration. The registry is untouched, so the next
    iteration starts from the full set unless a hook decides again.

    ``append_note`` is the second grant: text the loop appends to the last
    transcript message (a blank line between) before the next model call, in
    the three iteration phases. Chained: every hook's note lands, in order.

    ``notes`` is the diagnostic trail: free-form strings a hook leaves about
    what it saw and decided. The loop deliberately does not act on them;
    ``CompositeHook`` chains them in hook order onto the decision it returns
    -- a halting decision carries the notes collected before it, then its
    own -- so a caller or harness reads one ordered list.

    Hooks should not mix the halting states in a single decision -- the
    first halting state wins and the rest would be moot.
    """

    pass_through: bool = True
    short_circuit_result: Any | None = None
    modified_content: str | None = None
    rollback: bool = False
    rollback_overrides: dict[str, Any] | None = None
    rollback_inject: list[dict[str, Any]] | None = None
    modified_tools: list[dict[str, Any]] | None = None
    append_note: str | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AgentHook ABC
# ---------------------------------------------------------------------------


class AgentHook(ABC):
    """Base class for AgentLoop lifecycle hooks.

    All phases default to no-op (return a pass-through ``HookDecision``).
    Subclasses override only the methods they need.

    ``rolls_back_iterations`` is how a subclass says its ``after_iteration`` can
    answer ``rollback``. It is declared rather than read off the override,
    because overriding the phase only says the hook *observes* it: the loop
    holds a response's deltas for a hook that can send the response back, which
    costs the reader the answer typing out, and an observer would pay that for
    nothing. ``eval_engine``'s judge is the case in point -- it overrides the
    phase, awaits an LLM judge inside it, and its own contract says an evaluator
    never interrupts the reply chain.
    """

    #: See the class docstring: the loop reads this to decide whether this
    #: turn's response deltas are worth holding.
    rolls_back_iterations: bool = False

    @property
    def name(self) -> str:
        """Human-readable identifier — appears in error logs.

        Subclasses should override for clarity (e.g. "SentinelInjectHook"
        rather than "InjectHook" so the source subsystem is visible at
        a glance).
        """
        return type(self).__name__

    # ── User-inbound phase ─────────────────────────────────────────────

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        """Fires when a fresh user message arrives, before AgentLoop
        dispatches it to the LLM.

        Used for:
          - Sentinel ``decision_consumer`` returns a short-circuit
            reply when the inbound is a /pick reply to a previously
            dispatched task-discovery menu.
          - Sentinel ``FeedbackTracker`` observes (no short-circuit)
            to mark recent nudges as accepted / dismissed.
          - Personalizer Step 1+2 may short-circuit with a clarification
            question instead of running the main loop.
          - Rewriting the inbound text before dispatch (``modified_content``):
            a research memo prepended, a format reminder appended.

        Context fields populated: ``session_key``, ``turn_request``,
        ``inbound_content``.
        """
        return HookDecision()

    # ── ReAct iteration phases ────────────────────────────────────────

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        """Fires before each LLM call in the ReAct loop.

        Used for:
          - Token budget check (refuse to start another iteration if
            we'd blow the budget).
          - Pruning (skip iteration when the work is clearly done).
          - Withholding a tool for this iteration (``modified_tools``).
          - Leaving the model a note before this call (``append_note``).

        Context fields populated: ``session_key``, ``iteration``,
        ``messages``, ``tools``, ``turn_question``, ``turn_base``,
        ``session_history``, ``max_iterations``, ``context_window_tokens``.
        """
        return HookDecision()

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        """Fires after the LLM returns ``tool_calls`` but before the
        tools actually execute.

        Used for:
          - Pre-tool-call audit / approval.
          - Discarding the proposal and re-sampling (``rollback``).
          - Leaving the model a note beside the proposal (``append_note``).

        Context fields populated: ``session_key``, ``iteration``,
        ``messages``, ``response`` (the LLM response carrying tool calls),
        ``session_history``, ``max_iterations``, ``context_window_tokens``.
        """
        return HookDecision()

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        """Fires after each iteration completes (LLM call + any tool
        execution for that iteration).

        Used for:
          - Judging loop completion (is this turn done?).
          - Judging case success (did we succeed?) → writes to ``case.md``
            via memory_engine.
          - Bouncing a draft back with feedback (``rollback`` +
            ``rollback_inject``) or replacing it (``short_circuit_result``).
          - Leaving the model a note on the tool results it is about to read
            (``append_note``).

        Fires twice per iteration shape: after tool results are in (the
        response carried tool calls) and, for a text response, before that
        text is persisted -- so a short-circuit replaces it without leaving
        the replaced text in history.

        Context fields populated: ``session_key``, ``iteration``,
        ``messages``, ``response``, ``session_history``,
        ``max_iterations``, ``context_window_tokens``.
        """
        return HookDecision()

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        """Fires once, after the loop has ended with no visible answer.

        The iteration phases only see turns that reached a text response; a
        turn can also end through a provider error or an exhausted budget
        whose wrap-up came back empty, and those exits reach the user with
        reasoning but no answer. This is the one seam where a terminal gate
        can still commit one.

        ``short_circuit_result`` replaces the final answer and is persisted
        as the turn's last assistant message; ``rollback`` and
        ``modified_content`` are meaningless here -- the loop is over.

        Context fields populated: ``session_key``, ``messages``,
        ``metadata["turn_end"]`` (status and iteration count),
        ``session_history``, ``max_iterations``, ``context_window_tokens``.
        ``response`` is ``None``.
        """
        return HookDecision()

    # ── Outbound phase ─────────────────────────────────────────────────

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        """Fires when the final outbound content has been assembled,
        before it is sent as the reply.

        Used for:
          - Sentinel ``NudgeInjector`` appends a queued nudge to the
            outbound reply (via ``modified_content``).
          - Personalizer ``post_learn`` observes the final exchange
            and updates user behaviors (no short-circuit, no mod).

        Context fields populated: ``session_key``, ``outbound_content``,
        ``session_history``.
        Returning ``HookDecision(modified_content=...)`` rewrites the
        outbound text — ``CompositeHook`` chains modifications so a
        downstream hook sees the upstream one's output.
        """
        return HookDecision()


#: The hook surface's own version, bumped whenever the vocabulary grows or a
#: phase changes meaning (2: the turn-scoped ``metadata`` dict,
#: ``session_history``, and the ``before_user_inbound`` rewrite grant;
#: 3: ``session_history`` at every phase, the turn's ``max_iterations`` and
#: ``context_window_tokens`` on the iteration context, ``HookDecision.notes``
#: chained by the composite, and the observers stamp moved to persist time,
#: after the send fire). The
#: fingerprint test (tests/test_agent_hook_contract.py) pins the field and
#: phase rosters to this number, so growth is a bump a reviewer -- and a
#: product hook author -- sees, rather than a drift nobody counted.
FACTORY_LOOP_SURFACE_VERSION = 3

__tier__ = "factory_loop"
__all__ = ["AgentHook", "AgentHookContext", "FACTORY_LOOP_SURFACE_VERSION", "HookDecision"]
