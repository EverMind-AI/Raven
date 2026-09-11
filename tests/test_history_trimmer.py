"""HistoryTrimmer keeps provider reasoning fields and drops non-provider keys."""

from __future__ import annotations

from raven.context_engine.history_trimmer import HistoryTrimmer


def test_history_from_ids_preserves_reasoning_fields():
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "chain of thought",
            "thinking_blocks": [{"thinking": "block"}],
        },
    ]

    history = HistoryTrimmer.history_from_ids(messages, [0, 1])

    assert history[1]["reasoning_content"] == "chain of thought"
    assert history[1]["thinking_blocks"] == [{"thinking": "block"}]


def test_history_from_ids_drops_non_provider_keys():
    messages = [
        {"role": "user", "content": "hi", "timestamp": "2026-07-08T00:00:00"},
    ]

    history = HistoryTrimmer.history_from_ids(messages, [0])

    assert history == [{"role": "user", "content": "hi"}]


# --- Budget trimming keeps tool calls and their results together ---------------
#
# Measured 2026-09-11: a 54-message session sized against a 65,536 default window
# (the model's real window is 1,048,576) was trimmed one id at a time. The drop
# loop took the assistant that declared three parallel calls and left its three
# results, so every request carried orphan tool results and DeepSeek's API
# refused each one ("No tool call found for tool output"). The reverse happened
# an hour earlier (parent kept, one result dropped). ``trim`` now drops a call
# and its results as one group and re-closes the selection after every drop.


class _CharProvider:
    """Stands in for the provider `estimate_prompt_tokens_chain` may consult."""

    def estimate_tokens(self, *_args, **_kwargs):
        raise RuntimeError("not consulted")


def _parallel_call_session() -> list[dict]:
    return [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": "reading three things",
            "tool_calls": [
                {"id": "c0", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c0", "content": "x" * 400},
        {"role": "tool", "tool_call_id": "c1", "content": "y" * 400},
        {"role": "tool", "tool_call_id": "c2", "content": "z" * 400},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "next"},
    ]


def _trimmer(monkeypatch, *, window: int) -> HistoryTrimmer:
    from raven.context_engine import history_trimmer as module

    # One token per four characters, deterministic and independent of any model
    # table: the test is about which ids survive, not how many tokens they cost.
    monkeypatch.setattr(
        module,
        "estimate_prompt_tokens_chain",
        lambda _provider, _model, messages, _tools: (
            sum(len(str(m.get("content") or "")) for m in messages) // 4,
            "test",
        ),
    )
    return HistoryTrimmer(_CharProvider(), "fake", lambda: [], window)


def test_tool_group_binds_an_assistant_to_all_of_its_results():
    messages = _parallel_call_session()

    assert HistoryTrimmer.tool_group(messages, 1) == {1, 2, 3, 4}
    assert HistoryTrimmer.tool_group(messages, 3) == {1, 2, 3, 4}
    assert HistoryTrimmer.tool_group(messages, 5) == {5}
    assert HistoryTrimmer.tool_group(messages, 0) == {0}


def test_trim_drops_a_tool_call_and_its_results_as_one_group(monkeypatch):
    messages = _parallel_call_session()
    trimmer = _trimmer(monkeypatch, window=200)  # room for ~800 chars: not all three results

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(len(messages))),
        protected_ids={0},  # the opening user message stays, so the group is the first droppable
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert outcome.ok
    assert HistoryTrimmer.structural_errors(built) == []
    # Neither the parent without its results nor a result without its parent.
    kept = set(outcome.included_ids)
    assert not ({1, 2, 3, 4} & kept) or {1, 2, 3, 4} <= kept
    assert all(f"dropped message {mid} to fit budget" in outcome.warnings for mid in (1, 2, 3, 4))


def test_trim_re_anchors_on_a_user_message_after_a_group_drop(monkeypatch):
    messages = _parallel_call_session()
    trimmer = _trimmer(monkeypatch, window=200)

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(len(messages))),
        protected_ids=set(),  # nothing protected: the opening user message is the first droppable
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    history = built[1:-1]
    assert history and history[0]["role"] == "user"
    assert HistoryTrimmer.structural_errors(built) == []
    # What sat between the dropped user message and the next one went with it,
    # and the warning says why -- the rule that history starts at a user turn.
    assert any("nothing before the first remaining user message" in w for w in outcome.warnings)


def test_trim_refuses_a_selection_whose_parent_the_session_lost(monkeypatch):
    # A plan can name a result whose parent is no longer in the session at all;
    # the closure cannot add a parent that does not exist, so the sweep after
    # trimming has to drop the result instead of shipping it.
    messages = [
        {"role": "user", "content": "start"},
        {"role": "tool", "tool_call_id": "gone", "content": "orphan"},
        {"role": "assistant", "content": "done"},
    ]
    trimmer = _trimmer(monkeypatch, window=10_000)

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=[0, 1, 2],
        protected_ids=set(),
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert HistoryTrimmer.structural_errors(built) == []
    assert 1 not in outcome.included_ids
    assert any(w.startswith("dropped message 1:") for w in outcome.warnings)


# --- A budget drop must not cost a protected exchange its anchor ---------------
#
# Re-closing after a drop re-anchors the history on the first surviving user
# message, and that step did not consult ``protected_ids``. With
# ``[user(0), pinned call(1), pinned result(2), user(3)]`` and ``{1, 2}``
# protected, dropping the unprotected ``0`` first left ``[3]`` -- the pinned
# skill body gone while ordinary history remained, against CONTEXT.md's Pinned
# contract ("trimmed last").


def _pinned_exchange_session() -> list[dict]:
    return [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": "fetching the guide",
            "tool_calls": [{"id": "p0", "type": "function", "function": {"name": "read_skill", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "p0", "content": "g" * 400},
        {"role": "user", "content": "n" * 400},
    ]


def test_a_budget_drop_keeps_a_protected_exchange_and_its_anchor(monkeypatch):
    messages = _pinned_exchange_session()
    trimmer = _trimmer(monkeypatch, window=150)  # [0, 1, 2] fits; all four do not

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=[0, 1, 2, 3],
        protected_ids={1, 2},
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert outcome.ok
    assert {1, 2} <= set(outcome.included_ids)
    assert 3 not in outcome.included_ids
    assert built[1]["role"] == "user"  # the anchor stayed with what it anchors
    assert HistoryTrimmer.structural_errors(built) == []


def test_a_protected_exchange_goes_last_and_only_when_nothing_else_fits(monkeypatch):
    messages = _pinned_exchange_session()
    trimmer = _trimmer(monkeypatch, window=40)  # not even [0, 1, 2] fits

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=[0, 1, 2, 3],
        protected_ids={1, 2},
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert outcome.ok
    assert outcome.included_ids == [0]  # the exchange went as a unit, its anchor stayed
    assert any(w.startswith("dropped protected message") for w in outcome.warnings)
    assert HistoryTrimmer.structural_errors(built) == []


# --- A protection boundary that splits a parallel-call group -------------------
#
# ``protect_first_n=3`` protects ids 0..5. When the third head exchange has its
# assistant call at 5 and parallel results at 6 and 7, ranking the *candidate
# id* called 6 unprotected, expanded it to the group {5, 6, 7}, and dropped
# protected 5 while unprotected 8 and 9 survived. A group with any protected
# member ranks as protected.


def _split_boundary_session() -> list[dict]:
    return [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "three"},
        {
            "role": "assistant",
            "content": "fetching",
            "tool_calls": [
                {"id": "h0", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "h1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "h0", "content": "r" * 200},
        {"role": "tool", "tool_call_id": "h1", "content": "s" * 200},
        {"role": "assistant", "content": "t" * 200},
        {"role": "user", "content": "u" * 200},
    ]


def test_a_group_with_a_protected_member_ranks_as_protected(monkeypatch):
    messages = _split_boundary_session()
    trimmer = _trimmer(monkeypatch, window=120)  # 0..7 fit (~110 tokens); 8 and 9 must go

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(10)),
        protected_ids=set(range(6)),  # protect_first_n=3 -> ids 0..5, splitting {5, 6, 7}
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert outcome.ok
    assert set(range(8)) <= set(outcome.included_ids)
    assert not ({8, 9} & set(outcome.included_ids))
    assert not any(w.startswith("dropped protected message") for w in outcome.warnings)
    assert HistoryTrimmer.structural_errors(built) == []


def test_a_split_group_goes_whole_and_last_when_it_must(monkeypatch):
    messages = _split_boundary_session()
    trimmer = _trimmer(monkeypatch, window=30)  # even 0..7 do not fit

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(10)),
        protected_ids=set(range(6)),
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    kept = set(outcome.included_ids)
    assert not ({8, 9} & kept)
    assert not ({5, 6, 7} & kept) or {5, 6, 7} <= kept  # never split
    assert HistoryTrimmer.structural_errors(built) == []
