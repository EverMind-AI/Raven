"""Filesystem tools: the unified diff a write reports for the UI.

A whole-file write is the only change no UI can reconstruct afterwards -- the
previous content is gone the moment it lands -- so the tool has to hand it over.
"""

import nturl2path
import os
import re
from pathlib import Path

import pytest

from raven.agent.tools import filesystem, remote_files
from raven.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    _is_reserved_windows_component,
    resolve_path,
)
from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import ToolOutput, ToolResult
from raven.ops import remote_path


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
    # Multibyte plus a newline: a character count and a byte count differ on
    # every platform, and the newline keeps the CRLF translation in play.
    content = "🙂\n"
    out = await WriteFileTool(workspace=tmp_path).execute(path=str(target), content=content)

    assert isinstance(out, ToolResult)
    # The write goes through a text layer, so a CRLF platform holds one more
    # byte for the newline -- the count the tool reports is the count that landed.
    written = target.stat().st_size
    assert written == len(content.replace("\n", os.linesep).encode("utf-8"))
    assert out.model_text.startswith(f"Successfully wrote {written} bytes to ")


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

    answer = await ReadFileTool(workspace=tmp_path).execute(path=target.as_uri())

    assert isinstance(answer, str)
    assert str(target) in answer
    assert "web_fetch" not in answer


@pytest.mark.asyncio
async def test_a_file_url_is_answered_with_a_path_the_caller_can_open(tmp_path: Path):
    """The answer names a path, so the path has to be one the tool can open.

    A file URL escapes what a path cannot carry, and the URL's own spelling is
    not a path on this machine: the drive of a Windows URL sits behind a
    leading slash, and a name with a space arrives percent-encoded. Handing
    the URL's spelling back sent the caller after a file that is not there.
    """
    target = tmp_path / "notes and facts.md"
    target.write_text("facts", encoding="utf-8")

    answer = await ReadFileTool(workspace=tmp_path).execute(path=target.as_uri())

    assert isinstance(answer, str)
    assert str(target) in answer
    hint = answer.split("-- try ")[-1]
    assert hint == str(target), f"the answer must name the path, not the URL: {answer}"

    re_read = await ReadFileTool(workspace=tmp_path).execute(path=hint)

    assert isinstance(re_read, str)
    assert re_read.startswith("1| facts"), f"the named path must open: {re_read}"


@pytest.mark.asyncio
async def test_a_file_url_for_another_machine_names_no_local_path(tmp_path: Path):
    """A URL with a host names somebody else's file, so there is no path to try."""
    read = ReadFileTool(workspace=tmp_path)

    answer = await read.execute(path="file://fileserver/share/notes.md")

    assert isinstance(answer, str)
    assert "file://" in answer
    assert "-- try" not in answer, f"no local path may be named: {answer}"


@pytest.mark.asyncio
async def test_a_file_url_for_localhost_names_the_local_path(tmp_path: Path):
    """A ``localhost`` authority names this machine, as the rest of the repo reads it."""
    target = tmp_path / "notes.md"
    target.write_text("facts", encoding="utf-8")

    answer = await ReadFileTool(workspace=tmp_path).execute(
        path=target.as_uri().replace("file://", "file://localhost", 1)
    )

    assert isinstance(answer, str)
    assert answer.split("-- try ")[-1] == str(target), f"localhost names this machine: {answer}"


@pytest.mark.asyncio
async def test_a_windows_localhost_file_url_keeps_its_unc_share(tmp_path: Path, monkeypatch):
    """A localhost authority names this machine, but a share stays a UNC share.

    ``url2pathname`` folds the authority away, so the bare conversion answers
    ``\\share\\notes.md`` -- the current drive's share, not the one the URL
    named. The rest of the repo keeps the host: media attachments and the ACP
    server both read ``file://localhost/share/x`` as ``\\\\localhost\\share\\x``.
    The platform and the converter are substituted, as Windows' own converter
    is the same pure string conversion.
    """
    monkeypatch.setattr("raven.agent.tools.filesystem._WINDOWS", True)
    monkeypatch.setattr("raven.agent.tools.filesystem.url2pathname", nturl2path.url2pathname)
    read = ReadFileTool(workspace=tmp_path)

    answer = await read.execute(path="file://localhost/share/notes.md")

    assert isinstance(answer, str)
    assert answer.split("-- try ")[-1] == "\\\\localhost\\share\\notes.md", answer


@pytest.mark.asyncio
async def test_a_windows_localhost_file_url_keeps_a_drive_path(tmp_path: Path, monkeypatch):
    """A localhost authority in front of a drive names this machine, not a share."""
    monkeypatch.setattr("raven.agent.tools.filesystem._WINDOWS", True)
    monkeypatch.setattr("raven.agent.tools.filesystem.url2pathname", nturl2path.url2pathname)
    read = ReadFileTool(workspace=tmp_path)

    answer = await read.execute(path="file://localhost/C:/work/notes.md")

    assert isinstance(answer, str)
    assert answer.split("-- try ")[-1] == "C:\\work\\notes.md", answer


@pytest.mark.parametrize(
    "encoded, decoded",
    [("%0A", "\n"), ("%00", "\x00"), ("%E2%80%A8", "\u2028")],
)
@pytest.mark.asyncio
async def test_a_file_url_whose_decoded_path_has_a_control_character_names_no_path(
    tmp_path: Path, encoded: str, decoded: str
):
    """A percent-encoded line break would forge a line in the tool result.

    The resolver refuses such a path on the next call anyway, so naming one
    would only put the control character into this answer.
    """
    read = ReadFileTool(workspace=tmp_path)

    answer = await read.execute(path=f"file:///tmp/notes{encoded}forged.md")

    assert isinstance(answer, str)
    assert "file:// URL" in answer
    assert "-- try" not in answer, f"no path may be named: {answer!r}"
    assert decoded not in answer, f"the decoded character must not reach the answer: {answer!r}"


@pytest.mark.asyncio
async def test_a_file_url_with_a_literal_line_break_does_not_carry_it_into_the_answer(tmp_path: Path):
    """The URL parser drops a newline, so the path it would report is not the caller's.

    The refusal escapes the raw argument; the answer must not carry the line
    break, which would let the argument forge a line of the result.
    """
    read = ReadFileTool(workspace=tmp_path)

    answer = await read.execute(path="file:///tmp/notes\nforged.md")

    assert isinstance(answer, str)
    assert "-- try" not in answer, f"no path may be named: {answer!r}"
    assert "\n" not in answer, f"the line break must not reach the answer: {answer!r}"


@pytest.mark.asyncio
async def test_a_file_url_that_cannot_be_parsed_names_no_local_path(tmp_path: Path):
    """A host that is not a valid IPv6 literal leaves the URL without a path."""
    read = ReadFileTool(workspace=tmp_path)

    answer = await read.execute(path="file://[::1/notes.md")

    assert isinstance(answer, str)
    assert "file://" in answer
    assert "-- try" not in answer, f"no local path may be named: {answer}"


@pytest.mark.skipif(
    os.name == "nt",
    reason="a colon cannot appear in a Windows directory name, so the file: hazard cannot exist there",
)
@pytest.mark.asyncio
async def test_a_file_url_whose_spelling_resolves_is_still_refused_by_scheme(tmp_path: Path):
    """A URL's own spelling can reach a real file, and the answer must not read it.

    Under Windows the spelling is drive-relative; on POSIX a directory may be
    named ``file:``, so the URL read as a relative path lands on a real file
    under the workspace. The refusal has to happen by scheme, before the path
    resolves, or the tool reads that file and reports an ordinary success.
    """
    hazard = tmp_path / "file:" / "tmp" / "notes.md"
    hazard.parent.mkdir(parents=True)
    hazard.write_text("HAZARD-CONTENT", encoding="utf-8")

    answer = await ReadFileTool(workspace=tmp_path).execute(path="file:///tmp/notes.md")

    assert isinstance(answer, str)
    assert "file:// URL" in answer
    assert "HAZARD-CONTENT" not in answer, f"the URL's spelling must not be read: {answer}"
    assert str(hazard) not in answer, f"the hazard path must not be named: {answer}"


@pytest.mark.asyncio
async def test_a_file_url_that_raises_in_the_platform_conversion_stays_a_refusal(tmp_path: Path, monkeypatch):
    """A malformed Windows spelling is refused, not raised.

    ``nturl2path.url2pathname`` raises ``OSError`` for a doubled drive colon
    (``file:///C::/notes.md``); the entry guard runs before ``_missing``, so
    the conversion failure would leave the call as an unhandled exception
    rather than a refusal. The CI host is POSIX, where the converter accepts
    the spelling, so the failure is driven by substituting a converter that
    raises -- the same one Windows ships for this shape.
    """
    monkeypatch.setattr(
        "raven.agent.tools.filesystem.url2pathname",
        lambda _p: (_ for _ in ()).throw(OSError("Bad URL")),
    )
    read = ReadFileTool(workspace=tmp_path)

    answer = await read.execute(path="file:///C::/notes.md")

    assert isinstance(answer, str)
    assert "file:// URL" in answer
    assert "plain path" in answer
    assert "-- try" not in answer, f"a path that cannot be converted must not be named: {answer}"


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


@pytest.mark.parametrize("chunk", [1, 3, 8, 64])
def test_a_line_longer_than_a_chunk_is_whole_on_its_page_and_only_counted_off_it(monkeypatch, chunk: int):
    from io import BytesIO

    from raven.agent.tools import filesystem

    monkeypatch.setattr(filesystem, "_PAGE_CHUNK", chunk)
    text = b"first\n" + b"L" * 50 + b"\r\nthird\n\n" + b"M" * 30
    want = text.decode("utf-8").splitlines()
    assert len(want) == 5

    for start, limit in ((0, 1), (0, 2), (1, 1), (1, 2), (2, 5), (4, 1), (5, 1)):
        page, total = filesystem._page_of_lines(BytesIO(text), start, limit)
        assert total == 5 and [line.decode("utf-8") for line in page] == want[start : start + limit], (start, limit)


def test_a_long_line_is_looked_at_once_whatever_its_length(monkeypatch):
    """Each chunk is split once, never the whole of the line again: a long line costs its length, not its square.

    Held whole and split again with every chunk, a file with no break in it
    took half a minute at 256 MiB where the whole-file read took under a
    second (measured 2026-10-13).
    """
    from io import BytesIO

    from raven.agent.tools import filesystem

    seen: list[int] = []
    split = filesystem._split_lines
    monkeypatch.setattr(filesystem, "_split_lines", lambda body: (seen.append(len(body)), split(body))[1])
    monkeypatch.setattr(filesystem, "_PAGE_CHUNK", 16)
    one_line = b"x" * 1000 + b"\xe2\x80" + b"y" * 500  # a held-back pair inside it, and no break at all

    page, total = filesystem._page_of_lines(BytesIO(one_line), 0, 5)
    assert (page, total) == ([one_line], 1)
    assert max(seen) <= 16 + 2 and sum(seen) == len(one_line), "every byte looked at once, plus a held-back pair"

    seen.clear()
    assert filesystem._page_of_lines(BytesIO(one_line + b"\nnext"), 1, 5) == ([b"next"], 2)
    assert max(seen) <= 16 + 2, "the line that is not on the page is counted, not kept"

    # A byte held back at the very end: a break that ends the last line, or part of it.
    assert filesystem._page_of_lines(BytesIO(b"abc\r"), 0, 5) == ([b"abc"], 1)
    assert filesystem._page_of_lines(BytesIO(b"abc\xe2"), 0, 5) == ([b"abc\xe2"], 1)
    assert filesystem._page_of_lines(BytesIO(b"\r"), 0, 5) == ([b""], 1)


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


# --- a line longer than a page --------------------------------------------------------------


_NOTE = (
    "(Line {n} is {length} characters long; the first {room} are shown. "
    "Use offset={n} char_offset={room} to read the next stretch of it.)"
)


@pytest.mark.asyncio
async def test_a_line_longer_than_a_page_shows_its_head_and_says_how_to_see_the_rest(tmp_path: Path):
    """One line over the page's character budget: the page used to be empty, with offset=1 to continue.

    A minified script, a one-line JSON, a CSV row of a few hundred thousand
    characters: the trim kept whole lines within the budget, the first line
    did not fit, so nothing was shown and the footer sent the reader back to
    the line it had just not shown (measured 2026-10-15: "(Showing lines 1-0
    of 1. Use offset=1 to continue.)").
    """
    target = tmp_path / "bundle.min.js"
    target.write_text("x" * 200_000, encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target))

    room = ReadFileTool._MAX_CHARS - len("1| ")
    body, note, footer = got.split("\n\n")
    assert body == "1| " + "x" * room
    assert note == _NOTE.format(n=1, length=200_000, room=room)
    assert footer == "(Showing lines 1-1 of 1.)"


@pytest.mark.asyncio
async def test_paging_moves_past_a_line_longer_than_a_page_instead_of_going_round(tmp_path: Path):
    """The lines before it fill a page of their own; the page after shows its head and moves on to the next line."""
    target = tmp_path / "rows.csv"
    target.write_text("header\n" + "y" * 200_000 + "\nlast\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    first = await read.execute(path=str(target))
    second = await read.execute(path=str(target), offset=2)
    third = await read.execute(path=str(target), offset=3)

    assert first == "1| header\n\n(Showing lines 1-1 of 3. Use offset=2 to continue.)"
    room = ReadFileTool._MAX_CHARS - len("2| ")
    assert second == (
        "2| " + "y" * room + "\n\n" + _NOTE.format(n=2, length=200_000, room=room) + "\n\n(Showing lines 2-2 of 3.)"
    )
    assert third == "3| last\n\n(End of file — 3 lines total)"


@pytest.mark.asyncio
async def test_a_tool_s_own_cut_per_line_lands_before_the_page_s_budget(tmp_path: Path):
    """A tool with a budget per line (Raven-Code's read: 2000, with a mark) cuts on the hook, so minified lines share a page."""

    class Cut(ReadFileTool):
        def _cut_line(self, line: str) -> str:
            return line[:8] + "..." if len(line) > 8 else line

    target = tmp_path / "bundle.min.js"
    target.write_text("\n".join("y" * 200_000 for _ in range(3)) + "\nend\n", encoding="utf-8")

    got = await Cut(workspace=tmp_path).execute(path=str(target))

    assert got == "1| yyyyyyyy...\n2| yyyyyyyy...\n3| yyyyyyyy...\n4| end\n\n(End of file \u2014 4 lines total)"
    assert "(Line 1 is" not in got and "(Showing lines" not in got


@pytest.mark.asyncio
async def test_a_first_line_that_just_fills_the_page_is_shown_whole_and_the_page_moves_on(tmp_path: Path):
    """At the edge the budget counts the line break too: the line fits, and is not cut or noted."""
    room = ReadFileTool._MAX_CHARS - len("1| ")
    target = tmp_path / "edge.txt"
    target.write_text("z" * room + "\nnext\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target))

    assert got == "1| " + "z" * room + "\n\n(Showing lines 1-1 of 2. Use offset=2 to continue.)"


# --- a character offset inside a line ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_one_line_file_is_read_from_a_character_offset_inside_it(tmp_path: Path):
    """A one-line file over the tool-output budget: the line number cannot move within it, so the character offset does.

    A minified bundle, a one-line JSON, a CSV row of a few hundred thousand
    characters: the file has one line, so paging by lines never reaches the
    tail of it. The character offset does, and says where it stopped.
    """
    target = tmp_path / "bundle.min.js"
    target.write_text("x" * 200_000, encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), char_offset=150_000)

    body, note, footer = got.split("\n\n")
    assert body == "1| " + "x" * 50_000
    assert note == "(Line 1 is 200000 characters long; characters 150000-199999 are shown, the rest of it.)"
    assert footer == "(End of file — 1 lines total)"


@pytest.mark.asyncio
async def test_a_character_offset_into_a_line_still_over_the_budget_says_where_to_continue(tmp_path: Path):
    """The stretch read is still over the page's budget: it is cut, and the offset that follows it is named."""
    target = tmp_path / "bundle.min.js"
    target.write_text("x" * 200_000, encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), char_offset=50_000)

    room = ReadFileTool._MAX_CHARS - len("1| ")
    body, note, footer = got.split("\n\n")
    assert body == "1| " + "x" * room
    assert note == (
        "(Line 1 is 200000 characters long; characters 50000-177996 are shown. "
        "Use offset=1 char_offset=177997 to read the next stretch of it.)"
    )
    assert footer == "(Showing lines 1-1 of 1.)"


@pytest.mark.asyncio
async def test_a_character_offset_reads_the_line_the_line_number_names(tmp_path: Path):
    """The character offset reaches into the line the page starts at, not into the first of the file."""
    target = tmp_path / "rows.csv"
    target.write_text("header\n" + "y" * 200_000 + "\nlast\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), offset=2, char_offset=199_000)

    body, note, footer = got.split("\n\n")
    assert body == "2| " + "y" * 1000
    assert note == "(Line 2 is 200000 characters long; characters 199000-199999 are shown, the rest of it.)"
    assert footer == "(Showing lines 2-2 of 3. Use offset=3 to continue.)"


@pytest.mark.asyncio
async def test_a_character_offset_past_the_end_of_the_line_says_so(tmp_path: Path):
    target = tmp_path / "rows.csv"
    target.write_text("header\nshort\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), offset=2, char_offset=6)

    assert got == "Error: char_offset 6 is beyond the end of line 2 (5 characters)"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-5, "5", None, True])
async def test_a_character_offset_that_is_not_a_count_reads_the_page_from_its_start(bad, tmp_path: Path):
    """The registry casts and checks the argument; one that arrives anyway is read as no offset at all."""
    target = tmp_path / "notes.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), char_offset=bad)

    assert got == "1| first\n2| second\n\n(End of file — 2 lines total)"


@pytest.mark.asyncio
async def test_a_character_offset_leaves_the_rest_of_the_read_untouched(tmp_path: Path):
    """A short line, an offset inside it: the page is the same page, with the line cut where the offset says."""
    target = tmp_path / "notes.txt"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), char_offset=3)

    assert got == (
        "1| st\n\n"
        "(Line 1 is 5 characters long; characters 3-4 are shown, the rest of it.)\n\n"
        "(Showing lines 1-1 of 3. Use offset=2 to continue.)"
    )


@pytest.mark.asyncio
async def test_a_cut_line_in_the_middle_of_a_file_holds_the_page_on_its_own_line(tmp_path: Path):
    """A long line that is not the first: the note names the line its cursor continues, and the
    footer counts the page without handing the next line on, so nothing skips the rest of the line."""
    target = tmp_path / "rows.csv"
    target.write_text("header\n" + "y" * 200_000 + "\nlast\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), offset=2, char_offset=10_000)

    room = ReadFileTool._MAX_CHARS - len("2| ")
    body, note, footer = got.split("\n\n")
    assert body == "2| " + "y" * room
    assert note == (
        f"(Line 2 is 200000 characters long; characters 10000-{10_000 + room - 1} are shown. "
        f"Use offset=2 char_offset={10_000 + room} to read the next stretch of it.)"
    )
    assert footer == "(Showing lines 2-2 of 3.)"


@pytest.mark.asyncio
async def test_the_continuation_hint_can_be_followed_verbatim_to_the_end_of_the_line(tmp_path: Path):
    """Every hint the read emits names the line and the character cursor together, so following
    them exactly walks the whole line: no error, no skipped stretch, no going round."""
    target = tmp_path / "bundle.min.js"
    target.write_text("x" * 300_000 + "\ntail\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    seen: list[str] = []
    offset, char_offset, got = 1, 0, ""
    for _ in range(40):
        got = await read.execute(path=str(target), offset=offset, char_offset=char_offset)
        assert not got.startswith("Error"), got
        first_line = got.split("\n", 1)[0]
        seen.append(first_line[len("1| ") :])
        hint = re.search(r"Use offset=(\d+) char_offset=(\d+)", got)
        if hint is None:
            break
        offset, char_offset = int(hint.group(1)), int(hint.group(2))
    else:
        raise AssertionError("the walk never reached the end of the line")

    assert "".join(seen) == "x" * 300_000
    assert "the rest of it" in got
    assert "(Showing lines 1-1 of 2. Use offset=2 to continue.)" in got
    assert await read.execute(path=str(target), offset=2) == "2| tail\n\n(End of file — 2 lines total)"


@pytest.mark.asyncio
async def test_the_hint_from_a_line_the_file_does_not_end_with_walks_that_line_to_its_end(tmp_path: Path):
    """The reported scenario pinned end to end: a long line in the middle of the file,
    paged from a character offset inside it, every emitted hint followed verbatim."""
    target = tmp_path / "rows.csv"
    target.write_text("header\n" + "y" * 300_000 + "\nlast\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    seen: list[str] = []
    offset, char_offset, got = 2, 1_000, ""
    for _ in range(40):
        got = await read.execute(path=str(target), offset=offset, char_offset=char_offset)
        assert not got.startswith("Error"), got
        first_line = got.split("\n", 1)[0]
        seen.append(first_line[len("2| ") :])
        hint = re.search(r"Use offset=(\d+) char_offset=(\d+)", got)
        if hint is None:
            break
        offset, char_offset = int(hint.group(1)), int(hint.group(2))
    else:
        raise AssertionError("the walk never reached the end of the line")

    assert "".join(seen) == "y" * (300_000 - 1_000)
    assert "the rest of it" in got
    assert "(Showing lines 2-2 of 3. Use offset=3 to continue.)" in got
    assert await read.execute(path=str(target), offset=3) == "3| last\n\n(End of file — 3 lines total)"


@pytest.mark.asyncio
async def test_a_character_offset_equal_to_the_line_s_length_is_refused_not_read_as_empty(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("short\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), offset=1, char_offset=5)

    assert got == "Error: char_offset 5 is beyond the end of line 1 (5 characters)"


@pytest.mark.asyncio
async def test_the_last_character_of_a_line_is_reachable(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("short\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), offset=1, char_offset=4)

    body, note, footer = got.split("\n\n")
    assert body == "1| t"
    assert note == "(Line 1 is 5 characters long; characters 4-4 are shown, the rest of it.)"
    assert footer == "(End of file — 1 lines total)"


@pytest.mark.asyncio
async def test_a_character_offset_counts_code_points_not_bytes(tmp_path: Path):
    """The offset reaches into text the file carries as UTF-8: it counts the code points the
    reader sees, and the stretch it shows is cut between characters rather than through one."""
    wide = "\u6982" * 30
    rocket = "\U0001f680" * 10
    tail = "z" * 20
    target = tmp_path / "wide.txt"
    target.write_text(wide + rocket + tail + "\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), offset=1, char_offset=35)

    body, note, footer = got.split("\n\n")
    assert body == "1| " + "\U0001f680" * 5 + tail
    assert note == "(Line 1 is 60 characters long; characters 35-59 are shown, the rest of it.)"
    assert footer == "(End of file — 1 lines total)"


@pytest.mark.asyncio
async def test_a_limit_below_one_reads_the_default_page_rather_than_erroring(tmp_path: Path):
    """The schema asks for at least one line; a caller that names none still gets a page."""
    target = tmp_path / "notes.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    read = ReadFileTool(workspace=tmp_path)

    got = await read.execute(path=str(target), limit=0)
    below = await read.execute(path=str(target), limit=-3)

    assert got == below == "1| first\n2| second\n\n(End of file — 2 lines total)"


# ---------------------------------------------------------------------------
# The Windows reserved-name fence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "segment",
    [
        "nul",
        "NUL",
        "nul.md",
        "Nul.Md",
        "nul.tar.gz",
        "con",
        "con.txt",
        "aux",
        "prn",
        "com1",
        "com9",
        "lpt1",
        "lpt9",
        "com¹",
        "COM¹.txt",
        "LPT²",
        "lpt³.md",
        "com1.md",
        "conin$",
        "conout$",
        "NUL:",
        "nul:stream",
        "notes.md:",
        "nul ",
        "nul.",
        "nul .md",
        "notes.md.",
        "notes.md ",
        "...",
    ],
)
def test_a_reserved_segment_is_one_in_any_spelling(segment):
    assert _is_reserved_windows_component(segment)


@pytest.mark.parametrize(
    "segment",
    [
        "report.md",
        "null",
        "nulx",
        "nulx.md",
        "com0",
        "lpt0",
        "com0.txt",
        "lpt0.txt",
        "com10",
        "com19",
        "COM⁴",
        "config.json",
        "auxiliary.md",
        "con an.md",
        "",
        ".",
        "..",
    ],
)
def test_an_ordinary_segment_is_not_reserved(segment):
    assert not _is_reserved_windows_component(segment)


def test_a_reserved_name_is_refused_the_way_an_out_of_fence_path_is(monkeypatch, tmp_path: Path):
    """On Windows ``nul`` names the NUL device in every directory (CPython 95486).

    ``Path.resolve()`` cannot see it coming: ``ntpath.realpath`` maps only a
    path that is exactly ``nul`` to ``\\\\.\\NUL`` and hands
    ``<workspace>\\nul`` back unchanged, so the allowed-dir check accepts it
    and a write answers success for content no file received. The fence has to
    catch the name itself, in any component, because Windows applies its
    naming rules to directories too, and in the other shapes that lose bytes
    the same way: a trailing dot or space lands them in the trimmed name while
    the receipt reports the given one, and a colon lands them in an NTFS
    stream no directory listing shows.
    """
    monkeypatch.setattr(filesystem, "_WINDOWS", True)
    for name in (
        "nul",
        "nul.md",
        "con.md",
        "subdir/nul",
        "nul/report.md",
        "subdir/nul.md/report.md",
        "COM¹.txt",
        "LPT²/log.txt",
        "notes.md.",
        "notes.md ",
        "notes.md:hidden",
        "NUL:",
    ):
        with pytest.raises(PermissionError, match="reserved name on Windows"):
            resolve_path(name, workspace=tmp_path, allowed_dirs=(tmp_path,))


def test_a_reserved_name_is_refused_when_the_path_is_absolute(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    with pytest.raises(PermissionError, match="reserved name on Windows"):
        resolve_path(str(tmp_path / "subdir" / "nul"), workspace=tmp_path, allowed_dirs=(tmp_path,))


def test_a_drive_letter_written_with_double_slashes_stays_in_the_reserved_check(monkeypatch, tmp_path: Path):
    """``C://work/nul`` is a drive path on Windows, not a URL.

    ``_URL_SCHEME_RE`` accepts a one-letter scheme, so a bare scheme test
    reads ``C`` as one and skips the fence -- yet Windows parses the same
    string as ``C:\\work\\nul``. The exemption only holds for schemes Windows
    cannot read as a drive, i.e. two letters or more, so every reserved shape
    behind a ``C://`` prefix is refused like its plain spelling.
    """
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    for name in ("C://work/nul", "C://work/notes.md.", "C://work/notes.md:hidden", "C://work/COM¹.txt"):
        with pytest.raises(PermissionError, match="reserved name on Windows"):
            resolve_path(name, workspace=tmp_path, allowed_dirs=(tmp_path,))


def test_a_multi_letter_url_scheme_is_still_left_to_the_url_hint(monkeypatch, tmp_path: Path):
    """The two-letter floor keeps the real URL exemption intact."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    resolved = resolve_path("https://example.com/nul.md", workspace=tmp_path, allowed_dirs=(tmp_path,))

    assert resolved == tmp_path / "https:" / "example.com" / "nul.md"


def test_the_refusal_shows_the_path_with_repr(monkeypatch, tmp_path: Path):
    """The character at fault is often invisible, so the message quotes the path.

    A bare ``Path notes.md  is a reserved name`` hides the trailing space a
    reader has to drop; ``!r`` makes it retryable.
    """
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    with pytest.raises(PermissionError) as exc_info:
        resolve_path("notes.md ", workspace=tmp_path, allowed_dirs=(tmp_path,))

    assert "'notes.md '" in str(exc_info.value)


def test_an_ordinary_name_resolves_with_the_gate_on(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    assert resolve_path("report.md", workspace=tmp_path, allowed_dirs=(tmp_path,)) == tmp_path / "report.md"


def test_a_zero_numbered_com_name_is_an_ordinary_file_with_the_gate_on(monkeypatch, tmp_path: Path):
    """Microsoft's naming rules reserve COM/LPT 1-9; 0 never named a device."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    for name in ("com0.txt", "lpt0.txt"):
        assert resolve_path(name, workspace=tmp_path, allowed_dirs=(tmp_path,)) == tmp_path / name


def test_a_url_argument_is_left_to_the_tools_url_hint_with_the_gate_on(monkeypatch, tmp_path: Path):
    """The URL's scheme colon is one of the reserved shapes; the hint must win.

    ``_missing`` answers a URL with the tool that can serve it, so the fence
    must not refuse a URL argument first -- a scheme of two letters or more
    names a resource no local drive can spell.
    """
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    resolved = resolve_path("https://example.com/a.md", workspace=tmp_path, allowed_dirs=(tmp_path,))

    assert resolved == tmp_path / "https:" / "example.com" / "a.md"


def test_a_device_like_name_stays_legal_off_windows(monkeypatch, tmp_path: Path):
    """POSIX puts no devices in names: a workspace file called ``nul`` is a file."""
    monkeypatch.setattr(filesystem, "_WINDOWS", False)

    assert resolve_path("nul", workspace=tmp_path, allowed_dirs=(tmp_path,)) == tmp_path / "nul"


@pytest.mark.asyncio
async def test_a_write_to_a_device_name_answers_an_error_and_writes_nothing(monkeypatch, tmp_path: Path):
    """The defect was a success receipt for content no file received."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    answer = await WriteFileTool(workspace=tmp_path).execute(path="nul", content="hello\n")

    assert isinstance(answer, str)
    assert "reserved name on Windows" in answer
    # On Windows the device itself answers ``exists()``, so the directory
    # entries are what can prove no file was created.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_write_to_a_colon_device_spelling_answers_an_error_and_writes_nothing(monkeypatch, tmp_path: Path):
    """``NUL:`` is the device itself; its receipt must not claim a save either."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    answer = await WriteFileTool(workspace=tmp_path).execute(path="NUL:", content="hello\n")

    assert isinstance(answer, str)
    assert "reserved name on Windows" in answer
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_write_to_a_trailing_dot_name_answers_an_error_and_writes_nothing(monkeypatch, tmp_path: Path):
    """Windows trims the dot before creating: the bytes land in ``notes.md``."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    answer = await WriteFileTool(workspace=tmp_path).execute(path="notes.md.", content="hello\n")

    assert isinstance(answer, str)
    assert "reserved name on Windows" in answer
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_write_to_a_zero_numbered_com_name_writes_a_real_file(monkeypatch, tmp_path: Path):
    """The control side of the 1-9 rule: ``com0.txt`` is a file Windows creates."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    answer = await WriteFileTool(workspace=tmp_path).execute(path="com0.txt", content="hello\n")

    assert isinstance(answer, ToolResult)
    assert answer.model_text.startswith("Successfully wrote ")
    assert [p.name for p in tmp_path.iterdir()] == ["com0.txt"]


@pytest.mark.asyncio
async def test_a_read_of_a_device_name_answers_an_error_not_a_missing_file(monkeypatch, tmp_path: Path):
    """ "File not found" reads as a misspelling; the name is the problem itself."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    answer = await ReadFileTool(workspace=tmp_path).execute(path="nul.md")

    assert isinstance(answer, str)
    assert "reserved name on Windows" in answer


@pytest.mark.asyncio
async def test_a_read_of_a_url_still_answers_the_fetch_hint_with_the_gate_on(monkeypatch, tmp_path: Path):
    """The gate must not swallow the URL answer: its colon is not a stream."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)

    answer = await ReadFileTool(workspace=tmp_path).execute(path="https://example.com/a.md")

    assert isinstance(answer, str)
    assert "web_fetch" in answer


REMOTE_RESERVED_SPELLINGS = (
    "box:raven-work/t1/bundle.js",
    "root@203.0.113.7:/srv/x.md",
    "box:raven-work/nul",
    "box:nul",
    "root@203.0.113.7:/srv/aux",
    "box:raven-work/com1",
    "box:raven-work/nul.md",
    "box:raven-work/nul:hidden",
    "scp://box:2222//raven-work/nul",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", REMOTE_RESERVED_SPELLINGS)
@pytest.mark.parametrize(
    "tool_name,call",
    [
        ("read_file", {}),
        ("write_file", {"content": "hello\n"}),
        ("edit_file", {"old_text": "a", "new_text": "b"}),
        ("list_dir", {}),
    ],
)
async def test_a_remote_path_reaches_its_machine_with_the_gate_on(
    monkeypatch, tmp_path: Path, spelling: str, tool_name: str, call: dict
):
    """A remote path's components are that machine's files, ordinary names there.

    ``nul``, ``aux`` and ``com1`` are legal file names on a Linux machine, so a
    remote spelling must reach ``remote_files.answer`` whatever its components
    say. Only the head, before the separator, could be the device instead of a
    machine. The native Windows CI job caught the earlier guard refusing
    ``box:raven-work/...``; the remote reserved-name shapes are the same class
    one component further in.
    """
    monkeypatch.setattr(filesystem, "_WINDOWS", True)
    seen: list[tuple[str, str]] = []

    async def answer(tool: str, path: str, params: dict) -> str | None:
        if not remote_path.looks_remote(path):
            return None
        seen.append((tool, path))
        return f"Error: {path} is on another machine, and this process does not read registered machines' files"

    monkeypatch.setattr(remote_files, "answer", answer)
    tools = {
        "read_file": ReadFileTool,
        "write_file": WriteFileTool,
        "edit_file": EditFileTool,
        "list_dir": ListDirTool,
    }

    out = await tools[tool_name](workspace=tmp_path).execute(path=spelling, **call)

    assert isinstance(out, str)
    assert "reserved name on Windows" not in out
    assert "another machine" in out
    assert seen == [(tool_name, spelling)]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_local_reserved_component_still_answers_the_refusal_with_the_gate_on(monkeypatch, tmp_path: Path):
    """The control for the test above: a local spelling keeps every component checked."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)
    seen: list[tuple[str, str]] = []

    async def answer(tool: str, path: str, params: dict) -> str | None:
        if not remote_path.looks_remote(path):
            return None
        seen.append((tool, path))
        return f"Error: {path} is on another machine"

    monkeypatch.setattr(remote_files, "answer", answer)

    for spelling in ("subdir/nul", "nul/report.md", "notes.md.", "notes.md "):
        out = await WriteFileTool(workspace=tmp_path).execute(path=spelling, content="hello\n")
        assert isinstance(out, str)
        assert "reserved name on Windows" in out, spelling
    assert seen == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_colon_device_spelling_never_reaches_its_machine_with_the_gate_on(monkeypatch, tmp_path: Path):
    """The control for the test above: ``NUL:`` is the device, not a host named ``NUL``."""
    monkeypatch.setattr(filesystem, "_WINDOWS", True)
    seen: list[str] = []

    async def answer(tool: str, path: str, params: dict) -> str:
        seen.append(path)
        return f"Error: {path} is on another machine"

    monkeypatch.setattr(remote_files, "answer", answer)

    refused = await WriteFileTool(workspace=tmp_path).execute(path="NUL:", content="hello\n")

    assert isinstance(refused, str)
    assert "reserved name on Windows" in refused
    assert seen == []
    assert list(tmp_path.iterdir()) == []
