"""Reporting upward to the orchestrating agent, and a receiver that scores it.

An on-call loop has two escalation paths and they are not the same capability.
Reaching a person spends trust and has to be rare; handing back to the
orchestrating agent costs a message and should be routine. What the two share is
the failure that makes escalating pointless: a report the receiver cannot act on
without going and looking itself.

For a person, "enough to act on" is a judgement and needs an eval set. For a
program it is a schema, which is the whole reason this path is worth building
separately: the hardest half of the metric collapses into an invariant that
either holds or is a bug. What is left needing judgement is only *when* to
report.

Two constraints from the product shape are enforced here rather than graded:

  - **the loop suggests, it does not dispatch.** Which atomic agent runs next is
    the orchestrator's decision. A report that names an action to take is
    accepted; one that claims to have taken it is rejected.
  - **one signal, one report.** The dedupe key is the loop's own statement of
    what this report is about, so the receiver can reject the second copy
    instead of trusting the loop to have remembered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from raven.ops.action_log import ActionLog, Claim, prose_findings

FINISHED = "finished"
CONDITION_MET = "condition_met"
FAILED = "failed"
NEEDS_DECISION = "needs_decision"

_KINDS = frozenset({FINISHED, CONDITION_MET, FAILED, NEEDS_DECISION})

# A relative condition is one stated against where things started ("3 more
# comments", "ranking dropped"), so the starting value is part of the claim
# rather than context. Absolute conditions ("above 500") carry their own
# reference point and need no baseline.
RELATIVE = "relative"
ABSOLUTE = "absolute"


@dataclass(frozen=True)
class Suggestion:
    """Which atomic agent the on-call loop thinks should run next, and why.

    Advisory by construction: there is no field for a dispatch, because routing
    is the orchestrator's call and a suggestion that could execute itself would
    quietly become one.
    """

    agent: str
    reason: str


@dataclass(frozen=True)
class Report:
    """One handoff to the orchestrating agent."""

    campaign: str
    subject: str
    kind: str
    at_ms: int
    dedupe_key: str
    observed: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    condition_type: str = ABSOLUTE
    options: list[str] = field(default_factory=list)
    suggestion: Suggestion | None = None
    dispatched: str | None = None
    # Assertions that the loop already did something. Structured rather than
    # prose so the receiver can check them against what was actually called.
    claims: list[Claim] = field(default_factory=list)
    narrative: str = ""


def missing_fields(report: Report) -> list[str]:
    """What a receiver would have to go and look up itself.

    This is "enough to act on" stated as a schema. It is deliberately not a
    judgement call: every entry here names a specific thing the orchestrator
    cannot proceed without, so a failure points at the field to fill rather than
    at a quality score to argue about.
    """
    gaps: list[str] = []
    if report.kind not in _KINDS:
        gaps.append("kind")
    if not report.subject:
        gaps.append("subject")
    if not report.dedupe_key:
        gaps.append("dedupe_key")
    if not report.observed:
        gaps.append("observed")
    if report.condition_type == RELATIVE and not report.baseline:
        # The gap measured on SentinelBench: about three quarters of reports on
        # relative conditions never said what the value started at, leaving the
        # reader unable to check the claim. Against a program receiver it can
        # simply be required.
        gaps.append("baseline")
    if report.kind == NEEDS_DECISION and not (report.options or report.suggestion):
        gaps.append("options")
    return gaps


def _looks_measured(value: Any) -> bool:
    """Whether one value could plausibly be a reading rather than a placeholder."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            return float(text) != 0
        except ValueError:
            return True
    if isinstance(value, dict):
        return any(_looks_measured(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_looks_measured(v) for v in value)
    return True


def unmeasured_fields(report: Report) -> list[str]:
    """Required values that are present but carry no reading.

    Requiring a field without being able to check it is worse than not
    requiring it: the requirement produces pressure to fill something in, and
    filling it in destroys the one reliable signal a reader had. Measured on a
    real campaign -- a loop that had omitted the baseline entirely, once the
    field became mandatory, supplied ``{"ndcg": 0}`` against a true 0.3674, and
    the gate passed it because a non-empty dict satisfied a presence test. An
    omission is visible to whoever reads the report; a zero reads as "it
    started from nothing and improved".

    This only rejects a value that carries no reading at all. A wrong but
    plausible number still passes -- catching that needs the true value, which
    is what ``MockOrchestrator(expected_baseline=...)`` is for, and is only
    available when the loop was already told it.
    """
    problems: list[str] = []
    if report.condition_type == RELATIVE and report.baseline and not _looks_measured(report.baseline):
        problems.append("baseline")
    return problems


def baseline_mismatches(report: Report, expected: dict[str, Any]) -> list[str]:
    """Reported baseline values that disagree with the recorded starting values.

    Precondition: ``expected`` must be a value the loop was already given (a
    number stated in its own task description). Checking that it was copied
    across correctly is transcription, not the comparison the loop is being
    measured on, and tells it nothing it did not already have. Configuring this
    with a starting value the loop was *not* given would leak that value
    through the refusal text.
    """
    problems: list[str] = []
    for key, want in expected.items():
        if key not in report.baseline:
            problems.append(f"baseline is missing {key}; the recorded starting value is {want}")
            continue
        got = report.baseline[key]
        if isinstance(want, (int, float)) and not isinstance(want, bool):
            try:
                if abs(float(got) - float(want)) <= 5e-4:
                    continue
            except (TypeError, ValueError):
                pass
        elif got == want:
            continue
        problems.append(f"baseline {key}={got!r}; the recorded starting value is {want}")
    return problems


def is_self_sufficient(report: Report) -> bool:
    """Whether the orchestrator can act on this report without re-querying."""
    return not missing_fields(report) and not unmeasured_fields(report)


@dataclass
class Receipt:
    accepted: bool
    reason: str | None = None
    missing: list[str] = field(default_factory=list)


class MockOrchestrator:
    """Stand-in receiver for the upward path, with the scoring surface attached.

    Rejection is the point. A receiver that accepts everything would make the
    loop look like it always reported well, and the thing worth measuring is how
    often the orchestrator would have had to do the work again.
    """

    def __init__(
        self,
        action_log: ActionLog | None = None,
        expected_baseline: dict[str, Any] | None = None,
    ) -> None:
        self._accepted: list[Report] = []
        self._rejected: list[tuple[Report, Receipt]] = []
        self._seen_keys: set[str] = set()
        self._log = action_log
        self._prose_flags: list[tuple[str, list[str]]] = []
        self._prose_unparsed = 0
        self._expected_baseline = dict(expected_baseline or {})

    def receive(self, report: Report) -> Receipt:
        if report.dispatched:
            receipt = Receipt(False, reason="dispatch is the orchestrator's decision, not the loop's")
            self._rejected.append((report, receipt))
            return receipt
        if report.dedupe_key and report.dedupe_key in self._seen_keys:
            receipt = Receipt(False, reason="duplicate report for the same signal")
            self._rejected.append((report, receipt))
            return receipt
        gaps = missing_fields(report)
        if gaps:
            receipt = Receipt(False, reason="not enough to act on", missing=gaps)
            self._rejected.append((report, receipt))
            return receipt
        placeholders = unmeasured_fields(report)
        if placeholders:
            receipt = Receipt(
                False,
                reason="a required value is present but is not a measurement",
                missing=placeholders,
            )
            self._rejected.append((report, receipt))
            return receipt
        if self._expected_baseline:
            mismatches = baseline_mismatches(report, self._expected_baseline)
            if mismatches:
                receipt = Receipt(False, reason="; ".join(mismatches), missing=["baseline"])
                self._rejected.append((report, receipt))
                return receipt
        # A claim the log does not back is refused rather than scored down:
        # accepting it would pass a misleading report to whoever acts next.
        if self._log is not None:
            for claim in report.claims:
                ok, why = self._log.verify(claim)
                if not ok:
                    receipt = Receipt(False, reason=f"unsubstantiated claim: {why}")
                    self._rejected.append((report, receipt))
                    return receipt
        if self._log is not None and report.narrative:
            found = prose_findings(report.narrative, self._log, subject=report.subject)
            self._prose_unparsed += found.unparsed
            if found.unsupported:
                self._prose_flags.append((report.dedupe_key, found.unsupported))
        self._seen_keys.add(report.dedupe_key)
        self._accepted.append(report)
        return Receipt(True)

    # ---- measurement surface ----

    def accepted(self) -> list[Report]:
        return list(self._accepted)

    def rejected(self) -> list[tuple[Report, Receipt]]:
        return list(self._rejected)

    def duplicates(self) -> int:
        return sum(1 for _, r in self._rejected if r.reason and r.reason.startswith("duplicate"))

    def dispatch_attempts(self) -> int:
        """Reports where the loop acted instead of suggesting. Not a style
        complaint: the orchestrator holds the routing decision, so a loop that
        dispatches has taken a decision that was not delegated to it."""
        return sum(1 for _, r in self._rejected if r.reason and r.reason.startswith("dispatch"))

    def missing_field_counts(self) -> dict[str, int]:
        """Which fields were left out, and how often.

        Reported per field rather than as a rate: "eight reports were thin" is
        not actionable, "eight reports omitted the baseline" names the fix.
        """
        counts: dict[str, int] = {}
        for _, receipt in self._rejected:
            for name in receipt.missing:
                counts[name] = counts.get(name, 0) + 1
        return counts

    def unsubstantiated_claims(self) -> int:
        return sum(1 for _, r in self._rejected if r.reason and r.reason.startswith("unsubstantiated"))

    def prose_flags(self) -> list[tuple[str, list[str]]]:
        """Accepted reports whose prose asserts an action the log does not back.

        Advisory, and separate from the refusals on purpose: the report was
        structurally sound, and a person reading it would still be misled.
        """
        return list(self._prose_flags)

    def suggestions(self) -> list[Suggestion]:
        return [r.suggestion for r in self._accepted if r.suggestion is not None]

    def summary(self) -> dict[str, Any]:
        total = len(self._accepted) + len(self._rejected)
        return {
            "reports": total,
            "accepted": len(self._accepted),
            "rejected": len(self._rejected),
            "duplicates": self.duplicates(),
            "dispatch_attempts": self.dispatch_attempts(),
            "missing_fields": self.missing_field_counts(),
            "with_suggestion": len(self.suggestions()),
            "unsubstantiated_claims": self.unsubstantiated_claims(),
            "prose_flagged": len(self._prose_flags),
            "prose_unparsed": self._prose_unparsed,
        }
