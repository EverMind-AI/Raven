"""The three research modes are three stop rules, not three sizes of one budget.

Until 2026-09-07 every knob that differed between ``medium`` and ``high`` was a
permission -- a ceiling, a gate threshold, a timeout -- and a 5-question x
3-mode batch on deepseek-v4-flash-0731 found the two arms statistically
indistinguishable: iterations 6.6 vs 7.4, searches 5.6 vs 6.0, wall clock 51.9
vs 50.6 minutes, every paired difference inside the within-arm spread and half
of them the wrong sign. The cause is structural rather than numeric. What ends a
turn is the model deciding it has enough, and a ceiling the turn never
approaches cannot move that decision: the arms used 3-11 of the 20 and 30
iterations they were allowed.

So each mode now owns a different terminator, and these tests hold that shape:

* ``medium`` keeps the sufficiency release -- an independent judge may end the
  turn as soon as the retrieved pages decide the question;
* ``high`` and ``max`` drop that door entirely and terminate on the reviewer
  instead, which is why their reject -> revise path may run more than once and
  why a rejection buys a deeper retrieval round;
* ``max`` additionally demands an evidence floor before any draft ships, so a
  draft on too few readable pages or too few sites goes back to research.

``medium`` is also the only mode with a door BEFORE research: the plain-first gate
withholds the web tools for the first model call and lets a judged answer to a
settled question ship without a round. The deep modes are chosen for depth, so
their overlays turn it off.

The first version of this redesign gave max the retrieval ladder and a wider
evidence round instead, and a second batch found high and max INVERTED: both
levers need a trigger (a dry search streak, a reviewer's rejection) and neither
arrived in fifteen turns, so the two modes ran the same flow under two labels.
Hence the last rule below: what separates two modes has to be a gate consulted on
every draft, not one that waits on an event the turn may never produce.

The guard tests are the ones that would have caught both collapses: a mode has to
name a knob that changes which gate ends its turn, the retired overlay is kept
here as the counter-example that guard has to reject, and adjacent modes have to
differ on an unconditional terminator.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENT = REPO / "agents" / "raven-research"
RUN_PY = AGENT / "run.py"
PLUGIN_DIR = AGENT / "plugins" / "research-flow"

sys.path.insert(0, str(PLUGIN_DIR))
from research_flow.config import FlowConfig  # noqa: E402

#: The knobs that decide WHICH gate can end a turn. Everything else a mode may
#: carry -- a cap, a timeout, a gate's trigger threshold, a widen point -- only
#: says how much room the turn has or when a gate is consulted, and the batch
#: measured a mode built from those alone as the baseline wearing another label.
TERMINATOR_KEYS = frozenset(
    {
        "sufficiency.enabled",
        "verify.enabled",
        "verify.maxRevisions",
        "verify.reviewFinalDraft",
        "verify.evidenceRound",
        "evidenceFloor.enabled",
    }
)

#: The subset consulted on EVERY draft. ``verify.maxRevisions`` and
#: ``verify.evidenceRound`` only matter once a reviewer has rejected something, and on
#: the measured batch the reviewer rejected 2 drafts in 15 turns -- so two modes that
#: differ only on those ran identically. Adjacent modes must differ on one of these.
UNCONDITIONAL_TERMINATOR_KEYS = frozenset({"sufficiency.enabled", "verify.enabled", "evidenceFloor.enabled"})

#: ``modes/max.json`` as it stood between the two batches of 2026-09-07: high's
#: review bar plus a soonest-widening ladder and a wider evidence round. Every key
#: that differs from high's overlay here is a lever with a trigger, and the batch
#: measured max BELOW high on llm calls (0/5), searches (1/5) and fetches (1/5).
RETIRED_MAX_OVERLAY = {
    "maxIterations": None,
    "budgetNote": {"warnRatio": 0.8},
    "sufficiency": {"enabled": False},
    "verify": {
        "maxRevisions": 3,
        "reviewFinalDraft": True,
        "evidenceRound": True,
        "evidenceRoundSearches": 12,
        "timeoutSeconds": 360,
        "attemptTimeoutSeconds": 120,
    },
    "search": {"saturation": {"k": 3, "maxPages": 3}},
}

#: ``modes/high.json`` as it stood until 2026-09-07, the arm that measured
#: indistinguishable from the baseline. Kept as the counter-example the guard
#: below has to reject: every key in it is a limit or a trigger point, and not
#: one of them changes what ends the turn.
RETIRED_HIGH_OVERLAY = {
    "maxIterations": 30,
    "budgetNote": {"warnRatio": 0.8},
    "sufficiency": {"minSearches": 5, "timeoutSeconds": 60, "attemptTimeoutSeconds": 30},
    "verify": {"timeoutSeconds": 360, "attemptTimeoutSeconds": 120},
    "search": {"saturation": {"k": 10}},
    "finalShape": {"reportBounce": True},
}


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("agents_research_run_tiers", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def overlays(launcher) -> dict[str, dict]:
    """Each mode's ``drFlow`` diff, keyed by mode; the baseline's is empty."""
    out: dict[str, dict] = {}
    for mode in launcher.MODE_LABELS:
        if mode == launcher.BASELINE_MODE:
            out[mode] = {}
            continue
        raw = json.loads((AGENT / "modes" / f"{mode}.json").read_text(encoding="utf-8"))
        out[mode] = raw.get("drFlow") or {}
    return out


@pytest.fixture(scope="module")
def flows(overlays) -> dict[str, FlowConfig]:
    """The config each mode's session actually runs, merged the way the hook merges it."""
    slice_ = json.loads((AGENT / "config.json").read_text(encoding="utf-8"))["plugins"]["config"]["research-flow"]
    base = FlowConfig.from_slice(slice_)
    return {mode: base.with_overlay(diff) for mode, diff in overlays.items()}


def test_the_baseline_is_the_only_mode_with_an_early_release_door(flows):
    """A tier that can be released early and a tier that cannot are different products.

    The old shape gave every mode the same door at a different threshold
    (``minSearches`` 1 vs 5), which is a difference in when a judge is consulted,
    not in what ends the turn -- and on the measured batch the judge returned no
    usable verdict at either threshold, so the two arms ran identically.
    """
    assert flows["medium"].sufficiency.enabled is True
    assert flows["high"].sufficiency.enabled is False
    assert flows["max"].sufficiency.enabled is False


def test_only_the_baseline_may_answer_before_it_researches(flows):
    """The plain-first door is a medium-only shortcut; a deep mode never skips research."""
    assert flows["medium"].plain_first.enabled is True
    assert flows["medium"].plain_first.judge is True
    assert flows["high"].plain_first.enabled is False
    assert flows["max"].plain_first.enabled is False


def test_the_deep_modes_terminate_on_the_reviewer(flows):
    """High and max ship when the review is clean, so the revise path must be able to run."""
    medium = flows["medium"].verify
    assert (medium.max_revisions, medium.review_final_draft, medium.evidence_round) == (1, False, False)
    for mode in ("high", "max"):
        verify = flows[mode].verify
        assert verify.enabled is True
        assert verify.max_revisions > medium.max_revisions, f"{mode}: one revision is the baseline's promise"
        assert verify.review_final_draft is True, f"{mode}: the shipped draft must not be the unread one"
        assert verify.evidence_round is True, f"{mode}: a rejection must be able to buy retrieval"


def test_the_reviewer_gets_one_uninterrupted_attempt_in_every_mode(flows):
    """A slice shorter than the budget restarts a call that was about to answer: the
    reviewer ran 44-300s per call on the measured model, so the 40s and 120s slices
    failed 13 of 18 hard-batch reviews open. The slice equals the budget, and the cap
    clears the longest thinking seen (about 10k tokens)."""
    for mode, flow in flows.items():
        assert flow.verify.attempt_timeout_seconds == flow.verify.timeout_seconds, mode
        assert flow.verify.timeout_seconds >= 300, mode
        assert flow.verify.max_tokens >= 16384, mode
    assert (
        flows["high"].verify.timeout_seconds
        == flows["max"].verify.timeout_seconds
        > flows["medium"].verify.timeout_seconds
    )


def test_the_reviewer_is_asked_for_a_verdict_not_for_reasoning(flows):
    """Every mode pins the reviewer's effort low.

    Inherited from the agent default (``high``) the verdict -- one small JSON
    object -- was truncated before it was emitted on 6 of 15 turns of one batch,
    and each truncation failed the gate open. A tier whose promise is the review
    cannot rest on a reviewer that answers 60% of the time.
    """
    for mode, flow in flows.items():
        assert flow.verify.reasoning_effort == "low", mode


def test_only_max_demands_evidence_before_a_draft_ships(flows):
    """Max's stop rule is a demand, not an allowance: a draft below the floor is sent
    back to research on every draft, with no reviewer verdict or dry streak needed
    first. High and medium never floor a draft, so the floor is the axis that
    separates max from high the way the release door separates high from medium.
    """
    assert flows["medium"].evidence_floor.enabled is False
    assert flows["high"].evidence_floor.enabled is False
    floor = flows["max"].evidence_floor
    assert floor.enabled is True
    assert floor.min_pages > 0 and floor.min_domains > 0
    assert floor.max_rollbacks >= 1, "a floor that can never bounce is a note, not a floor"


def test_the_floor_is_set_above_where_high_lands_on_its_own(flows):
    """A floor high already clears organically separates nothing.

    High's five turns on the measured batch read 7 / 9 / 14 / 15 / 20 substantive
    pages; the floor sits at that distribution's upper end so it bites on the typical
    max turn. Re-derive both numbers from a larger batch before moving either.
    """
    high_pages_measured = (7, 9, 14, 15, 20)
    assert flows["max"].evidence_floor.min_pages > sorted(high_pages_measured)[3]


def test_max_still_buys_more_retrieval_per_rejection_than_high(flows):
    """The wider evidence round stays, as a secondary lever: it is conditional on a
    rejection, so it may not be the ONLY thing separating the two modes (see the
    guard below), but when a rejection does come max should recover more corpus."""
    assert flows["max"].verify.evidence_round_searches > flows["high"].verify.evidence_round_searches


def test_max_runs_the_baselines_retrieval_ladder(flows):
    """The soonest-widening ladder was max's first lever and it fired zero times in
    five turns (no dry streak ever formed); a knob that is never exercised is not a
    tier definition, so max no longer carries one."""
    assert flows["max"].search.saturation == flows["medium"].search.saturation


def test_the_caps_still_order_the_modes(flows, launcher):
    """A tier may raise ceilings too -- it just may not be only ceilings."""
    caps = {
        mode: launcher.iteration_cap(
            json.loads((AGENT / "config.json").read_text(encoding="utf-8"))["plugins"]["config"]["research-flow"],
            40,
            json.loads((AGENT / "modes" / f"{mode}.json").read_text(encoding="utf-8")) if mode != "medium" else {},
        )
        for mode in launcher.MODE_LABELS
    }
    assert caps["medium"] < caps["high"] < caps["max"]


def test_every_mode_changes_which_gate_ends_the_turn(overlays, launcher):
    """The regression this whole change exists to prevent.

    ``high`` used to differ from the baseline by two ceilings, two timeout
    pairs, one gate trigger point and two knobs that never fired. All of that is
    room and timing; none of it is a different ending, which is why the batch
    could not tell the two arms apart. A mode has to name at least one
    terminator knob, so the next overlay that only buys allowance fails here
    instead of in a three-hour batch.
    """
    for mode, diff in overlays.items():
        if mode == launcher.BASELINE_MODE:
            continue
        assert _terminator_keys(diff), (
            f"{mode}: this overlay moves no terminator knob, only room and timing. A mode "
            f"the turn never reaches the limit of is not a different mode -- give it a "
            f"different stop rule, not a bigger allowance. Terminators: "
            f"{sorted(TERMINATOR_KEYS)}"
        )


def test_the_guard_rejects_the_overlay_it_was_written_for(overlays):
    """The retired ``high`` overlay must fail the test above, or it guards nothing."""
    assert not _terminator_keys(RETIRED_HIGH_OVERLAY)
    assert _terminator_keys(overlays["high"]), "the shipped overlay must pass the guard it replaced"


def test_adjacent_modes_differ_on_a_gate_consulted_every_draft(overlays, launcher):
    """The second collapse: two modes whose only difference waits on an event.

    Each mode is compared with the one below it on the unconditional terminators
    alone. A pair that agrees on all of them differs only by allowance or by levers
    with triggers, which is the shape that measured as one arm twice.
    """
    order = list(launcher.MODE_LABELS)
    for lower, upper in zip(order, order[1:]):
        below = _terminator_values(overlays[lower], UNCONDITIONAL_TERMINATOR_KEYS)
        above = _terminator_values(overlays[upper], UNCONDITIONAL_TERMINATOR_KEYS)
        assert below != above, (
            f"{lower} -> {upper}: no unconditional terminator changes between these modes; "
            f"whatever else differs needs a trigger the turn may never produce. "
            f"Unconditional terminators: {sorted(UNCONDITIONAL_TERMINATOR_KEYS)}"
        )


def test_the_second_guard_rejects_the_overlay_it_was_written_for(overlays):
    """The retired ``max`` overlay passes the first guard (it names terminators) and
    must fail the second against the shipped high: on unconditional terminators the
    two are identical, which is exactly what the batch measured."""
    assert _terminator_keys(RETIRED_MAX_OVERLAY), "it did name terminators -- that is why the first guard missed it"
    retired = _terminator_values(RETIRED_MAX_OVERLAY, UNCONDITIONAL_TERMINATOR_KEYS)
    assert retired == _terminator_values(overlays["high"], UNCONDITIONAL_TERMINATOR_KEYS)
    assert retired != _terminator_values(overlays["max"], UNCONDITIONAL_TERMINATOR_KEYS)


def _terminator_keys(diff: dict, prefix: str = "") -> list[str]:
    """Leaf paths in a mode diff that change which gate can end the turn."""
    return sorted(_terminator_values(diff, TERMINATOR_KEYS))


def _terminator_values(diff: dict, keys: frozenset[str], prefix: str = "") -> dict[str, object]:
    """The terminator leaves a mode diff sets, with their values, restricted to ``keys``."""
    found: dict[str, object] = {}
    for key, value in diff.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found.update(_terminator_values(value, keys, f"{path}."))
        elif path in keys:
            found[path] = value
    return found


def test_every_modes_worst_case_rollbacks_fit_the_loops_cap(flows):
    """Each gate that can bounce a draft is bounded on its own; the loop caps their sum
    per turn and refuses the next rollback past it, which would leave a gate's promise
    unkept with no record of why. max sits exactly on the cap by design: one spin
    break, one salvage nudge, one report-shape bounce, three revisions and two floor
    bounces. A new bouncing gate, or a wider knob, has to take room from another.
    """
    from raven.agent.loop.main import AgentLoop

    cap = AgentLoop._MAX_HOOK_ROLLBACKS
    for mode, flow in flows.items():
        worst = (
            (flow.spin_breaker.max_triggers if flow.spin_breaker.enabled else 0)
            + (flow.force_finalize.max_nudges if flow.force_finalize.enabled else 0)
            + (1 if flow.final_shape.report_bounce else 0)
            + (flow.verify.max_revisions if flow.verify.enabled else 0)
            + (flow.evidence_floor.max_rollbacks if flow.evidence_floor.enabled else 0)
            + (1 if flow.plain_first.enabled else 0)
        )
        assert worst <= cap, f"{mode}: worst-case rollbacks {worst} exceed the loop cap {cap}"
    assert flows["max"].evidence_floor.enabled and flows["max"].plain_first.enabled is False
