"""The workspace fence: what it lets through, and what it must not.

Every case here is one a live headless deck run produced. The fence refuses by
ending the tool call, so a refusal the caller cannot repair ends the run.
"""

from __future__ import annotations

from pathlib import Path

from raven.agent.tools.shell import ExecTool


def _tool(workspace: Path) -> ExecTool:
    return ExecTool(working_dir=str(workspace), restrict_to_workspace=True)


def test_muting_a_stream_is_not_leaving_the_workspace(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    assert tool._check_workspace_restriction(f"ls {tmp_path}/build 2>/dev/null", str(tmp_path)) is None


def test_a_chosen_working_dir_does_not_shrink_the_fence(tmp_path: Path) -> None:
    """A run that cd'd into its build directory could not list the figures beside it."""
    build = tmp_path / "deck" / "build"
    figures = tmp_path / "deck" / "ingest" / "figures"
    tool = _tool(tmp_path)
    assert tool._check_workspace_restriction(f"ls {figures}", str(build)) is None


def test_a_chosen_working_dir_cannot_widen_the_fence(tmp_path: Path) -> None:
    """`working_dir="/"` made every absolute path a child of the working directory."""
    tool = _tool(tmp_path)
    refusal = tool._check_workspace_restriction("cat /etc/shadow", "/")
    assert refusal is not None
    assert "working_dir" in refusal
    assert str(tmp_path) in refusal


def test_a_path_outside_the_workspace_is_still_refused(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    refusal = tool._check_workspace_restriction("ls /etc/passwd", str(tmp_path))
    assert refusal is not None
    assert "/etc/passwd" in refusal


def test_the_refusal_names_the_path_that_caused_it(tmp_path: Path) -> None:
    """One refusal covers the whole command, so it has to say which half offended."""
    tool = _tool(tmp_path)
    refusal = tool._check_workspace_restriction(f"ls {tmp_path}/figures && ls /tmp", str(tmp_path))
    assert refusal is not None
    assert "/tmp is outside" in refusal


def test_without_a_declared_workspace_the_call_site_bounds_it(tmp_path: Path) -> None:
    tool = ExecTool(restrict_to_workspace=True)
    assert tool._check_workspace_restriction(f"ls {tmp_path}", str(tmp_path)) is None
    assert tool._check_workspace_restriction("ls /etc", str(tmp_path)) is not None
