"""Loop termination — the "never stop early" discipline, as code not prompt.

The SOP stops on the first of these conditions, and the exhaustion signal is
always measured against VANILLA (the fixed cold-start baseline) on train, never
against the previous parent and never against the sealed test set:

- ``patience`` consecutive rounds in which no candidate beat vanilla on train
  (exploration exhausted — the primary signal), or
- ``max_rounds`` reached (a hard cap backstop), or
- ``max_consecutive_errors`` rounds in a row that produced no real decision
  (driver/apply/eval outage). An errored round is NOT evidence about
  exploration, so it must not burn patience — but an endless outage must not
  loop either, so it gets its own counter and an honest ``errors_exhausted``
  stop reason.

``record_round(promoted=...)`` takes the vanilla-comparison signal: True iff at
least one candidate's full-train confirm beat vanilla this round (regardless of
whether it also beat its ratcheted parent baseline and banked). Keeping this in
a small, unit-tested tracker is exactly what lets a weak driver model run the
loop: the stop decision is the harness's, not something the model has to
remember to check.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TerminationTracker:
    """Track per-round outcomes across rounds and decide when to stop."""

    patience: int = 10
    max_rounds: int = 20
    max_consecutive_errors: int = 5
    rounds_completed: int = 0
    consecutive_no_promotion: int = 0
    consecutive_errors: int = 0

    def __post_init__(self) -> None:
        if self.patience < 1:
            raise ValueError("patience must be >= 1")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if self.max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be >= 1")

    def record_round(self, promoted: bool, *, errored: bool = False) -> None:
        """Record one completed round's outcome.

        ``promoted`` is the SOP exhaustion signal: True iff at least one
        candidate beat VANILLA on the full train set this round. ``errored``
        marks a round that produced no real decision (every candidate/phase
        errored); it advances the round counter and the error counter but
        leaves patience untouched — an outage says nothing about exploration.
        """
        self.rounds_completed += 1
        if errored:
            self.consecutive_errors += 1
            return
        self.consecutive_errors = 0
        if promoted:
            self.consecutive_no_promotion = 0
        else:
            self.consecutive_no_promotion += 1

    def should_stop(self) -> tuple[bool, str | None]:
        """Return ``(stop, reason)`` given rounds recorded so far.

        ``reason`` is None while the loop should continue. ``max_rounds`` is
        checked first so hitting the cap reports the cap even if patience was
        also exhausted on the same round.
        """
        if self.rounds_completed >= self.max_rounds:
            return True, "max_rounds"
        if self.consecutive_errors >= self.max_consecutive_errors:
            return True, "errors_exhausted"
        if self.consecutive_no_promotion >= self.patience:
            return True, "patience_exhausted"
        return False, None


__all__ = ["TerminationTracker"]
