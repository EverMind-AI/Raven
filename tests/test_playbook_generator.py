"""Generator loop mechanics against a scripted provider (no real LLM)."""

import json

import pytest

from raven.memory_engine.playbook import (
    PlaybookGenerationError,
    PlaybookGenerator,
    PlaybookStore,
    StaticInventory,
)

ROSTER = {
    "research-raven": "深度检索与事实核查",
    "code-raven": "仓库级代码工作",
    "data-raven": "数据全链路",
    "content-raven": "文本与交付物",
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
    "description": "每周用户反馈分析",
    "mode": "dag",
    "confirm": True,
    "triggers": {"keywords": ["用户反馈", "反馈周报"]},
    "params": {"week_of": {"type": "string", "required": True, "description": "分析哪一周？"}},
    "nodes": [
        {
            "id": "pull",
            "agent": "data-raven",
            "promptTemplate": "拉取 ${params.week_of} 的反馈",
            "skills": ["sql-queries"],
        },
        {
            "id": "report",
            "agent": "content-raven",
            "promptTemplate": "按 {{ pull.output }} 写周报",
            "dependsOn": ["pull"],
        },
    ],
    "provenance": {"specifiedByUser": ["数据源为 slack"], "blockingQuestions": []},
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
    spec = await gen.generate("每周反馈分析,流程固定...", skills=["sql-queries"])
    assert spec.name == "weekly-feedback"
    assert spec.status == "draft"
    assert spec.provenance.source_input.startswith("每周反馈分析")
    assert spec.nodes[1].depends_on == ["pull"]


async def test_repair_loop_feeds_errors_back():
    bad = dict(GOOD_DAG)
    bad["nodes"] = [dict(GOOD_DAG["nodes"][0], agent="ghost-agent"), GOOD_DAG["nodes"][1]]
    gen, _ = _generator([bad, GOOD_DAG])
    spec = await gen.generate("每周反馈分析", skills=["sql-queries"])
    assert spec.name == "weekly-feedback"
    repair_msg = gen._provider.calls[1][-1]["content"]
    assert "ghost-agent" in repair_msg and "emit_playbook" in repair_msg


async def test_gives_up_after_budget_with_error_detail():
    bad = {"name": "x!", "mode": "dag"}
    gen, _ = _generator([bad, bad, bad, bad])
    with pytest.raises(PlaybookGenerationError):
        await gen.generate("anything")


async def test_envelope_wrapped_spec_is_unwrapped():
    gen, _ = _generator([{"playbook": GOOD_DAG}])
    spec = await gen.generate("每周反馈分析")
    assert spec.name == "weekly-feedback"


async def test_unknown_skills_degrade_into_missing_capabilities():
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["nodes"][0]["skills"] = ["ghost-skill"]
    gen, _ = _generator([payload])
    spec = await gen.generate("每周反馈分析")
    assert any("ghost-skill" in m for m in spec.provenance.missing_capabilities)


async def test_model_proposed_triggers_go_through_the_guards():
    """The emit_playbook schema exposes ``triggers``, so the model writes the L1
    vocabulary directly. Every guard in triggers.py has to apply to what it
    writes: a stop word, an entry under the length rule and a duplicate case
    variant must not reach the index just because a model proposed them rather
    than a person.
    """
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["triggers"] = {"keywords": ["帮我", "的", "用户反馈", "用户反馈", "SEO", "seo"]}
    gen, _ = _generator([payload])

    spec = await gen.generate("每周反馈分析")

    assert spec.triggers.keywords == ["用户反馈", "seo"]


async def test_all_junk_triggers_feed_the_repair_loop():
    """An emptied vocabulary is a repairable mistake, not a silent widening:
    keeping one of the dropped words would reinstate the entry a guard just
    rejected, and an index entry is a permanent per-message cost.
    """
    junk = json.loads(json.dumps(GOOD_DAG))
    junk["triggers"] = {"keywords": ["的", "帮我", "一下"]}
    gen, _ = _generator([junk, GOOD_DAG])

    spec = await gen.generate("每周反馈分析")

    assert spec.triggers.keywords == ["用户反馈", "反馈周报"]
    repair_msg = gen._provider.calls[1][-1]["content"]
    assert "every trigger candidate was dropped" in repair_msg


async def test_revise_enforces_the_immutable_region(tmp_path):
    gen, _ = _generator([GOOD_DAG])
    spec = await gen.generate("每周反馈分析")
    PlaybookStore(tmp_path).save(spec)

    dropped = json.loads(json.dumps(GOOD_DAG))
    dropped["provenance"]["specifiedByUser"] = []  # drops the user-specified fact
    fixed = json.loads(json.dumps(GOOD_DAG))
    gen2, _ = _generator([dropped, fixed])
    revised = await gen2.revise(spec, "报告改成日报")
    assert revised.name == spec.name
    assert "数据源为 slack" in revised.provenance.specified_by_user
