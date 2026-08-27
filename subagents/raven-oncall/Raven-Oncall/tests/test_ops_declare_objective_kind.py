"""A campaign declares which shape its target has, instead of always a number.

``metric`` and ``goal`` were required of every campaign, and not every campaign
has a number that ranks one round above another. Measured 2026-08-21 across seven
campaigns an arm declared for itself, three invented one to get past the
requirement:

  dam-break-lega    completion max   the task was "run to endTime, results usable"
  dam-break-legb2   completion max   the same
  beam-limit-load   total_load max   total_load is the load it CHOOSES each round,
                                     not something the solver reports, so best()
                                     was permanently None

The other four had a real number and got it right -- max_penetration min, l2_error
min, elapsed_seconds min twice -- which is the point. Reading the owner's intent is
what the loop is good at; being made to state it in a shape the task does not have
is what produced the fiction.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.tools.ops_declare import OpsDeclareTool


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every path this tool reads comes from the developer's own machine
    otherwise: the ops home, and the connection registry it checks the machine
    against. A suite whose result depends on whose laptop ran it is not a suite."""
    import raven.agent.tools.ops as ops_mod

    (tmp_path / "ops").mkdir()
    monkeypatch.setattr(ops_mod, "_ops_home", lambda: tmp_path / "ops")
    monkeypatch.setattr(
        "raven.ops.connections.get",
        lambda cid: {"id": "conn_cpu", "transport": "ssh"} if cid == "conn_cpu" else None,
    )
    monkeypatch.setattr("raven.ops.connections.describe", lambda: "conn_cpu")
    monkeypatch.setattr("raven.ops.connections.display_name", lambda cid: cid)
    return tmp_path


async def _declare(**over):
    args = dict(
        campaign="c",
        objective="get the dam-break case to endTime with usable results",
        objective_kind="complete",
        connection="conn_cpu",
        staged_case="/srv/case",
        command="bash {job_dir}/Allrun",
        remote_dir="/srv/rounds",
    )
    args.update(over)
    return await OpsDeclareTool().execute(**args)


def _meta(tmp_path):
    return json.loads((tmp_path / "ops" / "c" / "meta.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_run_to_the_end_needs_no_number(_isolated) -> None:
    """The declaration the two CFD cases could not make."""
    out = await _declare()

    assert "Declared 'c'" in out
    assert _meta(_isolated)["objective"] == {"kind": "complete"}


@pytest.mark.asyncio
async def test_the_readback_says_what_finished_means_rather_than_naming_a_metric(_isolated) -> None:
    out = await _declare(condition="reaches endTime=1.0 without the phase fraction leaving [0,1]")

    assert "run it to its own end" in out
    assert "finished when reaches endTime=1.0" in out


@pytest.mark.asyncio
async def test_an_optimize_campaign_still_needs_both_halves(_isolated) -> None:
    refusal = await _declare(objective_kind="optimize", metric="l2_error")

    assert refusal.startswith("REFUSED") and "'max' or 'min'" in refusal
    assert not (_isolated / "ops" / "c" / "meta.json").exists(), "nothing was written"


@pytest.mark.asyncio
async def test_optimize_without_a_metric_is_refused_and_points_at_complete(_isolated) -> None:
    """The refusal has to name the way out, or it just re-creates the pressure to
    invent a metric."""
    refusal = await _declare(objective_kind="optimize", goal="min")

    assert refusal.startswith("REFUSED")
    assert "complete" in refusal


@pytest.mark.asyncio
async def test_the_metric_must_be_something_the_run_reports(_isolated) -> None:
    """beam-limit-load's failure, stated where the declaration is made: an input
    the loop picks for each round cannot rank the rounds that picked it."""
    refusal = await _declare(objective_kind="optimize", goal="max")

    assert "cannot rank the rounds that chose it" in refusal


@pytest.mark.asyncio
async def test_an_optimize_campaign_records_the_number_as_before(_isolated) -> None:
    await _declare(objective_kind="optimize", metric="max_penetration", goal="min")

    assert _meta(_isolated)["objective"] == {
        "kind": "optimize", "metric": "max_penetration", "direction": "min",
    }


@pytest.mark.asyncio
async def test_a_condition_campaign_must_say_what_it_is_watching_for(_isolated) -> None:
    refusal = await _declare(objective_kind="condition")

    assert refusal.startswith("REFUSED") and "condition" in refusal


@pytest.mark.asyncio
async def test_a_condition_campaign_records_the_condition(_isolated) -> None:
    await _declare(
        objective_kind="condition",
        objective="buy 3 shares of VOLT if it drops more than 10%",
        condition="VOLT is 10% below its price at declare time",
    )

    assert _meta(_isolated)["objective"] == {
        "kind": "condition", "condition": "VOLT is 10% below its price at declare time",
    }


@pytest.mark.asyncio
async def test_a_condition_campaign_is_told_to_look_rather_than_to_submit(_isolated) -> None:
    """Its first move is to read the thing it is watching. "Submit round 0" names
    a step it has no reason to take."""
    out = await _declare(
        objective_kind="condition",
        condition="VOLT is 10% below its price at declare time",
    )

    assert "ops_check_later" in out and "Submit round 0" not in out


@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused_with_the_three_shapes(_isolated) -> None:
    refusal = await _declare(objective_kind="tuning")

    assert refusal.startswith("REFUSED")
    for shape in ("optimize", "condition", "complete"):
        assert shape in refusal


@pytest.mark.asyncio
async def test_a_missing_kind_is_refused_rather_than_assumed(_isolated) -> None:
    """The schema requires it; a required field is not always enforced by the
    provider, and assuming 'optimize' here would put the old requirement back."""
    refusal = await _declare(objective_kind="")

    assert refusal.startswith("REFUSED")


@pytest.mark.asyncio
async def test_a_complete_campaign_whose_words_ask_for_a_number_is_warned(_isolated) -> None:
    """A narrow second net under an easy mistake, in the same discipline as
    ops_finish's comparative word list: it can only warn, never refuse. A task can
    say "as fast as possible" about a run that has to finish before speed means
    anything, and refusing that would be worse than printing both."""
    out = await _declare(objective="summarise the batch, 越快越好")

    assert "Declared 'c'" in out
    assert "NOTE" in out and "optimize" in out


@pytest.mark.asyncio
async def test_an_optimize_campaign_is_not_warned_about_its_own_words(_isolated) -> None:
    out = await _declare(
        objective="get the L2 error as small as possible",
        objective_kind="optimize", metric="l2_error", goal="min",
    )

    assert "NOTE" not in out


@pytest.mark.asyncio
async def test_a_complete_campaign_may_still_record_a_number_it_watches(_isolated) -> None:
    """The optional optimisation dimension: recorded, and not what says the work
    is done. G2's two hard constraints are of this kind -- truncated_items and the
    memory peak decide whether the result is deliverable, and neither ranks rounds."""
    out = await _declare(metric="elapsed_seconds", goal="min")

    assert _meta(_isolated)["objective"]["metric"] == "elapsed_seconds"
    assert "not what says the work is done" in out
