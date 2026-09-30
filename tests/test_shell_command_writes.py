"""The exec tool's record of what a command wrote: its directory either side.

A command returns its output and nothing else, so the tool lists the directory
it runs in before and after, and -- where a shadow repo covers the directory --
stages the tree first so a rewritten or removed file can be shown against what
it held. The shadow repo here is a stand-in: these are the tool's decisions
(when to stage, when not to run, what to hand back), and the real repo has its
own tests in ``test_runtime_checkpoint.py``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Collection

import pytest

from raven.agent.loop.checkpoint import StagingTimeoutError
from raven.agent.tools import command_writes, snapshot
from raven.agent.tools.shell import ExecTool


class _Shadow:
    """A shadow repo whose tree is whatever the directory held at staging."""

    def __init__(
        self, root: Path, *, stage: Any = None, ignored: Collection[str] = (), answer_after: float = 0.0
    ) -> None:
        self._root = root
        self._stage = stage
        self._answer_after = answer_after
        self._ignored = set(ignored)
        self._trees: dict[str, dict[str, bytes]] = {}
        self.staged = 0
        self.warmed = 0

    async def warm(self) -> None:
        self.warmed += 1

    async def stage_tree(self) -> str | None:
        self.staged += 1
        if self._stage is not None:
            return self._stage()
        tree = f"t{self.staged}"
        self._trees[tree] = {str(p): p.read_bytes() for p in self._root.rglob("*") if p.is_file()}
        return tree

    async def read_blobs(self, tree: str, paths: Collection[str], *, max_bytes: int) -> dict[str, bytes]:
        assert tree is not None, "no tree was staged, so there is none to read"
        held = self._trees.get(tree, {})
        return {path: held[path] for path in paths if path in held and len(held[path]) <= max_bytes}

    async def trackable(self, paths: Collection[str]) -> set[str]:
        await asyncio.sleep(self._answer_after)
        return {path for path in paths if Path(path).name not in self._ignored}


def _tool(root: Path, shadow: _Shadow | None = None, *, record_writes: bool = True) -> ExecTool:
    return ExecTool(
        working_dir=str(root),
        restrict_to_workspace=True,
        record_writes=record_writes,
        shadow=(lambda _root: shadow) if shadow is not None else None,
    )


async def test_a_rewrite_is_measured_against_the_tree_staged_just_before_it(tmp_path):
    (tmp_path / "notes.md").write_text("one\n")
    shadow = _Shadow(tmp_path)

    result = await _tool(tmp_path, shadow).execute(command="echo two >> notes.md")

    assert shadow.staged == 1
    [write] = result.written
    assert (write.path, write.created, write.added, write.removed) == (str(tmp_path / "notes.md"), False, 1, 0)
    assert "+two" in write.diff.splitlines()


@pytest.mark.parametrize("error", [TimeoutError, StagingTimeoutError])
async def test_a_command_whose_tree_is_not_staged_in_time_still_runs_without_a_diff(tmp_path, error):
    """The command matters more than its diff. Past the wait it runs all the
    same and is still listed, so the file it rewrote is reported -- only not
    what that file held. The checkpoint's own timeout must be a ``TimeoutError``
    for this: the tool holds the repo through a protocol and cannot import it."""
    (tmp_path / "notes.md").write_text("one\n")

    def _too_slow() -> str:
        raise error

    result = await _tool(tmp_path, _Shadow(tmp_path, stage=_too_slow)).execute(command="echo two >> notes.md")

    assert (tmp_path / "notes.md").read_text() == "one\ntwo\n", "the command must have run"
    assert result.ok is True
    [write] = result.written
    assert (write.created, write.added, write.diff) == (False, None, None)


async def test_a_tree_that_cannot_be_staged_leaves_the_command_to_run_without_what_its_files_held(tmp_path):
    """A git that failed, a directory the repo cannot hold: nothing about the
    filesystem says the command should not run, so it does. A rewrite has no
    earlier text to be shown against; a created file needs none."""
    (tmp_path / "notes.md").write_text("one\n")

    result = await _tool(tmp_path, _Shadow(tmp_path, stage=lambda: None)).execute(
        command="printf 'x\\n' > made.txt && echo two >> notes.md"
    )

    assert (tmp_path / "made.txt").read_text() == "x\n"
    written = {Path(w.path).name: w for w in result.written}
    assert (written["made.txt"].created, written["made.txt"].lines, written["made.txt"].added) == (True, 1, 1)
    assert "+x" in written["made.txt"].diff.splitlines()
    assert (written["notes.md"].added, written["notes.md"].diff) == (None, None)


def _no_tree() -> None:
    return None


def _late_tree() -> str:
    raise TimeoutError


@pytest.mark.parametrize("stage", [_no_tree, _late_tree], ids=["not-staged", "staged-too-late"])
async def test_a_tree_that_was_not_staged_still_leaves_the_repo_to_rule_on_what_may_be_shown(tmp_path, stage):
    """Whether a file's text may be shown is the repo's rules, which need no
    tree. A staging that failed or ran late costs the diffs a tree would give;
    it must not also let a ``.env`` the command removed by name, or created,
    go out with its text, while an ordinary file keeps its own."""
    (tmp_path / ".env").write_text("API_KEY=top-secret\n")
    (tmp_path / "notes.md").write_text("one\n")

    result = await _tool(tmp_path, _Shadow(tmp_path, stage=stage, ignored={".env", "made.env"})).execute(
        command="rm .env notes.md && printf 'TOKEN=x\\n' > made.env"
    )

    assert {Path(r.path).name: r.before for r in result.removed} == {".env": None, "notes.md": "one\n"}
    [write] = result.written
    assert (Path(write.path).name, write.added, write.diff) == ("made.env", 1, None)


async def test_a_created_file_the_repo_would_not_store_keeps_its_counts_and_loses_its_text(tmp_path):
    result = await _tool(tmp_path, _Shadow(tmp_path, ignored={".env"})).execute(
        command="printf 'API_KEY=top-secret\\n' > .env && printf 'ok\\n' > notes.md"
    )

    written = {Path(w.path).name: w for w in result.written}
    assert written[".env"].diff is None and written[".env"].added == 1
    assert "+ok" in written["notes.md"].diff.splitlines()


async def test_a_removed_file_carries_what_the_staged_tree_held(tmp_path):
    (tmp_path / "doomed.txt").write_text("one\ntwo\n")

    result = await _tool(tmp_path, _Shadow(tmp_path)).execute(command="find . -name '*.txt' -delete")

    assert [(r.path, r.before) for r in result.removed] == [(str(tmp_path / "doomed.txt"), "one\ntwo\n")]


async def test_a_file_the_command_named_keeps_its_text_only_where_the_repo_would_store_it(tmp_path):
    """The command's own watch reads a named file before it goes. That text is
    held to the same rule as every other file's: a ``.env`` removed by name goes
    out without its body, an ordinary file with it."""
    (tmp_path / ".env").write_text("API_KEY=top-secret\n")
    (tmp_path / "notes.md").write_text("one\n")

    result = await _tool(tmp_path, _Shadow(tmp_path, ignored={".env"})).execute(command="rm .env notes.md")

    assert {Path(r.path).name: r.before for r in result.removed} == {".env": None, "notes.md": "one\n"}


async def test_a_tool_not_asked_to_record_writes_does_not_list_or_stage(tmp_path, monkeypatch):
    """A sub-agent's runner lists every call itself, so its ``exec`` does not
    walk the directory a second time around each command."""
    roots: list[Any] = []
    monkeypatch.setattr(snapshot, "take", lambda root: roots.append(root))
    shadow = _Shadow(tmp_path)

    result = await _tool(tmp_path, shadow, record_writes=False).execute(command="echo x > made.txt")

    assert roots == [] and shadow.staged == 0
    assert result.written == ()


async def test_a_background_command_takes_no_listing(tmp_path, monkeypatch):
    """Its files land after the call has returned, so a listing either side of
    the start would describe nothing it did."""
    roots: list[Any] = []
    monkeypatch.setattr(snapshot, "take", lambda root: roots.append(root))

    await _tool(tmp_path, _Shadow(tmp_path)).execute(command="true", run_in_background=True)

    assert roots == []


async def test_a_directory_too_large_to_list_is_not_staged(tmp_path, monkeypatch):
    """No listing means nothing to report, so a staging would be paid for nothing."""
    monkeypatch.setattr(snapshot, "take", lambda root: None)
    shadow = _Shadow(tmp_path)

    result = await _tool(tmp_path, shadow).execute(command="echo x > made.txt")

    assert shadow.staged == 0
    assert result.written == ()


async def test_reading_the_written_files_never_runs_on_the_event_loop(tmp_path, monkeypatch):
    """This reads every written file whole, and one command can write a
    hundred; every other session on the process waits behind the loop."""
    real = command_writes._writes
    threads: list[int] = []

    def watched(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(command_writes, "_writes", watched)

    result = await _tool(tmp_path).execute(command="echo x > made.txt")

    assert result.written
    assert threads and threading.get_ident() not in threads


async def test_past_the_calls_diff_budget_the_counts_still_go_and_the_diff_does_not(tmp_path, monkeypatch):
    """One command can rewrite a hundred files. The diffs share the call's
    budget, and one past it is dropped whole -- half a diff reads as a smaller
    change -- while its counts, which cost nothing, still say how big it was."""
    monkeypatch.setattr(command_writes, "DIFF_BUDGET_CHARS", 60)

    result = await _tool(tmp_path, _Shadow(tmp_path)).execute(
        command="printf 'one\\ntwo\\n' > a.txt && printf 'three\\nfour\\n' > b.txt"
    )

    first, second = sorted(result.written, key=lambda w: w.path)
    assert first.diff is not None and second.diff is None
    assert (second.added, second.removed) == (2, 0)


async def test_warming_stages_through_the_shadow_repo_and_is_a_no_op_without_one(tmp_path):
    shadow = _Shadow(tmp_path)

    await _tool(tmp_path, shadow).warm(tmp_path)
    await _tool(tmp_path).warm(tmp_path)
    await _tool(tmp_path, shadow, record_writes=False).warm(tmp_path)

    assert shadow.warmed == 1, "only the tool that records writes warms for them"


def test_a_measuring_tools_ceiling_covers_the_longest_command_and_the_longest_waits():
    """The registry kills a call past the tool's ceiling. A measured command may
    wait out its staging, run to the executor's own cap and then have its files
    measured, and a ceiling under the three together would kill a command the
    executor was still allowed to run -- losing its output. Asked twice, the
    tool raises its ceiling once."""
    from raven.agent.loop import checkpoint

    tool = ExecTool(record_writes=True)
    spent = checkpoint._STAGE_WAIT_SECONDS + ExecTool._MAX_TIMEOUT + command_writes.AFTER_WAIT_SECONDS
    assert tool.timeout_seconds > spent
    assert command_writes.MEASURE_SECONDS == checkpoint._STAGE_WAIT_SECONDS + command_writes.AFTER_WAIT_SECONDS
    tool.measure_writes(None)
    assert tool.timeout_seconds == ExecTool.timeout_seconds + command_writes.MEASURE_SECONDS
    assert ExecTool().timeout_seconds == ExecTool.timeout_seconds > ExecTool._MAX_TIMEOUT
    assert command_writes.AFTER_WAIT_SECONDS > command_writes.READ_WAIT_SECONDS


async def test_a_shadow_repo_too_slow_to_answer_after_the_command_leaves_every_file_bare(tmp_path, monkeypatch):
    """The command has run; what the repo would have said about its files is
    worth less than its output. Past the wait the files go out without text --
    including a named removal's, which the repo never got to rule on."""
    monkeypatch.setattr(command_writes, "READ_WAIT_SECONDS", 0.05)
    (tmp_path / "notes.md").write_text("one\n")
    (tmp_path / "gone.txt").write_text("secret\n")

    result = await _tool(tmp_path, _Shadow(tmp_path, answer_after=5.0)).execute(
        command="echo two >> notes.md && rm gone.txt"
    )

    assert "Exit code: 0" in result
    [write] = result.written
    assert (write.added, write.diff) == (None, None)
    assert [(Path(r.path).name, r.before) for r in result.removed] == [("gone.txt", None)]


async def test_measuring_that_runs_past_its_bound_still_returns_the_commands_output(tmp_path, monkeypatch):
    """Everything after the command is bounded short of the registry's ceiling,
    so a slow walk costs the record of the files and never the output."""
    monkeypatch.setattr(command_writes, "AFTER_WAIT_SECONDS", 0.2)
    real_take = snapshot.take
    walks: list[int] = []

    def _slow_second_walk(root: Any) -> Any:
        walks.append(1)
        if len(walks) == 2:
            import time

            time.sleep(1.0)
        return real_take(root)

    monkeypatch.setattr(snapshot, "take", _slow_second_walk)

    result = await _tool(tmp_path, _Shadow(tmp_path)).execute(command="echo done && echo x > made.txt")

    assert "done" in result and result.ok is True
    assert result.written == ()
