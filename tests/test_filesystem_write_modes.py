"""write_file's overwrite / append modes (raven/agent/tools/filesystem.py).

Append exists because one oversized write risks being cut off mid-argument and
losing the whole call. That makes the empty-append case load-bearing rather
than pedantic: a call truncated before its content field is exactly an append
with nothing to append, and the one thing it must never silently become is an
overwrite of the part already written.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.tools.filesystem import WriteFileTool


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.mark.asyncio
async def test_default_mode_still_overwrites(workspace) -> None:
    """The pre-existing behaviour is untouched by the new parameter."""
    tool = WriteFileTool(str(workspace))
    target = workspace / "a.txt"

    await tool.execute(path=str(target), content="first")
    await tool.execute(path=str(target), content="second")

    assert target.read_text() == "second"


@pytest.mark.asyncio
async def test_append_adds_to_the_end(workspace) -> None:
    tool = WriteFileTool(str(workspace))
    target = workspace / "a.txt"

    await tool.execute(path=str(target), content="part one\n")
    result = await tool.execute(path=str(target), content="part two\n", mode="append")

    assert target.read_text() == "part one\npart two\n"
    assert "appended" in result


@pytest.mark.asyncio
async def test_append_to_a_missing_file_creates_it(workspace) -> None:
    """A model writing the first chunk should not have to know it is first."""
    tool = WriteFileTool(str(workspace))
    target = workspace / "nested" / "a.txt"

    await tool.execute(path=str(target), content="only chunk", mode="append")

    assert target.read_text() == "only chunk"


@pytest.mark.asyncio
async def test_empty_append_is_refused_and_leaves_the_file_alone(workspace) -> None:
    """The shape a truncated call takes. It must not degrade into an overwrite."""
    tool = WriteFileTool(str(workspace))
    target = workspace / "a.txt"
    await tool.execute(path=str(target), content="already written")

    result = await tool.execute(path=str(target), content="", mode="append")

    assert result.startswith("Error")
    assert target.read_text() == "already written"


@pytest.mark.asyncio
async def test_unknown_mode_is_refused_rather_than_guessed(workspace) -> None:
    tool = WriteFileTool(str(workspace))
    target = workspace / "a.txt"
    await tool.execute(path=str(target), content="original")

    result = await tool.execute(path=str(target), content="new", mode="apend")

    assert result.startswith("Error")
    assert target.read_text() == "original"


def test_the_schema_says_what_append_is_for_without_borrowing_one_caller() -> None:
    """Every caller of write_file reads this, not only a truncated one.

    So it says what the parameter does and when it is useful, in terms that
    hold for appending to a log or resuming a file. What happens when a call
    runs past the argument limit belongs to `truncation_hint`, which only that
    caller ever sees -- putting it here would let one scenario rewrite a shared
    tool's contract.
    """
    tool = WriteFileTool(".")

    assert "Write content to a file at the given path" in tool.description
    assert "mode=append" in tool.description
    for scenario_specific in ("truncat", "cut off", "limit", "discard"):
        assert scenario_specific not in tool.description.lower(), (
            f"{scenario_specific!r} is one caller's concern, not the tool's contract"
        )

    mode = tool.parameters["properties"]["mode"]["description"]
    assert "continue a file you have already started" in mode
    assert "across several calls" in mode


def test_the_truncation_hint_carries_what_only_that_caller_needs() -> None:
    """The fact a model cannot infer: an over-long call is lost whole.

    Without it there is no reason to split anything up -- a model may well
    assume the part that fit was saved.
    """
    hint = WriteFileTool(".").truncation_hint or ""

    assert "discarded whole" in hint
    assert "mode=append" in hint


def test_the_truncation_hint_does_not_assume_the_file_is_empty() -> None:
    """One static string reaches a first attempt and a fourth one alike.

    A model that landed chunks 1 and 2 and had chunk 3 cut reads the same
    sentence as one that has written nothing. Told unconditionally to open with
    mode=overwrite, it discards what did land -- the loss the pre-dispatch
    refusal exists to prevent, suggested here rather than caused.

    Which of the two it is in cannot be decided from this side. It is decidable
    from the model's own earlier calls, so the hint names both modes and says
    what tells them apart, instead of picking one.
    """
    hint = WriteFileTool(".").truncation_hint or ""

    assert "the first with mode=overwrite" not in hint, "an unconditional restart"
    assert "mode=overwrite" in hint, "the fresh-file case is still named"
    assert "earlier calls" in hint, "and where the model can tell which case it is in"
