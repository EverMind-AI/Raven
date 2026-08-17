"""The memo's round trip through the real ``AgentLoop`` methods.

The reason this file exists rather than more unit tests: the last mechanism added
here passed eleven tests of its own object and still had a real bug, because its
observation point sat on a branch the dominant case returned before reaching. The
memo has the same shape of risk in a worse place - it is injected in one method,
stripped in a second and updated in a third, and if the strip ever misses, the
memo is persisted into history and the next turn renders a second one describing
the same pages. Nothing about that failure is visible in the reply.

So these call ``AgentLoop``'s own bound methods against a stub carrying only the
attributes they read. That is deliberately not a mock of the methods: a copy of
the injector would agree with a copy of the stripper no matter what either did.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from raven.agent.flow.conversation import MEMO_CLOSE, MEMO_OPEN, ResearchMemo, stash_turn_rows
from raven.agent.loop.main import AgentLoop
from raven.session.manager import Session


class _Flow:
    """Stands in for a ``DRFlowAssembly``; only the memo fields are read."""

    def __init__(self, **kw):
        self.research_memo = kw.get("research_memo", True)
        self.memo_max_chars = kw.get("memo_max_chars", 2000)
        self.memo_max_sources = kw.get("memo_max_sources", 12)
        self.memo_max_queries = kw.get("memo_max_queries", 12)


class _Loop:
    """Minimum surface the three methods under test touch."""

    _TOOL_RESULT_MAX_CHARS = AgentLoop._TOOL_RESULT_MAX_CHARS

    def __init__(self, flow=None):
        self._dr_flow = flow
        self._now_fn = datetime.now
        self._flow_version = "dr@3.0/test"

    inject = AgentLoop._inject_research_memo
    update = AgentLoop._update_research_memo
    save = AgentLoop._save_turn


def _session(memo=None):
    s = Session(key="cli:direct")
    if memo is not None:
        s.metadata["dr_research_memo"] = memo
    return s


def _stored(*urls, queries=()):
    memo = ResearchMemo().merge_ledger(
        [{"op": "search", "query": q} for q in queries]
        + [{"op": "fetch", "url": u, "chars": 100, "ok": True} for u in urls],
        max_sources=12,
        max_queries=12,
    )
    return memo.to_metadata()


# --------------------------------------------------------------------------- #
# Injection                                                                   #
# --------------------------------------------------------------------------- #


def test_the_memo_is_prepended_to_the_current_user_message():
    loop = _Loop(_Flow())
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "and in 2025?"}]
    loop.inject(_session(_stored("https://a.example")), messages)
    assert messages[-1]["content"].startswith(MEMO_OPEN)
    assert "https://a.example" in messages[-1]["content"]
    assert messages[-1]["content"].endswith("and in 2025?")
    # The system message is untouched: the memo must never enter the cached
    # prefix, or every turn re-bills the whole conversation at uncached rates.
    assert messages[0] == {"role": "system", "content": "sys"}


def test_nothing_is_injected_when_the_feature_is_off():
    loop = _Loop(_Flow(research_memo=False))
    messages = [{"role": "user", "content": "q"}]
    loop.inject(_session(_stored("https://a.example")), messages)
    assert messages == [{"role": "user", "content": "q"}]


def test_nothing_is_injected_without_a_flow_assembly():
    """The flow-off anchor has no assembly, so it cannot reach this at all."""
    loop = _Loop(None)
    messages = [{"role": "user", "content": "q"}]
    loop.inject(_session(_stored("https://a.example")), messages)
    assert messages == [{"role": "user", "content": "q"}]


def test_an_empty_memo_injects_nothing_rather_than_an_empty_header():
    loop = _Loop(_Flow())
    messages = [{"role": "user", "content": "q"}]
    loop.inject(_session(), messages)
    assert messages[-1]["content"] == "q"


def test_a_multimodal_message_gets_the_memo_as_a_leading_text_part():
    loop = _Loop(_Flow())
    parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    messages = [{"role": "user", "content": list(parts)}]
    loop.inject(_session(_stored("https://a.example")), messages)
    assert messages[-1]["content"][0]["text"].startswith(MEMO_OPEN)
    assert messages[-1]["content"][1:] == parts


def test_injection_is_skipped_when_the_last_message_is_not_the_user_turn():
    loop = _Loop(_Flow())
    messages = [{"role": "assistant", "content": "hi"}]
    loop.inject(_session(_stored("https://a.example")), messages)
    assert messages == [{"role": "assistant", "content": "hi"}]


# --------------------------------------------------------------------------- #
# The round trip: inject then persist                                         #
# --------------------------------------------------------------------------- #


def test_what_is_injected_is_not_what_is_persisted():
    """The whole point, on the real pair of methods.

    If the stripper ever misses, turn three carries turn two's memo as history
    plus a freshly rendered one listing the same pages, growing every turn - and
    the reply looks fine throughout.
    """
    loop = _Loop(_Flow())
    session = _session(_stored("https://a.example"))
    messages = [{"role": "user", "content": "and in 2025?"}]
    loop.inject(session, messages)
    assert MEMO_OPEN in messages[0]["content"]

    loop.save(session, messages, 0)

    persisted = session.messages[0]["content"]
    assert persisted == "and in 2025?"
    assert MEMO_OPEN not in persisted and MEMO_CLOSE not in persisted


def test_the_multimodal_round_trip_drops_the_memo_part_too():
    loop = _Loop(_Flow())
    session = _session(_stored("https://a.example"))
    messages = [{"role": "user", "content": [{"type": "text", "text": "and in 2025?"}]}]
    loop.inject(session, messages)
    loop.save(session, messages, 0)
    texts = [c["text"] for c in session.messages[0]["content"]]
    assert texts == ["and in 2025?"]


def test_a_user_message_that_merely_mentions_the_marker_survives_persistence():
    """The stripper is prefix-anchored; a quoted marker is the user's own words."""
    loop = _Loop(_Flow())
    session = _session()
    text = f"why does the output contain {MEMO_OPEN}?"
    loop.save(session, [{"role": "user", "content": text}], 0)
    assert session.messages[0]["content"] == text


# --------------------------------------------------------------------------- #
# Update                                                                      #
# --------------------------------------------------------------------------- #


def test_the_turns_rows_land_on_the_session_metadata():
    loop = _Loop(_Flow())
    session = _session()
    stash_turn_rows(
        [
            {"op": "search", "query": "EU AI Act 2025"},
            {"op": "fetch", "url": "https://a.example", "chars": 900, "ok": True},
        ]
    )
    loop.update(session)
    stored = session.metadata["dr_research_memo"]
    assert [s["url"] for s in stored["sources"]] == ["https://a.example"]
    assert stored["queries"] == ["EU AI Act 2025"]
    assert stored["turns"] == 1


def test_a_second_turn_accumulates_rather_than_replaces():
    loop = _Loop(_Flow())
    session = _session()
    stash_turn_rows([{"op": "fetch", "url": "https://a.example", "chars": 1, "ok": True}])
    loop.update(session)
    stash_turn_rows([{"op": "fetch", "url": "https://b.example", "chars": 1, "ok": True}])
    loop.update(session)
    stored = session.metadata["dr_research_memo"]
    assert [s["url"] for s in stored["sources"]] == ["https://b.example", "https://a.example"]
    assert stored["turns"] == 2


def test_rows_are_consumed_so_a_later_turn_cannot_inherit_them():
    """A read-once handoff.

    The alternative - leaving the rows in place - would credit a turn that opened
    nothing with the previous turn's pages, and the memo would say a page was read
    in a turn where it was not.
    """
    loop = _Loop(_Flow())
    session = _session()
    stash_turn_rows([{"op": "fetch", "url": "https://a.example", "chars": 1, "ok": True}])
    loop.update(session)
    loop.update(session)
    assert session.metadata["dr_research_memo"]["turns"] == 2
    assert len(session.metadata["dr_research_memo"]["sources"]) == 1


@pytest.mark.parametrize("flow", [None, _Flow(research_memo=False)])
def test_no_metadata_is_written_when_the_feature_is_off(flow):
    loop = _Loop(flow)
    session = _session()
    stash_turn_rows([{"op": "fetch", "url": "https://a.example", "chars": 1, "ok": True}])
    loop.update(session)
    assert "dr_research_memo" not in session.metadata


def test_a_turn_that_opened_nothing_writes_no_empty_memo():
    """An empty memo must not appear in metadata as though research had happened
    and found nothing - the render would be blank either way, so the state would
    be unreadable after the fact."""
    loop = _Loop(_Flow())
    session = _session()
    stash_turn_rows([])
    loop.update(session)
    assert "dr_research_memo" not in session.metadata


# --------------------------------------------------------------------------- #
# The per-turn invariant checker must be fed one turn                         #
# --------------------------------------------------------------------------- #


def test_the_invariant_checker_is_scoped_to_this_turns_messages():
    """Reproduced from the eight-turn run, where all eight turns reported
    ``tool_error_result`` - including the four that called no tool at all.

    ``turn_invariants`` is a per-turn checker and was handed the whole assembled
    list. On a single-turn bench run those are the same list, so nothing measured
    moves; in a conversation, one failed fetch in turn one made every later turn
    red forever. A check that fires on every turn is not a check - it trains
    everyone to ignore its red light, and the tool-surface health gate reads this
    field.
    """
    from raven.agent.loop.invariants import turn_invariants

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "web_fetch"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "web_fetch", "content": "Error: 403 Forbidden"},
        {"role": "assistant", "content": "reasoning</think>an answer"},
    ]
    this_turn = [
        {"role": "user", "content": "compress that to three sentences"},
        {"role": "assistant", "content": "reasoning</think>three sentences"},
    ]
    declared = ("web_search", "web_fetch")

    whole = turn_invariants(history + this_turn, declared_tools=declared, closing_tag_required=True)
    assert whole["violations"] == ["tool_error_result"], "the pre-fix reading, kept as the baseline"

    sliced = turn_invariants(this_turn, declared_tools=declared, closing_tag_required=True)
    assert sliced["ok"] is True
    assert "violations" not in sliced
    # And the tool counts describe this turn, not the conversation.
    assert sliced["tool_calls"] == {}
    assert sliced["tool_results"] == {}


def test_a_turn_with_its_own_tool_error_is_still_caught_after_slicing():
    """The reverse direction. Narrowing the input must not make the check unreachable."""
    from raven.agent.loop.invariants import turn_invariants

    this_turn = [
        {"role": "user", "content": "and Tavily?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c9", "function": {"name": "web_fetch"}}]},
        {"role": "tool", "tool_call_id": "c9", "name": "web_fetch", "content": "Error: 403 Forbidden"},
        {"role": "assistant", "content": "reasoning</think>an answer"},
    ]
    stamp = turn_invariants(this_turn, declared_tools=("web_search", "web_fetch"), closing_tag_required=True)
    assert stamp["violations"] == ["tool_error_result"]
