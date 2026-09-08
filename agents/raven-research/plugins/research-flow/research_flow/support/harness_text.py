"""Text the harness itself writes into the conversation.

Some bodies in a trajectory did not come from the model and did not come from
the world: the harness wrote them. An elision placeholder standing in for a
dropped tool result, a rule refusing to run a search. They look exactly like
tool output to everything downstream, and two things must never treat them as
evidence:

  * the salvage/verify evidence packs, which would ask a model to answer out of
    a placeholder;
  * the final-answer channel, which would ship the harness's own sentence as the
    run's answer.

The second one is not hypothetical. On the dr@3.0 live-web batch, ``hle-256``
finished with a ``final_answer`` byte-identical to the search-closed notice: the
turn overflowed, 39 of its 43 tool bodies had been elided, the four survivors
were all that same notice, and the salvage model - handed four copies of it as
its only evidence - returned it verbatim. It scored zero on accuracy and, worse,
counted as ``closed_with_answer`` on the answer-rate endpoint.

That failure has three cousins already on the record, which is why this module
exists instead of one more special case: MiroFlow's ``\\boxed{}`` extraction
dropped 10.83% of its own correct answers, dr@1.6's salvage seam blanked 15, and
``_completion_clamp`` was a dead backstop. The last inch of the answer channel
loses answers, repeatedly, and each time the fix was local.

Two predicates, deliberately
----------------------------
``is_harness_authored`` and ``is_harness_echo`` ask the same question and give
different answers, because the two channels have **opposite error preferences**:

  * Evidence selection: a false positive costs nothing - the pack simply reaches
    one item further back. So the predicate may be permissive (substring/prefix).
  * The answer channel: a false positive **destroys a real answer**. That is the
    exact shape of the three failures above. So the predicate must be strict -
    the whole answer has to *be* the harness string, not merely contain it.

Collapsing them into one predicate is wrong on one of the two channels no matter
which strictness is chosen. Anything added below must be added to
``_HARNESS_BODIES`` (permissive matching) and, only if a model could plausibly
emit it as an entire answer, considered for the strict path too.
"""

from __future__ import annotations

TOOL_OUTPUT_ELIDED = "[earlier tool output elided to fit the context window]"
"""Body substituted for an older tool result when the turn must be shrunk to fit.

Shared so that every consumer agrees on what an elided body looks like: any
component that reasons over evidence has to be able to tell a real tool result
from this constant, or it silently verifies against a placeholder.
"""

SEARCH_CLOSED_PREFIX = "Search is closed for this task:"
"""Opening of the saturation stop rule's refusal (``search_saturation``).

Matched as a prefix because the notice interpolates ``k``. Defined here rather
than at the emit site so the recogniser and the emitter cannot drift - the
drift would be silent, and its symptom would be a harness sentence quietly
becoming eligible as evidence again.
"""


def search_closed_notice(k: int) -> str:
    """The refusal the search tool returns once the saturation rule has stopped.

    Single source of truth: ``WebSearchTool`` builds the string from here, and
    the predicates below recognise it from here.
    """
    return (
        f"{SEARCH_CLOSED_PREFIX} the last {k} searches returned no page you had "
        "not already been shown. Open the pages you have already found with "
        "web_fetch and answer from them."
    )


FETCH_GATE_PREFIX = "Search is paused for this task:"
"""Opening of the fetch gate's notice (``fetch_gate``).

Deliberately not the saturation rule's wording. The two rules fire on different
predicates - that one on *dry* searches, this one on *unread* ones - and a turn
can hit either without the other, so a reader who finds this sentence in a
trajectory has to be able to tell which rule wrote it.
"""


def fetch_gate_notice() -> str:
    """The one sentence the fetch gate writes when it closes ``web_search``.

    Takes no argument, and that is a decision rather than an omission. The
    obvious version interpolates the streak that triggered it, which would make
    the string vary per firing and force the strict recogniser below into
    reconstructing it across a range of counts - the workaround
    ``search_closed_notice`` already needs, and one that cannot be right for a
    streak with no bound (171 was observed on one dr@3.0 turn). The count is a
    counter; it belongs in ``counters()`` and the log line, where something can
    aggregate it. Nothing downstream can aggregate a number embedded in prose.
    """
    return (
        f"{FETCH_GATE_PREFIX} an answer has to rest on pages you have opened, "
        "and this turn has run a long stretch of searches without opening one. "
        "web_search will come back once you open a page with web_fetch. Pick "
        "the most promising result you already have and open it."
    )


SUFFICIENCY_PREFIX = "Research is sufficient for this task:"
"""Opening of the sufficiency gate's note (``flow.sufficiency``).

Its own wording for the same reason the fetch gate has one: three rules now write a
sentence into a trajectory and a reader who finds one has to be able to tell which fired.
This one is also the only note of the three that RELEASES rather than restricts, which is
the distinction most worth keeping legible.
"""


def sufficiency_notice() -> str:
    """The one sentence the sufficiency gate writes when the evidence already decides it.

    No interpolation, for the reason ``fetch_gate_notice`` gives at length: a varying
    string forces the strict recogniser below to reconstruct it across a range of values.
    The verdict's reason and the judge's latency belong in ``counters()`` and the ledger,
    where something can aggregate them.
    """
    return (
        f"{SUFFICIENCY_PREFIX} an independent review of the pages you have opened "
        "finds they already decide the answer. Do not open new leads. Write the "
        "final answer now from the evidence above: the answer itself first, then "
        "the evidence that decides it with source URLs."
    )


# Permissive recognisers: (name, matcher). Used only where a false positive is
# free. Keep the names stable - they are the diagnostic a reader gets back.
_HARNESS_BODIES: tuple[tuple[str, object], ...] = (
    ("tool_output_elided", lambda s: TOOL_OUTPUT_ELIDED in s),
    ("search_closed", lambda s: s.lstrip().startswith(SEARCH_CLOSED_PREFIX)),
)
# ``fetch_gate`` and ``sufficiency`` are deliberately absent from the permissive
# table, which inverts the rule stated at the top of this module. The reason is that
# they are the two harness bodies that do not occupy a message of their own: each
# appends its sentence to the newest tool result, because a turn whose tool list just
# shrank - or whose research was just called finished - needs the explanation attached
# to what it is already reading. So a permissive match would condemn the *real tool
# result the note rides on*, and the stated price of a permissive false positive - "the
# pack reaches one item further back" - is not what would be paid; the pack would lose
# evidence. Strict-only, below, where the whole-string test cannot mistake a note for
# its host.


def is_elided_tool_output(content: object) -> bool:
    """True when a tool message body carries no evidence, only the elision marker.

    Kept as its own predicate, and deliberately *not* widened to cover the other
    harness bodies: ``verify`` counts elisions with it to produce the published
    ``rejected_on_elided`` observer. Widening it here would silently redefine a
    metric that already has readings on disk - the measurement would move while
    the name stayed put, which is this project's most-repeated failure. Use
    ``is_harness_authored`` for *selection*; use this one only for *counting
    elisions*.
    """
    return TOOL_OUTPUT_ELIDED in str(content or "")


def harness_body_kind(content: object) -> str | None:
    """Which harness body this is, or ``None`` if it carries real content.

    Returns the name rather than a bool so callers can say *which* one they
    skipped; "we dropped some evidence" and "we dropped a placeholder" are
    different enough that a log line collapsing them is not worth writing.
    """
    s = str(content or "")
    if not s.strip():
        return None
    for name, matches in _HARNESS_BODIES:
        if matches(s):  # type: ignore[operator]
            return name
    return None


def is_harness_authored(content: object) -> bool:
    """Permissive: this body was written by the harness, so it is not evidence.

    For evidence selection only. A false positive here costs one item of
    look-back; a false negative puts a placeholder in front of a model that is
    about to commit an answer.
    """
    return harness_body_kind(content) is not None


def is_harness_echo(answer: object) -> bool:
    """Strict: this *entire* answer is a harness sentence, not a model answer.

    For the answer channel only. Matching is whole-string (after stripping
    surrounding whitespace) against the exact bodies the harness emits, so an
    answer that merely quotes or explains one survives. The asymmetry is the
    point: on this channel a false positive is the failure being fixed, not a
    cheap retry.

    ``search_closed`` is matched by prefix in the permissive path because ``k``
    varies, so the strict path reconstructs the notice for every ``k`` a batch
    could plausibly use rather than accepting the prefix. An answer that merely
    *starts* with the prefix but continues into real content is therefore not an
    echo - which is correct: that is a model writing about the refusal.
    """
    s = str(answer or "").strip()
    if not s:
        return False
    if s == TOOL_OUTPUT_ELIDED:
        return True
    if s == fetch_gate_notice():
        return True
    if s == sufficiency_notice():
        return True
    # ``k`` is a small configured integer (``saturation.k``, default 10). Rather
    # than plumb the live value into every caller - which would make the check
    # depend on config and therefore fail open when the config is absent - the
    # notice is reconstructed across the range a batch could use. Cheap, and it
    # cannot fail open.
    return any(s == search_closed_notice(k) for k in range(1, 101))


__all__ = [
    "FETCH_GATE_PREFIX",
    "SUFFICIENCY_PREFIX",
    "SEARCH_CLOSED_PREFIX",
    "TOOL_OUTPUT_ELIDED",
    "fetch_gate_notice",
    "harness_body_kind",
    "is_elided_tool_output",
    "is_harness_authored",
    "is_harness_echo",
    "search_closed_notice",
    "sufficiency_notice",
]
