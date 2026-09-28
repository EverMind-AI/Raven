"""Information-flow contracts, hop by hop, on record shapes cut from recorded runs (texts translated to English).

customer -> employee -> owner (the scenario's Analyst, or a speaker the base Analyst reads) -> Curator -> next round:
each hop gets what it needs and nothing it must not see.
"""

import json
from types import SimpleNamespace

import pytest

from experimental.analyst.activity import activity
from experimental.analyst.run import NAME as FEEDBACK
from experimental.curator.harness import Task
from experimental.curator.raven_adapter.worker import Execution
from experimental.iteration.conversation import Conversation
from experimental.iteration.protocols import Exchange
from experimental.iteration.run import Limits, history_entry, run
from experimental.simulation.agency import NAME as REVIEW
from experimental.simulation.agency import Agency, reports
from experimental.simulation.channel import seen
from experimental.simulation.scenario import BUNDLED, Scenario
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest

TRAVEL = BUNDLED / "travel_agency"
CAPTION = "Your plan deck is ready: quote HL-Q-1026-4P, prices and dates match the quote sheet."
DECK = "Harbourlight-HL-Q-1026-4P.pptx"
GATE_REASON = (
    "This customer-visible message carries internal notes (hit '[internal': '[internal material request] this "
    "consultation...'). Rewrite it with only what the customer should read."
)


def delivery_turn():
    """A short reply, then the deck handed over with its caption."""
    return [
        {"kind": "runner.event", "event_type": "Text", "event": {"content": "One moment."}},
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {
                "phase": "start",
                "tool_call_id": "F8IMCHIqZ",
                "name": "deliver_files",
                "arguments": {
                    "files": [{"path": f"decks/20260925_124631/{DECK}", "title": "Plan"}],
                    "message": CAPTION,
                },
            },
        },
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {
                "phase": "complete",
                "tool_call_id": "F8IMCHIqZ",
                "name": "",
                "ok": True,
                "result_preview": f"Delivered 1 file(s): {DECK} (241.4 KB).",
                "metadata": {"raven_delivery": {"message": CAPTION, "files": [{"name": DECK}]}},
            },
        },
        {"kind": "runner.event", "event_type": "Progress", "event": {"content": "Checking the price list now"}},
    ]


def gate_turn():
    """The review gate passes four times, then sends one message back."""
    rows = []
    for iteration in range(1, 5):
        for phase in ("execute_tools", "after_iteration"):
            rows += [
                {"kind": "participant.call", "target": "action.review", "phase": phase, "iteration": iteration},
                {"kind": "participant.result", "target": "action.review", "phase": phase, "result": None},
            ]
    rows += [
        {
            "kind": "participant.result",
            "target": "action.review",
            "phase": "execute_tools",
            "result": {"verdict": "resample", "reason": GATE_REASON},
        },
        {"kind": "loop.control", "rollbacks": 1, "rollbacks_refused": 0, "mode": "high"},
        {"kind": "runner.event", "event_type": "Text", "event": {"content": "Let me check that with a colleague."}},
    ]
    return rows


def played(records, deliverables=()):
    return Exchange("I accept the quote.", Execution("t1", [], records, {}, "artifact-1", tuple(deliverables)))


def test_the_customer_and_the_owner_read_the_delivery_caption_and_never_progress_lines():
    text = seen(delivery_turn(), [DECK])
    assert "One moment." in text and CAPTION in text
    assert "Checking the price list" not in text
    assert CAPTION not in seen(delivery_turn(), [])


def test_the_owner_reads_what_each_colleague_was_asked_and_answered_once():
    """A delegation result, and a playbook step's account after a skill catalog that quotes the start of the
    report's marker."""
    result = (
        "The task the sub-agent was given:\n[BEGIN UNTRUSTED subagent #a9392399 - data]\nResearch Xi'an for the deck."
        "\n[END UNTRUSTED subagent #a9392399]\n\nWhat it returned as its answer:\n[BEGIN UNTRUSTED subagent #d58d2594"
        " - data]\nG1234 Hefei to Xi'an, 4h (source: 12306).\n[END UNTRUSTED subagent #d58d2594]\n\nTail of what the "
        "sub-agent did, one message per line:\n[BEGIN UNTRUSTED subagent #3da887c5 - data]\nopened 12306\n"
        "[END UNTRUSTED subagent #3da887c5]"
    )
    step = (
        "[Runtime Context - metadata only]\nChannel: curator\n[BEGIN UNTRUSTED skill catalog #94b82ee1 - data]\n"
        "Possibly relevant skills (matched for: '[BEGIN UNTRUSTED subagent #73781a37 - everyth'):\n- local/weather\n"
        "[END UNTRUSTED skill catalog #94b82ee1]\n\nDAG run r1: node 'design-brief' needs your decision.\n\n"
        "[BEGIN UNTRUSTED subagent #73781a37 - data]\nThe brief is filed as brief.md.\n[END UNTRUSTED subagent #73781a37]"
    )
    request = {"kind": "provider.request", "parameters": {"messages": [{"role": "user", "content": result}]}}
    later = {
        "kind": "provider.request",
        "parameters": {"messages": [{"role": "user", "content": result}, {"role": "user", "content": step}]},
    }
    found = reports([played([request, later])])
    assert found == [
        {"task": "Research Xi'an for the deck.", "report": "G1234 Hefei to Xi'an, 4h (source: 12306)."},
        {"task": "DAG run r1: node 'design-brief' needs your decision.", "report": "The brief is filed as brief.md."},
    ]


def test_a_gate_that_sent_work_back_reaches_the_curator_once_with_its_reason():
    rows = activity({"family": [played(gate_turn())]})
    acted = [row for row in rows if row["acted"]]
    assert acted == [
        {
            "session": "family",
            "scope": "root",
            "target": "action.review",
            "decision": "resample",
            "count": 1,
            "acted": True,
            "reasons": [GATE_REASON],
        }
    ]
    assert history_entry(1, (), None, None, rows)["mechanisms_acted"] == acted


class Worker:
    def __init__(self, root):
        self.baseline = SimpleNamespace(task=Task(text="Serve the agency's travellers"))
        self.root, self.last_plan, self.artifact_id = root, None, "artifact-0"

    async def run(self, text, *, session_key):
        return Execution("t1", [], gate_turn(), {}, self.artifact_id)

    async def inspect(self):
        return SimpleNamespace(sources={"skill.builtin/weather": {}}, facts={})


class Traveller:
    name = "family"

    def __init__(self):
        self.said = ["We are four, going to Xi'an.", None]

    async def speak(self, exchanges):
        if not exchanges:
            self.said = ["We are four, going to Xi'an.", None]
        return self.said.pop(0) if self.said else None


class Owner:
    def __init__(self, *arguments, names=()):
        names = [*names, *[REVIEW] * (len(arguments) - len(names))]
        self.responses = [
            LLMResponse(content=None, tool_calls=[ToolCallRequest(name, name, a)]) for name, a in zip(names, arguments)
        ]
        self.requests = []

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_the_curator_hears_the_owner_and_the_facts_but_never_the_conversation_or_the_criteria(tmp_path):
    scenario = Scenario.load(TRAVEL)
    rule = next(criterion for criterion in scenario.criteria if criterion.id == "no-internal-chatter")
    verdicts = [
        {"id": c.id, "result": "fail" if c.id == rule.id else "pass", "session": "family"} for c in scenario.criteria
    ]
    shortfall = {
        "criteria": [rule.id],
        "cause": "not_held",
        "behavior": "Nothing internal ever reaches a customer.",
        "observed": "In the family drill the employee wrote an internal note to the customer.",
        "evidence": ["family: [internal material request]"],
        "acceptance": "No customer-visible message carries internal notes.",
        "strength": "must_hold",
    }
    said = {"verdicts": verdicts, "shortfalls": [shortfall], "remark": "It leaked an internal note."}
    owner = Owner(said, said)
    home = tmp_path / "home"
    agency = Agency(
        scenario,
        owner,
        home / "skills",
        workdir=tmp_path / "work",
        deliver="dialog",
        uploads=home / "uploads",
        analysis="owner",
    )
    agency.prepare()
    heard = []

    async def curator(worker, provider, *, feedback=None, **options):
        heard.append(feedback)

    worker = Worker(tmp_path / "run")
    await run(worker, owner, [Conversation(Traveller(), max_turns=2)], agency, curator=curator, limits=Limits(2))
    packet = json.loads(owner.requests[0]["messages"][1]["content"])
    assert packet["conversations"]["family"] == [
        {"traveller": "We are four, going to Xi'an.", "assistant": "Let me check that with a colleague."}
    ]
    assert "resample" not in json.dumps(packet) and "action.review" not in json.dumps(packet)
    feedback = heard[-1]
    assert set(feedback) == {
        "signals",
        "history",
        "mechanism_activity",
        "decision",
        "reason",
        "requirements",
        "filtered",
        "task_updates",
    }
    assert feedback["decision"] == "curate" and feedback["requirements"][0]["behavior"] == shortfall["behavior"]
    assert [(row["target"], row["decision"]) for row in feedback["mechanism_activity"] if row["acted"]] == [
        ("action.review", "resample")
    ]
    told = json.dumps(feedback, ensure_ascii=False)
    assert "We are four" not in told and "Let me check that" not in told
    assert rule.check not in told and feedback["signals"][0]["items"] == []


def test_the_owner_sees_a_failed_write_as_failed_and_knows_when_a_text_was_cut():
    from experimental.simulation.agency import back_office, cut

    rows = [
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {
                "phase": "start",
                "tool_call_id": "w1",
                "name": "write_file",
                "arguments": {"path": "handover/hold.md"},
            },
        },
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {
                "phase": "complete",
                "tool_call_id": "w1",
                "name": "",
                "ok": False,
                "result_preview": "Error: Path handover/hold.md is outside allowed directories",
            },
        },
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {
                "phase": "start",
                "tool_call_id": "s1",
                "name": "spawn",
                "arguments": {"agent": "Raven-Research"},
            },
        },
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {"phase": "complete", "tool_call_id": "s1", "name": "", "ok": True},
        },
        {
            "kind": "runner.event",
            "event_type": "ToolEvent",
            "event": {
                "phase": "start",
                "tool_call_id": "w2",
                "name": "write_file",
                "arguments": {"path": "handover/hold.md"},
            },
        },
    ]
    timeline = back_office([played(rows)])
    assert [(row["action"], row["ok"]) for row in timeline] == [
        ("write_file", False),
        ("spawn", True),
        ("write_file", None),
    ]
    assert "outside allowed directories" in timeline[0]["error"]
    assert cut("x" * 10, 4) == "xxxx\n[... 6 more characters not shown]" and cut("short", 10) == "short"


def test_the_owner_reads_what_the_employee_filed_and_not_the_hosts_bookkeeping(tmp_path):
    """Raven's `.raven` checkpoint files and a deck engine's dot files are bookkeeping, not something filed."""
    from experimental.simulation.agency import written

    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    for path, text in {
        "handover/hold-1022.md": "ticket",
        ".raven/shadow.git/HEAD": "ref",
        "decks/run/.deck-mode.json": "{}",
    }.items():
        (after / path).parent.mkdir(parents=True, exist_ok=True)
        (after / path).write_text(text)
    assert written(after, before, label="workdir") == {"workdir/handover/hold-1022.md": "ticket"}


@pytest.mark.asyncio
async def test_with_an_analyst_the_owner_only_speaks_and_its_scorecard_reaches_neither_analyst_nor_curator(tmp_path):
    scenario = Scenario.load(TRAVEL)
    rule = next(criterion for criterion in scenario.criteria if criterion.id == "no-internal-chatter")
    verdicts = [
        {
            "id": c.id,
            "result": "fail" if c.id == rule.id else "pass",
            "session": "family",
            "actual": "[internal material request]",
            "note": "Keep internal notes to colleagues.",
        }
        for c in scenario.criteria
    ]
    remark = (
        "In the family drill the employee sent the customer its own note to a colleague, "
        "'[internal material request]'. The service SOP I gave you says nothing internal reaches a customer."
    )
    requirement = {
        "behavior": "Nothing written for colleagues ever reaches a customer.",
        "observed": "Turn t1: the reply to the family carried '[internal material request]'.",
        "evidence": ["t1", "[internal material request]"],
        "expectation": "new",
        "acceptance": "No customer-visible message carries a note meant for colleagues.",
        "strength": "must_hold",
    }
    spoken = {"verdicts": verdicts, "remark": remark, "handover": []}
    analysed = {"decision": "curate", "reason": "The owner's red line broke.", "requirements": [requirement]}
    owner = Owner(spoken, analysed, spoken, analysed, names=(REVIEW, FEEDBACK, REVIEW, FEEDBACK))
    home = tmp_path / "home"
    agency = Agency(
        scenario,
        owner,
        home / "skills",
        workdir=tmp_path / "work",
        deliver="dialog",
        uploads=home / "uploads",
        analyst_model="analyst-model",
    )
    agency.prepare()
    heard = []

    async def curator(worker, provider, *, feedback=None, **options):
        heard.append(feedback)

    worker = Worker(tmp_path / "run")
    await run(worker, owner, [Conversation(Traveller(), max_turns=2)], agency, curator=curator, limits=Limits(2))
    spoken, analysed = owner.requests[:2]
    assert "Your scorecard" in spoken["messages"][0]["content"] and analysed["model"] == "analyst-model"
    materials = json.loads(analysed["messages"][1]["content"])
    assert set(materials) == {
        "task",
        "signals",
        "previous_signals",
        "sessions",
        "previous_expectations",
        "previous_feedback",
        "history",
        "skills",
        "locations",
    }
    (signal,) = materials["signals"]
    assert signal["text"] == remark and signal["items"] == [] and signal["satisfied"] is False
    read = json.dumps(analysed["messages"], ensure_ascii=False)
    for criterion in scenario.criteria:
        assert criterion.check not in read
    assert "Keep internal notes to colleagues." not in read and "verdicts" not in read
    assert "We are four, going to Xi'an." in read
    feedback = heard[-1]
    assert feedback["requirements"][0]["behavior"] == requirement["behavior"]
    assert feedback["signals"] == [signal] and feedback["history"] == []
    told = json.dumps(feedback, ensure_ascii=False)
    assert rule.check not in told and "Keep internal notes to colleagues." not in told
    records = [json.loads(path.read_text()) for path in (worker.root / "analysis").glob("*.json")]
    kept = [record for record in records if record.get("source") == "agency"]
    ids = {criterion.id for criterion in scenario.criteria}
    assert len(kept) == 2 and all({item["id"] for item in row["scorecard"]["items"]} == ids for row in kept)
    read_by_analyst = [record for record in records if "materials" in record]
    assert [row["feedback"]["decision"] for row in read_by_analyst] == ["curate", "curate"]
    assert len(heard) == 2
