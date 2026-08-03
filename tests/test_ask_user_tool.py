"""Tests for the ask_user tool's display contract (raven/agent/tools/ask_user.py).

``ask_user`` is the first tool to return :class:`ToolResult`, so this pins the
split it introduces: ``model_text`` keeps the natural-language phrasing the
model reads, ``display_text`` carries the question/answer pairing the transcript
renders, and ``display_call`` labels the row with the question rather than the
raw arguments blob.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.ask_user import AskUserTool
from raven.agent.tools.base import ToolResult


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
