"""The ExecTool workspace guard.

Pins the null-device exemption: under ``restrict_to_workspace`` the guard walks
every absolute path a command names, and ``2>/dev/null`` names a path only to
the extraction regex -- muting a stream is not an escape from the workspace.
The exemption was lost once already in a vendored sync, invisibly, because no
test held it.
"""

from __future__ import annotations

from raven.agent.tools.shell import ExecTool


def _tool(tmp_path) -> ExecTool:
    return ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)


def test_muting_a_stream_is_not_a_workspace_escape(tmp_path):
    tool = _tool(tmp_path)
    assert tool._check_workspace_restriction("grep -r needle . 2>/dev/null", str(tmp_path)) is None


def test_the_whole_device_family_is_exempt(tmp_path):
    tool = _tool(tmp_path)
    for cmd in (
        "cat /dev/stdin",
        "head -c 16 /dev/urandom",
        "dd if=/dev/zero of=out.bin bs=1 count=1",
        "echo hi > /dev/tty",
    ):
        assert tool._check_workspace_restriction(cmd, str(tmp_path)) is None, cmd


def test_a_real_absolute_path_outside_the_workspace_is_still_blocked(tmp_path):
    tool = _tool(tmp_path)
    error = tool._check_workspace_restriction("cat /etc/passwd 2>/dev/null", str(tmp_path))
    assert error is not None and "outside working dir" in error


def test_a_device_path_under_dev_but_not_in_the_family_is_blocked(tmp_path):
    tool = _tool(tmp_path)
    error = tool._check_workspace_restriction("cat /dev/sda", str(tmp_path))
    assert error is not None and "outside working dir" in error
