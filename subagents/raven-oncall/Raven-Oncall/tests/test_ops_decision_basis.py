"""Every decision has to cite a reading taken since the previous one.

Round 3 measured the gap this closes: the loop woke, read in-flight scores of
0.287-0.298 against a 0.3674 starting value it had been given, and its next
action was to schedule another 30 minutes of waiting. Waiting cost nothing and
recorded nothing.

The requirement is deliberately on all three decisions -- wait, kill, submit a
further round. Putting it only on waiting would add friction to one side of a
choice and none to the others, which is a push toward the others.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.ops.state_claims import (
    StateFacts,
    basis_problems,
    read_decision_seq,
    write_decision_seq,
    write_facts,
)


def _facts(tmp: Path, *, readings=(0.2904, 0.2874), seq=1) -> None:
    write_facts(tmp, StateFacts(metric_readings={"ndcg": tuple(readings)}, probe_seq=seq))


# ---- the check itself ----------------------------------------------------


def test_a_reading_from_a_fresh_probe_passes() -> None:
    facts = StateFacts(metric_readings={"ndcg": (0.2904, 0.2874)}, probe_seq=2)
    assert basis_problems("nDCG is 0.2904, still early in training", facts, last_seq=1) == []


def test_a_stale_reading_is_refused_however_true_it_is() -> None:
    """The hole the wording "last time I read 0.29" opens: a real number from an
    old probe otherwise satisfies every later decision, forever."""
    facts = StateFacts(metric_readings={"ndcg": (0.2904,)}, probe_seq=1)

    problems = basis_problems("last time I read 0.2904", facts, last_seq=1)

    assert len(problems) == 1
    assert "no observation has been recorded since the previous decision" in problems[0]


def test_a_number_that_was_never_read_is_refused() -> None:
    facts = StateFacts(metric_readings={"ndcg": (0.2904,)}, probe_seq=2)
    assert basis_problems("nDCG is 0.35", facts, last_seq=1) == [
        "the number in basis is not among the readings recorded for this campaign"
    ]


def test_prose_with_no_number_is_refused() -> None:
    facts = StateFacts(metric_readings={"ndcg": (0.2904,)}, probe_seq=2)
    assert basis_problems("it looks like it is still converging", facts, last_seq=1) == ["basis cites no number"]


def test_an_empty_basis_is_refused() -> None:
    facts = StateFacts(metric_readings={"ndcg": (0.2904,)}, probe_seq=2)
    assert basis_problems("   ", facts, last_seq=1) == ["basis is empty"]


def test_rounding_to_two_decimals_still_matches() -> None:
    """Same tolerance as the baseline gate. Accepting 0.29 for 0.2904 is the
    documented behaviour, not a leak: the check is whether a real reading is
    cited, never whether the reasoning off it is any good."""
    facts = StateFacts(metric_readings={"ndcg": (0.2904,)}, probe_seq=2)
    assert basis_problems("about 0.29", facts, last_seq=1) == []


def test_no_readings_recorded_does_not_refuse() -> None:
    """This assertion used to be the opposite, and the opposite was wrong.

    Refusing when the harness holds no readings blocks the kill of a run whose
    only output is a non-finite loss -- the case where killing is least in doubt.
    An existing simworld test caught it; the first version of this file had pinned
    the defect as expected behaviour."""
    facts = StateFacts(metric_readings={}, probe_seq=2)
    assert basis_problems("nDCG is 0.2904", facts, last_seq=1) == []


def test_the_refusal_never_quotes_the_recorded_readings() -> None:
    """Unlike a starting value the task already handed over, a reading is what the
    caller was supposed to go and fetch. Refusals are free to retry, so printing
    it would hand over a readings list to a caller that never looked."""
    facts = StateFacts(metric_readings={"ndcg": (0.2904, 0.2975, 0.2874)}, probe_seq=2)

    for basis in ("nDCG is 0.35", "no numbers here", "", "last time I read 0.2904"):
        for problem in basis_problems(basis, facts, last_seq=2):
            assert "0.2904" not in problem
            assert "0.2975" not in problem
            assert "0.2874" not in problem


# ---- the decision-side counter -------------------------------------------


def test_the_counter_starts_at_zero_and_round_trips(tmp_path: Path) -> None:
    assert read_decision_seq(tmp_path) == 0
    write_decision_seq(tmp_path, 4)
    assert read_decision_seq(tmp_path) == 4


def test_an_unreadable_decisions_file_reads_as_zero(tmp_path: Path) -> None:
    """Never block a decision over our own bookkeeping being corrupt."""
    (tmp_path / "decisions.json").write_text("not json", encoding="utf-8")
    assert read_decision_seq(tmp_path) == 0


# ---- symmetry: the same field on all three decisions ----------------------


def test_all_three_decision_tools_require_a_basis() -> None:
    """Friction on only one side of a choice is a nudge toward the other sides."""
    from raven.agent.tools.ops import OpsCheckLaterTool, OpsKillTool, OpsSubmitTool

    assert "basis" in OpsCheckLaterTool().parameters["required"]
    assert "basis" in OpsKillTool().parameters["required"]
    # submit exempts round 0 -- nothing has been observed yet -- so the field is
    # declared and enforced in execute() by round, not in the schema.
    assert "basis" in OpsSubmitTool().parameters["properties"]
    assert "round 1" in OpsSubmitTool().parameters["properties"]["basis"]["description"]


@pytest.mark.asyncio
async def test_check_later_refuses_without_a_fresh_reading(tmp_path: Path) -> None:
    from raven.agent.tools.ops import OpsCheckLaterTool

    (tmp_path / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    _facts(tmp_path, seq=1)
    write_decision_seq(tmp_path, 1)

    out = await OpsCheckLaterTool().execute(
        campaign="c",
        ledger=str(tmp_path / "ledger.json"),
        eta_seconds=1800,
        basis="last time I read 0.2904",
    )

    assert out.startswith("REFUSED")
    assert "0.2904" not in out


@pytest.mark.asyncio
async def test_kill_refuses_on_the_same_grounds(tmp_path: Path) -> None:
    from raven.agent.tools.ops import OpsKillTool

    (tmp_path / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    (tmp_path / "ledger.json").write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
    _facts(tmp_path, seq=1)
    write_decision_seq(tmp_path, 1)

    out = await OpsKillTool().execute(
        campaign="c", trials=["t1"], basis="it seems hopeless", ledger=str(tmp_path / "ledger.json")
    )

    assert out.startswith("REFUSED")


# ---- a run with no readings must still be killable ------------------------
# The gate's worst possible failure direction: a run whose only output is a
# non-finite loss records no readings at all, and that is exactly the run whose
# kill is least in doubt. Caught by an existing simworld test, not by design.


def test_no_readings_at_all_does_not_refuse() -> None:
    facts = StateFacts(probe_seq=2)
    assert basis_problems("loss went NaN at step 2", facts, last_seq=1) == []


def test_naming_a_non_finite_reading_counts_as_citing_it() -> None:
    """A number cannot match NaN, so naming it has to count -- and this is not the
    deleted "non-finite means diverged" hint: it says nothing about what the
    reading implies, only that the reading was real."""
    nan = float("nan")
    for readings in ((nan,), (2.0, nan)):
        facts = StateFacts(metric_readings={"loss": readings}, probe_seq=2)
        assert basis_problems("loss is NaN now", facts, last_seq=1) == []


def test_non_finite_readings_do_not_excuse_citing_nothing() -> None:
    facts = StateFacts(metric_readings={"loss": (float("nan"),)}, probe_seq=2)
    assert basis_problems("looks bad to me", facts, last_seq=1) == ["basis cites no number"]


def test_staleness_still_applies_when_there_are_no_readings() -> None:
    """The two checks are independent: not holding readings excuses the citation,
    never the requirement that an observation happened."""
    problems = basis_problems("loss went NaN", StateFacts(probe_seq=1), last_seq=1)
    assert len(problems) == 1
    assert "no observation has been recorded" in problems[0]
