"""Per-turn fetch gate: close ``web_search`` until a page has been opened.

An answer can only be grounded in a page that was opened. A turn that keeps
searching without opening one is collecting leads it will never use, and the
project has now measured that shape three times from three directions:

  * ``P(judged correct | gold document fetched)`` is 94.6% against 5.6% when it
    was not, on the dr@3.0 corpus batch - so opening the right page is very
    nearly sufficient *and* necessary;
  * on the same batch the gold document first surfaced at rank 1 within the
    first 3% of the timeline, after which the arm issued a median of 104 more
    searches before giving up. The retrieval budget was never the constraint;
  * questions that ended correct had a median of 35 searches and a 0.0% elision
    rate; the three failure buckets had 107-128 searches and 74-81%.

Why this is not ``fetch_floor``
-------------------------------
``fetch_floor`` observes the same streak and appends a sentence asking the model
to open a page. That was measured: on the four dr@3.0 arms, the note moved
``P(next action is fetch)`` by +4.2pp and +2.3pp against the two anchor arms, on
a base rate near 10%. Real, reproducible, and an order of magnitude too small -
it buys "about seven more searches before a page is opened" against streaks that
reached 171. The advisory channel is not underpowered here by accident; it is
the wrong channel.

Why this is not the saturation stop either
------------------------------------------
``SearchSaturation`` already refuses to run a search once the query family has
gone dry, and its refusal is control flow rather than advice - the call does not
happen. It fired on 84 questions of that batch and did not work, for two reasons
that are worth separating because only one of them is about the mechanism:

  * **Timing.** It first refused at a median of the 92nd search. Healthy turns
    open their first page at the 6th. By the 92nd the context is nearly spent.
  * **Action space.** The refusal is a *return value*, so the model can simply
    call the tool again, and it did: 72 of the 84 chose ``search`` as their very
    next action, 50 never fetched again, and one question produced 195 suppressed
    rows. A refusal the model can re-request 195 times is not a control-flow
    decision, it is an unusually expensive advisory - each refusal costs context
    and the behaviour never changes.

This gate is the same intent applied one layer down: the tool is removed from
the schema for the iteration. ⚠️ 20260825 correction: that makes it un-OFFERED,
not un-AVAILABLE — ``ToolRegistry.execute`` resolves against the registry and
``modified_tools`` never edits the registry, so a model that names the tool anyway
still runs it. ``FetchGateObserver.before_execute_tools`` records those as
``gate_called_when_closed`` (observation only, following ``AskUserGate``); any
claim about "the action space" must be read against that counter. The
predicates also differ and neither subsumes the other - saturation asks whether
searches come back **dry**, this asks whether they come back **unread**. Four
questions on that batch ran 67-93 searches with zero suppressed rows: their
queries kept returning new documents that were never opened, which the
saturation predicate cannot see by construction.

Streak caliber
--------------
The streak is zeroed by a **successful** fetch, not by any fetch. This differs
from ``fetch_floor``, deliberately and load-bearingly: that observer zeroes on
any ``web_fetch`` call, and the two calibers select different populations - 58
of 240 questions against 127 of 240 at the same threshold on the same batch.
For an advisory the choice is close to cosmetic. For a gate it is the whole
mechanism, because zeroing on a failed fetch would let one dead link reopen
search for the rest of the turn, i.e. the gate would be satisfiable without ever
reading anything.

Release valve
-------------
The gate opens permanently for the turn once ``release_after_failed_fetches``
consecutive fetch attempts have failed while it was closed. Without it the gate
is a threshold that can kill a run, and this project's rule is that any such
threshold is structurally a zero-score bucket only one arm can fall into.

The pre-registration also listed a second valve - release when no un-fetched URL
remains in context - which is **not** implemented, and that is a decision rather
than an omission. Two reasons. It is not observable from where this rule lives:
URL ownership sits in the search tool's identity sets and the client ledger, so
a hook could only recover it by re-parsing rendered tool text, which is the
"probe took a different path from the code under test" shape that has produced
two self-consistent wrong diagnoses here. And it is not needed: a turn with
nothing left to open and no ``web_search`` simply answers, which ends the loop -
there is no state in which the absent valve leaves a turn spinning.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FetchGate:
    """Decides, per turn, whether ``web_search`` is currently available.

    One instance per turn-scoped tool set, like ``SearchSaturation``, and for the
    same reason: the state, the decision and the counters are one unit that no
    other knob can half-enter. ``counters()`` is emitted on every row including
    the ones where nothing happened, because a counter that appears only when the
    mechanism fires cannot tell "did not fire" from "was not installed".
    """

    k: int = 15
    release_after_failed_fetches: int = 2

    streak: int = 0
    """Searches since the last successful fetch. Zeroed by success only."""
    max_streak: int = 0
    closed: bool = False
    released: bool = False
    fired: int = 0
    """Number of times the gate went from open to closed. Transitions, not
    iterations: a gate that stays closed for eleven iterations fired once, and
    counting iterations would report a longer stall as a more active rule."""
    streak_at_fire: list[int] = field(default_factory=list)
    failed_fetches_while_closed: int = 0
    opened: int = 0
    """Successful fetches that reopened a closed gate - the mechanism check."""

    def reset(self) -> None:
        """Drop all per-turn state.

        Everything here is a decision or a budget, so all of it clears at a turn
        boundary. Carrying ``released`` across turns would let one question's dead
        links disable the rule for a question that had not been asked yet.
        """
        self.streak = 0
        self.max_streak = 0
        self.closed = False
        self.released = False
        self.fired = 0
        self.streak_at_fire = []
        self.failed_fetches_while_closed = 0
        self.opened = 0

    def observe_search(self) -> None:
        """Record one ``web_search`` tool result."""
        self.streak += 1
        if self.streak > self.max_streak:
            self.max_streak = self.streak

    def observe_fetch(self, ok: bool) -> None:
        """Record one ``web_fetch`` tool result.

        A successful fetch is the only thing that reopens the gate; see the
        module docstring on caliber.
        """
        if ok:
            if self.closed:
                self.opened += 1
            self.streak = 0
            self.closed = False
            self.failed_fetches_while_closed = 0
            return
        if not self.closed:
            return
        self.failed_fetches_while_closed += 1
        if self.failed_fetches_while_closed >= self.release_after_failed_fetches:
            self.released = True
            self.closed = False

    def evaluate(self) -> bool:
        """Whether ``web_search`` should be withheld from this iteration.

        Called once per iteration, after the iteration's tool results have been
        observed. Returns the decision rather than mutating a tool list, so the
        caller owns the action and this object stays testable without a loop.
        """
        if self.released:
            return False
        if self.streak < self.k:
            return False
        if not self.closed:
            self.closed = True
            self.fired += 1
            self.streak_at_fire.append(self.streak)
        return True

    def counters(self) -> dict[str, object]:
        """Per-row diagnostics.

        ``gate_fired`` / ``gate_released`` / ``gate_streak_at_fire`` are the three
        fields the pre-registration names as first-class trajectory fields, so
        acceptance counts them off ``traj_raw.jsonl`` rather than off a hook - a
        key that only exists inside the hook has been dropped by a serialisation
        allowlist here before.
        """
        return {
            "gate_k": self.k,
            "gate_fired": self.fired,
            "gate_released": self.released,
            "gate_streak_at_fire": list(self.streak_at_fire),
            "gate_reopened": self.opened,
            "gate_max_streak": self.max_streak,
            "gate_closed_now": self.closed,
            "gate_failed_fetches_while_closed": self.failed_fetches_while_closed,
        }


__all__ = ["FetchGate"]
