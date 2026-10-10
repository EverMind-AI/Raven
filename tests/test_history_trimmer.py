"""HistoryTrimmer keeps provider reasoning fields and drops non-provider keys."""

from __future__ import annotations

import json
import random

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


def test_closure_brings_a_selected_results_call_and_its_sibling_results():
    messages = _parallel_call_session()

    assert HistoryTrimmer.canonical_ids(messages, [0, 3]) == [0, 1, 2, 3, 4]


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


# --- A call id that a later turn reuses ----------------------------------------
#
# Nothing makes a tool-call id unique across a session: the streaming path keeps
# whatever id the upstream sent. A result belongs to the nearest earlier call
# that declared its id, and closure and ``tool_group`` have to agree on that --
# otherwise re-closing after a budget drop puts the dropped group back, the
# selection stops shrinking, and ``trim`` never returns.


def _reused_call_id_session() -> list[dict]:
    return [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": "reading",
            "tool_calls": [{"id": "call_0", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "r" * 200},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "three"},
        {
            "role": "assistant",
            "content": "reading again",
            "tool_calls": [{"id": "call_0", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "s" * 200},
        {"role": "assistant", "content": "t" * 200},
        {"role": "user", "content": "u" * 200},
    ]


def test_a_reused_call_id_pairs_each_result_with_the_nearest_call():
    messages = _reused_call_id_session()

    assert HistoryTrimmer.tool_group(messages, 1) == {1, 2}
    assert HistoryTrimmer.tool_group(messages, 8) == {7, 8}
    assert HistoryTrimmer.canonical_ids(messages, [0, 1]) == [0, 1, 2]


def test_trim_ends_when_a_later_turn_reuses_a_call_id(monkeypatch):
    from raven.context_engine import history_trimmer as module

    messages = _reused_call_id_session()
    trimmer = _trimmer(monkeypatch, window=120)  # 0..5 and 10 fit (~104 tokens); 6..9 must go
    estimate = module.estimate_prompt_tokens_chain
    estimates: list[None] = []

    def bounded(*args):
        # A loop that stopped shrinking fails here instead of hanging pytest.
        estimates.append(None)
        assert len(estimates) <= 4 * len(messages), "trim stopped shrinking the selection"
        return estimate(*args)

    monkeypatch.setattr(module, "estimate_prompt_tokens_chain", bounded)

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(len(messages))),
        protected_ids=set(range(6)),  # protect_first_n=3 -> ids 0..5, holding the first call_0 exchange
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert outcome.ok
    assert outcome.included_ids == [0, 1, 2, 3, 4, 5, 10]
    assert HistoryTrimmer.structural_errors(built) == []


# --- The closing sweep looks at each group once --------------------------------
#
# Every member answers from the same indexed group. Each lookup still copies
# its members into a set, so the sweep asks once per group rather than repeating
# that work for every selected member.


def _tool_heavy_session(turns: int) -> list[dict]:
    messages: list[dict] = []
    for b in range(turns):
        messages += [
            {"role": "user", "content": f"q{b}"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"a{b}", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                    {"id": f"b{b}", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": f"a{b}", "content": "r"},
            {"role": "tool", "tool_call_id": f"b{b}", "content": "r"},
        ]
    return messages


def test_trimming_reuses_tokenization_of_retained_history(monkeypatch):
    from raven.utils import tokens

    messages = _tool_heavy_session(80)
    tools = [{"type": "function", "function": {"name": "read"}}]

    def build(history):
        return [{"role": "system", "content": "system"}, *history, {"role": "user", "content": "next"}]

    initial = tokens.estimate_prompt_tokens(build(messages), tools)
    real = tokens.tiktoken.get_encoding("cl100k_base")
    encoded: list[int] = []

    class MeasuredEncoding:
        def encode(self, payload):
            encoded.append(len(payload))
            return real.encode(payload)

    monkeypatch.setattr(tokens.tiktoken, "get_encoding", lambda _name: MeasuredEncoding())
    trimmer = HistoryTrimmer(_CharProvider(), "fake", lambda: tools, initial // 2)
    _built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(len(messages))),
        protected_ids={0},
        reserved_output=0,
        build_messages=build,
    )

    assert outcome.ok and len(outcome.included_ids) < len(messages) - 10
    assert encoded and sum(encoded) < 2 * len(json.dumps(messages))


def test_cached_trimming_matches_uncached_choices_and_estimates():
    rng = random.Random(886)
    trimmer = HistoryTrimmer(_CharProvider(), "fake", lambda: [], 350)
    for _ in range(24):
        messages = _tool_heavy_session(rng.randrange(8, 18))
        for message in messages:
            message["content"] = rng.choice(["text", " ?!\n ", "\n\n", "item 12345", "A\u0301"]) * rng.randrange(1, 8)
        arguments = {
            "session_messages": messages,
            "ids": list(range(len(messages))),
            "protected_ids": {0, rng.randrange(len(messages))},
            "reserved_output": rng.randrange(0, 100),
            "build_messages": lambda history: [
                {"role": "system", "content": "system\n"},
                *history,
                {"role": "user", "content": " current"},
            ],
        }
        expected = HistoryTrimmer.trim.__wrapped__(trimmer, **arguments)
        assert trimmer.trim(**arguments) == expected


def test_trimming_keeps_the_providers_own_counter(monkeypatch):
    from raven.utils import tokens

    calls: list[int] = []

    class Provider:
        def estimate_prompt_tokens(self, messages, tools, model):
            assert tools == [{"name": "read"}] and model == "custom"
            calls.append(len(messages))
            return len(messages) * 100, "native"

    monkeypatch.setattr(
        tokens.tiktoken, "get_encoding", lambda _name: (_ for _ in ()).throw(AssertionError("fallback used"))
    )
    messages = [{"role": "user", "content": "message"} for _ in range(6)]
    trimmer = HistoryTrimmer(Provider(), "custom", lambda: [{"name": "read"}], 200)
    _built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(6)),
        protected_ids=set(),
        reserved_output=0,
        build_messages=lambda history: history,
    )
    assert calls == [6, 5, 4, 3, 2]
    assert outcome.source == "native" and outcome.estimated_tokens == 200


def test_the_closing_sweep_looks_at_each_tool_group_once(monkeypatch):
    messages = _tool_heavy_session(10)  # ten user messages, ten calls with two results each
    trimmer = _trimmer(monkeypatch, window=10_000)  # everything fits: no drop, only the sweep
    tool_group = HistoryTrimmer.tool_group
    looks: list[int] = []

    def counted(session_messages, mid, index=None):
        looks.append(mid)
        return tool_group(session_messages, mid, index)

    monkeypatch.setattr(HistoryTrimmer, "tool_group", staticmethod(counted))

    built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(40)),
        protected_ids=set(),
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert outcome.included_ids == list(range(40))
    assert HistoryTrimmer.structural_errors(built) == []
    assert len(looks) == 20  # one look per group, not one per selected message


# --- A drop re-closes only the candidates it tries -----------------------------
#
# Re-closing walks the whole session, and ``trim`` runs synchronously inside
# context assembly, so choosing a drop re-closes candidates one at a time in the
# order they could go and stops at the first clean one: one re-closing per drop,
# not one per group per drop.


def test_a_budget_drop_re_closes_only_the_candidate_it_takes(monkeypatch):
    messages = [{"role": ("user", "assistant")[i % 2], "content": "x" * 40} for i in range(40)]
    trimmer = _trimmer(monkeypatch, window=300)  # 40 messages cost 400 tokens: ten must go
    canonical_ids = HistoryTrimmer.canonical_ids
    closures: list[None] = []

    def counted(session_messages, ids):
        closures.append(None)
        return canonical_ids(session_messages, ids)

    monkeypatch.setattr(HistoryTrimmer, "canonical_ids", staticmethod(counted))

    _built, outcome = trimmer.trim(
        session_messages=messages,
        ids=list(range(40)),
        protected_ids={0},
        reserved_output=0,
        build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
    )

    assert outcome.included_ids == [0, *range(11, 40)]
    assert len(closures) == 11  # the selection's own closure, then one per drop


# --- A trim that drops nothing pairs the session a fixed number of times -------
#
# Pairing is a pass over the whole session, and the closing sweep asks for the
# group of every call it selected. The groups are indexed once per trim, so twice
# the calls cost the same number of passes rather than twice as many.


def test_a_trim_without_drops_pairs_the_session_a_fixed_number_of_times(monkeypatch):
    from raven.context_engine import history_trimmer as module

    tool_parents = module._tool_parents
    passes: list[None] = []

    def counted(session_messages):
        passes.append(None)
        return tool_parents(session_messages)

    monkeypatch.setattr(module, "_tool_parents", counted)

    def passes_for(turns: int) -> int:
        messages = _tool_heavy_session(turns)
        trimmer = _trimmer(monkeypatch, window=10_000)  # everything fits: no drop
        passes.clear()
        _built, outcome = trimmer.trim(
            session_messages=messages,
            ids=list(range(len(messages))),
            protected_ids=set(),
            reserved_output=0,
            build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
        )
        assert outcome.included_ids == list(range(len(messages)))
        return len(passes)

    assert passes_for(20) == passes_for(10)


def test_a_drop_pairs_the_session_no_more_often_behind_a_larger_protected_head(monkeypatch):
    from raven.context_engine import history_trimmer as module

    tool_parents = module._tool_parents
    passes: list[None] = []

    def counted(session_messages):
        passes.append(None)
        return tool_parents(session_messages)

    monkeypatch.setattr(module, "_tool_parents", counted)

    def passes_for(head_turns: int) -> int:
        head = _tool_heavy_session(head_turns)  # protected: every drop looks past these groups first
        tail = [{"role": ("user", "assistant")[i % 2], "content": "x" * 40} for i in range(20)]
        messages = head + tail
        trimmer = _trimmer(monkeypatch, window=108)  # exactly ten tail messages must go
        passes.clear()
        _built, outcome = trimmer.trim(
            session_messages=messages,
            ids=list(range(len(messages))),
            protected_ids=set(range(len(head))),
            reserved_output=0,
            build_messages=lambda h: [{"role": "system", "content": "s"}, *h, {"role": "user", "content": "u"}],
        )
        assert outcome.included_ids == [*range(len(head)), *range(len(head) + 10, len(messages))]
        return len(passes)

    assert passes_for(6) == passes_for(2)


# --- The group index keeps one copy of each group ------------------------------
#
# A parallel call with many results is one group, and every member maps to it:
# the index stores that group once and shares it, so it stays linear in memory
# however wide the call is.


def test_the_group_index_keeps_one_copy_of_each_group():
    from raven.context_engine import history_trimmer as module

    calls = [{"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{}"}} for i in range(20)]
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": calls},
        *({"role": "tool", "tool_call_id": f"c{i}", "content": "r"} for i in range(20)),
    ]

    index = module._group_index(messages)

    assert len(index) == 21
    assert len({id(group) for group in index.values()}) == 1
