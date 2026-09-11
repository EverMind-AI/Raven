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
async def test_rewriting_identical_content_reports_no_change(workspace) -> None:
    """A rewrite that changes nothing has to say so.

    ``model_text`` is the only account of the call the model gets, and a byte
    count reads as progress. An edit loop that is rewriting the same bytes can
    then spend several rounds before anything tells it so.
    """
    tool = WriteFileTool(str(workspace))
    target = workspace / "a.txt"

    await tool.execute(path=str(target), content="same")
    result = await tool.execute(path=str(target), content="same")

    assert "unchanged" in result.model_text
    assert target.read_bytes() == b"same"


@pytest.mark.asyncio
async def test_crlf_on_disk_is_not_read_as_an_unchanged_write(workspace) -> None:
    """Text equality hides a rewrite that changes the bytes.

    Path.read_text() applies universal-newline translation, so a file holding
    CRLF reads back equal to the LF content being written. The overwrite it
    stands in for does change the file, and skipping it keeps the old bytes.
    """
    tool = WriteFileTool(str(workspace))
    target = workspace / "crlf.txt"
    target.write_bytes(b"a\r\n")

    result = await tool.execute(path=str(target), content="a\n")

    assert target.read_bytes() == b"a\n"
    assert "unchanged" not in result.model_text


@pytest.mark.asyncio
async def test_crlf_content_already_on_disk_is_a_no_op(workspace) -> None:
    """A text gate in front of the byte check answers the question itself.

    read_text() normalizes, so content that carries CRLF never equals the text
    read back from the file already holding exactly those bytes -- and a
    pre-filter on that comparison turns an identical write into a rewrite.
    """
    tool = WriteFileTool(str(workspace))
    target = workspace / "crlf.txt"
    target.write_bytes(b"a\r\n")

    result = await tool.execute(path=str(target), content="a\r\n")

    assert "unchanged" in result.model_text
    assert target.read_bytes() == b"a\r\n"


@pytest.mark.asyncio
async def test_an_unreadable_file_can_still_be_overwritten(workspace, monkeypatch) -> None:
    """Reading to decide whether to write must not become a precondition.

    A POSIX mode-0200 file is writable and not readable, and overwriting one
    worked before the no-op check existed: the failed read left no previous
    content and the write went ahead. Driven by a denied read rather than by
    a real chmod, because the suite runs as root, where 0200 blocks nothing.
    """
    tool = WriteFileTool(str(workspace))
    target = workspace / "write-only.txt"
    target.write_text("old")

    def _denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _denied)
    monkeypatch.setattr(Path, "read_bytes", _denied)

    result = await tool.execute(path=str(target), content="new")

    monkeypatch.undo()
    assert target.read_bytes() == b"new"
    assert "Successfully wrote" in result.model_text


@pytest.mark.asyncio
async def test_a_rewrite_that_changes_something_still_reports_the_write(workspace) -> None:
    tool = WriteFileTool(str(workspace))
    target = workspace / "a.txt"

    await tool.execute(path=str(target), content="first")
    result = await tool.execute(path=str(target), content="second")

    assert "Successfully wrote" in result.model_text
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
    assert "mode=overwrite to start a file" in hint, "the fresh-file case is named as a choice"
    assert "mode=append to continue one you have already begun" in hint, "and so is the other"
