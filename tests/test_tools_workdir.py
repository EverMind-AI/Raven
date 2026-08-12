"""Path tools resolve against the working directory bound for the turn."""

from pathlib import Path

import pytest

from raven.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from raven.agent.tools.shell import ExecTool
from raven.agent.workdir import bind


@pytest.mark.asyncio
async def test_write_file_lands_in_the_bound_workdir(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback"
    session = tmp_path / "session"
    fallback.mkdir()
    session.mkdir()
    tool = WriteFileTool(workspace=fallback)

    with bind(session):
        await tool.execute(path="out.txt", content="hello")

    assert (session / "out.txt").read_text(encoding="utf-8") == "hello"
    assert not (fallback / "out.txt").exists()


@pytest.mark.asyncio
async def test_write_file_falls_back_without_a_binding(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    tool = WriteFileTool(workspace=fallback)

    await tool.execute(path="out.txt", content="hello")

    assert (fallback / "out.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_read_file_resolves_relative_paths_against_the_binding(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "note.txt").write_text("from session", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path / "fallback")

    with bind(session):
        result = await tool.execute(path="note.txt")

    assert "from session" in str(result)


@pytest.mark.asyncio
async def test_list_dir_defaults_to_the_binding(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "marker.txt").write_text("x", encoding="utf-8")
    tool = ListDirTool(workspace=tmp_path / "fallback")

    with bind(session):
        result = await tool.execute(path=".")

    assert "marker.txt" in str(result)


@pytest.mark.asyncio
async def test_exec_runs_in_the_bound_workdir(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    tool = ExecTool(working_dir=str(tmp_path / "fallback"))

    with bind(session):
        result = await tool.execute(command="pwd")

    assert str(session.resolve()) in str(result)


@pytest.mark.asyncio
async def test_exec_per_call_working_dir_still_wins(tmp_path: Path) -> None:
    """The explicit argument outranks the binding, as it did the constructor."""
    session = tmp_path / "session"
    explicit = tmp_path / "explicit"
    session.mkdir()
    explicit.mkdir()
    tool = ExecTool()

    with bind(session):
        result = await tool.execute(command="pwd", working_dir=str(explicit))

    assert str(explicit.resolve()) in str(result)


@pytest.mark.asyncio
async def test_fence_allows_the_session_workdir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    session = tmp_path / "session"
    home.mkdir()
    session.mkdir()
    tool = WriteFileTool(workspace=session, allowed_dirs=(session, home))

    with bind(session):
        await tool.execute(path=str(session / "a.txt"), content="x")

    assert (session / "a.txt").exists()


@pytest.mark.asyncio
async def test_fence_allows_agent_home(tmp_path: Path) -> None:
    """The agent must still reach the memory paths its system prompt names."""
    home = tmp_path / "home"
    session = tmp_path / "session"
    (home / "user_memory").mkdir(parents=True)
    session.mkdir()
    tool = ReadFileTool(workspace=session, allowed_dirs=(session, home))
    (home / "user_memory" / "profile.md").write_text("me", encoding="utf-8")

    with bind(session):
        result = await tool.execute(path=str(home / "user_memory" / "profile.md"))

    assert "me" in str(result)


@pytest.mark.asyncio
async def test_fence_rejects_outside_both_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    session = tmp_path / "session"
    outside = tmp_path / "outside"
    for d in (home, session, outside):
        d.mkdir()
    tool = WriteFileTool(workspace=session, allowed_dirs=(session, home))

    with bind(session):
        result = await tool.execute(path=str(outside / "a.txt"), content="x")

    assert "outside allowed" in str(result)
    assert not (outside / "a.txt").exists()


@pytest.mark.asyncio
async def test_fence_admits_a_bound_session_dir_not_under_the_static_allowed_dirs(
    tmp_path: Path,
) -> None:
    """Mirrors the real wiring in AgentLoop._register_default_tools: the tool is
    constructed once, before any turn, with only agent home in ``allowed_dirs``
    (``self.workspace``). A later-bound session workdir that lives outside home
    (e.g. an explicit override or a launch-dir policy) must still be reachable,
    since ``workdir.bind`` is how each turn's session directory reaches the tool.
    """
    home = tmp_path / "home"
    session = tmp_path / "session"
    home.mkdir()
    session.mkdir()
    tool = WriteFileTool(workspace=home, allowed_dirs=(home,))

    with bind(session):
        result = await tool.execute(path=str(session / "a.txt"), content="x")

    assert (session / "a.txt").exists(), result


@pytest.mark.asyncio
async def test_subagent_style_tool_ignores_the_ambient_binding(tmp_path: Path) -> None:
    """A tool built with follow_binding=False (the sub-agent backend's shape)
    fences on the directory captured for its run, never the ambient turn
    binding: a sub-agent run is a background asyncio task that can outlive the
    turn that spawned it, so the binding it sees by the time it executes may
    belong to a different, later turn entirely.
    """
    captured = tmp_path / "captured"
    other_session = tmp_path / "other_session"
    captured.mkdir()
    other_session.mkdir()
    tool = WriteFileTool(workspace=captured, allowed_dirs=(captured,), follow_binding=False)

    with bind(other_session):
        result = await tool.execute(path=str(other_session / "a.txt"), content="x")

    assert "outside allowed" in str(result)
    assert not (other_session / "a.txt").exists()


@pytest.mark.asyncio
async def test_subagent_style_tool_still_resolves_relative_paths_against_its_capture(
    tmp_path: Path,
) -> None:
    """The anchor for relative paths must also come from the capture, not the
    ambient binding, when follow_binding=False."""
    captured = tmp_path / "captured"
    other_session = tmp_path / "other_session"
    captured.mkdir()
    other_session.mkdir()
    tool = WriteFileTool(workspace=captured, allowed_dirs=(captured,), follow_binding=False)

    with bind(other_session):
        await tool.execute(path="a.txt", content="x")

    assert (captured / "a.txt").exists()
    assert not (other_session / "a.txt").exists()


@pytest.mark.asyncio
async def test_subagent_style_exec_ignores_the_ambient_binding(tmp_path: Path) -> None:
    """The shell counterpart of test_subagent_style_tool_ignores_the_ambient_binding:
    an ExecTool built with follow_binding=False (the sub-agent backend's shape)
    must run in the directory captured for its run, never the ambient turn
    binding, or it disagrees with its own fs tools about which directory the
    sub-agent is in.
    """
    captured = tmp_path / "captured"
    other_session = tmp_path / "other_session"
    captured.mkdir()
    other_session.mkdir()
    tool = ExecTool(working_dir=str(captured), follow_binding=False)

    with bind(other_session):
        result = await tool.execute(command="pwd")

    assert str(captured.resolve()) in str(result)
    assert str(other_session.resolve()) not in str(result)
