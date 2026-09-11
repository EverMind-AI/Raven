"""When a tool call that keeps working is still getting nowhere.

``failure_streak`` catches the model repeating a call that fails. This catches
the other shape: a call that succeeds every time and answers the same thing
every time. The failure streak cannot see it -- a success resets that counter to
zero -- and one live run spent an hour reading one file 127 times, exit 0 on
every call, ending the turn no further along than it started.

Counted per turn rather than consecutively. The run that motivated this
alternated two spellings of one read, a plain read and the same read behind a
checksum, so no two neighbouring calls were identical and a consecutive streak
would have stayed at one forever. What decides is how many times one exact call
has already given one exact answer in this turn, not whether those times were
adjacent -- at the cost that two spellings each reach the threshold on their
own, so an alternating pair takes twice the calls to fire.

The nudge alone was not enough. It is advisory text, it can only fire once for
a given answer, and the model it has to reach may be a fixed point: one measured
turn sent 288 byte-identical ``exec`` calls, each answered with an identical
287-token reply over a prompt-cached prefix, so nothing perturbed the input and
nothing changed the output. The nudge fired at the eighth call and the remaining
280 happened behind it. So the nudge is now the first step of a ladder
(:class:`NoProgressGuard`): nudge, then refuse the call without running it, then
end the turn. The turn is the largest thing this may stop -- an LLM-side fault
degrades, it does not take the run with it.

What the enforcing steps stand on is evidence rather than a verdict: the freeze
records what a call answered *and* the novelty count that made that certain, and
:meth:`NoProgressGuard.check` asks the streak's question again at every attempt
instead of reading the answer off a table. A pair whose certainty has gone stale
runs. Read the other way, a model that reaches the freeze and then does real work
could not re-read what its own work had changed, and the way out the refusal
offers -- change the call so it asks a different question -- would mean editing a
command string in order to run the same command. Second chances are finite, or the
freeze would bound nothing: every time a pair is established after the first, it
spends from the same budget its refusals spend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from hashlib import sha256
from typing import Any


def no_progress_key(tool: str, arguments: Any, result: str, blocks: Any = None) -> str:
    """One exact call together with its exact answer, as a dict key.

    The answer is half the key on purpose: a poll whose answer changes is making
    progress, and only an unchanging one is the case this counts. Reading the raw
    result is what makes that true -- the untrusted fence carries a random nonce,
    so a key built after fencing would never match itself.

    ``blocks`` is the other half of the answer, and leaving it out was wrong.
    ``model_text`` is not everything the model receives: an image read answers
    with stable metadata -- path, dimensions, a token estimate -- while the
    pixels ride in the blocks. A render-edit-inspect pass redrawing a slide at
    one size therefore reads identical text and different pictures, which is
    progress; keyed on the text alone it counts as a repeat and the nudge fires
    on the eighth read, in exactly the workflow this guard exists to help.
    """
    material = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    picture = json.dumps(blocks, sort_keys=True, ensure_ascii=False, default=str) if blocks else ""
    return sha256(f"{tool}\x00{material}\x00{result}\x00{picture}".encode()).hexdigest()


def no_progress_nudge(tool: str, n: int) -> str:
    """Injected when one call has answered identically N times in a turn.

    Points at the expectation rather than at the tool: the call is working, so
    "try a different tool" is the wrong advice. What has gone wrong is upstream
    of the read -- either the answer was never the one that settles the
    question, or something else is writing what the model keeps reading.
    """
    return (
        f"[loop] `{tool}` has returned the same result for the same arguments {n} times in this turn. "
        "Calling it again will not answer differently. Stop re-reading and change something: if the "
        "answer contradicts what you expect, the expectation is what to check -- something other than "
        "this call may be writing what you are reading."
    )


def no_progress_pair_key(tool: str, arguments: Any) -> str:
    """One exact call without its answer, as a dict key.

    The escalation below needs a key it can look up *before* the call runs, and
    the result is not knowable then. Sound only because a pair is never entered
    into that table until the full key above has answered identically enough
    times to say what this call returns.
    """
    material = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(f"{tool}\x00{material}".encode()).hexdigest()


def no_progress_refusal(tool: str, n: int, refusals_left: int) -> str:
    """Answered in place of running a call whose answer is already known.

    Replaces the result instead of riding along with it, which is the difference
    from the nudge. The nudge is appended to a working result, and the measured
    turn shows how that reads: ``Exit code: 0`` and then "stop re-reading", with
    the success as the louder half. A call that did not run has no success to
    contradict, and its text is the only place the model can learn what happened
    -- so the text has to carry the way out as well as the refusal.

    The way out is spelled out mechanically for the same reason. A model waiting
    on a background job is told to wait rather than re-read, which is advice it
    cannot act on if the thing being refused *is* its poll; what it can act on is
    that a wait of a different length is a different call, and that any new answer
    anywhere un-freezes this one.
    """
    if refusals_left > 0:
        again = (
            f" Sending this same call again will be refused {refusals_left} more "
            f"time{'s' if refusals_left != 1 else ''}, and then this turn ends."
        )
    else:
        again = " Sending this same call again ends this turn."
    return (
        f"Error: [loop] `{tool}` was not run. This exact call has already returned this exact "
        f"result {n} times in this turn, and nothing else answered anything new in between, so "
        "running it again cannot tell you more than you already have. Use the result you have "
        "and take the next real step, or change the call so that it asks a different question. "
        "If you are waiting on something else to change this result, wait for it in one longer "
        "wait instead of polling: a wait of a different length is a different call, and this "
        "one runs again as soon as anything else has answered something new." + again
    )


def no_progress_stop(tool: str, refusals: int, rescues: int) -> str:
    """The last thing the model is told about the call, as the turn ends.

    Says which way the budget went, because a pair can spend it either way: on
    refusals it ignored, or on second chances that led back to the same answer.
    A model told it was refused three times when it was in fact run again twice
    reads the wrong lesson out of the stop.
    """
    spent = []
    if refusals:
        spent.append(f"refused {refusals} time{'s' if refusals != 1 else ''}")
    if rescues:
        spent.append(
            f"run again {rescues} time{'s' if rescues != 1 else ''} after other work answered "
            "something new, and settled back on this same answer each time"
        )
    if not spent:
        # Reachable only with ``max_refusals`` at 0, where the turn ends on the
        # freeze itself and neither half of the budget was ever drawn on.
        spent.append("already established")
    return (
        f"Error: [loop] `{tool}` was not run and this turn is ending. The same call was "
        + " and ".join(spent)
        + ", so it is stopped here rather than repeated to the iteration limit."
    )


class NoProgressAction(Enum):
    """What to do with a call the model is about to make again."""

    RUN = auto()  # nothing has repeated enough to act on
    REFUSE = auto()  # do not run it; answer with the refusal text
    END_TURN = auto()  # the refusals were ignored too; stop the turn


@dataclass
class _Frozen:
    """A pair whose answer is established, and the evidence that established it.

    ``novelty`` is the counter's value at the moment of the freeze. It is what
    makes the freeze re-checkable: the streak that armed it asked whether
    anything else had answered anything new, and that question has a different
    answer once the counter has moved.
    """

    repeats: int
    novelty: int


@dataclass
class _Spend:
    """What one pair has already used of its per-turn budget.

    Held apart from ``_Frozen`` because it has to outlive it. A pair that runs
    again whenever something else has answered something new, and is refused
    only while nothing has, would run forever in a turn that produces one new
    answer per ``refuse_at`` repeats -- 12 wasted calls in 13 and no bound at
    all. So the un-freezing is counted too, and the count is never refunded.
    """

    refusals: int = 0
    establishments: int = 0

    @property
    def used(self) -> int:
        """Chances spent of ``max_refusals``.

        The first establishment is the evidence itself rather than a chance;
        each one after it says the chance that was granted led back here.
        """
        return self.refusals + max(0, self.establishments - 1)


class NoProgressGuard:
    """The per-turn ladder against a call the model keeps making to no effect.

    Pure decision logic, no I/O -- the loop owns the side effects (appending the
    nudge, answering the refusal, ending the turn), mirroring ``recovery.py``.
    Two counters per answer, deliberately not one:

    ``seen`` is cumulative over the turn and drives the nudge, unchanged from
    when the nudge was the whole guard. It is lenient on purpose: injecting a
    line of text into a workflow that is in fact progressing costs a line of
    text.

    ``streak`` drives the refusal, and asks the harder question -- has anything
    else answered anything new since this call last answered this? Refusing a
    call the model needs is expensive, so the enforcing step wants the evidence
    the advisory one does not need. It is what keeps a wait loop alive, on one
    condition that has to be said out loud: the checks in between have to answer
    something new. A model sleeping between checks makes an identical ``sleep``
    call every time, and while the check beside it moves -- a growing log, a
    percentage -- the sleep's streak resets every round and only its ``seen``
    climbs.

    A poll answering ``status: RUNNING`` byte-identically satisfies no part of
    that. ``_novelty`` counts first sightings of a (call, answer) key, so the
    second identical poll contributes none; neither key resets, both freeze, and
    the turn ends on a wait that was working. Counting something else instead
    would not help, because there is nothing else to count: a fixed-interval wait
    with a stable poll and the two-spellings stuck read above hand this class the
    same transcript -- two pairs, each answering its own constant, alternating,
    nothing else -- so any rule that keeps one running keeps the other running
    too. That is pinned as a test rather than left as a claim, so a later change
    cannot quietly blind one and keep the other.

    What the wait gets instead is a way out and a proportionate stop. Anything
    new at all un-freezes the poll beside it, a wait of a different length
    included, which is what the refusal now tells the model to do; and the end of
    a turn hands the model a tools-disabled wrap-up naming what it was waiting
    for, rather than taking the run with it.
    """

    def __init__(self, *, nudge_at: int, refuse_at: int, max_nudges: int, max_refusals: int) -> None:
        self._nudge_at = nudge_at
        self._refuse_at = refuse_at
        self._max_nudges = max_nudges
        self._max_refusals = max_refusals
        self._seen: dict[str, int] = {}
        self._nudged: set[str] = set()
        self._streak: dict[str, int] = {}
        self._novelty = 0
        self._novelty_at: dict[str, int] = {}
        self._frozen: dict[str, _Frozen] = {}
        self._spend: dict[str, _Spend] = {}
        self._armed: tuple[str, str, int] | None = None

    def check(self, tool: str, arguments: Any) -> tuple[NoProgressAction, str]:
        """Whether this call may run, and what to answer it with when it may not.

        Called before the call executes, so the saving is the loop it ends and
        not the tool time: on the measured turn the repeated ``exec`` cost 0.25s
        of the 9.7s each iteration took. Refusing before the call still matters
        for two reasons that are not time -- the model reads an error instead of
        a success, and a call with side effects does not make them again.

        Three questions in the order that matters. Is anything established about
        this pair at all; has the pair spent its chances, which ends the turn
        whatever else is true; and is the evidence still standing -- if the
        novelty counter has moved since the freeze then something else has
        answered something new, which is exactly what the streak asked and did
        not find, so the call runs. Only a pair that is established, in budget,
        and still the only thing happening is refused.
        """
        pair = no_progress_pair_key(tool, arguments)
        frozen = self._frozen.get(pair)
        if frozen is None:
            return NoProgressAction.RUN, ""
        spend = self._spend.setdefault(pair, _Spend())
        if spend.used >= self._max_refusals:
            rescues = max(0, spend.establishments - 1)
            return NoProgressAction.END_TURN, no_progress_stop(tool, spend.refusals, rescues)
        if frozen.novelty != self._novelty:
            del self._frozen[pair]
            return NoProgressAction.RUN, ""
        spend.refusals += 1
        return (
            NoProgressAction.REFUSE,
            no_progress_refusal(tool, frozen.repeats, self._max_refusals - spend.used),
        )

    def record(self, tool: str, arguments: Any, result: str, blocks: Any = None) -> None:
        """Count what this call answered, and arm the nudge or freeze the pair."""
        key = no_progress_key(tool, arguments, result, blocks)
        seen = self._seen.get(key, 0) + 1
        self._seen[key] = seen
        if seen >= self._nudge_at and key not in self._nudged and len(self._nudged) < self._max_nudges:
            self._armed = (key, tool, seen)

        first_sight = key not in self._novelty_at
        if first_sight:
            self._novelty += 1
        # Something else answered something new since this key last spoke, so
        # the turn is not sitting still even though this call is.
        moved = not first_sight and self._novelty_at[key] != self._novelty
        streak = 1 if first_sight or moved else self._streak.get(key, 0) + 1
        self._streak[key] = streak
        self._novelty_at[key] = self._novelty
        if streak >= self._refuse_at:
            pair = no_progress_pair_key(tool, arguments)
            if pair not in self._frozen:
                self._spend.setdefault(pair, _Spend()).establishments += 1
                self._frozen[pair] = _Frozen(repeats=streak, novelty=self._novelty)

    def take_nudge(self) -> str | None:
        """The armed nudge, once. Only call it where it can be appended.

        Left armed until then: the caller can only append it while the last
        message is still the tool result it belongs to, and a nudge taken at a
        moment it cannot be placed would be silently dropped.
        """
        if self._armed is None:
            return None
        key, tool, seen = self._armed
        self._armed = None
        self._nudged.add(key)
        return no_progress_nudge(tool, seen)


__all__ = [
    "NoProgressAction",
    "NoProgressGuard",
    "no_progress_key",
    "no_progress_nudge",
    "no_progress_pair_key",
    "no_progress_refusal",
    "no_progress_stop",
]
