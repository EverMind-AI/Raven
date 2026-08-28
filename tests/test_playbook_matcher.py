"""Trigger vocabulary: offline expansion, its guards, the index, and retrieval.

The LLM gate this file used to cover is gone. It judged one message with no
conversation history and, on a hit, dispatched a whole graph before the model
ran -- see :mod:`raven.playbook.matcher` for why that moved. Keywords now decide
which playbooks get *described* to the model when the library is too big to list
whole, which is what the router tests at the bottom cover.
"""

from raven.playbook import (
    RouterSizes,
    TriggerIndex,
    Triggers,
    expand_triggers,
    find_collisions,
    normalize,
    select_playbooks,
)
from raven.playbook.types import PlaybookSpec
from raven.providers.base import ErrorClassification, LLMResponse


class _ToolCall:
    def __init__(self, arguments):
        self.arguments = arguments
        self.name = "emit_triggers"


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
        if isinstance(payload, LLMResponse):
            return payload
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


async def test_expansion_provider_error_stops_additional_sampling_rounds():
    classification = ErrorClassification(
        "upstream_transport_failure",
        retryable=True,
        should_fallback=True,
    )
    response = LLMResponse(
        content="upstream did not process the request",
        finish_reason="error",
        error_classification=classification,
    )
    provider = ScriptedProvider([response, {"keywords": ["should not run"]}])

    trig = await expand_triggers(
        provider, description="specific trigger expansion", seeds=Triggers(keywords=["proper-noun"]), rounds=2
    )

    assert trig.keywords == ["proper-noun"]
    assert len(provider.calls) == 1


async def test_expansion_missing_tool_stops_additional_sampling_rounds():
    provider = ScriptedProvider([None, {"keywords": ["should not run"]}])

    trig = await expand_triggers(
        provider, description="specific trigger expansion", seeds=Triggers(keywords=["proper-noun"]), rounds=2
    )

    assert trig.keywords == ["proper-noun"]
    assert len(provider.calls) == 1


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


# --- retrieval: which playbooks this turn describes in full ----------------


def _spec(name: str, *, keywords: list[str], description: str = "does a thing") -> PlaybookSpec:
    return PlaybookSpec(
        name=name,
        description=description,
        task_summary="does a thing",
        mode="dag",
        triggers=Triggers(keywords=keywords),
        nodes=[{"id": "a", "subagent": "raven", "promptTemplate": "go"}],
    )


def test_a_small_library_is_returned_whole():
    """Narrowing five playbooks would spend a scan to drop nothing -- and the
    listing is what the model reads to choose, so dropping any of it is a cost."""
    specs = {n: _spec(n, keywords=[n]) for n in ("a", "b", "c")}
    assert select_playbooks(specs, "anything") == ["a", "b", "c"]


def test_a_large_library_is_ranked_by_trigger_hits():
    specs = {f"p{i}": _spec(f"p{i}", keywords=[f"word{i}"]) for i in range(10)}
    chosen = select_playbooks(specs, "please handle word7 for me", sizes=RouterSizes(top_k=3))

    assert len(chosen) == 3
    assert chosen[0] == "p7"  # the keyword hit outranks everything unmatched


def test_the_description_is_a_weaker_signal_than_a_keyword():
    """A playbook whose vocabulary is thin should still be reachable by what it
    says it does -- but a keyword hit is the author's own statement of when their
    playbook applies, so it wins."""
    specs = {f"filler{i}": _spec(f"filler{i}", keywords=[f"nomatch{i}"]) for i in range(8)}
    specs["by-words"] = _spec("by-words", keywords=["zzz"], description="reconcile invoices monthly")
    specs["by-keyword"] = _spec("by-keyword", keywords=["invoices"])

    chosen = select_playbooks(specs, "reconcile the invoices", sizes=RouterSizes(top_k=2))
    assert chosen[0] == "by-keyword"
    assert "by-words" in chosen


def test_a_message_matching_nothing_still_gets_a_deterministic_selection():
    """Rendering nothing until a keyword hits is how a playbook whose vocabulary
    misses becomes invisible. And the order must not change between two turns
    asking the same thing, or the model is handed a reshuffled list for no reason."""
    specs = {f"p{i}": _spec(f"p{i}", keywords=[f"word{i}"]) for i in range(10)}
    first = select_playbooks(specs, "something unrelated entirely", sizes=RouterSizes(top_k=4))
    second = select_playbooks(specs, "something unrelated entirely", sizes=RouterSizes(top_k=4))

    assert len(first) == 4
    assert first == second


def test_the_index_counts_hits_rather_than_only_nominating():
    """The count is the ranking signal, and a funnel had no use for it.

    ``match`` answers "nominated or not", which is all a trigger needed. Ranking
    needs to know that a message hitting three of a playbook's words fits better
    than one hitting a single generic word.
    """
    idx = TriggerIndex(
        {
            "specific": Triggers(keywords=["weekly feedback", "user feedback", "feedback report"]),
            "generic": Triggers(keywords=["report"]),
        }
    )

    counts = idx.hit_counts("put together the weekly feedback report")

    # Two of the three: "user feedback" is not in that sentence.
    assert counts["specific"] == 2
    assert counts["generic"] == 1
    assert idx.hit_counts("nothing relevant here") == {}


def test_the_ranking_normalizes_the_vocabulary_once_not_per_turn():
    """The index exists to normalize at load; the ranking must not redo it.

    A hundred playbooks with twenty keywords each is two thousand normalize()
    calls a turn to reach an answer the index already holds.
    """
    calls = 0
    real = normalize

    def counting(text: str) -> str:
        nonlocal calls
        calls += 1
        return real(text)

    specs = {f"p{i}": _spec(f"p{i}", keywords=[f"word{i}a", f"word{i}b"]) for i in range(8)}
    index = TriggerIndex({pid: s.triggers for pid, s in specs.items()})

    import raven.playbook.router as router_mod

    original = router_mod.normalize
    router_mod.normalize = counting
    try:
        select_playbooks(specs, "please handle word3a", index=index, sizes=RouterSizes(top_k=2))
    finally:
        router_mod.normalize = original

    # One for the message; the rest only for descriptions of playbooks whose
    # keywords missed. Never one per keyword.
    assert calls <= 1 + len(specs)
