"""dr@3.2: the harness must not be able to answer its own question.

Both directions, and specifically the property that makes this two predicates
instead of one: the evidence channel is permissive on purpose and the answer
channel is strict on purpose. A test suite that only checked "harness text is
recognised" would pass with a single predicate and would not notice the day
someone collapses them - which is the day a real answer starts getting blanked.
"""

from __future__ import annotations

import pytest

from raven.agent.context import TOOL_OUTPUT_ELIDED, is_elided_tool_output
from raven.agent.harness_text import (
    SEARCH_CLOSED_PREFIX,
    harness_body_kind,
    is_harness_authored,
    is_harness_echo,
    search_closed_notice,
)

# --- the emitter and the recognisers share one string -----------------------


def test_web_search_refusal_is_built_from_the_shared_constant():
    """The tool must emit exactly what the recognisers look for.

    Regression target: they were two literals in two files. Drift between them
    is silent, and its symptom is the refusal becoming eligible as evidence
    again - the state that shipped ``hle-256``'s final answer.
    """
    import inspect

    from raven.agent.tools import web

    src = inspect.getsource(web.WebSearchTool)
    assert "search_closed_notice(" in src
    assert SEARCH_CLOSED_PREFIX not in src, (
        "the refusal is spelled out in web.py again - that is the drift this constant exists to prevent"
    )


def test_notice_round_trips_through_both_predicates():
    notice = search_closed_notice(10)
    assert harness_body_kind(notice) == "search_closed"
    assert is_harness_authored(notice)
    assert is_harness_echo(notice)


# --- direction 1: harness bodies are recognised -----------------------------


@pytest.mark.parametrize("k", [1, 5, 10, 20, 100])
def test_search_closed_recognised_for_every_k(k):
    assert is_harness_authored(search_closed_notice(k))
    assert is_harness_echo(search_closed_notice(k)), (
        "k is configurable; a strict check that only knows the default would "
        "fail open on any arm that tunes saturation.k"
    )


def test_elision_placeholder_recognised_by_both_names():
    assert harness_body_kind(TOOL_OUTPUT_ELIDED) == "tool_output_elided"
    assert is_harness_authored(TOOL_OUTPUT_ELIDED)
    assert is_elided_tool_output(TOOL_OUTPUT_ELIDED)


# --- direction 2: real content is NOT recognised ----------------------------


@pytest.mark.parametrize(
    "body",
    [
        "The capital of France is Paris.",
        "19",
        '{"organic": [{"title": "Search is closed", "link": "http://x"}]}',
        "",
        "   ",
    ],
)
def test_real_bodies_are_not_harness_authored(body):
    assert harness_body_kind(body) is None
    assert not is_harness_authored(body)
    assert not is_harness_echo(body)


# --- ★ the asymmetry itself -------------------------------------------------


def test_answer_that_merely_quotes_the_refusal_survives_the_strict_gate():
    """The load-bearing case. An answer ABOUT the refusal is a real answer.

    Blanking it would recreate the failure class this module exists to stop:
    MiroFlow's boxed extraction dropped 10.83% of its own correct answers,
    dr@1.6's salvage seam blanked 15. Shaping may improve an answer; it may
    never empty one.
    """
    answer = (
        f"{SEARCH_CLOSED_PREFIX} the last 10 searches returned nothing new. "
        "Working from the pages already fetched, the answer is 19."
    )
    assert not is_harness_echo(answer), "a strict gate must not blank a real answer"
    # ...while the permissive side may still decline to feed it back as evidence.
    assert is_harness_authored(answer)


def test_the_two_predicates_actually_disagree():
    """If these ever agree on everything, someone collapsed them.

    This test exists to fail loudly at that moment, because the collapse has no
    other symptom until an answer goes missing in a batch.
    """
    quoting = f"{SEARCH_CLOSED_PREFIX} ... and therefore the answer is 42."
    assert is_harness_authored(quoting) and not is_harness_echo(quoting)


def test_whitespace_around_a_bare_echo_still_counts():
    assert is_harness_echo(f"\n  {search_closed_notice(10)}  \n")


# --- the seam that produced the bug -----------------------------------------


def test_evidence_pack_skips_a_pack_that_is_entirely_harness_text():
    """``hle-256`` reproduced: 4 surviving tool bodies, all the same refusal.

    Before dr@3.2 only the elision marker was skipped, so the pack handed the
    salvage model four copies of the refusal and it returned one verbatim.
    """
    from raven.agent.flow.finalize import ForcedFinalizeGate

    gate = object.__new__(ForcedFinalizeGate)
    gate._evidence_items = 8
    gate._evidence_item_chars = 4000
    messages = [
        {"role": "tool", "name": "web_search", "content": TOOL_OUTPUT_ELIDED},
        {"role": "tool", "name": "web_search", "content": search_closed_notice(10)},
        {"role": "tool", "name": "web_search", "content": search_closed_notice(10)},
    ]
    assert gate._evidence_pack(messages) == "", (
        "a pack of nothing but harness text must come back empty, so the salvage "
        "prompt says '(no tool evidence was gathered)' instead of pretending"
    )


def test_evidence_pack_reaches_past_harness_text_to_real_evidence():
    """Skipping must reach further back, not just shorten the pack."""
    from raven.agent.flow.finalize import ForcedFinalizeGate

    gate = object.__new__(ForcedFinalizeGate)
    gate._evidence_items = 2
    gate._evidence_item_chars = 4000
    messages = [
        {"role": "tool", "name": "web_fetch", "content": "Real page: the answer is 19."},
        {"role": "tool", "name": "web_search", "content": search_closed_notice(10)},
        {"role": "tool", "name": "web_search", "content": TOOL_OUTPUT_ELIDED},
    ]
    pack = gate._evidence_pack(messages)
    assert "the answer is 19" in pack
    assert SEARCH_CLOSED_PREFIX not in pack
    assert TOOL_OUTPUT_ELIDED not in pack
