"""Unit tests for the playbook match funnel: trigger guards, L1 index, L2 gate."""

import json

import pytest

from raven.memory_engine.playbook import (
    MatchCandidate,
    TriggerIndex,
    Triggers,
    expand_triggers,
    find_collisions,
    gate,
    normalize,
)
from raven.memory_engine.playbook.types import ParamSpec


class _ToolCall:
    def __init__(self, arguments):
        self.arguments = arguments


class _Response:
    def __init__(self, args):
        self.has_tool_calls = args is not None
        self.tool_calls = [_ToolCall(args)] if args is not None else []


class ScriptedProvider:
    """Returns queued tool-call argument payloads, one per call."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def chat_with_retry(self, messages, tools=None, model=None, tool_choice=None, **_):
        self.calls.append(messages)
        payload = self._payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _Response(payload)


NEGATIVE = [
    "帮我看看这段代码为什么报错",
    "明天上午的会改到下午三点",
    "这篇文章写得挺好的,转给团队看看",
    "把上周的文章数据统计一下",
    "中午吃什么",
    "文章里第二段逻辑有问题",
    "帮我订一张去上海的票",
    "服务器磁盘又满了",
    "这个 bug 修了吗",
    "周报别忘了写",
]


# ---------------------------------------------------------------- triggers


async def test_expansion_guards_drop_generic_and_short_entries():
    provider = ScriptedProvider(
        [
            {
                "keywords": ["SEO", "文章", "搜索排名", "写", "收录"],
                "phrases": ["写一篇能被搜到的", "帮我"],
            }
        ]
    )
    trig = await expand_triggers(
        provider,
        description="给定主题产出一篇 SEO 优化文章",
        source_input="帮我搞一个 SEO 的工作流",
        negative_samples=NEGATIVE,
        rounds=1,
    )
    assert "seo" in trig.keywords  # normalized to lowercase
    assert "搜索排名" in trig.keywords
    assert "写一篇能被搜到的" in trig.keywords  # phrases merge into the one list
    assert "文章" not in trig.keywords  # generic: hits 3/10 negatives
    assert "写" not in trig.keywords  # rule filter: too short
    assert "帮我" not in trig.keywords  # stopword


async def test_expansion_keeps_seeds_unless_guards_drop_them():
    provider = ScriptedProvider([{"keywords": [], "phrases": []}])
    trig = await expand_triggers(
        provider,
        description="d" * 30,
        seeds=Triggers(keywords=["专名词", "文章"]),
        negative_samples=NEGATIVE,
        rounds=1,
    )
    assert trig.keywords == ["专名词"]


async def test_expansion_unions_multiple_rounds():
    provider = ScriptedProvider(
        [
            {"keywords": ["发推"]},
            {"keywords": ["推文", "写一篇能被搜到的"]},
        ]
    )
    trig = await expand_triggers(provider, description="d" * 30, rounds=2)
    assert set(trig.keywords) == {"发推", "推文", "写一篇能被搜到的"}
    assert len(provider.calls) == 2


def test_find_collisions_reports_shared_entries():
    lib = {
        "a": Triggers(keywords=["seo", "收录"]),
        "b": Triggers(keywords=["SEO"]),
    }
    collisions = find_collisions(lib)
    assert collisions == {"seo": ["a", "b"]}


# ---------------------------------------------------------------- L1 index


def test_index_matches_normalized_substrings():
    idx = TriggerIndex(
        {
            "seo": Triggers(keywords=["SEO", "搜索排名", "写一篇能被搜到的"]),
            "feedback": Triggers(keywords=["用户反馈"]),
        }
    )
    assert idx.match("帮我做下ＳＥＯ优化") == ["seo"]  # full-width folded by NFKC
    assert idx.match("写一篇能被搜到的向量数据库文章") == ["seo"]
    assert idx.match("整理一下用户反馈,顺便看看搜索排名") == ["seo", "feedback"]
    assert idx.match("中午吃什么") == []
    assert idx.match("") == []


def test_normalize_folds_case_width_and_whitespace():
    assert normalize("ＳＥＯ  Ranking\n提升") == "seo ranking 提升"


# ---------------------------------------------------------------- L2 gate

CANDIDATES = [
    MatchCandidate(
        playbook_id="seo",
        description="给定主题产出一篇 SEO 优化文章",
        params={
            "topic": ParamSpec(required=True, description="文章主题"),
            "word_count": ParamSpec(type="integer", default=1500, description="字数"),
        },
    ),
    MatchCandidate(playbook_id="feedback", description="每周用户反馈分析"),
]


async def test_gate_returns_actionable_verdict_with_params():
    provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "match": "seo",
                    "confidence": "high",
                    "reason": "用户明确要写SEO文章",
                    "params": {"topic": "向量数据库"},
                    "missing": [],
                }
            )
        ]
    )
    verdict = await gate(provider, "帮我写篇向量数据库的SEO文章", CANDIDATES)
    assert verdict.actionable
    assert verdict.params == {"topic": "向量数据库"}
    # candidate params were rendered into the prompt
    assert "word_count" in provider.calls[0][0]["content"]


async def test_gate_low_confidence_is_not_actionable():
    provider = ScriptedProvider([{"match": "seo", "confidence": "low", "reason": "只是聊到"}])
    verdict = await gate(provider, "seo这东西有用吗", CANDIDATES)
    assert verdict.match == "seo"
    assert not verdict.actionable


@pytest.mark.parametrize(
    "payload",
    [
        None,  # no tool call
        "not json {",  # unparseable
        {"match": "unknown-id", "confidence": "high", "reason": "x"},  # unknown id
        {"match": 3, "confidence": "high"},  # invalid shape
        RuntimeError("provider down"),  # transport failure
    ],
)
async def test_gate_failures_resolve_to_pass_through(payload):
    provider = ScriptedProvider([payload])
    verdict = await gate(provider, "帮我写篇SEO文章", CANDIDATES)
    assert verdict.match is None
    assert not verdict.actionable


async def test_gate_with_no_candidates_never_calls_the_model():
    provider = ScriptedProvider([])
    verdict = await gate(provider, "随便聊聊", [])
    assert not verdict.actionable
    assert provider.calls == []
