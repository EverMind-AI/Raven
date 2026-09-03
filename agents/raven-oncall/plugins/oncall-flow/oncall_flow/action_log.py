"""What the loop actually did, so a claim about it can be checked.

The failure this exists for: an on-call loop read "you are right, cancel it",
wrote "the owner confirmed the issue and authorized cancellation" into its
conclusion, and never called cancel. Judged on its text it looked correct. In a
product that is allowed to cancel other people's jobs, narrating an action is the
most expensive kind of mistake it can make, because the report reads as done.

The fix is not to read the prose more carefully. It is to make a claim a
structured field and check it against a log of calls that actually happened:

  - **the log is written by the tool layer, not by the model.** Nothing the loop
    says can add an entry.
  - **an unbacked claim is refused, not scored down.** A receiver that accepted
    it would pass the misleading report through to whoever acts next.
  - **a verb the log cannot interpret is refused and counted separately.**
    Guessing would let an unrecognised claim through; blaming the model would
    hide a gap in this table.

Prose is still what a person reads, so it is checked too -- but only ever as an
advisory flag, never as grounds for refusal. That check is a heuristic, and a
heuristic that silently drops what it cannot parse is the bug this whole line has
hit four times, so it reports its own misses.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

CANCEL = "cancel"
ESCALATE = "escalate"
REPORT = "report"

# Verbs a loop plausibly uses for each action. A claim whose verb is absent here
# cannot be verified, so it is refused -- and the refusal names the verb, because
# the likeliest cause is a missing row rather than a lying agent.
_VERBS: dict[str, str] = {
    "cancel": CANCEL,
    "cancelled": CANCEL,
    "canceled": CANCEL,
    "kill": CANCEL,
    "killed": CANCEL,
    "stop": CANCEL,
    "stopped": CANCEL,
    "terminate": CANCEL,
    "terminated": CANCEL,
    "escalate": ESCALATE,
    "escalated": ESCALATE,
    "ask": ESCALATE,
    "asked": ESCALATE,
    "contact": ESCALATE,
    "contacted": ESCALATE,
    "notify": ESCALATE,
    "notified": ESCALATE,
    "report": REPORT,
    "reported": REPORT,
}


def action_for_verb(verb: str) -> str | None:
    return _VERBS.get((verb or "").strip().lower())


@dataclass(frozen=True)
class Claim:
    """An assertion by the loop that it did something.

    Structured on purpose. Parsing a claim out of prose is how a harness ends up
    measuring its own regex, so the claim is a field and the prose is advisory.
    """

    verb: str
    target: str


@dataclass(frozen=True)
class ActionRecord:
    kind: str
    target: str
    at_ms: int
    outcome: str = "ok"


@dataclass
class ActionLog:
    """Append-only record of state-changing calls, written by the tool layer."""

    records: list[ActionRecord] = field(default_factory=list)

    def record(self, kind: str, target: str, at_ms: int, outcome: str = "ok") -> ActionRecord:
        rec = ActionRecord(kind=kind, target=target, at_ms=at_ms, outcome=outcome)
        self.records.append(rec)
        return rec

    def has(self, kind: str, target: str) -> bool:
        return any(r.kind == kind and r.target == target and r.outcome == "ok" for r in self.records)

    def kinds_done(self) -> set[str]:
        return {r.kind for r in self.records if r.outcome == "ok"}

    def verify(self, claim: Claim) -> tuple[bool, str | None]:
        """Whether the log backs this claim, and why not if it does not."""
        kind = action_for_verb(claim.verb)
        if kind is None:
            return False, f"cannot verify the verb {claim.verb!r}; no action is mapped to it"
        if not self.has(kind, claim.target):
            return False, f"claimed to have {claim.verb} {claim.target}, but no {kind} of it was called"
        return True, None

    def to_dict(self) -> dict[str, Any]:
        return {"records": [asdict(r) for r in self.records]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActionLog:
        return cls(records=[ActionRecord(**r) for r in (payload or {}).get("records", [])])


# Past-tense assertions about the loop's own actions. Deliberately narrow: it
# only fires on a first-person past-tense verb, because "cancel it if it
# diverges" and "the owner said to cancel" are not claims of having acted.
_PROSE = re.compile(
    r"\bI\s+(?:have\s+)?(cancell?ed|killed|stopped|terminated|escalated|asked|contacted|notified|reported)\b",
    re.I,
)
_PASSIVE = re.compile(
    r"\b(?:has|have|was|were|been)\s+(cancell?ed|killed|stopped|terminated|escalated|reported)\b",
    re.I,
)


@dataclass
class ProseFindings:
    """Advisory only. ``unparsed`` is the count of past-tense action phrases the
    patterns matched but could not map to an action -- reported rather than
    dropped, so a gap in the table shows up as a number instead of as silence."""

    asserted: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    unparsed: int = 0


def prose_findings(text: str, log: ActionLog, subject: str | None = None) -> ProseFindings:
    """Past-tense action assertions in prose that the log does not back.

    Never grounds for refusing a report: prose is for people, and a false
    negative here is better than a harness that rejects a correct loop over
    phrasing. It exists because the prose is what a person reads, so a report
    whose structured claims are empty can still mislead.

    ``subject`` is checked when given, and it matters: without it the only thing
    a verb can be compared against is whether an action of that kind happened at
    all, so "I cancelled train-c" would be backed by having cancelled some other
    job. A report is about one subject, so that is the target to check.
    """
    out = ProseFindings()
    done = log.kinds_done()
    for pattern in (_PROSE, _PASSIVE):
        for verb in pattern.findall(text or ""):
            out.asserted.append(verb.lower())
            kind = action_for_verb(verb)
            if kind is None:
                out.unparsed += 1
            elif not (log.has(kind, subject) if subject else kind in done):
                out.unsupported.append(verb.lower())
    return out
