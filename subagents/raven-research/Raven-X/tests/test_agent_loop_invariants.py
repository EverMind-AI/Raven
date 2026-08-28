"""Per-turn invariant checker — the four checks and the three design properties.

The properties matter as much as the checks: the stamp must appear on a clean turn
too, it must not depend on a hook being registered, and its error caliber must be
the one applied before the untrusted fence goes on.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.loop.invariants import CHECKER_VERSION, classify_tool_result, turn_invariants

DECLARED = ("web_search", "web_fetch")


def _turn(*messages: dict) -> list[dict]:
    return [{"role": "user", "content": "who founded X?"}, *messages]


def test_a_clean_turn_is_still_stamped():
    """Absence of the stamp must mean "not checked", never "checked, clean" — the
    omit-when-quiet convention of the observer namespaces is what let a missing
    key be read as a measured zero."""
    stamp = turn_invariants(
        _turn({"role": "assistant", "content": "reasoning</think>Ada, in 1992."}),
        declared_tools=DECLARED,
    )

    assert stamp["ok"] is True
    assert stamp["checker"] == CHECKER_VERSION
    assert "violations" not in stamp
    json.dumps(stamp)


def test_a_chain_of_thought_committed_as_an_answer_is_caught():
    stamp = turn_invariants(
        _turn({"role": "assistant", "content": "The user wants a brand. Candidates: A? B?"}),
        declared_tools=DECLARED,
        closing_tag_required=True,
    )

    # Also unrecorded, since this turn carries no ``turn_end`` — a chain of thought
    # committed with nothing written down trips both invariants, which is correct.
    assert "answer_shape_unaccounted" in stamp["violations"]


def test_a_marked_salvage_accounts_for_its_own_missing_tag():
    """A committed salvage carries no closing tag by construction — the counter is
    what separates it from a turn that never reached an answer."""
    stamp = turn_invariants(
        _turn({"role": "assistant", "content": "**Answer:** Three Months\n\nKey Evidence: ..."}),
        declared_tools=DECLARED,
        metadata={"force_finalize": {"terminal_hits": 1, "salvage_committed": 1}},
        closing_tag_required=True,
    )

    assert stamp["ok"] is True
    assert stamp["salvage_committed"] == 1


def test_a_tool_the_config_cannot_account_for_is_caught():
    stamp = turn_invariants(
        _turn(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "function": {"name": "understand_media"}}],
            },
            {"role": "tool", "tool_call_id": "1", "name": "understand_media", "content": "ok"},
            {"role": "assistant", "content": "r</think>done"},
        ),
        declared_tools=DECLARED,
    )

    assert "undeclared_tool" in stamp["violations"]
    assert stamp["undeclared"] == ["understand_media"]


def test_error_text_inside_a_successful_looking_result_is_caught():
    """The gate that six checks failed to trip: the tool returned, the process was
    healthy, and the payload was an error."""
    stamp = turn_invariants(
        _turn(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "function": {"name": "web_search"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "1",
                "name": "web_search",
                "content": '[BEGIN UNTRUSTED web_search #abc]\nError: {"message":"Not enough credits"}\n',
            },
            {"role": "assistant", "content": "r</think>done"},
        ),
        declared_tools=DECLARED,
    )

    assert "tool_error_result" in stamp["violations"]
    assert stamp["tool_results"]["web_search"] == {"error": 1}


@pytest.mark.parametrize(
    "content,expected",
    [
        ("Error: boom", "error"),
        ("Proxy error: boom", "error"),
        ('{"error": "Proxy error: boom", "url": "u"}', "error"),
        ("Error: Serper API key not configured. Set it in ...", "error"),
        # Every backend names itself in that message. These two reach the
        # verdict through the "Error:" head like the line above them; the
        # pair below is what isolates the substring net, which is the only
        # branch that reads the provider name.
        ("Error: SerpApi API key not configured. Set it in ...", "error"),
        ("Error: AnySearch API key not configured. Set it in ...", "error"),
        # No "Error:" head, so only the substring net can classify it. A
        # provider-specific match reads an unanswering retrieval surface as
        # a legitimate result, and that verdict is what invalidates a batch.
        ("web_search said: SerpApi API key not configured.", "error"),
        ("web_search said: AnySearch API key not configured.", "error"),
        ("No results for: who founded X", "empty"),
        ("1. A page\n2. Another page", "ok"),
        # The fence is applied by the loop, so the caliber has to see through it.
        # Anchoring on the first line is what silently zeroed this counter twice.
        ("[BEGIN UNTRUSTED web_search #abc]\nProxy error: boom\n[END UNTRUSTED #abc]", "error"),
        ("[BEGIN UNTRUSTED web_search #abc]\nNo results for: q\n", "empty"),
    ],
)
def test_tool_result_caliber_sees_through_the_untrusted_fence(content, expected):
    assert classify_tool_result(content) == expected


def test_an_empty_search_is_not_counted_as_an_error():
    """A search that legitimately found nothing is a retrieval outcome; only an
    error means the retrieval surface itself stopped answering, and only the
    second one invalidates a batch."""
    stamp = turn_invariants(
        _turn(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "1", "function": {"name": "web_search"}}],
            },
            {"role": "tool", "tool_call_id": "1", "name": "web_search", "content": "No results for: q"},
            {"role": "assistant", "content": "r</think>done"},
        ),
        declared_tools=DECLARED,
    )

    assert stamp["ok"] is True
    assert stamp["tool_results"]["web_search"] == {"empty": 1}


def test_an_empty_turn_the_loop_recorded_is_not_a_violation_on_either_arm():
    """``silently_empty`` must not be keyed on the terminal gate. With the flow off
    that gate is never built, so keying on it would make every empty anchor turn a
    violation while the identical turn on the treated arm read clean — an
    arm-correlated counter, which is a bias rather than a measurement."""
    anchor = turn_invariants(
        _turn({"role": "assistant", "content": ""}),
        declared_tools=DECLARED,
        metadata={"turn_end": {"answerless": True, "status": "ok"}},
        closing_tag_required=True,
    )
    treated = turn_invariants(
        _turn({"role": "assistant", "content": ""}),
        declared_tools=DECLARED,
        metadata={"turn_end": {"answerless": True}, "force_finalize": {"terminal_hits": 1}},
        closing_tag_required=True,
    )

    assert anchor["ok"] is True and treated["ok"] is True


def test_an_empty_turn_nobody_recorded_is_a_violation():
    stamp = turn_invariants(
        _turn({"role": "assistant", "content": ""}), declared_tools=DECLARED, closing_tag_required=True
    )

    assert stamp["violations"] == ["silently_empty"]


def test_tool_name_is_recovered_from_the_call_id_when_the_result_omits_it():
    stamp = turn_invariants(
        _turn(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "web_fetch"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "Error: boom"},
            {"role": "assistant", "content": "r</think>done"},
        ),
        declared_tools=DECLARED,
    )

    assert stamp["tool_results"]["web_fetch"] == {"error": 1}
    assert stamp["tool_calls"] == {"web_fetch": 1}
