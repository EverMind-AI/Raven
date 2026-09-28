"""The agency is the scenario's Analyst: what it had handed over and did not hold becomes a requirement, what waits
on material is handed over instead, and the decision follows from that in code. With an Analyst reading the owner's
words, the owner keeps its verdicts on a scorecard of its own and the Analyst decides."""

import json
from types import SimpleNamespace

import pytest

from experimental.analyst.run import NAME as FEEDBACK
from experimental.curator.harness import Task
from experimental.curator.raven_adapter.worker import Execution
from experimental.iteration.exchange import ExchangeError
from experimental.iteration.protocols import Exchange
from experimental.simulation.agency import NAME as REVIEW
from experimental.simulation.agency import Agency
from experimental.simulation.scenario import BUNDLED, Scenario
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest

TRAVEL = BUNDLED / "travel_agency"
QUOTE, DECK = "quote-sheet-correct", "deck-aesthetics"


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def response(arguments, name=REVIEW):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(name, name, arguments)])


def played(reply):
    records = [{"kind": "runner.event", "event_type": "Text", "event": {"content": reply}}]
    return [Exchange("Cheap trip?", Execution("t1", [], records, {}, "artifact-1"))]


def submission(scenario, failed=(), shortfalls=(), handover=(), remark="Prices were guessed."):
    rows = [
        {"id": criterion.id, "result": "fail" if criterion.id in failed else "pass", "session": "student"}
        for criterion in scenario.criteria
    ]
    return {"verdicts": rows, "shortfalls": list(shortfalls), "remark": remark, "handover": list(handover)}


def shortfall(*criteria, cause="not_held", material=None):
    return {
        "criteria": list(criteria),
        "cause": cause,
        "material": material,
        "behavior": "Quote only from the price list, for any party and season.",
        "observed": "In the student drill it guessed a price.",
        "evidence": ["student: About 300 each."],
        "acceptance": "Every price quoted appears in the price list for that party and season.",
        "strength": "should",
    }


def owner(tmp_path, provider, **options):
    scenario = Scenario.load(TRAVEL)
    home = tmp_path / "home"
    agency = Agency(
        scenario,
        provider,
        home / "skills",
        workdir=tmp_path / "work",
        deliver="dialog",
        uploads=home / "uploads",
        reply=lambda: SimpleNamespace(understanding="Prices come from the list."),
        **options,
    )
    agency.prepare()
    return scenario, agency


@pytest.mark.asyncio
async def test_what_did_not_hold_is_raised_and_what_waits_on_material_is_handed_over(tmp_path):
    scenario = Scenario.load(TRAVEL)
    missing = shortfall(DECK, cause="material_missing", material="brand-design-guide")
    provider = Provider(
        response(submission(scenario, {QUOTE, DECK}, [shortfall(QUOTE), missing], ["brand-design-guide"])),
        response(submission(scenario, {QUOTE}, [shortfall(QUOTE)], remark="Guessed again.")),
        response(submission(scenario, remark="All held.")),
    )
    scenario, agency = owner(tmp_path, provider, analysis="owner")
    worker = SimpleNamespace(root=tmp_path / "run")
    sessions = {"student": played("About 300 each.")}

    first = await agency.review(worker, sessions)
    feedback = first.feedback
    assert feedback.decision == "curate" and len(feedback.requirements) == 1
    (requirement,) = feedback.requirements
    assert requirement.strength == "must_hold" and requirement.expectation == "new" and requirement.recurrence == 0
    assert feedback.filtered == (f"{DECK}: waits on material handed over with this review (brand-design-guide)",)
    assert "brand-design-guide" in feedback.reason and first.handover == ("brand-design-guide",)
    (heard,) = first.relayed
    assert heard.items == () and heard.text.startswith("Prices were guessed.")
    assert scenario.document("brand-design-guide").strip() in heard.text
    assert "uploads/brand-design-guide/brand-design-guide.md" in heard.attachments
    assert {item.id for item in first.signals[0].items} == {criterion.id for criterion in scenario.criteria}
    (record,) = [json.loads(path.read_text()) for path in (worker.root / "analysis").glob("*.json")]
    assert record["requirement_criteria"] == [[QUOTE]] and record["feedback"]["decision"] == "curate"
    assert [row["cause"] for row in record["shortfalls"]] == ["not_held", "material_missing"]

    second = await agency.review(worker, sessions)
    (again,) = second.feedback.requirements
    assert again.expectation == "unmet" and again.recurrence == 1 and second.handover == ()
    earlier = json.loads(provider.requests[1]["messages"][1]["content"])["your_earlier_reviews"]
    assert earlier[0]["raised"] == [QUOTE] and earlier[0]["waiting_on_material"] == ["brand-design-guide"]

    third = await agency.review(worker, sessions)
    assert third.feedback.decision == "stop" and not agency.withheld


@pytest.mark.asyncio
async def test_every_failure_needs_a_shortfall_and_a_missing_material_must_be_one_still_held_and_handed_over(tmp_path):
    scenario = Scenario.load(TRAVEL)
    provider = Provider(
        response(submission(scenario, {QUOTE})),
        response(submission(scenario, {QUOTE}, [shortfall(QUOTE, cause="material_missing", material="price-list")])),
        response(
            submission(scenario, {DECK}, [shortfall(DECK, cause="material_missing", material="brand-design-guide")])
        ),
        response(
            submission(
                scenario,
                {DECK},
                [shortfall(DECK, cause="material_missing", material="brand-design-guide")],
                ["brand-design-guide"],
            )
        ),
    )
    scenario, agency = owner(tmp_path, provider, analysis="owner", max_calls=4)
    review = await agency.review(SimpleNamespace(root=tmp_path / "run"), {"student": played("Here.")})
    errors = [message["content"] for message in provider.requests[-1]["messages"] if message["role"] == "tool"]
    assert "give a shortfall for every failed criterion" in errors[0]
    assert "a missing material is one still withheld" in errors[1]
    assert "hand over 'brand-design-guide'" in errors[2]
    assert review.feedback.decision == "supplement" and review.feedback.requirements == ()
    assert review.handover == ("brand-design-guide",)


@pytest.mark.asyncio
async def test_a_failed_owner_call_is_recorded_as_a_failed_analysis(tmp_path):
    scenario = Scenario.load(TRAVEL)
    provider = Provider(response(submission(scenario, {QUOTE})))
    scenario, agency = owner(tmp_path, provider, analysis="owner", max_calls=1)
    worker = SimpleNamespace(root=tmp_path / "run")
    with pytest.raises(ExchangeError):
        await agency.review(worker, {"student": played("Here.")})
    (record,) = [json.loads(path.read_text()) for path in (worker.root / "analysis").glob("*.json")]
    assert record["source"] == "agency" and record["error"]


def scorecard(scenario, failed=(), waits=None, handover=(), remark="The price was guessed again."):
    rows = [
        {"id": criterion.id, "result": "fail" if criterion.id in failed else "pass", "session": "student"}
        for criterion in scenario.criteria
    ]
    for row in rows:
        row["waits_on"] = (waits or {}).get(row["id"])
    return {"verdicts": rows, "remark": remark, "handover": list(handover)}


@pytest.mark.asyncio
async def test_with_an_analyst_the_owner_keeps_a_scorecard_and_hands_over_what_a_miss_waits_on(tmp_path):
    scenario = Scenario.load(TRAVEL)
    waits = {DECK: "brand-design-guide"}
    provider = Provider(
        response(scorecard(scenario, {QUOTE}, waits)),
        response(scorecard(scenario, {QUOTE, DECK}, waits)),
        response(scorecard(scenario, {QUOTE, DECK}, waits, ["brand-design-guide"])),
        response({"decision": "supplement", "reason": "The owner hands over its design guide."}, FEEDBACK),
    )
    scenario, agency = owner(tmp_path, provider, max_calls=4)

    async def inspect():
        return SimpleNamespace(sources={}, facts={})

    worker = SimpleNamespace(
        root=tmp_path / "run", baseline=SimpleNamespace(task=Task(text="Serve")), last_plan=None, inspect=inspect
    )
    review = await agency.review(worker, {"student": played("About 300 each.")})
    errors = [message["content"] for message in provider.requests[2]["messages"] if message["role"] == "tool"]
    assert "only a failed verdict waits on a material" in errors[0]
    assert "hand over 'brand-design-guide'" in errors[1]
    assert review.feedback.decision == "supplement" and review.handover == ("brand-design-guide",)
    (spoken,) = review.signals
    assert review.relayed is None and spoken.items == () and spoken.satisfied is False
    assert spoken.text.startswith("The price was guessed again.")
    assert scenario.document("brand-design-guide").strip() in spoken.text
    assert agency.reviews[0]["waiting_on_material"] == ["brand-design-guide"] and "raised" not in agency.reviews[0]
    records = [json.loads(path.read_text()) for path in (worker.root / "analysis").glob("*.json")]
    (kept,) = [record for record in records if record.get("source") == "agency"]
    assert kept["waiting_on_material"] == waits and len(kept["scorecard"]["items"]) == len(scenario.criteria)
