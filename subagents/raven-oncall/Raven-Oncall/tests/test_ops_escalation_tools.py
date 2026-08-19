"""The two escalation paths, as tools.

These exist because the first real-task round had neither. The interruption
contract was written into the task prompt and wired to nothing, so the threshold
was never evaluated once; the report requirements were prose, so a final report
that never mentioned its baseline was accepted. Both tests below fail on a
prompt-only guarantee and pass only on a mechanical one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops_escalation import OpsAskOwnerTool, OpsFinishTool
from raven.ops.instrument import read_events


def _campaign(tmp_path: Path, contract: dict | None = None, start_hour: int = 9) -> Path:
    cdir = tmp_path / "camp"
    cdir.mkdir(parents=True, exist_ok=True)
    meta: dict = {"host": "h", "port": 1, "key": "k", "start_hour": start_hour}
    if contract is not None:
        meta["interruption_contract"] = contract
    (cdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return cdir


def _ledger(cdir: Path) -> str:
    return str(cdir / "ledger.json")


class _FakeMessageTool:
    """Stands in for the registered messaging tool that actually reaches a person."""

    def __init__(self, reply="Message sent"):
        self.sent: list[str] = []
        self._reply = reply

    async def execute(self, content, channel=None, chat_id=None, **kw):
        self.sent.append(content)
        return self._reply


class _FakeRegistry:
    def __init__(self, tool=None):
        self._tool = tool

    def get(self, name):
        return self._tool if name == "message" else None


def _ask(msg: _FakeMessageTool | None = None) -> OpsAskOwnerTool:
    """The tool with a delivery path wired, which is the normal case.

    Guard behaviour and delivery are separate concerns; a guard test that also
    had no messaging tool would fail for the wrong reason.
    """
    return OpsAskOwnerTool(registry=_FakeRegistry(msg if msg is not None else _FakeMessageTool()))



# --------------------------------------------------------------------------- #
# ops_ask_owner                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_interruption_over_the_bar_is_delivered(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 30 * 60_000})
    out = await _ask().execute(
        campaign="c", question="loss diverged; cancel?", expected_loss_minutes=200, ledger=_ledger(cdir)
    )
    assert "Delivered" in out
    assert json.loads((cdir / "interruptions.json").read_text())["allowed"] == 1


@pytest.mark.asyncio
async def test_an_interruption_under_the_bar_is_not_delivered(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 30 * 60_000})
    out = await _ask().execute(
        campaign="c", question="looks odd", expected_loss_minutes=4, ledger=_ledger(cdir)
    )
    assert "NOT DELIVERED" in out
    assert "under the" in out
    assert "Nobody has seen this" in out, "that nobody saw it is the load-bearing fact"
    assert "handle it yourself" not in out, "what to do next is the loop's call"
    state = json.loads((cdir / "interruptions.json").read_text())
    assert state["allowed"] == 0
    assert len(state["breach_attempts"]) == 1, "the attempt is the model signal and must be kept"


@pytest.mark.asyncio
async def test_the_interruption_budget_survives_a_cold_wake(tmp_path):
    """Each wake turn constructs the tool afresh, as a cron-woken turn does."""
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0, "max_asks": 1})
    first = await _ask().execute(
        campaign="c", question="q1", expected_loss_minutes=100, ledger=_ledger(cdir)
    )
    second = await _ask().execute(
        campaign="c", question="q2", expected_loss_minutes=100, ledger=_ledger(cdir)
    )
    assert "Delivered" in first
    assert "NOT DELIVERED" in second, (
        "a budget held only in memory is handed back on every wake, which is no budget at all"
    )


@pytest.mark.asyncio
async def test_an_unpriced_interruption_is_delivered_and_counted_apart(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 6 * 60 * 60_000})
    out = await _ask().execute(
        campaign="c", question="something I do not recognise", expected_loss_minutes=-1, ledger=_ledger(cdir)
    )
    assert "Delivered" in out
    assert "could not price" in out
    assert json.loads((cdir / "interruptions.json").read_text())["unestimated"] == 1


@pytest.mark.asyncio
async def test_a_campaign_with_no_contract_configured_does_not_get_a_secret_default(tmp_path):
    cdir = _campaign(tmp_path, contract=None)
    out = await _ask().execute(
        campaign="c", question="q", expected_loss_minutes=1, ledger=_ledger(cdir)
    )
    assert "Delivered" in out, "no contract means no threshold, not an invented one"


@pytest.mark.asyncio
async def test_the_delivered_reply_stays_at_the_level_of_fact(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0})
    out = await _ask().execute(
        campaign="c", question="q", expected_loss_minutes=100, ledger=_ledger(cdir)
    )
    assert "Delivered" in out
    assert "never reply" in out, "that the owner may not answer is a fact about the world"
    for directive in ("Silence is not consent", "ops_check_later", "keep watching"):
        assert directive not in out, (
            f"{directive!r} tells the loop how to handle silence, which is the exact "
            "failure this round scores; the contract is already in the task description"
        )


@pytest.mark.asyncio
async def test_every_ask_lands_in_the_campaign_trail(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 30 * 60_000})
    await _ask().execute(campaign="c", question="a", expected_loss_minutes=200, ledger=_ledger(cdir))
    await _ask().execute(campaign="c", question="b", expected_loss_minutes=1, ledger=_ledger(cdir))

    asks = [e for e in read_events(cdir) if e["kind"] == "ask_owner"]
    assert [a["allowed"] for a in asks] == [True, False]
    assert all("expected_loss_minutes" in a for a in asks)


# --------------------------------------------------------------------------- #
# ops_finish                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_relative_report_without_its_baseline_is_refused(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="train-a", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.3606}, condition_type="relative", ledger=_ledger(cdir),
    )
    assert "REFUSED" in out and "baseline" in out
    assert "STARTED at" in out, "the refusal must name what the field is"
    assert "unverifiable" not in out, (
        "saying why the comparison matters names the judgement the run is scored "
        "on, so a report that then carries the baseline is unattributable"
    )
    assert not (cdir / "reports.jsonl").exists()


@pytest.mark.asyncio
async def test_the_same_report_with_its_baseline_is_accepted(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="train-a", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.3606}, baseline={"ndcg": 0.3663},
        condition_type="relative", ledger=_ledger(cdir),
    )
    assert "Accepted" in out
    row = json.loads((cdir / "reports.jsonl").read_text().splitlines()[0])
    assert row["baseline"] == {"ndcg": 0.3663}


@pytest.mark.asyncio
async def test_a_report_whose_state_claim_the_readings_contradict_is_refused(tmp_path):
    """The round-1 failure: the loop asserted the peak checkpoint had probably
    been pruned because only three are kept, without looking. With a recorded
    listing, the count is checkable."""
    from raven.ops.state_claims import StateFacts, write_facts

    cdir = _campaign(tmp_path)
    write_facts(cdir, StateFacts(checkpoints=("step-200", "step-1200", "step-2400", "step-4800")))

    out = await OpsFinishTool().execute(
        campaign="c", subject="train-a", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.3606}, condition_type="absolute", ledger=_ledger(cdir),
        narrative="Only the three most recent checkpoints are kept, so the peak is gone.",
    )

    assert "REFUSED" in out
    assert "three most recent" in out or "3 checkpoint(s) are kept" in out
    # The refusal must NOT state the listing or the true count. No ops tool lists
    # checkpoints, and a refused report neither lands on disk nor consumes its
    # dedupe_key -- so printing the truth here made the refusal a free, repeatable
    # way to read state the tool surface withholds.
    assert "step-200" not in out
    assert "step-4800" not in out
    for advice in ("should", "you can", "instead", "try", "recommend"):
        assert advice not in out.lower(), "the refusal states facts, not what to do about them"
    assert not (cdir / "reports.jsonl").exists()
    kinds = [e.get("reason") for e in read_events(cdir) if e.get("kind") == "report_refused"]
    assert "state_claim_contradicted" in kinds


@pytest.mark.asyncio
async def test_a_state_claim_with_no_recorded_reading_is_accepted(tmp_path):
    """No probe means no refusal: "we did not look" must not be reported as "the
    loop is wrong"."""
    cdir = _campaign(tmp_path)

    out = await OpsFinishTool().execute(
        campaign="c", subject="train-a", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.3606}, condition_type="absolute", ledger=_ledger(cdir),
        narrative="Only the three most recent checkpoints are kept, so the peak is gone.",
    )

    assert "Accepted" in out


@pytest.mark.asyncio
async def test_a_state_claim_the_readings_support_is_accepted(tmp_path):
    from raven.ops.state_claims import StateFacts, write_facts

    cdir = _campaign(tmp_path)
    write_facts(cdir, StateFacts(checkpoints=("step-2400", "step-4800"), job_statuses=("running",)))

    out = await OpsFinishTool().execute(
        campaign="c", subject="train-a", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.3606}, condition_type="absolute", ledger=_ledger(cdir),
        narrative="step-2400 is still there and the job is still running.",
    )

    assert "Accepted" in out


@pytest.mark.asyncio
async def test_a_report_with_no_observation_is_refused(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="train-a", outcome="done", dedupe_key="k1",
        observed={}, condition_type="absolute", ledger=_ledger(cdir),
    )
    assert "REFUSED" in out and "observed" in out


@pytest.mark.asyncio
async def test_reporting_the_same_signal_twice_is_refused_across_cold_wakes(tmp_path):
    cdir = _campaign(tmp_path)
    kw = dict(campaign="c", subject="train-a", outcome="done", dedupe_key="k1",
              observed={"ndcg": 0.36}, condition_type="absolute", ledger=_ledger(cdir))
    assert "Accepted" in await OpsFinishTool().execute(**kw)
    out = await OpsFinishTool().execute(**kw)
    assert "REFUSED" in out and "already reported" in out
    assert len((cdir / "reports.jsonl").read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_a_different_finding_still_gets_through(tmp_path):
    cdir = _campaign(tmp_path)
    base = dict(campaign="c", subject="train-a", outcome="done",
                observed={"ndcg": 0.36}, condition_type="absolute", ledger=_ledger(cdir))
    assert "Accepted" in await OpsFinishTool().execute(dedupe_key="k1", **base)
    assert "Accepted" in await OpsFinishTool().execute(dedupe_key="k2", **base)


@pytest.mark.asyncio
async def test_needing_the_owner_to_choose_is_not_an_ending(tmp_path):
    """The old tool had a fourth kind, needs_decision, and it was the one that
    left campaigns open: a report that asked a question also closed nothing, so
    the loop had said its piece and the campaign kept waking. Asking is
    ops_ask_owner's job; this tool only ends things."""
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="train-a", outcome="needs_decision", dedupe_key="k1",
        observed={"ndcg": 0.36}, condition_type="absolute", ledger=_ledger(cdir),
    )
    assert "REFUSED" in out and "ops_ask_owner" in out
    assert not (cdir / "concluded.json").exists()


@pytest.mark.asyncio
async def test_refusals_and_acceptances_both_land_in_the_trail(tmp_path):
    cdir = _campaign(tmp_path)
    await OpsFinishTool().execute(campaign="c", subject="s", outcome="done", dedupe_key="k1",
                                  observed={}, condition_type="absolute", ledger=_ledger(cdir))
    await OpsFinishTool().execute(campaign="c", subject="s", outcome="done", dedupe_key="k1",
                                  observed={"ndcg": 0.36}, condition_type="absolute", ledger=_ledger(cdir))
    kinds = [e["kind"] for e in read_events(cdir)]
    assert "report_refused" in kinds and "report_accepted" in kinds


# --------------------------------------------------------------------------- #
# ops_ask_owner: the guard must sit in front of a door that actually opens     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_allowed_interruption_actually_reaches_the_messaging_tool(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0})
    msg = _FakeMessageTool()
    tool = _ask(msg)

    out = await tool.execute(campaign="c", question="cancel?", expected_loss_minutes=100,
                             ledger=_ledger(cdir))

    assert "Delivered" in out
    assert len(msg.sent) == 1, (
        "a guarded path that reaches nobody makes every interruption reading a "
        "reading of something that never happened"
    )
    assert "cancel?" in msg.sent[0]


@pytest.mark.asyncio
async def test_a_refused_interruption_never_reaches_the_messaging_tool(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 30 * 60_000})
    msg = _FakeMessageTool()
    await _ask(msg).execute(
        campaign="c", question="minor", expected_loss_minutes=1, ledger=_ledger(cdir)
    )
    assert msg.sent == [], "the guard is the enforcement; a refused ask must not be sent"


@pytest.mark.asyncio
async def test_a_delivery_failure_is_not_reported_as_delivered(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0})
    tool = _ask(_FakeMessageTool(reply="Error: not configured"))

    out = await tool.execute(campaign="c", question="q", expected_loss_minutes=100,
                             ledger=_ledger(cdir))

    assert "NOT delivered" in out or "NOT DELIVERED" in out
    assert "Nobody has seen it" in out
    assert "Delivered to the owner" not in out


@pytest.mark.asyncio
async def test_with_no_messaging_tool_registered_it_says_so(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0})
    out = await OpsAskOwnerTool(registry=_FakeRegistry(None)).execute(
        campaign="c", question="q", expected_loss_minutes=100, ledger=_ledger(cdir)
    )
    assert "NOT delivered" in out
    assert "no messaging tool" in out


@pytest.mark.asyncio
async def test_delivery_outcome_lands_in_the_campaign_trail(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0})
    await _ask().execute(
        campaign="c", question="q", expected_loss_minutes=100, ledger=_ledger(cdir)
    )
    deliveries = [e for e in read_events(cdir) if e["kind"] == "ask_owner_delivery"]
    assert deliveries and deliveries[0]["sent"] is True


# --------------------------------------------------------------------------- #
# ops_finish: a relative claim cannot slip through by omission                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_omitting_condition_type_is_refused_rather_than_defaulted(tmp_path):
    """The exact case an audit probe walked through: no condition_type, prose
    claiming improvement, no baseline -- previously accepted and stored."""
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="s", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.36},
        narrative="Training improved the model over where it started",
        ledger=_ledger(cdir),
    )
    assert "REFUSED" in out and "condition_type" in out
    assert not (cdir / "reports.jsonl").exists()


@pytest.mark.asyncio
async def test_prose_that_compares_cannot_be_declared_absolute_without_a_baseline(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="s", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.36}, condition_type="absolute",
        narrative="Training improved the model over where it started",
        ledger=_ledger(cdir),
    )
    assert "REFUSED" in out
    assert "improved" in out, "the refusal names the words that made it relative"
    assert not (cdir / "reports.jsonl").exists()


@pytest.mark.asyncio
async def test_the_same_claim_declared_relative_with_a_baseline_is_accepted(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="s", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.36}, baseline={"ndcg": 0.3674}, condition_type="relative",
        narrative="Training improved the model over where it started",
        ledger=_ledger(cdir),
    )
    assert "Accepted" in out


@pytest.mark.asyncio
async def test_an_absolute_claim_with_no_comparison_in_the_prose_is_fine(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c", subject="s", outcome="done", dedupe_key="k1",
        observed={"ndcg": 0.36}, condition_type="absolute",
        narrative="The run reached nDCG@10 of 0.36 and then exhausted its budget.",
        ledger=_ledger(cdir),
    )
    assert "Accepted" in out, "the net must not fire on a claim that makes no comparison"


# ---- the transcription check reaches the live tool, not only the eval world ----
# The layer that catches a wrong-but-plausible baseline was implemented on
# MockOrchestrator, which only the scripted eval world uses. On the real
# ops_finish path only the "is it a measurement" layer was active, so a report
# claiming baseline 0.30 against a stated 0.3674 would still have been accepted.


async def _report(cdir, **over):
    from raven.agent.tools.ops_escalation import OpsFinishTool

    args = dict(
        campaign="c", ledger=str(cdir / "ledger.json"), subject="eval_ndcg",
        outcome="done", dedupe_key="k1", observed={"best": 0.34},
        baseline={"ndcg": 0.30}, condition_type="relative", narrative="",
    )
    args.update(over)
    return await OpsFinishTool().execute(**args)


async def test_a_baseline_that_disagrees_with_the_campaign_meta_is_refused(tmp_path) -> None:
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "meta.json").write_text(
        json.dumps({"backend": "process", "expected_baseline": {"ndcg": 0.3674}}), encoding="utf-8"
    )

    out = await _report(cdir)

    assert out.startswith("REFUSED")
    assert "0.3674" in out
    assert not (cdir / "reports.jsonl").exists()


async def test_the_same_report_is_accepted_once_the_baseline_matches(tmp_path) -> None:
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "meta.json").write_text(
        json.dumps({"backend": "process", "expected_baseline": {"ndcg": 0.3674}}), encoding="utf-8"
    )

    out = await _report(cdir, baseline={"ndcg": 0.3674})

    assert out.startswith("Accepted")


async def test_a_campaign_without_an_expected_baseline_keeps_the_old_behaviour(tmp_path) -> None:
    """Most campaigns never state a starting value, and must not start failing."""
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")

    assert (await _report(cdir)).startswith("Accepted")
