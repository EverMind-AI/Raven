"""Program-side triage, one bounded model exchange and the recorded feedback stay faithful to the judgements."""

import json
from types import SimpleNamespace

import pytest

from experimental.analyst.feedback import Feedback
from experimental.analyst.materials import RECORDS, read_records
from experimental.analyst.run import NAME, Limits, analyse, triage
from experimental.curator.harness import Plan, Task
from experimental.curator.raven_adapter.worker import Execution
from experimental.iteration.exchange import ExchangeError
from experimental.iteration.protocols import Exchange, Item, Signal
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeWorker:
    def __init__(self, root, plan=None):
        self.baseline = SimpleNamespace(task=Task(text="Serve the agency's travellers"))
        self.root = root
        self.last_plan = plan

    async def inspect(self):
        return SimpleNamespace(sources={"skill.local/pricing": {}, "reference.index": {}, "skill.local/sop": {}})


def response(name, arguments):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(name, name, arguments)])


def execution(turn_id, reply, *extra):
    records = [{"kind": "runner.event", "event_type": "Text", "event": {"content": reply}}, *extra]
    return Execution(turn_id, [], records, {}, "artifact-1")


@pytest.fixture
def sessions():
    return {
        "student": [
            Exchange("Plan me a trip", execution("t1", "Here is a three-day itinerary.")),
            Exchange("How much?", execution("t2", "About 3000.", {"kind": "tool.error", "error": "no pricing tool"})),
        ]
    }


REQUIREMENT = {
    "behavior": "Before proposing an itinerary, ask for budget, dates, party size and departure city.",
    "observed": "In turn t1 the assistant proposed a three-day itinerary knowing only the destination.",
    "evidence": ["t1", "Here is a three-day itinerary."],
    "expectation": "new",
    "acceptance": "No itinerary appears before those four items have been asked for.",
    "strength": "must_hold",
}


def test_triage_decides_only_when_nothing_needs_interpretation():
    assert triage(()).decision == "continue"
    passed = Signal("verifier", items=(Item("asks budget", "pass"),), satisfied=True)
    assert triage((passed,)).decision == "continue"
    assert triage((Signal("verifier", items=(Item("asks budget", "fail"),), satisfied=False),)) is None
    assert triage((Signal("verifier", "Too pushy.", satisfied=True),)) is None
    assert triage((Signal("dataset", metrics={"pass_rate": 1.0}),)) is None
    assert triage((Signal("human", "Fine."),)) is None


def test_feedback_requires_requirements_exactly_for_curate():
    with pytest.raises(ValueError, match="curate requires"):
        Feedback(decision="curate", reason="r")
    with pytest.raises(ValueError, match="carry none"):
        Feedback(decision="continue", reason="r", requirements=(REQUIREMENT,))


@pytest.mark.asyncio
async def test_model_reads_records_then_submits_and_the_round_is_recorded(tmp_path, sessions):
    plan = Plan(
        understanding="u",
        design="d",
        changes=[{"target": "planning.strategy", "reason": "r", "expected": "asks first", "verification": "v"}],
    )
    worker = FakeWorker(tmp_path, plan)
    signals = (Signal("human", "It never asked my budget before recommending."),)
    provider = Provider(
        response(RECORDS, {"turn_id": "t2", "kind": "tool."}),
        response(NAME, {"decision": "curate", "reason": "The intake gap recurs.", "requirements": [REQUIREMENT]}),
    )
    previous = (Signal("dataset", metrics={"pass_rate": 0.5}),)
    feedback = await analyse(worker, provider, signals, sessions, previous_signals=previous, model="analyst-model")
    assert feedback.decision == "curate" and feedback.requirements[0].expectation == "new"
    materials = json.loads(provider.requests[0]["messages"][1]["content"])
    assert materials["signals"][0]["text"] == "It never asked my budget before recommending."
    assert materials["previous_signals"][0]["metrics"] == {"pass_rate": 0.5}
    assert materials["previous_expectations"][0]["expected"] == "asks first"
    assert materials["skills"] == ["skill.local/pricing", "skill.local/sop"]
    assert materials["sessions"]["student"][1]["errors"] == ["no pricing tool"]
    assert materials["sessions"]["student"][1]["record_kinds"] == {"runner.event": 1, "tool.error": 1}
    tools = {entry["function"]["name"] for entry in provider.requests[0]["tools"]}
    assert tools == {RECORDS, NAME}
    assert provider.requests[0]["tools"][0]["function"]["parameters"]["properties"]["turn_id"]["enum"] == ["t1", "t2"]
    tool_replies = [row for row in provider.requests[1]["messages"] if row["role"] == "tool"]
    query_result = json.loads(tool_replies[0]["content"])
    assert query_result["turn_id"] == "t2" and "no pricing tool" in query_result["text"]
    assert "runner.event" not in query_result["text"]
    assert all(request["model"] == "analyst-model" for request in provider.requests)
    record = json.loads(next((tmp_path / "analysis").glob("*.json")).read_text())
    assert record["feedback"]["decision"] == "curate" and record["materials"]["task"] == worker.baseline.task.text
    assert [row["event"] for row in record["trace"]] == ["model.call", "query", "model.call", NAME]


@pytest.mark.asyncio
async def test_invalid_submissions_are_returned_and_the_budget_is_final(tmp_path, sessions):
    worker = FakeWorker(tmp_path)
    signals = (Signal("human", "Not good."),)
    provider = Provider(
        response(NAME, {"decision": "curate", "reason": "r"}),
        LLMResponse(content="Let me think."),
    )
    with pytest.raises(ExchangeError, match="analyst: submission budget exhausted") as info:
        await analyse(worker, provider, signals, sessions, limits=Limits(max_calls=2))
    assert [row["event"] for row in info.value.trace] == [
        "model.call",
        "output.rejected",
        "model.call",
        "output.missing",
    ]
    rejection = json.loads(next(row for row in provider.requests[1]["messages"] if row["role"] == "tool")["content"])
    assert "curate requires" in rejection["error"]
    record = json.loads(next((tmp_path / "analysis").glob("*.json")).read_text())
    assert record["error"].startswith("analyst: submission budget exhausted") and "feedback" not in record


def test_record_query_filters_by_kind_and_bounds_offsets(sessions):
    result = read_records(sessions, {"turn_id": "t2", "kind": "runner", "length": 40})
    assert result["next_offset"] == 40 and result["total_characters"] > 40
    assert read_records(sessions, {"turn_id": "t1"})["next_offset"] is None
    with pytest.raises(ValueError, match="offset exceeds"):
        read_records(sessions, {"turn_id": "t1", "offset": 10**6})
    with pytest.raises(ValueError, match="no turn"):
        read_records(sessions, {"turn_id": "missing"})


def test_limits_reject_non_positive_bounds():
    for bad in ({"max_calls": 0}, {"call_timeout": 0}, {"call_timeout": float("inf")}):
        with pytest.raises(ValueError, match="finite positive"):
            Limits(**bad)


def test_read_records_rejects_an_unknown_turn_as_a_parameter_error(sessions):
    with pytest.raises(ValueError, match="no turn in this round has the id 'ghost'"):
        read_records(sessions, {"turn_id": "ghost"})
    assert json.loads(read_records(sessions, {"turn_id": "t2", "kind": "tool."})["text"])[0]["kind"] == "tool.error"


@pytest.mark.asyncio
async def test_the_last_call_offers_only_the_submission_tool(tmp_path, sessions):
    worker = FakeWorker(tmp_path)
    provider = Provider(
        response(RECORDS, {"turn_id": "t1"}),
        response(NAME, {"decision": "continue", "reason": "Nothing to change."}),
    )
    feedback = await analyse(worker, provider, (Signal("human", "Hm."),), sessions, limits=Limits(max_calls=2))
    assert feedback.decision == "continue"
    assert [entry["function"]["name"] for entry in provider.requests[0]["tools"]] == [RECORDS, NAME]
    assert [entry["function"]["name"] for entry in provider.requests[1]["tools"]] == [NAME]
    nudges = [
        row for row in provider.requests[1]["messages"] if row["role"] == "user" and "last call" in str(row["content"])
    ]
    assert len(nudges) == 1
