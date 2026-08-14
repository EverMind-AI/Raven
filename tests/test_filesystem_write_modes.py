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


def test_the_schema_says_what_append_is_for_before_anything_fails() -> None:
    """The schema is the only thing the model reads before its first call.

    An error message arrives too late to prevent the call that produced it, so
    both the description and the parameter say what append is used for -- not
    what goes wrong without it, and with no invented threshold for "long",
    which depends on a ceiling this file knows nothing about.
    """
    tool = WriteFileTool(".")

    assert "mode=append" in tool.description
    mode = tool.parameters["properties"]["mode"]["description"]
    assert "continue a file you have already started" in mode
    assert "across several calls" in mode
