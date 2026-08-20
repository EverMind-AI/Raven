"""Wall-clock budget awareness for the agent loop.

An agent running under an external deadline (benchmark harness, cron slot) is
otherwise blind to it: it cannot decide to start a long build now instead of
deliberating, because nothing tells it time is passing. When a deadline is
provided, this emits a short reminder line the first time consumption crosses
each threshold; the loop appends it to the latest tool result.

Two threshold kinds, deliberately both:

*Fractions of the budget* pace a long run — they are what tells an agent with
three hours left that it is halfway through.

*Absolute seconds remaining* catch the end of a short one. On a three-hour
budget 90% still leaves eighteen minutes, which is not "wrap up now"; on a
ten-minute budget 90% arrives with sixty seconds left, far too late to write
anything. Whichever kind fires first owns the rest of the turn: once the
wrap-up line has gone out, the remaining fraction thresholds are dropped,
because "start long-running work now" directly contradicts "compute nothing
new" and the model would receive both.

The hard stop is separate from any reminder: the loop asks
:meth:`TimeBudgetReminder.should_stop` before starting an iteration whose model
call could no longer finish. An unwritten perfect answer scores nothing, and a
turn killed mid-call by the harness leaves no reply at all.

Pure decision logic, no I/O, mirroring recovery.py.
"""

from __future__ import annotations

import os

DEADLINE_ENV = "RAVEN_TASK_DEADLINE_EPOCH"
# Opt-in: also ask for a durable checkpoint once the budget is mostly spent.
# Off by default because it costs a turn, and the turn is only worth spending
# where unpersisted work is actually lost -- a harness that grades the live
# environment already sees the work, one that grades a committed diff does not.
CHECKPOINT_ENV = "RAVEN_DEADLINE_CHECKPOINT"

_DEFAULT_THRESHOLDS = (0.5, 0.75, 0.9)
# From this fraction of the budget on, the reminder also asks for a durable
# checkpoint of the work so far.
_CHECKPOINT_THRESHOLD = 0.75

# Defaults for the absolute-seconds pair. Both are overridden per run profile
# (see raven.agent.profile): a coding task needs longer to wrap up than an
# answer task because wrapping up means running the suite and committing, and
# an attended session must not be cut off at all.
WRAPUP_SEC = 420.0
HARD_STOP_SEC = 60.0

_WRAPUP_LINE = (
    "[deadline notice] About {remaining} seconds of task budget remain. Stop "
    "exploration and analysis now. Use the remaining time only to deliver: produce "
    "the final answer from results you already have, write any output files the "
    "task instructions require, and compute nothing new beyond what writing the "
    "answer requires. If several candidate answers exist, commit to the most "
    "literal reading of the question. A partial but delivered answer beats an "
    "unfinished perfect one."
)
# Appended to the wrap-up line, and to the fraction reminders from
# _CHECKPOINT_THRESHOLD on, when the profile or the env opt-in asks for it.
_CHECKPOINT_CLAUSE = (
    " Checkpoint your work NOW before continuing: put whatever already works "
    "where the task will be evaluated -- final file paths, and a commit on the "
    "required branch if the task asks for committed changes, rather than waiting "
    "until everything is finished. Work left only in scratch space or "
    "uncommitted does not count if you run out of time."
)


class TimeBudgetReminder:
    """Emits one reminder per crossed budget threshold, and owns the hard stop."""

    def __init__(
        self,
        deadline_epoch: float,
        start_epoch: float,
        checkpoint: bool = False,
        wrapup_sec: float = WRAPUP_SEC,
        hard_stop_sec: float | None = HARD_STOP_SEC,
    ):
        self.deadline = deadline_epoch
        self.start = start_epoch
        self.checkpoint = checkpoint
        self.wrapup_sec = wrapup_sec
        # ``None`` disables the hard stop: an attended session must not be
        # ended from under the person typing into it.
        self.hard_stop_sec = hard_stop_sec
        self._pending = [t for t in _DEFAULT_THRESHOLDS if deadline_epoch > start_epoch]
        self._wrapup_emitted = False

    @classmethod
    def from_env(
        cls,
        now: float,
        checkpoint_default: bool = False,
        wrapup_sec: float = WRAPUP_SEC,
        hard_stop_sec: float | None = HARD_STOP_SEC,
    ) -> "TimeBudgetReminder | None":
        raw = os.environ.get(DEADLINE_ENV)
        if not raw:
            return None
        try:
            deadline = float(raw)
        except ValueError:
            return None
        if deadline <= now:
            return None
        # A truthy env value opts in exactly as before; when the env is unset
        # the run profile's default applies (delivery="commit" turns it on).
        checkpoint = bool(os.environ.get(CHECKPOINT_ENV)) or checkpoint_default
        return cls(
            deadline,
            now,
            checkpoint=checkpoint,
            wrapup_sec=wrapup_sec,
            hard_stop_sec=hard_stop_sec,
        )

    def should_stop(self, now: float) -> bool:
        """Whether the loop should stop before starting another iteration.

        Asked before the model call, not after: the point is to not begin work
        that cannot finish, leaving the turn to be killed with no reply.
        """
        if self.hard_stop_sec is None:
            return False
        return self.deadline - now <= self.hard_stop_sec

    def in_wrapup(self, now: float) -> bool:
        """Whether the wrap-up window is open (used to pick the delivery path)."""
        return self.deadline - now <= self.wrapup_sec

    def poll(self, now: float) -> str | None:
        """Return a reminder line when a new threshold was crossed, else None."""
        if not self._wrapup_emitted and self.in_wrapup(now):
            # The end of the budget outranks any fraction still pending: drop
            # them so the model never gets "start long work now" after this.
            self._wrapup_emitted = True
            self._pending.clear()
            remaining_sec = max(0, int(self.deadline - now))
            line = _WRAPUP_LINE.format(remaining=remaining_sec)
            if self.checkpoint:
                line += _CHECKPOINT_CLAUSE
            return line
        if not self._pending:
            return None
        total = self.deadline - self.start
        if total <= 0:
            return None
        frac = (now - self.start) / total
        crossed: float | None = None
        while self._pending and frac >= self._pending[0]:
            crossed = self._pending.pop(0)
        if crossed is None:
            return None
        remaining_min = max(0, int((self.deadline - now) / 60))
        line = (
            f"[time budget] About {remaining_min} minutes of wall-clock remain for "
            "this task. If long-running work (builds, training, downloads, servers) "
            "is still needed, start it now and verify while it runs; keep further "
            "deliberation short."
        )
        # Work the deadline cuts off is only worth what has been persisted, so
        # from the second threshold on, ask for a durable checkpoint. An agent
        # that saves at 75% and again at 90% still leaves a usable result when
        # it never reaches its own finish line. Phrased around "where the task
        # is evaluated" rather than around git: committing is the right move
        # only when the task asks for it, and a needless commit costs a turn
        # exactly when turns are scarcest.
        if self.checkpoint and crossed >= _CHECKPOINT_THRESHOLD:
            line += _CHECKPOINT_CLAUSE + (" After checkpointing, keep going and checkpoint again as you make progress.")
        return line
