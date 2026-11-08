"""The two escalation paths, as tools.

These exist because the first real-task round had neither. The interruption
contract was written into the task prompt and wired to nothing, so the threshold
was never evaluated once; the report requirements were prose, so a final report
that never mentioned its baseline was accepted. Both tests below fail on a
prompt-only guarantee and pass only on a mechanical one.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.escalation import unanswered_question  # noqa: E402
from oncall_flow.instrument import read_events  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops import OpsNoteTool  # noqa: E402
from oncall_flow.tools.ops_escalation import OpsAskOwnerTool, OpsFinishTool  # noqa: E402

from raven.acp.capabilities import ClientCapabilities  # noqa: E402
from raven.acp.outbound import OutboundRequests  # noqa: E402
from raven.acp.questions import ANSWER_FIELD, AcpQuestions  # noqa: E402
from raven.acp.updates import AcpSession, UpdateTranslator  # noqa: E402
from raven.plugins.context import RuntimeHandles  # noqa: E402
from raven.proactive_engine.schedulers.cron.grant import NamespacedWakeScheduler  # noqa: E402
from raven.proactive_engine.schedulers.cron.service import CronService  # noqa: E402
from raven.rpc.question_broker import QuestionBroker  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


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


def _ask() -> OpsAskOwnerTool:
    return OpsAskOwnerTool()


# --------------------------------------------------------------------------- #
# ops_ask_owner                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_interruption_over_the_bar_is_delivered(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 30 * 60_000})
    out = await _ask().execute(
        campaign="c", question="loss diverged; cancel?", expected_loss_minutes=200, ledger=_ledger(cdir)
    )
    assert "ALLOWED" in out
    assert json.loads((cdir / "interruptions.json").read_text())["allowed"] == 1


@pytest.mark.asyncio
async def test_an_interruption_under_the_bar_is_not_delivered(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 30 * 60_000})
    out = await _ask().execute(campaign="c", question="looks odd", expected_loss_minutes=4, ledger=_ledger(cdir))
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
    first = await _ask().execute(campaign="c", question="q1", expected_loss_minutes=100, ledger=_ledger(cdir))
    second = await _ask().execute(campaign="c", question="q2", expected_loss_minutes=100, ledger=_ledger(cdir))
    assert "ALLOWED" in first
    assert "NOT DELIVERED" in second, (
        "a budget held only in memory is handed back on every wake, which is no budget at all"
    )


@pytest.mark.asyncio
async def test_an_unpriced_interruption_is_delivered_and_counted_apart(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 6 * 60 * 60_000})
    out = await _ask().execute(
        campaign="c", question="something I do not recognise", expected_loss_minutes=-1, ledger=_ledger(cdir)
    )
    assert "ALLOWED" in out
    assert "could not price" in out
    assert json.loads((cdir / "interruptions.json").read_text())["unestimated"] == 1


@pytest.mark.asyncio
async def test_a_campaign_with_no_contract_configured_does_not_get_a_secret_default(tmp_path):
    cdir = _campaign(tmp_path, contract=None)
    out = await _ask().execute(campaign="c", question="q", expected_loss_minutes=1, ledger=_ledger(cdir))
    assert "ALLOWED" in out, "no contract means no threshold, not an invented one"


@pytest.mark.asyncio
async def test_the_delivered_reply_stays_at_the_level_of_fact(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0})
    out = await _ask().execute(campaign="c", question="q", expected_loss_minutes=100, ledger=_ledger(cdir))
    assert "ALLOWED" in out
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
        campaign="c",
        subject="train-a",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3606},
        condition_type="relative",
        ledger=_ledger(cdir),
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
        campaign="c",
        subject="train-a",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3606},
        baseline={"ndcg": 0.3663},
        condition_type="relative",
        ledger=_ledger(cdir),
    )
    assert "Accepted" in out
    row = json.loads((cdir / "reports.jsonl").read_text().splitlines()[0])
    assert row["baseline"] == {"ndcg": 0.3663}


@pytest.mark.asyncio
async def test_a_report_whose_state_claim_the_readings_contradict_is_refused(tmp_path):
    """The round-1 failure: the loop asserted the peak checkpoint had probably
    been pruned because only three are kept, without looking. With a recorded
    listing, the count is checkable."""
    from oncall_flow.state_claims import StateFacts, write_facts

    cdir = _campaign(tmp_path)
    write_facts(cdir, StateFacts(checkpoints=("step-200", "step-1200", "step-2400", "step-4800")))

    out = await OpsFinishTool().execute(
        campaign="c",
        subject="train-a",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3606},
        condition_type="absolute",
        ledger=_ledger(cdir),
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
        campaign="c",
        subject="train-a",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3606},
        condition_type="absolute",
        ledger=_ledger(cdir),
        narrative="Only the three most recent checkpoints are kept, so the peak is gone.",
    )

    assert "Accepted" in out


@pytest.mark.asyncio
async def test_a_state_claim_the_readings_support_is_accepted(tmp_path):
    from oncall_flow.state_claims import StateFacts, write_facts

    cdir = _campaign(tmp_path)
    write_facts(cdir, StateFacts(checkpoints=("step-2400", "step-4800"), job_statuses=("running",)))

    out = await OpsFinishTool().execute(
        campaign="c",
        subject="train-a",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.3606},
        condition_type="absolute",
        ledger=_ledger(cdir),
        narrative="step-2400 is still there and the job is still running.",
    )

    assert "Accepted" in out


@pytest.mark.asyncio
async def test_a_report_with_no_observation_is_refused(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c",
        subject="train-a",
        outcome="done",
        dedupe_key="k1",
        observed={},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    assert "REFUSED" in out and "observed" in out


@pytest.mark.asyncio
async def test_reporting_the_same_signal_twice_is_refused_across_cold_wakes(tmp_path):
    cdir = _campaign(tmp_path)
    kw = dict(
        campaign="c",
        subject="train-a",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    assert "Accepted" in await OpsFinishTool().execute(**kw)
    out = await OpsFinishTool().execute(**kw)
    assert "REFUSED" in out and "already reported" in out
    assert len((cdir / "reports.jsonl").read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_a_different_finding_still_gets_through(tmp_path):
    cdir = _campaign(tmp_path)
    base = dict(
        campaign="c",
        subject="train-a",
        outcome="done",
        observed={"ndcg": 0.36},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
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
        campaign="c",
        subject="train-a",
        outcome="needs_decision",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    assert "REFUSED" in out and "ops_ask_owner" in out
    assert not (cdir / "concluded.json").exists()


@pytest.mark.asyncio
async def test_refusals_and_acceptances_both_land_in_the_trail(tmp_path):
    cdir = _campaign(tmp_path)
    await OpsFinishTool().execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    await OpsFinishTool().execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="absolute",
        ledger=_ledger(cdir),
    )
    kinds = [e["kind"] for e in read_events(cdir)]
    assert "report_refused" in kinds and "report_accepted" in kinds


# --------------------------------------------------------------------------- #
# ops_ask_owner: the guard must sit in front of a door that actually opens     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delivery_outcome_lands_in_the_campaign_trail(tmp_path):
    cdir = _campaign(tmp_path, {"min_expected_loss_ms": 0})
    await _ask().execute(campaign="c", question="q", expected_loss_minutes=100, ledger=_ledger(cdir))
    deliveries = [e for e in read_events(cdir) if e["kind"] == "ask_owner_delivery"]
    assert deliveries and deliveries[0]["sent"] is False, (
        "model-mediated (D4): the trail records the allowance; the sending is the message tool's record"
    )


# --------------------------------------------------------------------------- #
# ops_finish: a relative claim cannot slip through by omission                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_omitting_condition_type_is_refused_rather_than_defaulted(tmp_path):
    """The exact case an audit probe walked through: no condition_type, prose
    claiming improvement, no baseline -- previously accepted and stored."""
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
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
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="absolute",
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
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        baseline={"ndcg": 0.3674},
        condition_type="relative",
        narrative="Training improved the model over where it started",
        ledger=_ledger(cdir),
    )
    assert "Accepted" in out


@pytest.mark.asyncio
async def test_an_absolute_claim_with_no_comparison_in_the_prose_is_fine(tmp_path):
    cdir = _campaign(tmp_path)
    out = await OpsFinishTool().execute(
        campaign="c",
        subject="s",
        outcome="done",
        dedupe_key="k1",
        observed={"ndcg": 0.36},
        condition_type="absolute",
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
    from oncall_flow.tools.ops_escalation import OpsFinishTool

    args = dict(
        campaign="c",
        ledger=str(cdir / "ledger.json"),
        subject="eval_ndcg",
        outcome="done",
        dedupe_key="k1",
        observed={"best": 0.34},
        baseline={"ndcg": 0.30},
        condition_type="relative",
        narrative="",
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


# --------------------------------------------------------------------------- #
# ops_ask_owner puts the question itself, and does not wait for it            #
# --------------------------------------------------------------------------- #


class _Owner:
    """The ``direct_ask`` grant, answering only when the test says so."""

    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.answers: list[asyncio.Future] = []

    async def __call__(self, prompt, choices, conversation_id, timeout_s):
        answer = asyncio.get_running_loop().create_future()
        self.asked.append({"prompt": prompt, "choices": choices, "conversation_id": conversation_id})
        self.answers.append(answer)
        try:
            return await answer
        except asyncio.CancelledError:
            # The broker's own shape: a cancelled question resolves to its default.
            return ""


def _asking_tool(tmp_path: Path, owner: _Owner, *, scheduler=None) -> OpsAskOwnerTool:
    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(wake_scheduler=scheduler, direct_ask=owner))
    tool.set_context("acp", "s1", session_key="acp:s1")
    return tool


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _put(tool: OpsAskOwnerTool, cdir: Path, **over) -> str:
    args = {
        "campaign": "c",
        "question": "Water leaves through the open top. Does that count as conserved?",
        "expected_loss_minutes": 60,
        "blocks_progress": False,
        "options": ["A: yes, count the outflow -- done now", "B: no, close the top -- 165 more core-minutes"],
        "if_unanswered": "A, and the report says so",
        "ledger": _ledger(cdir),
        **over,
    }
    return await tool.execute(**args)


def _kinds(cdir: Path) -> list[str]:
    return [e["kind"] for e in read_events(cdir)]


@pytest.mark.asyncio
async def test_an_allowed_ask_is_put_to_the_owner_and_the_tool_does_not_wait(tmp_path):
    """The model-mediated send wrote the question into the turn, where an ACP
    host gathers it into the run's transcript and the main session never sees
    it (2026-10-14). Through the grant it is a question of its own; the call
    returns while it is still unanswered, so the loop carries on."""
    cdir = _campaign(tmp_path)
    owner = _Owner()
    out = await _put(_asking_tool(tmp_path, owner), cdir)
    await _settle()

    assert "put to the owner as a question" in out and "Do not send it again" in out
    assert "message tool now" not in out
    assert owner.asked == [
        {
            "prompt": "[ops campaign 'c'] Water leaves through the open top. Does that count as conserved?\n"
            "If there is no answer: A, and the report says so",
            "choices": ["A: yes, count the outflow -- done now", "B: no, close the top -- 165 more core-minutes"],
            "conversation_id": "acp:s1",
        }
    ]
    assert not owner.answers[0].done(), "still waiting on the owner, and the tool already returned"
    delivery = [e for e in read_events(cdir) if e["kind"] == "ask_owner_delivery"][-1]
    assert delivery["sent"] is True and delivery["detail"] == "question"
    assert unanswered_question(cdir).startswith("Water leaves")


@pytest.mark.asyncio
async def test_the_owners_answer_lands_in_the_notes_and_pulls_the_wake(tmp_path):
    cdir = _campaign(tmp_path)
    grant = NamespacedWakeScheduler(CronService(tmp_path / "jobs.json", allowed_channels=None), "oncall-flow")
    owner = _Owner()
    tool = _asking_tool(tmp_path, owner, scheduler=grant)
    await _put(tool, cdir)
    await _settle()
    look = grant.pending_wakes("c")[0]
    assert look.schedule.at_ms > int(time.time() * 1000) + 60_000, "the ask armed its ten-minute look"

    owner.answers[0].set_result("A")
    await _settle()

    notes = [json.loads(line) for line in (cdir / "notes.jsonl").read_text().splitlines()]
    ask_id = [e for e in read_events(cdir) if e["kind"] == "ask_owner"][-1]["ask_id"]
    assert notes[-1]["note"] == "A" and notes[-1]["source"] == "owner" and notes[-1]["ask_id"] == ask_id
    answered = [e for e in read_events(cdir) if e["kind"] == "owner_answered"][-1]
    assert answered["ask_id"] == ask_id and answered["via"] == "question" and answered["woke"] is True
    assert grant.pending_wakes("c")[0].schedule.at_ms <= int(time.time() * 1000), "read within seconds"
    assert unanswered_question(cdir) == ""


@pytest.mark.asyncio
async def test_a_question_closed_without_an_answer_stays_open(tmp_path):
    """The host shows a question for its own while; an empty answer is that
    window closing, or the owner dismissing it. Neither is an answer, and the
    question stays for the ask's own wake to follow up on."""
    cdir = _campaign(tmp_path)
    owner = _Owner()
    await _put(_asking_tool(tmp_path, owner), cdir)
    await _settle()
    owner.answers[0].set_result("")
    await _settle()

    unanswered = [e for e in read_events(cdir) if e["kind"] == "ask_owner_unanswered"]
    assert unanswered and unanswered[-1]["reason"] == "closed without an answer"
    assert unanswered_question(cdir).startswith("Water leaves")
    assert not (cdir / "notes.jsonl").exists()


@pytest.mark.asyncio
async def test_a_new_question_withdraws_the_one_before_and_its_late_answer_closes_nothing(tmp_path):
    cdir = _campaign(tmp_path)
    owner = _Owner()
    tool = _asking_tool(tmp_path, owner)
    await _put(tool, cdir)
    await _settle()
    await _put(tool, cdir, question="Second question: may I use more cores?", if_unanswered="stay at one core")
    await _settle()

    asks = [e for e in read_events(cdir) if e["kind"] == "ask_owner"]
    withdrawn = [e for e in read_events(cdir) if e["kind"] == "ask_withdrawn"]
    assert [w["ask_id"] for w in withdrawn] == [asks[0]["ask_id"]]
    assert owner.answers[0].cancelled(), "the first form was taken down"
    assert "ask_owner_unanswered" not in _kinds(cdir), "a withdrawn question is not recorded as unanswered too"
    assert unanswered_question(cdir) == "Second question: may I use more cores?"


@pytest.mark.asyncio
async def test_settling_on_the_default_lets_the_campaign_close_and_the_report_says_so(tmp_path):
    """Asked, nobody answered, the loop went ahead as it said it would. The
    close stops refusing once that is recorded as such -- not on any note the
    loop happens to write -- and the report names the decision the owner
    never made."""
    cdir = _campaign(tmp_path)
    owner = _Owner()
    await _put(_asking_tool(tmp_path, owner), cdir)
    await _settle()

    async def finish() -> str:
        return await OpsFinishTool().execute(
            campaign="c",
            subject="dam break",
            outcome="done",
            dedupe_key="final",
            observed={"water_change_pct": -1.3},
            condition_type="absolute",
            narrative="Both rounds reached the end time.",
            ledger=_ledger(cdir),
        )

    tools_base.set_home(tmp_path)
    note = OpsNoteTool()
    await note.execute(campaign="c", note="round 2 is done", ledger=_ledger(cdir))
    assert "REFUSED" in await finish(), "a progress note does not answer the owner's question"

    out = await note.execute(
        campaign="c",
        note="Going ahead with A as said: the outflow counts.",
        ledger=_ledger(cdir),
        settles_question=True,
    )
    assert "Settled without the owner" in out

    closed = await finish()
    assert "is closed" in closed
    assert "decided without the owner: Water leaves through the open top." in closed
    assert "Going ahead with A as said" in closed
    assert owner.answers[0].cancelled(), "closing takes the open form down"
    assert [e["reason"] for e in read_events(cdir) if e["kind"] == "ask_withdrawn"] == ["campaign closed"]


@pytest.mark.asyncio
async def test_without_the_grant_the_ask_falls_back_to_the_message_tool(tmp_path):
    cdir = _campaign(tmp_path)
    out = await _ask().execute(
        campaign="c",
        question="count the outflow?",
        expected_loss_minutes=60,
        if_unanswered="count it",
        ledger=_ledger(cdir),
    )
    assert "message tool now" in out and "If there is no answer: count it" in out
    assert [e for e in read_events(cdir) if e["kind"] == "ask_owner_delivery"][-1]["sent"] is False


@pytest.mark.asyncio
async def test_the_question_reaches_an_acp_host_as_an_elicitation(tmp_path):
    """The real path past the grant: the runtime's question broker emits
    ``clarify.request``, the ACP bridge turns it into ``elicitation/create``
    for a host that declared forms, and the host's answer comes back as the
    owner's."""
    cdir = _campaign(tmp_path)
    frames: list[dict] = []
    outbound: OutboundRequests

    def emit(frame: dict) -> None:
        frames.append(frame)
        if frame.get("method") == "elicitation/create":
            reply = {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {
                    "action": "accept",
                    "content": {
                        ANSWER_FIELD: frame["params"]["requestedSchema"]["properties"][ANSWER_FIELD]["enum"][0]
                    },
                },
            }
            asyncio.get_running_loop().call_soon(outbound.resolve, reply)

    outbound = OutboundRequests(emit=emit)
    translator = UpdateTranslator(emit=emit)
    translator.add(AcpSession(session_id="acp:s1", session_key="acp:s1", cwd="/w", subscription_id="sub"))
    questions = AcpQuestions(outbound=outbound, translator=translator, emit=emit)
    questions.set_client(ClientCapabilities.from_params({"clientCapabilities": {"elicitation": {"form": {}}}}))

    async def send_frame(frame: dict) -> None:
        questions.handle(frame["method"], frame["params"])

    broker = QuestionBroker(send_frame)
    questions.set_broker(broker)

    async def direct_ask(prompt, choices, conversation_id, timeout_s):
        return await broker.await_question(conversation_id, prompt=prompt, choices=choices or [], timeout_s=timeout_s)

    tool = OpsAskOwnerTool()
    tool.bind_runtime(RuntimeHandles(direct_ask=direct_ask))
    tool.set_context("acp", "s1", session_key="acp:s1")
    await _put(tool, cdir)
    for _ in range(50):
        if "owner_answered" in _kinds(cdir):
            break
        await asyncio.sleep(0.01)

    created = [f for f in frames if f.get("method") == "elicitation/create"]
    assert len(created) == 1 and created[0]["params"]["sessionId"] == "acp:s1"
    assert "Does that count as conserved?" in created[0]["params"]["message"]
    note = json.loads((cdir / "notes.jsonl").read_text().splitlines()[-1])
    assert note["note"].startswith("A: yes") and note["source"] == "owner"
    assert unanswered_question(cdir) == ""
