"""The exec tool's deletion watch: which files a command made vanish.

No tool deletes a file as its purpose, so a deletion has no tool result of its
own to read. What the tool can say is which of the paths its command named were
there before it ran and are not there after -- and, because after the command
there is nothing left to read, what each of them held.

The limits are the point of most of these: a path the command computes is not
watched (the fence declares the same limit for itself), a path outside the
workspace is not watched, and a command that removed nothing must come back
byte-for-byte the result it came back before this existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.tools.shell import ExecTool


@pytest.fixture
def tool(tmp_path: Path) -> ExecTool:
    return ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)


async def test_one_removed_file_is_reported_with_the_text_it_held(tool, tmp_path):
    (tmp_path / "notes.md").write_text("one\ntwo\n")

    result = await tool.execute(command="rm notes.md")

    assert [(r.path, r.before) for r in result.removed] == [(str(tmp_path / "notes.md"), "one\ntwo\n")]
    assert not (tmp_path / "notes.md").exists()


async def test_two_removed_files_are_each_reported(tool, tmp_path):
    (tmp_path / "a.txt").write_text("a\n")
    (tmp_path / "b.txt").write_text("b\n")

    result = await tool.execute(command="rm a.txt b.txt")

    assert {(r.path, r.before) for r in result.removed} == {
        (str(tmp_path / "a.txt"), "a\n"),
        (str(tmp_path / "b.txt"), "b\n"),
    }


async def test_a_file_the_command_left_alone_is_not_reported(tool, tmp_path):
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / "go.txt").write_text("go\n")

    result = await tool.execute(command="rm go.txt")

    assert [r.path for r in result.removed] == [str(tmp_path / "go.txt")]


async def test_a_file_outside_the_workspace_is_not_watched(tmp_path):
    """The watch's roots are the fence's roots. A command that reaches outside
    them is the fence's business to refuse, not this record's to describe."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "gone.txt").write_text("gone\n")
    inside = tmp_path / "work"
    inside.mkdir()
    tool = ExecTool(working_dir=str(inside), restrict_to_workspace=False)

    result = await tool.execute(command=f"rm {outside / 'gone.txt'}")

    assert not (outside / "gone.txt").exists(), "the command itself must have run"
    assert result.removed == ()


async def test_an_extra_allowed_dir_is_watched(tmp_path):
    """The operator's second root is a root: a file removed there is as much
    this run's doing as one removed in the working directory."""
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "gone.txt").write_text("gone\n")
    work = tmp_path / "work"
    work.mkdir()
    tool = ExecTool(working_dir=str(work), restrict_to_workspace=False, extra_allowed_dirs=(extra,))

    result = await tool.execute(command=f"rm {extra / 'gone.txt'}")

    assert [r.path for r in result.removed] == [str(extra / "gone.txt")]


async def test_a_directory_token_is_not_watched(tool, tmp_path):
    """Only regular files. A directory has no text to hold and a client has no
    row to draw for it, and statting one as a file would report the whole tree
    as a single deletion."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("x\n")

    result = await tool.execute(command="rm -r sub")

    assert not (tmp_path / "sub").exists()
    assert result.removed == ()


async def test_a_glob_token_is_not_watched(tool, tmp_path):
    """The shell expands it to names this tool never sees, so the token as
    written names no file at all."""
    (tmp_path / "a.log").write_text("a\n")

    result = await tool.execute(command="rm *.log")

    assert not (tmp_path / "a.log").exists()
    assert result.removed == ()


async def test_a_command_that_removes_nothing_carries_the_same_result_as_before(tool, tmp_path):
    (tmp_path / "keep.txt").write_text("keep\n")

    result = await tool.execute(command="cat keep.txt")

    assert result.removed == ()
    assert "keep" in str(result)
    assert result.ok is True


async def test_a_failing_command_still_reports_what_it_removed(tool, tmp_path):
    """The exit code and the record are two different answers: a command that
    deleted one file and then failed still deleted it."""
    (tmp_path / "gone.txt").write_text("gone\n")

    result = await tool.execute(command="rm gone.txt && exit 3")

    assert result.ok is False
    assert [r.path for r in result.removed] == [str(tmp_path / "gone.txt")]


async def test_the_text_is_dropped_past_the_byte_cap_but_the_removal_is_not(tool, tmp_path):
    """Half a file reads as a smaller deletion than the one that happened, so
    the body goes rather than being truncated -- the row itself stays."""
    big = "x" * (ExecTool._MAX_REMOVAL_BYTES + 1)
    (tmp_path / "big.txt").write_text(big)

    result = await tool.execute(command="rm big.txt")

    assert [(r.path, r.before) for r in result.removed] == [(str(tmp_path / "big.txt"), None)]


async def test_undecodable_bytes_leave_the_removal_without_its_text(tool, tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00binary")

    result = await tool.execute(command="rm blob.bin")

    assert [(r.path, r.before) for r in result.removed] == [(str(tmp_path / "blob.bin"), None)]


async def test_the_background_lane_stats_nothing(tool, tmp_path, monkeypatch):
    """A detached command finishes long after this result is gone, so there is
    no 'after' to compare against -- and the watch must not read the files
    either, which is what the stub proves by never being called."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    (tmp_path / "gone.txt").write_text("gone\n")
    called: list[str] = []
    monkeypatch.setattr(ExecTool, "_removal_watch", lambda self, command, cwd: called.append(command) or {})

    result = await tool.execute(command="rm gone.txt", run_in_background=True)

    assert called == []
    assert getattr(result, "removed", ()) == ()


async def test_a_command_on_another_machine_stats_nothing(tool, tmp_path, monkeypatch):
    """Those paths are that machine's, not this filesystem's: resolving them
    here would report a local file of the same name as removed."""
    called: list[str] = []
    monkeypatch.setattr(ExecTool, "_removal_watch", lambda self, command, cwd: called.append(command) or {})

    async def _fake_run(command, connection, cwd):
        return "ran elsewhere"

    monkeypatch.setattr("raven.agent.tools.machine_exec.run_on_machine", _fake_run)

    result = await tool.execute(command="rm gone.txt", machine="box")

    assert called == []
    assert getattr(result, "removed", ()) == ()


async def test_the_candidate_cap_bounds_how_many_paths_one_command_watches(tool, tmp_path):
    """One stat and one read per named path is the cost; the cap is what keeps a
    command with a thousand arguments from paying it a thousand times."""
    names = [f"f{i}.txt" for i in range(ExecTool._MAX_REMOVAL_CANDIDATES + 5)]
    for name in names:
        (tmp_path / name).write_text("x\n")

    watched = tool._removal_watch("rm " + " ".join(names), str(tmp_path))

    assert len(watched) == ExecTool._MAX_REMOVAL_CANDIDATES


async def test_quoting_the_lexer_cannot_close_still_watches_the_named_paths(tool, tmp_path):
    """The shell is a better lexer than this one: a command it can still run
    must not lose its record because ``shlex`` gave up on the quote."""
    (tmp_path / "gone.txt").write_text("gone\n")

    watched = tool._removal_watch("rm gone.txt 'unclosed", str(tmp_path))

    assert list(watched) == [str(tmp_path / "gone.txt")]


@pytest.mark.parametrize(
    "command",
    [
        "rm gone.txt;",
        "rm gone.txt && echo done",
        "rm gone.txt | cat",
        "(rm gone.txt)",
        "rm gone.txt >/dev/null 2>&1",
        "echo first; rm gone.txt",
        "rm 'gone.txt';echo tail",
    ],
)
async def test_an_operator_beside_the_path_does_not_hide_the_removal(tool, tmp_path, command):
    """The shell reads `gone.txt;` as a path followed by an operator; a lexer
    that does not split on operators reads it as a file called `gone.txt;` and
    watches nothing. The watch reads the command the way the fence does."""
    (tmp_path / "gone.txt").write_text("gone\n")

    result = await tool.execute(command=command)

    assert not (tmp_path / "gone.txt").exists(), "the command itself must have run"
    assert [(r.path, r.before) for r in result.removed] == [(str(tmp_path / "gone.txt"), "gone\n")]
