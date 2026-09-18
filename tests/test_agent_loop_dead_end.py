"""The dead-end predicate: is a finished turn worth running again from scratch?

The predicate is the whole mechanism until a loop consumes it, so these tests pin
what it classifies rather than what anything does about it. Two of them exist for
pairings that would have silently emptied the trigger: an appendix wrapped around
the no-answer sentence, and a budget-exhausted turn that wrapped up with a real
answer.
"""

from __future__ import annotations

import pytest

from raven.agent.loop._shared import _HOOK_INJECTED_KEY
from raven.agent.loop.dead_end import (
    NO_RESPONSE_FALLBACK,
    REFUSAL_MARKERS,
    dead_reasons,
    stranded_of,
)


def _asst(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _injected(text: str) -> dict:
    return {"role": "user", "content": text, _HOOK_INJECTED_KEY: True}


def test_an_appendix_wrapped_dud_is_still_a_dead_end():
    """The pairing that would have silently disabled the lever on the shipped config.

    The loop fills an answerless turn with the no-answer sentence, and the research
    appendix then wraps a full trail around it. Several hundred characters of real
    content, and nothing in it is an answer.
    """
    dressed = NO_RESPONSE_FALLBACK + "\n## How this was researched\n- 3 searches, 2 pages"
    spoke = [{"role": "user", "content": "q"}, _asst("still looking")]
    assert dead_reasons(messages=spoke, final_content=dressed) == ["no_response"]
    assert len(dressed) > 100


def test_a_real_answer_is_not_a_dead_end():
    msgs = [{"role": "user", "content": "q"}, _asst("## Answer\nAlice Smith.")]
    assert dead_reasons(messages=msgs, final_content="## Answer\nAlice Smith.") == []


def test_a_budget_exhausted_turn_that_wrapped_up_is_not_dead():
    """The name collision that would make a retry double a clock it had just spent.

    ``"interrupted"`` is the turn outcome's status and it means the turn hit its
    iteration cap or wall clock -- which runs the exhaustion wrap-up and returns a
    real answer. Reading it as "the run did not end cleanly" costs a whole second
    budget.
    """
    wrapped = "Wrapped up: found X, Y unverified."
    msgs = [{"role": "user", "content": "q"}, _asst(wrapped)]
    assert dead_reasons(messages=msgs, final_content=wrapped, status="interrupted") == []
    # ...but an exhausted turn that produced nothing is still dead, on the missing
    # answer rather than on the budget.
    silent = [{"role": "user", "content": "q"}, {"role": "tool", "content": "r"}]
    assert dead_reasons(messages=silent, final_content=None, status="interrupted") == [
        "stranded:tool_result",
        "empty_answer",
    ]


def test_a_turn_that_blew_up_is_dead():
    msgs = [{"role": "user", "content": "q"}, _asst("partial")]
    assert dead_reasons(messages=msgs, final_content="partial", status="error") == ["status:error"]


def test_stranded_is_reported_first():
    """Order is the design: the structural test must not sit behind the proxies."""
    msgs = [{"role": "user", "content": "q"}, {"role": "tool", "content": "results"}]
    assert dead_reasons(messages=msgs, final_content=None)[0] == "stranded:tool_result"
    assert stranded_of(msgs) == (True, "tool_result")


def test_a_declined_answer_is_dead_however_long_it_is():
    """The refusal opener is read from the module rather than spelled here: the
    strings are Chinese and live in the i18n lexicon, which is the only place in
    this repo that carries them."""
    declined = REFUSAL_MARKERS[0] + ", " + "x" * 500
    msgs = [{"role": "user", "content": "q"}, _asst(declined)]
    assert dead_reasons(messages=msgs, final_content=declined) == ["refusal_string"]


def test_an_empty_message_list_is_not_stranded():
    """``stranded`` says the trajectory ended somewhere other than model prose. A
    trajectory with nothing in it did not end anywhere, and reporting it as stranded
    would put every construction error into the same bucket as a real failure."""
    assert stranded_of([]) == (False, None)


def test_the_sub_label_names_the_tail_shape():
    question_only = [{"role": "user", "content": "q"}]
    tool_call = [_asst("")] + [{"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}]
    empty_assistant = [{"role": "user", "content": "q"}, _asst("   ")]
    assert stranded_of(question_only) == (True, "question")
    assert stranded_of(tool_call) == (True, "tool_call")
    assert stranded_of(empty_assistant) == (True, "empty_assistant")


def test_a_harness_ask_is_stranded_even_with_no_labeller():
    """The boolean is structural, the sub-label is textual, and only the second one
    needs the product's wording. A caller that supplies no labeller still gets the
    failure mode, under a name that says the label is missing rather than absent."""
    msgs = [{"role": "user", "content": "q"}, _injected("[finalize] commit now.")]
    assert stranded_of(msgs) == (True, "harness_ask_unknown")


def test_a_supplied_labeller_names_the_ask():
    msgs = [{"role": "user", "content": "q"}, _injected("[finalize] commit now.")]
    kinds = {"[finalize]": "finalize"}

    def _ask_kind(content: object) -> str | None:
        return next((v for k, v in kinds.items() if str(content).startswith(k)), None)

    assert stranded_of(msgs, ask_kind=_ask_kind) == (True, "finalize")
    assert dead_reasons(messages=msgs, final_content=None, ask_kind=_ask_kind)[0] == "stranded:finalize"


def test_an_injected_assistant_turn_is_not_the_models_own_prose():
    """Every injected entry carries the marker here, not only the user-role ones, so
    a harness-written assistant turn cannot pass as the answer the model gave."""
    msgs = [{"role": "user", "content": "q"}, {**_asst("the harness wrote this"), _HOOK_INJECTED_KEY: True}]
    assert stranded_of(msgs)[0] is True


@pytest.mark.parametrize(
    "content,spoke",
    [
        ([{"type": "text", "text": "Alice."}], True),
        ([{"type": "image", "url": "x"}], False),
        ("Alice.", True),
        (None, False),
        (42, True),
    ],
)
def test_what_counts_as_the_models_own_prose(content, spoke):
    """Channel-separated backends deliver a list of typed blocks, most deliver a
    string, and a stub can deliver anything. A turn ended on prose when flattening
    its last message leaves text; ``None`` flattens to nothing, and an unexpected
    type is stringified rather than silently read as empty -- an instrument that
    reports "no answer" for a shape it did not anticipate would invent dead turns.
    """
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": content}]
    assert stranded_of(msgs) == ((False, None) if spoke else (True, "empty_assistant"))
