"""Filesystem tools: the unified diff a write reports for the UI.

A whole-file write is the only change no UI can reconstruct afterwards -- the
previous content is gone the moment it lands -- so the tool has to hand it over.
"""

from pathlib import Path

import pytest

from raven.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import ToolOutput, ToolResult


def _diff_of(result: ToolResult | str) -> str | None:
    assert isinstance(result, ToolResult)
    return result.diff


@pytest.mark.asyncio
async def test_overwriting_a_file_reports_both_sides_of_the_change(tmp_path: Path):
    target = tmp_path / "letter.md"
    target.write_text("dear friend\nold line\nbye\n", encoding="utf-8")

    out = await WriteFileTool(workspace=tmp_path).execute(path=str(target), content="dear friend\nnew line\nbye\n")

    diff = _diff_of(out)
    assert diff is not None
    assert "-old line" in diff
    assert "+new line" in diff
    # The unchanged lines ride along as context, not as additions.
    assert "+dear friend" not in diff


@pytest.mark.asyncio
async def test_a_new_file_is_all_additions(tmp_path: Path):
    out = await WriteFileTool(workspace=tmp_path).execute(path=str(tmp_path / "fresh.txt"), content="one\ntwo\n")

    diff = _diff_of(out)
    assert diff is not None
    assert "+one" in diff and "+two" in diff
    assert "\n-" not in diff


@pytest.mark.asyncio
async def test_rewriting_identical_content_reports_no_diff(tmp_path: Path):
    target = tmp_path / "same.txt"
    target.write_text("unchanged\n", encoding="utf-8")

    out = await WriteFileTool(workspace=tmp_path).execute(path=str(target), content="unchanged\n")

    assert _diff_of(out) is None


@pytest.mark.asyncio
async def test_a_rewrite_too_large_to_render_is_dropped_whole(tmp_path: Path):
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"old {i}" for i in range(500)), encoding="utf-8")

    out = await WriteFileTool(workspace=tmp_path).execute(
        path=str(target), content="\n".join(f"new {i}" for i in range(500))
    )

    # Half a diff would be worse than none, so nothing is sent.
    assert _diff_of(out) is None


@pytest.mark.asyncio
async def test_the_model_text_of_a_write_is_unchanged_by_the_diff(tmp_path: Path):
    target = tmp_path / "note.txt"
    out = await WriteFileTool(workspace=tmp_path).execute(path=str(target), content="hi\n")

    assert isinstance(out, ToolResult)
    assert out.model_text.startswith("Successfully wrote 3 bytes to ")


@pytest.mark.asyncio
async def test_an_edit_reports_the_replacement_in_context(tmp_path: Path):
    target = tmp_path / "code.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    out = await EditFileTool(workspace=tmp_path).execute(path=str(target), old_text="b = 2", new_text="b = 22")

    diff = _diff_of(out)
    assert diff is not None
    assert "-b = 2" in diff and "+b = 22" in diff
    assert " a = 1" in diff


@pytest.mark.asyncio
async def test_the_registry_carries_the_diff_to_the_agent_loop(tmp_path: Path):
    target = tmp_path / "letter.md"
    target.write_text("before\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=tmp_path))

    out = await registry.execute("write_file", {"path": str(target), "content": "after\n"})

    assert isinstance(out, ToolOutput)
    assert out.diff is not None and "+after" in out.diff
    # It rides beside the model text, never inside it.
    assert "after" not in str(out).replace(str(target), "")


@pytest.mark.asyncio
async def test_a_url_is_refused_by_naming_the_tool_that_can_reach_it(tmp_path: Path):
    """ "File not found" describes the wrong problem for a URL.

    The file is not missing; the argument belongs to another tool. Reported as a
    missing file it reads as a misspelling, so the caller tries the same URL again
    with the workspace prefixed, percent-decoded, a directory up -- none of which
    can work, and the search that produced the URL stalls there.
    """
    read = ReadFileTool(workspace=tmp_path)

    for url in ("https://example.com/paper.pdf", "http://example.com/paper.pdf"):
        answer = await read.execute(path=url)
        assert isinstance(answer, str)
        assert "web_fetch" in answer, answer
        assert "File not found" not in answer, answer


@pytest.mark.asyncio
async def test_a_file_url_is_answered_with_the_path_it_means(tmp_path: Path):
    """A file:// URL does name a local file, so the next step is the path, not a fetch."""
    target = tmp_path / "notes.md"
    target.write_text("facts", encoding="utf-8")

    answer = await ReadFileTool(workspace=tmp_path).execute(path=f"file://{target}")

    assert isinstance(answer, str)
    assert str(target) in answer
    assert "web_fetch" not in answer


@pytest.mark.asyncio
async def test_a_path_that_is_merely_missing_still_says_so(tmp_path: Path):
    """The URL wording is an extra branch, not a replacement.

    A relative path with a directory in it must not read as a host name.
    """
    read = ReadFileTool(workspace=tmp_path)

    for missing in ("ghost.md", "docs/readme.md"):
        answer = await read.execute(path=missing)
        assert isinstance(answer, str)
        assert answer.startswith("Error: File not found:")
        assert "web_fetch" not in answer


# ---------------------------------------------------------------------------
# What an approval prompt shows before the write happens
# ---------------------------------------------------------------------------


def test_a_write_over_an_existing_file_previews_as_a_diff(tmp_path: Path):
    target = tmp_path / "letter.md"
    target.write_text("dear friend\nold line\nbye\n", encoding="utf-8")

    view = WriteFileTool(workspace=tmp_path).approval_evidence(
        {"path": "letter.md", "content": "dear friend\nnew line\nbye\n"}
    )

    assert view["path"] == str(target.resolve())
    assert view["created"] is False
    assert "-old line" in view["diff"] and "+new line" in view["diff"]
    assert target.read_text(encoding="utf-8") == "dear friend\nold line\nbye\n", "a preview writes nothing"


def test_a_write_to_a_new_file_previews_as_a_creation(tmp_path: Path):
    view = WriteFileTool(workspace=tmp_path).approval_evidence({"path": "fresh.txt", "content": "one\ntwo\n"})

    assert view["created"] is True
    assert "+one" in view["diff"] and "+two" in view["diff"]
    assert not (tmp_path / "fresh.txt").exists()


def test_an_append_previews_against_the_whole_file(tmp_path: Path):
    target = tmp_path / "log.txt"
    target.write_text("one\n", encoding="utf-8")

    view = WriteFileTool(workspace=tmp_path).approval_evidence(
        {"path": "log.txt", "content": "two\n", "mode": "append"}
    )

    assert "+two" in view["diff"] and "-one" not in view["diff"]


def test_a_file_that_cannot_be_read_as_text_previews_without_a_diff(tmp_path: Path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\xff\xfe\x00binary")

    view = WriteFileTool(workspace=tmp_path).approval_evidence({"path": "blob.bin", "content": "text"})

    assert view == {"path": str(target.resolve()), "created": False}


def test_a_path_outside_the_fence_previews_as_the_bare_path(tmp_path: Path):
    tool = WriteFileTool(workspace=tmp_path, allowed_dirs=(tmp_path,))

    assert tool.approval_evidence({"path": "/etc/hosts", "content": "x"}) == {"path": "/etc/hosts"}


def test_an_edit_previews_as_the_diff_of_its_two_snippets(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")

    view = EditFileTool(workspace=tmp_path).approval_evidence(
        {"path": "a.py", "old_text": "y = 2", "new_text": "y = 3"}
    )

    assert view["path"] == str((tmp_path / "a.py").resolve())
    assert view["created"] is False
    assert "-y = 2" in view["diff"] and "+y = 3" in view["diff"]


def test_the_file_tools_declare_the_layout_a_prompt_draws_them_in(tmp_path: Path):
    assert WriteFileTool(workspace=tmp_path).approval_kind == "file.write"
    assert EditFileTool(workspace=tmp_path).approval_kind == "file.write"
    assert ReadFileTool(workspace=tmp_path).approval_kind == ""


def test_a_file_too_large_to_preview_is_not_read_at_all(tmp_path: Path):
    """The preview runs on the gate's path before the call; a file past the cap
    is answered with its path alone rather than read and diffed."""
    from raven.agent.tools.filesystem import _PREVIEW_MAX_BYTES

    target = tmp_path / "big.log"
    target.write_bytes(b"x" * (_PREVIEW_MAX_BYTES + 1))

    view = WriteFileTool(workspace=tmp_path).approval_evidence({"path": "big.log", "content": "small"})

    assert view == {"path": str(target.resolve()), "created": False}


# --- a page of a text file, read a chunk at a time ------------------------------------


_EVERY_BREAK = (
    b"one\n"
    b"two\r\n"
    b"three\r"
    b"four\x0b"
    b"five\x0c"
    b"six\x1c"
    b"seven\x1d"
    b"eight\x1e"
    b"nine\xc2\x85"
    b"ten\xe2\x80\xa8"
    b"eleven\xe2\x80\xa9"
    b"\n"
    b"twelve \xc3\xa9 last, with no break after it"
)


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 7, 64])
def test_a_page_is_the_lines_splitlines_gives_whatever_the_chunk_edge(monkeypatch, chunk: int):
    """Every break ``str.splitlines`` knows, each landing on a chunk edge for some chunk size."""
    from io import BytesIO

    from raven.agent.tools import filesystem

    monkeypatch.setattr(filesystem, "_PAGE_CHUNK", chunk)
    want = _EVERY_BREAK.decode("utf-8").splitlines()
    assert len(want) == 13 and want[11] == "", "a blank line between two breaks counts; a trailing break does not"

    for start, limit in ((0, 2000), (0, 1), (1, 3), (8, 2), (12, 1), (13, 5)):
        page, total = filesystem._page_of_lines(BytesIO(_EVERY_BREAK), start, limit)
        assert total == 13 and [line.decode("utf-8") for line in page] == want[start : start + limit]
    assert filesystem._page_of_lines(BytesIO(b""), 0, 5) == ([], 0)
    assert filesystem._page_of_lines(BytesIO(b"\n"), 0, 5) == ([b""], 1)


@pytest.mark.asyncio
async def test_read_file_pages_a_file_without_holding_all_of_it(tmp_path: Path, monkeypatch):
    from raven.agent.tools import filesystem

    target = tmp_path / "breaks.txt"
    target.write_bytes(_EVERY_BREAK)
    monkeypatch.setattr(filesystem, "_PAGE_CHUNK", 5)
    read = ReadFileTool(workspace=tmp_path)

    first = await read.execute(path=str(target), limit=2)
    assert first == "1| one\n2| two\n\n(Showing lines 1-2 of 13. Use offset=3 to continue.)"
    last = await read.execute(path=str(target), offset=12)
    assert last == "12| \n13| twelve \u00e9 last, with no break after it\n\n(End of file \u2014 13 lines total)"
    assert await read.execute(path=str(target), offset=14) == "Error: offset 14 is beyond end of file (13 lines)"

    # A byte that is not UTF-8 refuses the page it is on, as the whole-file decode refused the file.
    target.write_bytes(b"fine\n\xff broken\n")
    assert (
        await read.execute(path=str(target), limit=1)
    ) == "1| fine\n\n(Showing lines 1-1 of 2. Use offset=2 to continue.)"
    assert str(await read.execute(path=str(target), offset=2)).startswith("Error reading file: 'utf-8' codec")
