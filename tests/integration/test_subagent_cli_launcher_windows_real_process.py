"""On native Windows, a relative launcher starts in the row's own directory.

``CreateProcess`` resolves a program that names a directory in the *caller's*
own directory -- raven's -- and never in the directory the child is given, so a
row that named its launcher relatively (``./agent``) and set a ``cwd`` looked
for it beside raven and never started. The launch now resolves it against that
``cwd``.

This is the only test that really starts one: the unit tests assert the
resolved argv, which cannot tell a resolved path that starts from one that
does not. The two directories hold different programs under the same name, so
the answer names the one that ran.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.backends.cli_agent import CliAgentBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="Windows resolves a relative program in the caller's own directory",
    ),
]

_SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
_MARKER = "RAVEN-ROW-LAUNCHER"


def _copy(name: str, target: Path) -> Path:
    """A copy of a system program under another name, so the test really runs one."""
    source = _SYSTEM32 / name
    if not source.is_file():
        pytest.skip(f"{source} is not there")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


async def test_a_relative_launcher_starts_in_the_rows_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's copy runs, not the one that sits where this process runs."""
    row = tmp_path / "row"
    row.mkdir()
    _copy("cmd.exe", row / "agent.exe")
    # The same name in the directory this process runs in: a launch that
    # resolved the program the Windows way would start this one instead, and
    # the marker would not be in the answer.
    here = tmp_path / "here"
    here.mkdir()
    _copy("where.exe", here / "agent.exe")
    monkeypatch.chdir(here)

    backend = CliAgentBackend(
        name="rowagent",
        command=f"./agent /c echo {_MARKER}",
        cwd=str(row),
        registry=instances_mod.InstanceRegistry(path=tmp_path / "inst.json"),
    )

    reply = await backend.run("task", task_id="t1", workspace=tmp_path, executor=None)

    assert _MARKER in reply
