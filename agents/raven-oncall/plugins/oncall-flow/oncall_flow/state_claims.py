"""State the tool layer probed, and the basis-freshness gate over it.

Part 2a carries the decision-basis half of the fork's ``state_claims``
module, kept name-for-name: :class:`StateFacts` and its file (the probe's
own record -- written by the tool layer only, so nothing the loop says can
add or change a fact), the decision counter, and :func:`basis_problems` --
the gate that refuses a wait/kill/resubmit whose basis does not rest on an
observation just made. The narrative contradiction checker
(``check()``/``collect_facts`` and the claim regex families) is the report
gate's half and lands with ``ops_finish`` in part 2b (verdict features 2/3).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FACTS_FILE = "state_facts.json"


@dataclass(frozen=True)
class StateFacts:
    """State the tool layer probed, as raw values.

    Every field is ``None`` when the harness does not hold it, and a family whose
    field is ``None`` refuses nothing. That is the difference between "the loop
    is wrong" and "we did not look", and conflating the two is how a checker
    starts inventing findings.
    """

    checkpoints: tuple[str, ...] | None = None
    remaining_minutes: float | None = None
    job_statuses: tuple[str, ...] | None = None
    observed_at_ms: int | None = None
    sources: dict[str, str] = field(default_factory=dict)
    # Readings this probe actually saw, and a counter of how many probes have
    # happened. The counter is what lets a gate ask "has a new observation
    # happened since your last decision?" without needing a clock or a notion of
    # where a turn begins: a decision that cites a reading has to come after a
    # probe that advanced it.
    metric_readings: dict[str, tuple[float, ...]] = field(default_factory=dict)
    probe_seq: int = 0
    # Every number the tool layer put in front of the loop this probe -- readings,
    # but also the configuration it is running, step counts, elapsed time, budget.
    # A basis may cite any of them.
    #
    # Restricting the accepted set to metric readings put the friction on one side
    # of the decision: "keep waiting" is justified by a score and passed, while
    # "switch the configuration" is justified by the parameter that is wrong and
    # was refused. Not acting already needs no successful tool call; a gate that
    # also refuses the reasons for acting points the gradient the same way twice.
    shown_values: tuple[float, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("checkpoints", "job_statuses"):
            if payload[key] is not None:
                payload[key] = list(payload[key])
        payload["metric_readings"] = {k: list(v) for k, v in self.metric_readings.items()}
        payload["shown_values"] = list(self.shown_values)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> StateFacts:
        data = dict(payload or {})
        ckpt = data.get("checkpoints")
        statuses = data.get("job_statuses")
        readings = data.get("metric_readings") or {}
        return cls(
            checkpoints=tuple(ckpt) if ckpt is not None else None,
            remaining_minutes=data.get("remaining_minutes"),
            job_statuses=tuple(statuses) if statuses is not None else None,
            observed_at_ms=data.get("observed_at_ms"),
            sources=dict(data.get("sources") or {}),
            metric_readings={
                str(k): tuple(float(x) for x in v) for k, v in readings.items() if isinstance(v, (list, tuple))
            },
            probe_seq=int(data.get("probe_seq") or 0),
            shown_values=tuple(float(x) for x in (data.get("shown_values") or ())),
        )


def write_facts(campaign_dir: str | Path, facts: StateFacts) -> None:
    """Record a probe. Written by the tool layer only -- nothing the loop says
    can add or change a fact here, which is what makes it usable as a check."""
    d = Path(campaign_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (FACTS_FILE + ".tmp")
    tmp.write_text(json.dumps(facts.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(d / FACTS_FILE)


def read_facts(campaign_dir: str | Path) -> StateFacts:
    """The last probe, or all-unknown when there is none. An unreadable file is
    all-unknown too: a checker that raised here would block reports over its own
    bookkeeping."""
    path = Path(campaign_dir).expanduser() / FACTS_FILE
    try:
        return StateFacts.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return StateFacts()


# ---- basis for a decision: cite a reading from an observation you just made ----

# Scientific notation included on purpose: a learning rate is written 2e-05, and
# the previous pattern cut that into "2" and "-05" -- so the one number most likely
# to justify changing a configuration could never be matched. Measured 2026-08-06.
_BASIS_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
BASIS_NUMBER_RE = _BASIS_NUMBER  # public alias: callers record what they showed
_BASIS_TOLERANCE = 5e-4
_NONFINITE_WORD = re.compile(r"\b(nan|inf|infinity|non-finite|not a number)\b", re.I)


def basis_problems(basis: str, facts: StateFacts, *, last_seq: int) -> list[str]:
    """Why this basis does not rest on a current observation, or an empty list.

    Two things are checked, and neither of them is whether the reasoning is any
    good -- that is the judgement being measured:

      - it cites a number the last probe actually put in front of the caller --
        a reading, or anything else the tool printed (the configuration being
        run, step counts, elapsed time, the budget). Accepting readings only put
        the friction on one side: "keep waiting" is justified by a score and
        passed, "switch the configuration" is justified by the parameter that is
        wrong and was refused;
      - that probe happened after the previous decision, i.e. the probe counter
        moved. Without this a reading from half an hour ago satisfies every later
        decision forever, which is the whole hole: "continue waiting" presupposes
        knowing the situation has not changed, and knowing now entails looking
        now.

    Refusals never quote the recorded readings. Unlike a starting value the task
    already handed over, a reading is the thing the caller was supposed to go and
    fetch; listing it in a refusal would deliver it for free, and refusals are
    free to retry.
    """
    problems: list[str] = []
    if not (basis or "").strip():
        return ["basis is empty"]
    if facts.probe_seq <= last_seq:
        # Naming the tool that takes an observation, because nothing else does.
        # Measured 2026-08-21: a watch campaign hit this refusal, went and ran a
        # curl through exec -- which reads the world but records nothing, so the
        # counter did not move -- and hit it again. Two turns to learn an order
        # that one clause states. The same rule already governs wake messages
        # ("every branch names the tool that performs it"); this refusal had been
        # left out of it.
        problems.append(
            "no observation has been recorded since the previous decision; "
            "a basis must cite a reading taken since then -- take one with "
            "ops_tune_status, which is what records that a look happened "
            "(a command run through exec reads the world but leaves no record, "
            "so it does not count as one)"
        )

    values = [v for series in facts.metric_readings.values() for v in series]
    if not values:
        # Nothing numeric was recorded, so there is nothing to check a citation
        # against. Refusing here would be the worst possible direction: a run whose
        # only output is a non-finite loss produces no readings at all, and that is
        # precisely the run whose kill is least in doubt. Same rule as every other
        # family in this file -- a fact the harness does not hold refuses nothing.
        return problems

    finite = [v for v in values if math.isfinite(v)]
    saw_nonfinite = len(finite) < len(values)
    cited = [float(m.group()) for m in _BASIS_NUMBER.finditer(basis)]
    names_nonfinite = bool(_NONFINITE_WORD.search(basis))

    # shown_values widens what a citation may match, and nothing else. It must not
    # join `values` above: that early return means "the harness recorded no reading,
    # so refuse nothing", and a run whose only output is a non-finite loss records
    # none -- exactly the run whose kill is least in doubt. Folding the printed
    # numbers in there filled that emptiness and brought the refusal back through
    # another door. Measured twice, 2026-08-05 and 2026-08-06.
    acceptable = finite + [v for v in facts.shown_values if math.isfinite(v)]

    if cited and any(abs(c - v) <= _BASIS_TOLERANCE for c in cited for v in acceptable):
        return problems
    if saw_nonfinite and names_nonfinite:
        # A non-finite reading cannot be matched by a number, so naming it counts as
        # citing it. This is not the deleted "non-finite means diverged" hint: it
        # says nothing about what the reading implies, only that it was real.
        return problems
    if not cited and not names_nonfinite:
        problems.append("basis cites no number")
    else:
        problems.append("the number in basis is not among the readings recorded for this campaign")
    return problems


DECISIONS_FILE = "decisions.json"


def read_decision_seq(campaign_dir: str | Path) -> int:
    """The probe counter as of the last accepted decision, or 0."""
    path = Path(campaign_dir).expanduser() / DECISIONS_FILE
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("last_probe_seq") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def write_decision_seq(campaign_dir: str | Path, seq: int) -> None:
    """Record the probe counter a decision was taken at.

    Written by the tool layer after a decision is accepted, so the next decision
    can be required to rest on a later observation. Kept out of StateFacts on
    purpose: that file is the probe's own record, and mixing the decision side
    into it would let one overwrite the other.
    """
    d = Path(campaign_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (DECISIONS_FILE + ".tmp")
    tmp.write_text(json.dumps({"last_probe_seq": int(seq)}, indent=2), encoding="utf-8")
    tmp.replace(d / DECISIONS_FILE)
