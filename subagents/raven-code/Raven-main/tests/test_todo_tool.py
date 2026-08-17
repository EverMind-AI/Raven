"""Tests for the todowrite checklist tool."""

from __future__ import annotations

import pytest

from raven.agent.tools.base import ToolResult
from raven.agent.tools.todo import TodoStore, TodoWriteTool


@pytest.fixture
def tool() -> tuple[TodoWriteTool, TodoStore]:
    store = TodoStore()
    return TodoWriteTool(store), store


@pytest.mark.asyncio
async def test_schema_is_valid_for_a_well_formed_list(tool):
    todo, _ = tool
    payload = {"todos": [{"content": "Run the build", "status": "in_progress", "priority": "high"}]}
    assert todo.validate_params(payload) == []


@pytest.mark.asyncio
async def test_writes_replace_the_previous_list(tool):
    todo, store = tool
    await todo.execute(todos=[{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}])
    await todo.execute(todos=[{"content": "c", "status": "in_progress"}])
    assert [item["content"] for item in store.items] == ["c"]


@pytest.mark.asyncio
async def test_priority_defaults_to_medium(tool):
    todo, store = tool
    await todo.execute(todos=[{"content": "a", "status": "pending"}])
    assert store.items[0]["priority"] == "medium"


@pytest.mark.asyncio
async def test_unknown_priority_falls_back_instead_of_failing(tool):
    todo, store = tool
    await todo.execute(todos=[{"content": "a", "status": "pending", "priority": "urgent"}])
    assert store.items[0]["priority"] == "medium"


@pytest.mark.asyncio
async def test_invalid_status_is_rejected(tool):
    todo, store = tool
    out = await todo.execute(todos=[{"content": "a", "status": "doing"}])
    assert "invalid status" in str(out)
    assert store.items == []


@pytest.mark.asyncio
async def test_empty_content_is_rejected(tool):
    todo, _ = tool
    out = await todo.execute(todos=[{"content": "   ", "status": "pending"}])
    assert "non-empty" in str(out)


@pytest.mark.asyncio
async def test_two_in_progress_warns_but_still_records(tool):
    """The one-at-a-time rule must not cost the model its status update."""
    todo, store = tool
    out = await todo.execute(
        todos=[
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ]
    )
    assert "2 items are 'in_progress'" in str(out)
    assert len(store.items) == 2


@pytest.mark.asyncio
async def test_nothing_in_progress_while_work_remains_warns(tool):
    todo, _ = tool
    out = await todo.execute(todos=[{"content": "a", "status": "pending"}])
    assert "No item is 'in_progress'" in str(out)


@pytest.mark.asyncio
async def test_all_completed_draws_no_warning(tool):
    todo, _ = tool
    out = await todo.execute(todos=[{"content": "a", "status": "completed"}])
    assert "Note:" not in str(out)


@pytest.mark.asyncio
async def test_empty_list_clears_the_checklist(tool):
    todo, store = tool
    await todo.execute(todos=[{"content": "a", "status": "pending"}])
    await todo.execute(todos=[])
    assert store.items == []
    assert store.render() == "(empty)"


@pytest.mark.asyncio
async def test_non_array_input_is_rejected(tool):
    todo, _ = tool
    out = await todo.execute(todos="Run the build")
    assert "must be an array" in str(out)


@pytest.mark.asyncio
async def test_result_carries_a_display_string(tool):
    todo, _ = tool
    out = await todo.execute(todos=[{"content": "Run the build", "status": "in_progress"}])
    assert isinstance(out, ToolResult)
    assert out.display_text == "[~] Run the build"


def test_display_call_summarises_progress(tool):
    todo, _ = tool
    label = todo.display_call(
        {
            "todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
            ]
        }
    )
    assert label == "1/2 done - b"


# --------------------------------------------------------------------------- #
# loop: the checklist is re-rendered onto every request                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def loop(tmp_path):
    from raven.agent.loop import AgentLoop
    from raven.providers.base import LLMProvider

    class _Stub(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")

        async def chat(self, messages, **kwargs):  # pragma: no cover - never called
            raise AssertionError("not used")

        def get_default_model(self) -> str:
            return "stub"

    return AgentLoop(provider=_Stub(), workspace=tmp_path, model="stub")


def test_no_reminder_while_the_checklist_is_empty(loop):
    msgs = [{"role": "user", "content": "go"}]
    assert loop._with_todo_reminder(msgs) == msgs


def test_reminder_carries_the_current_checklist(loop):
    loop._todos.replace(
        [
            {"content": "read the failing test", "status": "completed", "priority": "high"},
            {"content": "fix the parser", "status": "in_progress", "priority": "high"},
        ]
    )
    out = loop._with_todo_reminder([{"role": "user", "content": "go"}])

    assert len(out) == 2
    text = out[-1]["content"]
    assert "<system-reminder>" in text and "</system-reminder>" in text
    assert "[x] read the failing test" in text
    assert "[~] fix the parser" in text
    assert "not written by the user" in text


def test_reminder_never_accumulates_across_calls(loop):
    """It rides on the outgoing copy only, so the caller's list is untouched and
    two consecutive requests carry exactly one reminder each."""
    loop._todos.replace([{"content": "a", "status": "pending", "priority": "medium"}])
    msgs = [{"role": "user", "content": "go"}]

    first = loop._with_todo_reminder(msgs)
    second = loop._with_todo_reminder(msgs)

    assert len(msgs) == 1
    assert sum(1 for m in first if "<system-reminder>" in str(m.get("content"))) == 1
    assert sum(1 for m in second if "<system-reminder>" in str(m.get("content"))) == 1


def test_reminder_reflects_the_latest_write_not_the_first(loop):
    loop._todos.replace([{"content": "old plan", "status": "pending", "priority": "medium"}])
    loop._todos.replace([{"content": "new plan", "status": "in_progress", "priority": "medium"}])
    text = loop._with_todo_reminder([{"role": "user", "content": "go"}])[-1]["content"]

    assert "new plan" in text
    assert "old plan" not in text


def test_prompt_files_explain_the_system_reminder_marker():
    """The marker is only safe if the model is told the system inserts it."""
    from raven.context_engine.segments import identity_prompts

    for family in identity_prompts.available_families():
        text = (identity_prompts._PROMPT_DIR / f"{family}.txt").read_text(encoding="utf-8")
        assert "`<system-reminder>`" in text, family
