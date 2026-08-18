"""Unit tests for the playbook match funnel: trigger guards, L1 index, L2 gate."""

import json

import pytest

from raven.playbook import (
    MatchCandidate,
    TriggerIndex,
    Triggers,
    expand_triggers,
    find_collisions,
    gate,
    normalize,
)
from raven.playbook.types import ParamSpec


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


# CJK fixtures, written as unicode escapes to keep the source ASCII. The L1
# funnel must match Chinese utterances by plain substring (no segmentation)
# and fold full-width forms; these pin that behavior with real CJK data.
SEARCH_RANKING = "\u641c\u7d22\u6392\u540d"  # sou suo pai ming: "search ranking"
USER_FEEDBACK = "\u7528\u6237\u53cd\u9988"  # yong hu fan kui: "user feedback"
FULL_WIDTH_SEO_ASK = (
    "\u5e2e\u6211\u505a\u4e0b\uff33\uff25\uff2f\u4f18\u5316"  # "help me do some SEO tuning", with full-width SEO
)
MIXED_ASK = "\u6574\u7406\u4e00\u4e0b\u7528\u6237\u53cd\u9988,\u987a\u4fbf\u770b\u770b\u641c\u7d22\u6392\u540d"  # mentions both user feedback and search ranking
LUNCH = "\u4e2d\u5348\u5403\u4ec0\u4e48"  # "what's for lunch"
FULL_WIDTH_LINE = "\uff33\uff25\uff2f  Ranking\n\u63d0\u5347"  # ti sheng: "improve"
FOLDED_LINE = "seo ranking \u63d0\u5347"

#: Unrelated everyday messages for the generic-word filter. Three of the ten
#: contain "article", so "article" hits 30% and is dropped by the data, not
#: by anyone's intuition.
NEGATIVE = [
    "can you check why this code throws",
    "tomorrow's standup moves to 3pm",
    "this article is quite good, share it with the team",
    "pull last week's article stats for me",
    "what's for lunch",
    "the second paragraph of the article has a logic hole",
    "book me a ticket to shanghai",
    "the server disk is full again",
    "is that bug fixed yet",
    "don't forget the weekly report",
]


# ---------------------------------------------------------------- triggers


async def test_expansion_guards_drop_generic_and_short_entries():
    provider = ScriptedProvider(
        [
            {
                "keywords": ["SEO", "article", "search ranking", "w", "indexing"],
                "phrases": ["write one that ranks", "help"],
            }
        ]
    )
    trig = await expand_triggers(
        provider,
        description="produce one SEO-optimized article for a given topic",
        source_input="set me up an SEO workflow",
        negative_samples=NEGATIVE,
        rounds=1,
    )
    assert "seo" in trig.keywords  # normalized to lowercase
    assert "search ranking" in trig.keywords
    assert "write one that ranks" in trig.keywords  # phrases merge into the one list
    assert "article" not in trig.keywords  # generic: hits 3/10 negatives
    assert "w" not in trig.keywords  # rule filter: too short
    assert "help" not in trig.keywords  # stopword


async def test_expansion_keeps_seeds_unless_guards_drop_them():
    provider = ScriptedProvider([{"keywords": [], "phrases": []}])
    trig = await expand_triggers(
        provider,
        description="d" * 30,
        seeds=Triggers(keywords=["proper-noun", "article"]),
        negative_samples=NEGATIVE,
        rounds=1,
    )
    assert trig.keywords == ["proper-noun"]


async def test_expansion_unions_multiple_rounds():
    provider = ScriptedProvider(
        [
            {"keywords": ["post a tweet"]},
            {"keywords": ["tweet text", "write one that ranks"]},
        ]
    )
    trig = await expand_triggers(provider, description="d" * 30, rounds=2)
    assert set(trig.keywords) == {"post a tweet", "tweet text", "write one that ranks"}
    assert len(provider.calls) == 2


def test_find_collisions_reports_shared_entries():
    lib = {
        "a": Triggers(keywords=["seo", "indexing"]),
        "b": Triggers(keywords=["SEO"]),
    }
    collisions = find_collisions(lib)
    assert collisions == {"seo": ["a", "b"]}


# ---------------------------------------------------------------- L1 index


def test_index_matches_normalized_substrings():
    idx = TriggerIndex(
        {
            "seo": Triggers(keywords=["SEO", SEARCH_RANKING]),
            "feedback": Triggers(keywords=[USER_FEEDBACK]),
        }
    )
    assert idx.match(FULL_WIDTH_SEO_ASK) == ["seo"]  # full-width folded by NFKC
    assert idx.match(MIXED_ASK) == ["seo", "feedback"]
    assert idx.match(LUNCH) == []
    assert idx.match("") == []


def test_normalize_folds_case_width_and_whitespace():
    assert normalize(FULL_WIDTH_LINE) == FOLDED_LINE


# ---------------------------------------------------------------- L2 gate

CANDIDATES = [
    MatchCandidate(
        playbook_id="seo",
        description="produce one SEO-optimized article for a given topic",
        params={
            "topic": ParamSpec(required=True, description="what is the article about?"),
            "word_count": ParamSpec(type="integer", default=1500, description="target word count"),
        },
    ),
    MatchCandidate(playbook_id="feedback", description="weekly user-feedback analysis"),
]


async def test_gate_returns_actionable_verdict_with_params():
    provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "match": "seo",
                    "confidence": "high",
                    "reason": "the user clearly wants an SEO article",
                    "params": {"topic": "vector databases"},
                    "missing": [],
                }
            )
        ]
    )
    verdict = await gate(provider, "write me an SEO article on vector databases", CANDIDATES)
    assert verdict.actionable
    assert verdict.params == {"topic": "vector databases"}
    # candidate params were rendered into the prompt
    assert "word_count" in provider.calls[0][0]["content"]


async def test_gate_low_confidence_is_not_actionable():
    provider = ScriptedProvider([{"match": "seo", "confidence": "low", "reason": "merely mentioned"}])
    verdict = await gate(provider, "is seo even worth doing", CANDIDATES)
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
    verdict = await gate(provider, "write me an SEO article", CANDIDATES)
    assert verdict.match is None
    assert not verdict.actionable


async def test_gate_with_no_candidates_never_calls_the_model():
    provider = ScriptedProvider([])
    verdict = await gate(provider, "just chatting", [])
    assert not verdict.actionable
    assert provider.calls == []
