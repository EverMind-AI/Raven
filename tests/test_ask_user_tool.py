"""Tests for the ask_user tool's display contract (raven/agent/tools/ask_user.py).

``ask_user`` is the first tool to return :class:`ToolResult`, so this pins the
split it introduces: ``model_text`` keeps the natural-language phrasing the
model reads, ``display_text`` carries the question/answer pairing the transcript
renders, and ``display_call`` labels the row with the question rather than the
raw arguments blob.
"""

from __future__ import annotations

import copy
import json

import pytest

from raven.agent.tools.ask_user import _MAX_JSON_LAYERS, AskUserTool, _normalize_questions
from raven.agent.tools.base import ToolResult
from raven.agent.tools.registry import ToolRegistry


class _StubBroker:
    """Stands in for QuestionBroker: replies from a scripted answer map."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, list[str]]] = []

    async def await_question(self, cid: str, *, prompt: str, choices: list[str]) -> str:
        self.asked.append((prompt, choices))
        return self.answers.get(prompt, "")


def _tool(answers: dict[str, str]) -> tuple[AskUserTool, _StubBroker]:
    broker = _StubBroker(answers)
    tool = AskUserTool(broker=broker, conversation_id="tui:test")  # type: ignore[arg-type]
    return tool, broker


@pytest.mark.asyncio
async def test_single_question_splits_model_and_display_text():
    tool, broker = _tool({"Which package manager?": "uv"})

    result = await tool.execute(questions=[{"question": "Which package manager?", "options": ["uv", "pip"]}])

    assert isinstance(result, ToolResult)
    # The model reads the full sentence, including the question it asked.
    assert 'User answered: "Which package manager?" -> "uv".' in result.model_text
    assert result.model_text.endswith("Continue.")
    # A single question needs no pairing in the transcript; the row already
    # shows the question via display_call.
    assert result.display_text == "answered: uv"
    assert broker.asked == [("Which package manager?", ["uv", "pip"])]


@pytest.mark.asyncio
async def test_batch_pairs_each_question_with_its_answer():
    tool, _ = _tool({"Base branch?": "main", "Squash?": "yes"})

    result = await tool.execute(questions=[{"question": "Base branch?"}, {"question": "Squash?"}])

    assert isinstance(result, ToolResult)
    # With several questions the display text must say which answer belongs to
    # which question -- one line per pair, order preserved.
    assert result.display_text == "Base branch? -> main\nSquash? -> yes"
    assert 'User answered: "Base branch?" -> "main".' in result.model_text
    assert 'User answered: "Squash?" -> "yes".' in result.model_text


@pytest.mark.asyncio
async def test_unanswered_question_is_explicit_in_both_texts():
    tool, _ = _tool({})

    result = await tool.execute(questions=[{"question": "Ship it?"}])

    assert isinstance(result, ToolResult)
    assert "did not answer" in result.model_text
    assert result.display_text == "answered: (no answer)"


@pytest.mark.asyncio
async def test_error_paths_return_plain_strings():
    # No broker / no conversation id / no questions predate ToolResult and stay
    # bare strings, so the loop's str branch still has to work.
    tool = AskUserTool(broker=None, conversation_id="tui:test")
    assert await tool.execute(questions=[{"question": "hi"}]) == "Error: ask_user not configured (no question broker)"

    tool, _ = _tool({})
    assert await tool.execute(questions=[]) == "Error: ask_user requires at least one question"
    assert await tool.execute(questions=[{"question": "   "}]) == (
        "Error: ask_user requires at least one non-empty question"
    )


def test_display_call_labels_the_row_with_the_question():
    tool, _ = _tool({})

    assert tool.display_call({"questions": [{"question": "Which base branch?"}]}) == "Which base branch?"
    assert tool.display_call({"questions": [{"question": "Base?"}, {"question": "Squash?"}]}) == "Base? | Squash?"
    # Nothing worth showing falls back to the UI's generic preview.
    assert tool.display_call({"questions": []}) is None
    assert tool.display_call({"questions": [{"question": "  "}]}) is None
    assert tool.display_call({}) is None


def test_display_call_survives_a_json_encoded_questions_argument():
    # Models emit the array as a JSON string often enough that display_call --
    # which only labels a transcript row -- used to take the whole turn down
    # with an AttributeError raised on a character of that string.
    tool, _ = _tool({})

    assert tool.display_call({"questions": '[{"question": "Base?"}]'}) == "Base?"
    assert tool.display_call({"questions": '[{"question": "Base?"}, {"question": "Squash?"}]'}) == "Base? | Squash?"


def test_display_call_returns_none_for_unreadable_arguments():
    tool, _ = _tool({})

    assert tool.display_call({"questions": "not json at all"}) is None
    assert tool.display_call({"questions": 42}) is None
    assert tool.display_call({"questions": ["plain string", {"question": "Base?"}]}) == "Base?"


@pytest.mark.asyncio
async def test_execute_accepts_a_json_encoded_questions_argument():
    # The same coercion has to reach execute: a non-empty string is truthy, so
    # the empty-questions guard passed it straight through to entry.get().
    tool, broker = _tool({"Which base branch?": "main"})

    result = await tool.execute(questions='[{"question": "Which base branch?", "options": ["main"]}]')

    assert isinstance(result, ToolResult)
    assert broker.asked == [("Which base branch?", ["main"])]
    assert result.display_text == "answered: main"


@pytest.mark.asyncio
async def test_execute_rejects_unreadable_questions_instead_of_raising():
    tool, _ = _tool({})

    assert await tool.execute(questions="not json at all") == "Error: ask_user requires at least one question"
    assert await tool.execute(questions=42) == "Error: ask_user requires at least one question"


@pytest.mark.asyncio
async def test_deeply_nested_json_does_not_kill_the_turn():
    # json.loads answers deep nesting with RecursionError, which is not a
    # ValueError -- so a normalizer that catches only TypeError/ValueError lets
    # it escape by the exact route the normalizer exists to close.
    tool, _ = _tool({})
    payload = "[" * 20_000 + "]" * 20_000

    assert tool.display_call({"questions": payload}) is None
    assert await tool.execute(questions=payload) == "Error: ask_user requires at least one question"


@pytest.mark.asyncio
async def test_json_encoded_options_reach_the_user_as_options():
    # options carries the same declared array type as questions and arrives as
    # a JSON string just as often. Iterating that string offered the user one
    # suggested answer per character.
    tool, broker = _tool({"Which base?": "main"})

    await tool.execute(questions=[{"question": "Which base?", "options": '["main", "develop"]'}])

    assert broker.asked == [("Which base?", ["main", "develop"])]


@pytest.mark.asyncio
async def test_a_lone_option_string_is_one_option_not_its_letters():
    tool, broker = _tool({"Proceed?": "yes"})

    await tool.execute(questions=[{"question": "Proceed?", "options": "yes"}])

    assert broker.asked == [("Proceed?", ["yes"])]


@pytest.mark.asyncio
async def test_absent_or_unusable_options_are_simply_empty():
    tool, broker = _tool({"Q1?": "a", "Q2?": "b"})

    await tool.execute(questions=[{"question": "Q1?"}, {"question": "Q2?", "options": None}])

    assert broker.asked == [("Q1?", []), ("Q2?", [])]


@pytest.mark.asyncio
async def test_questions_written_as_plain_strings_still_reach_the_user():
    # A list with nothing object-shaped in it is a model that wrote the
    # questions as strings. Dropping them answered a perfectly clear question
    # with "requires at least one question" and asked the user nothing.
    tool, broker = _tool({"Which base branch?": "main"})

    result = await tool.execute(questions=["Which base branch?"])

    assert isinstance(result, ToolResult)
    assert broker.asked == [("Which base branch?", [])]
    assert tool.display_call({"questions": ["Which base branch?"]}) == "Which base branch?"


@pytest.mark.asyncio
async def test_an_object_entry_still_wins_over_a_loose_string():
    # The mixed list keeps its old meaning: the objects are the questions and
    # the loose string is noise, not a third question.
    tool, broker = _tool({"Base?": "main"})

    await tool.execute(questions=["noise", {"question": "Base?"}])

    assert broker.asked == [("Base?", [])]


@pytest.mark.asyncio
async def test_a_twice_encoded_argument_is_unwrapped():
    tool, broker = _tool({"Base?": "main"})

    await tool.execute(questions=json.dumps(json.dumps([{"question": "Base?"}])))

    assert broker.asked == [("Base?", [])]


def test_unwrapping_is_bounded():
    # Past a couple of layers this is no longer a quirk to absorb, and the
    # bound is what stops a crafted argument from spending the turn unwrapping.
    payload = json.dumps([{"question": "Base?"}])
    for _ in range(_MAX_JSON_LAYERS + 1):
        payload = json.dumps(payload)

    assert _normalize_questions(payload) == []


# --- through ToolRegistry, the path production actually takes -------------
#
# Everything above drives ``tool.execute`` directly, which is the function's
# contract but not the call chain. ``ToolRegistry.execute`` casts and validates
# against the declared schema first and returns the error without dispatching,
# so a coercion that lives only in ``execute`` never runs on the shapes that
# need it. These go through the registry.


def _registered(answers: dict[str, str]) -> tuple[ToolRegistry, _StubBroker]:
    tool, broker = _tool(answers)
    registry = ToolRegistry()
    registry.register(tool)
    return registry, broker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "questions",
    [
        pytest.param('[{"question": "Which base branch?"}]', id="array-as-json-string"),
        pytest.param(["Which base branch?"], id="entry-as-plain-string"),
        pytest.param({"question": "Which base branch?"}, id="single-object-unwrapped"),
        pytest.param(json.dumps(json.dumps([{"question": "Which base branch?"}])), id="twice-encoded"),
    ],
)
async def test_the_registry_path_asks_rather_than_rejecting(questions):
    registry, broker = _registered({"Which base branch?": "main"})

    out = await registry.execute("ask_user", {"questions": questions})

    assert broker.asked == [("Which base branch?", [])], out
    assert "Invalid parameters" not in out


@pytest.mark.asyncio
async def test_the_registry_path_normalizes_options_too():
    registry, broker = _registered({"Which base?": "main"})

    out = await registry.execute(
        "ask_user", {"questions": [{"question": "Which base?", "options": '["main", "develop"]'}]}
    )

    assert broker.asked == [("Which base?", ["main", "develop"])], out


@pytest.mark.asyncio
async def test_the_registry_path_reports_unreadable_input_in_the_tools_own_words():
    # Not a schema complaint about a shape the model cannot act on: by the time
    # validation runs the argument has been normalized, so what is left is a
    # genuinely empty question list and the tool says so.
    registry, _ = _registered({})

    out = await registry.execute("ask_user", {"questions": "not json at all"})

    assert "requires at least one question" in out
    assert "should be array" not in out


@pytest.mark.asyncio
async def test_the_registry_path_does_not_rewrite_the_callers_arguments():
    """Normalizing must leave the caller's dict alone.

    The same `arguments` object the registry is handed also goes to the START
    tool event and, on the assistant message, through `to_openai_tool_call`. Both
    happen before `tools.execute` today, so an in-place edit could not reach
    them -- but the object is shared, `cast_params` is the only thing standing
    between the model's text and a rewrite of it, and nothing was watching:
    dropping the copy leaves the whole suite green.
    """
    registry, broker = _registered({"Which base?": "main"})
    args = {"questions": [{"question": "Which base?", "options": '["main", "develop"]'}]}
    frozen = copy.deepcopy(args)

    await registry.execute("ask_user", args)

    assert broker.asked == [("Which base?", ["main", "develop"])]
    assert args == frozen
