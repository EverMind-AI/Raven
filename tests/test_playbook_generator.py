"""Generator loop mechanics against a scripted provider (no real LLM)."""

import json

import pytest

from raven.playbook import (
    PlaybookGenerationError,
    PlaybookGenerator,
    StaticInventory,
)

ROSTER = {
    "research-raven": "deep retrieval and fact-checking",
    "code-raven": "repository-level code work",
    "data-raven": "the whole data chain",
    "content-raven": "text and deliverables",
}


class ToolCall:
    def __init__(self, arguments):
        self.arguments = arguments


class Response:
    def __init__(self, args):
        self.has_tool_calls = args is not None
        self.tool_calls = [ToolCall(args)] if args is not None else []


class ScriptedProvider:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def chat_with_retry(self, messages, tools=None, model=None, tool_choice=None, **_):
        self.calls.append(messages)
        return Response(self._payloads.pop(0))


GOOD_DAG = {
    "name": "weekly-feedback",
    "description": "weekly user-feedback analysis",
    "mode": "dag",
    "confirm": True,
    "triggers": {"keywords": ["user feedback", "feedback weekly"]},
    "params": {"week_of": {"type": "string", "required": True, "description": "which week should be analyzed?"}},
    "nodes": [
        {
            "id": "pull",
            "agent": "data-raven",
            "promptTemplate": "pull the feedback for ${params.week_of}",
            "skills": ["sql-queries"],
        },
        {
            "id": "report",
            "agent": "content-raven",
            "promptTemplate": "write the weekly report from {{ pull.output }}",
            "dependsOn": ["pull"],
        },
    ],
    "assumptions": ["the data source is slack"],
    "blockingQuestions": [],
}


def _generator(payloads, skills=("sql-queries",)):
    return (
        PlaybookGenerator(
            ScriptedProvider(payloads),
            skill_router=None,
            agent_roster=ROSTER,
            inventory=StaticInventory(mcp=["slack"]),
        ),
        None,
    )


async def test_happy_path_fills_code_owned_fields():
    gen, _ = _generator([GOOD_DAG])
    result = await gen.generate("weekly feedback analysis, the flow is fixed...", skills=["sql-queries"])
    assert result.spec.name == "weekly-feedback"
    assert result.spec.nodes[1].depends_on == ["pull"]
    assert result.notes == ["Assumption: the data source is slack"]


async def test_repair_loop_feeds_errors_back():
    bad = dict(GOOD_DAG)
    bad["nodes"] = [dict(GOOD_DAG["nodes"][0], agent="ghost-agent"), GOOD_DAG["nodes"][1]]
    gen, _ = _generator([bad, GOOD_DAG])
    result = await gen.generate("weekly feedback analysis", skills=["sql-queries"])
    assert result.spec.name == "weekly-feedback"
    repair_msg = gen._provider.calls[1][-1]["content"]
    assert "ghost-agent" in repair_msg and "emit_playbook" in repair_msg


async def test_gives_up_after_budget_with_error_detail():
    bad = {"name": "x!", "mode": "dag"}
    gen, _ = _generator([bad, bad, bad, bad])
    with pytest.raises(PlaybookGenerationError):
        await gen.generate("anything")


async def test_envelope_wrapped_spec_is_unwrapped():
    gen, _ = _generator([{"playbook": GOOD_DAG}])
    result = await gen.generate("weekly feedback analysis")
    assert result.spec.name == "weekly-feedback"


async def test_unknown_skills_degrade_into_notes():
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["nodes"][0]["skills"] = ["ghost-skill"]
    gen, _ = _generator([payload])
    result = await gen.generate("weekly feedback analysis")
    assert any(n.startswith("Missing capability:") and "ghost-skill" in n for n in result.notes)


async def test_blocking_questions_become_notes_not_fields():
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["blockingQuestions"] = ["what is the report for?"]
    gen, _ = _generator([payload])
    result = await gen.generate("weekly feedback analysis")
    assert "Open question: what is the report for?" in result.notes
    # The self-report never reaches the machine fields.
    assert "blockingQuestions" not in result.spec.block_dump()


async def test_model_proposed_triggers_go_through_the_guards():
    """The emit_playbook schema exposes ``triggers``, so the model writes the L1
    vocabulary directly. Every guard in triggers.py has to apply to what it
    writes: a stop word, an entry under the length rule and a duplicate case
    variant must not reach the index just because a model proposed them rather
    than a person.
    """
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["triggers"] = {"keywords": ["help", "a", "user feedback", "user feedback", "SEO", "seo"]}
    gen, _ = _generator([payload])

    result = await gen.generate("weekly feedback analysis")

    assert result.spec.triggers.keywords == ["user feedback", "seo"]


async def test_all_junk_triggers_feed_the_repair_loop():
    """An emptied vocabulary is a repairable mistake, not a silent widening:
    keeping one of the dropped words would reinstate the entry a guard just
    rejected, and an index entry is a permanent per-message cost.
    """
    junk = json.loads(json.dumps(GOOD_DAG))
    junk["triggers"] = {"keywords": ["a", "help", "the"]}
    gen, _ = _generator([junk, GOOD_DAG])

    result = await gen.generate("weekly feedback analysis")

    assert result.spec.triggers.keywords == ["user feedback", "feedback weekly"]
    repair_msg = gen._provider.calls[1][-1]["content"]
    assert "every trigger candidate was dropped" in repair_msg


async def test_revise_keeps_the_name_and_reports_fresh_notes():
    gen, _ = _generator([GOOD_DAG])
    result = await gen.generate("weekly feedback analysis")

    edited = json.loads(json.dumps(GOOD_DAG))
    edited["name"] = "renamed-anyway"
    edited["assumptions"] = ["the report becomes daily"]
    gen2, _ = _generator([edited])
    revised = await gen2.revise(result.spec, "make the report daily")
    assert revised.spec.name == result.spec.name
    assert revised.notes == ["Assumption: the report becomes daily"]
