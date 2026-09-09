"""What the harness itself knows about the world, so a claim about it can be checked.

The failure this exists for: an on-call loop wrote that
``keep_recent_checkpoints=3`` meant only the three most recent checkpoints were
kept and the best one had probably already been discarded. It was wrong, and it
had not looked. ``action_log`` catches a claim about an *action* the loop says it
took; ``_comparative_words`` catches a claim that is *relative* while declaring
itself absolute. Neither catches a claim about the *state of the world*, which is
the class the loop got wrong.

Three decisions carry this file:

  - **only state the harness already holds.** Budget, job status and the
    checkpoint listing are things the tool layer probes anyway. Anything else --
    what the loss curve means, whether the run is converging, which checkpoint is
    best -- is not here and must not be added: computing "which is best" for the
    loop is the comparison the loop is being scored on.
  - **refuse on contradiction, never on uncertainty.** A hedged claim is still
    checked (the round-1 claim was hedged, and its factual half was false), but a
    claim is only refused when the facts say otherwise. No facts means no
    refusal.
  - **misses are counted, not hidden.** Every family reports what it saw and
    could not resolve, because a heuristic that silently drops what it cannot
    parse is the bug this line has already hit repeatedly.

**What this deliberately does not catch** -- read this before trusting a pass:

  - English only. A narrative in any other language passes every check here.
  - Only the three families below. A false claim about GPU memory, dataset size,
    learning rate, wall-clock, or anything else the harness does not probe is
    invisible.
  - A claim with no number and no identifier ("some checkpoints were dropped",
    "the budget is nearly gone") is counted as unresolved and passes.
  - "The best checkpoint was discarded" with no step id cannot be resolved:
    knowing which one was best is a comparison this module is not allowed to
    make. It is counted as unresolved and passes.
  - Paraphrase outside the word lists passes. The lists can only add refusals.
  - The facts themselves are a snapshot taken when the tool layer last probed.
    A claim that was true at probe time and false now (or the reverse) is judged
    against the snapshot, and ``observed_at_ms`` is what says how stale that is.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FACTS_FILE = "state_facts.json"

# Budget claims within this much of the probed value are not contradictions.
# Wide on purpose: the loop rounds, and the probe itself moves between the poll
# and the report. A tolerance this loose only ever lets claims through.
_BUDGET_ABS_TOLERANCE_MIN = 2.0
_BUDGET_REL_TOLERANCE = 0.10

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

# A checkpoint identifier as the training scripts write it: step-<n>.
_STEP_ID = re.compile(r"\bstep-(\d+)\b", re.I)

# "step-1200 was discarded" -- a named checkpoint asserted absent. The verb has
# to be adjacent to the id, so "step-1200 scored 0.36 before the pruning ran"
# does not match.
_ABSENT_NAMED = re.compile(
    r"\bstep-(\d+)\b[^.;\n]{0,40}?\b(?:was |were |has been |have been |is |are )?"
    r"(discarded|deleted|pruned|removed|dropped|lost|gone|overwritten|no longer (?:there|present|available))\b",
    re.I,
)
_PRESENT_NAMED = re.compile(
    r"\bstep-(\d+)\b[^.;\n]{0,40}?\b(?:was |were |has been |have been |is |are )?"
    r"(still (?:there|present|available)|saved|kept|retained|on disk|present)\b",
    re.I,
)

# "only the three most recent checkpoints are kept" -- a count of what exists.
# Requires an explicit only/just plus an existence verb, so quoting the config
# value ("keep_recent_checkpoints=3") is not itself a claim.
_COUNT_KEPT = re.compile(
    r"\b(?:only|just)\b[^.;\n]{0,40}?\b(\d+|one|two|three|four|five)\b[^.;\n]{0,40}?"
    r"\b(?:checkpoints?|ckpts?)\b[^.;\n]{0,30}?\b(?:are |is |were |remain|exist|kept|saved|retained)",
    re.I,
)
_COUNT_KEPT_ALT = re.compile(
    r"\b(?:keeps?|keeping|retains?|saves?)\b[^.;\n]{0,20}?\b(?:only|just)\b[^.;\n]{0,30}?"
    r"\b(?:the )?(?:most recent |last |latest )?(\d+|one|two|three|four|five)\b",
    re.I,
)

# An unresolved checkpoint-absence claim: the words are there, no id, no count.
_ABSENT_VAGUE = re.compile(
    r"\b(?:checkpoints?|ckpts?)\b[^.;\n]{0,60}?"
    r"\b(discarded|deleted|pruned|removed|dropped|lost|gone|overwritten)\b",
    re.I,
)

# "42 minutes remaining", "12 min left", "budget is 3.5 minutes".
_BUDGET_NUMBER = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:gpu[- ])?(?:minutes?|mins?|min)\b[^.;\n]{0,20}?"
    r"\b(remaining|left|of budget|in the budget)\b"
    r"|\b(?:remaining|left|budget(?: is| of)?)\b[^.;\n]{0,20}?\b(\d+(?:\.\d+)?)\s*(?:gpu[- ])?(?:minutes?|mins?|min)\b",
    re.I,
)
_BUDGET_VAGUE = re.compile(
    r"\b(?:budget)\b[^.;\n]{0,40}?\b(?:remaining|left|exhausted|spent|gone|used up)\b"
    r"|\b(?:remaining|left)\b[^.;\n]{0,20}?\b(?:budget)\b",
    re.I,
)

# Job-state words, grouped by what they assert. Only these three buckets: they
# are what poll() can distinguish.
_ALIVE = "alive"
_SUCCEEDED = "succeeded"
_FAILED = "failed"

_STATUS_WORDS: dict[str, str] = {
    "still running": _ALIVE,
    "is running": _ALIVE,
    "are running": _ALIVE,
    "still training": _ALIVE,
    "still alive": _ALIVE,
    "in progress": _ALIVE,
    "has finished": _SUCCEEDED,
    "have finished": _SUCCEEDED,
    "is finished": _SUCCEEDED,
    "has completed": _SUCCEEDED,
    "is complete": _SUCCEEDED,
    "ran to completion": _SUCCEEDED,
    "has crashed": _FAILED,
    "is crashed": _FAILED,
    "has died": _FAILED,
    "is dead": _FAILED,
    "has failed": _FAILED,
    "is stopped": _FAILED,
    "was killed": _FAILED,
    "has been killed": _FAILED,
}

# How a probed status value maps onto those buckets. A value absent here is not
# judged, which is why the map is explicit rather than a prefix test.
_STATUS_BUCKET: dict[str, str] = {
    "pending": _ALIVE,
    "queued": _ALIVE,
    "running": _ALIVE,
    "succeeded": _SUCCEEDED,
    "failed": _FAILED,
    "cancelled": _FAILED,
    "canceled": _FAILED,
    "timeout": _FAILED,
    "lost": _FAILED,
}


def _as_int(token: str) -> int | None:
    token = (token or "").strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


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
                str(k): tuple(float(x) for x in v)
                for k, v in readings.items()
                if isinstance(v, (list, tuple))
            },
            probe_seq=int(data.get("probe_seq") or 0),
            shown_values=tuple(float(x) for x in (data.get("shown_values") or ())),
        )


@dataclass
class StateFindings:
    """Contradictions, and everything the checker could not resolve.

    ``contradicted`` is the only field that refuses anything. The other two
    exist so a pass can be read for what it is: ``unresolved`` is a claim the
    patterns saw and could not pin to a value, ``unverifiable`` is a claim with
    no fact to check it against. Any rate computed from this must print all
    three counts alongside it.
    """

    contradicted: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.contradicted) + len(self.unresolved) + len(self.unverifiable)

    def counts(self) -> dict[str, int]:
        return {
            "contradicted": len(self.contradicted),
            "unresolved": len(self.unresolved),
            "unverifiable": len(self.unverifiable),
            "claims_seen": self.checked,
        }


def _check_checkpoints(text: str, facts: StateFacts, out: StateFindings) -> None:
    have = facts.checkpoints
    named_absent = {m.group(1) for m in _ABSENT_NAMED.finditer(text)}
    named_present = {m.group(1) for m in _PRESENT_NAMED.finditer(text)}
    counts = [_as_int(m.group(1)) for m in _COUNT_KEPT.finditer(text)]
    counts += [_as_int(m.group(1)) for m in _COUNT_KEPT_ALT.finditer(text)]
    counts = [c for c in counts if c is not None]

    claims = bool(named_absent or named_present or counts)
    if have is None:
        if claims or _ABSENT_VAGUE.search(text):
            out.unverifiable.append("a claim about which checkpoints exist; none were listed")
        return

    present_ids = {m.group(1) for name in have for m in [_STEP_ID.search(name)] if m}
    # Sorted by step number where there is one, so a person reading the refusal
    # sees 200 before 1200 rather than the lexicographic order.
    def _order(name: str) -> tuple[int, Any]:
        m = _STEP_ID.search(name)
        return (0, int(m.group(1))) if m else (1, name)

    listing = ", ".join(sorted(have, key=_order)) or "(none)"

    for step in sorted(named_absent, key=lambda s: int(s)):
        if step in present_ids:
            out.contradicted.append(f"step-{step} is described as gone; the recorded listing disagrees")
    for step in sorted(named_present, key=lambda s: int(s)):
        if step not in present_ids:
            out.contradicted.append(f"step-{step} is described as kept; the recorded listing disagrees")
    for count in counts:
        if count != len(have):
            out.contradicted.append(
                f"the text says {count} checkpoint(s) are kept; the recorded listing disagrees"
            )

    # Vague absence with no id and no count: the words are there but there is
    # nothing to compare. Counted, never refused.
    if _ABSENT_VAGUE.search(text) and not claims:
        out.unresolved.append("a checkpoint is described as gone, without naming which or how many")


# Refusals name the disagreement and stop, without printing the recorded value.
# Two reasons. The remaining-budget subtraction is deliberately NOT done by
# ops_tune_status, because deciding when to look again is the judgement being
# measured -- printing it in a refusal hands it over through the back door. And a
# refusal is free to retry: the state check runs before dedupe registration, so a
# refused report neither lands on disk nor consumes its dedupe_key, which would
# make "guess a number, read the truth off the refusal, send a correct one" a
# reliable way to read state the tools do not expose. The baseline family is the
# exception and does print the value, because there the recorded value is one the
# loop was already given.
def _check_budget(text: str, facts: StateFacts, out: StateFindings) -> None:
    claimed: list[float] = []
    for m in _BUDGET_NUMBER.finditer(text):
        raw = m.group(1) or m.group(3)
        if raw:
            try:
                claimed.append(float(raw))
            except ValueError:
                continue

    if facts.remaining_minutes is None:
        if claimed or _BUDGET_VAGUE.search(text):
            out.unverifiable.append("a claim about remaining budget; no budget reading was taken")
        return

    actual = float(facts.remaining_minutes)
    tolerance = max(_BUDGET_ABS_TOLERANCE_MIN, actual * _BUDGET_REL_TOLERANCE)
    for value in claimed:
        if abs(value - actual) > tolerance:
            out.contradicted.append(
                f"the text says {value:g} minute(s) remain; the recorded reading disagrees"
            )
    if not claimed and _BUDGET_VAGUE.search(text):
        out.unresolved.append("the budget is described without a number")


def _check_job_status(text: str, facts: StateFacts, out: StateFindings) -> None:
    lowered = (text or "").lower()
    asserted = {phrase: bucket for phrase, bucket in _STATUS_WORDS.items() if phrase in lowered}
    if not asserted:
        return
    if facts.job_statuses is None:
        out.unverifiable.append("a claim about whether the job is running; no status was polled")
        return

    observed_raw = list(facts.job_statuses)
    observed = {_STATUS_BUCKET[s] for s in observed_raw if s in _STATUS_BUCKET}
    unmapped = [s for s in observed_raw if s not in _STATUS_BUCKET]
    if unmapped:
        # A status value with no row in the table cannot be compared. Reported
        # rather than treated as agreement, because the likeliest cause is a
        # missing row rather than a lying loop.
        out.unresolved.append(f"polled status not in the table: {', '.join(sorted(set(unmapped)))}")
    if not observed:
        return
    polled = ", ".join(sorted(set(observed_raw)))
    for phrase in sorted(asserted):
        if asserted[phrase] not in observed:
            out.contradicted.append(f'the text says the job "{phrase}"; the poll returned: {polled}')


def check(narrative: str, facts: StateFacts) -> StateFindings:
    """Claims about harness-held state that the facts contradict.

    Narrow by construction: see the module docstring for what it cannot see.
    Adding a family here can only add refusals, never remove one.
    """
    out = StateFindings()
    text = narrative or ""
    if not text.strip():
        return out
    _check_checkpoints(text, facts, out)
    _check_budget(text, facts, out)
    _check_job_status(text, facts, out)
    return out


async def collect_facts(backend: Any, records: list[Any], *, now_ms: int) -> StateFacts:
    """Probe the three fact classes off a backend that already has the handles.

    Duck-typed on purpose: a backend without ``remaining_minutes`` or
    ``list_artifacts`` yields ``None`` for that class, which refuses nothing.

    The checkpoint listing is only kept when exactly one trial produced one. With
    two, a bare "step-1200 was discarded" cannot be attributed to a trial, and a
    union across trials would refuse a true claim about the other one.
    """
    statuses: list[str] = []
    for rec in records:
        value = getattr(getattr(rec, "status", None), "value", None)
        if isinstance(value, str):
            statuses.append(value)

    remaining: float | None = None
    sources: dict[str, str] = {}
    getter = getattr(backend, "remaining_minutes", None)
    if callable(getter):
        try:
            remaining = await getter()
            if remaining is not None:
                sources["remaining_minutes"] = f"{type(backend).__name__}.remaining_minutes()"
        except Exception:
            remaining = None

    listings: list[tuple[str, tuple[str, ...]]] = []
    lister = getattr(backend, "list_artifacts", None)
    if callable(lister):
        for rec in records:
            handle = getattr(rec, "handle", None)
            if handle is None:
                continue
            try:
                found = await lister(handle)
            except Exception:
                found = None
            if found is not None:
                listings.append((getattr(rec, "idem_key", "?"), tuple(found)))

    checkpoints: tuple[str, ...] | None = None
    if len(listings) == 1:
        key, checkpoints = listings[0]
        sources["checkpoints"] = f"{type(backend).__name__}.list_artifacts() on {key}"
    elif len(listings) > 1:
        sources["checkpoints"] = f"not kept: {len(listings)} trials returned a listing, so a bare step id is ambiguous"

    return StateFacts(
        checkpoints=checkpoints,
        remaining_minutes=remaining,
        job_statuses=tuple(statuses) if statuses else None,
        observed_at_ms=now_ms,
        sources=sources,
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
