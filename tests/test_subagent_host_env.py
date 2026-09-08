"""What the host tells a child about itself survives the login-shell boundary.

Both variables here are captured from raven's own environment and re-applied over
a login shell's, which is the step that drops them if it is missed: the capture
runs `$SHELL -lic` from a minimal base precisely so raven's own variables do not
leak, so anything the host means the child to see has to be overlaid back.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest

from raven.acp_client.client import AcpClient
from raven.agent.subagent.backends import env as backend_env
from raven.agent.subagent.role import SUBAGENT_ENV_VAR, is_subagent_process


def _reporting_child(script: Path, variable: str) -> str:
    """Write a child that reports one variable, and return the command that runs it.

    The write is staged and renamed so that the observed path existing means its
    content is whole. ``write_text`` creates the file before it writes into it, so a
    parent polling on existence can read an empty string from a child that is about
    to report correctly -- which is what a loaded CI runner saw as
    ``assert '' == '/tmp/.../custom-home'`` while the same test passed every local
    run. ``os.replace`` is atomic within a filesystem, so there is no window to lose.

    The fallback is a word rather than an empty string for the same reason: a
    variable that genuinely did not arrive must fail loudly, not look like the race.
    """
    script.write_text(
        "import os, pathlib, time\n"
        "target = pathlib.Path(os.environ['OUTPUT'])\n"
        "partial = target.with_name(target.name + '.partial')\n"
        f"partial.write_text(os.environ.get({variable!r}, '<missing>'))\n"
        "os.replace(partial, target)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


async def test_acp_child_inherits_custom_raven_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host_home = tmp_path / "custom-home"
    host_home.mkdir()
    observed = tmp_path / "observed.txt"
    command = _reporting_child(tmp_path / "child.py", "RAVEN_HOME")
    monkeypatch.setenv("RAVEN_HOME", str(host_home))
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": os.environ["PATH"], "OUTPUT": str(observed)})

    client = await AcpClient.launch(name="env-probe", command=command)
    try:
        for _ in range(100):
            if observed.exists():
                break
            await asyncio.sleep(0.01)
        assert observed.read_text(encoding="utf-8") == str(host_home)
    finally:
        await client.close()


async def test_acp_child_is_told_it_serves_as_a_subagent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the gate in ``raven.agent.subagent.role`` never fires: a child
    ``raven acp`` builds a full registry because nothing ever tells it otherwise,
    which is how a DAG node's sub-agent came to hold ``spawn`` and delegate the
    node's whole task to a background receipt."""
    observed = tmp_path / "observed.txt"
    command = _reporting_child(tmp_path / "child.py", SUBAGENT_ENV_VAR)
    # The capture the launch overlays onto, standing in for the user's login
    # shell: it carries no role variable, so a passing read proves the overlay
    # rather than a value inherited from the test runner.
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": os.environ["PATH"], "OUTPUT": str(observed)})

    client = await AcpClient.launch(name="role-probe", command=command)
    try:
        for _ in range(100):
            if observed.exists():
                break
            await asyncio.sleep(0.01)
        arrived = observed.read_text(encoding="utf-8")
    finally:
        await client.close()

    # Judged by the reader rather than against a literal: what the host writes and
    # what the child accepts are two halves of one contract, and a test holding
    # the written form alone would pass while the child ignored it.
    monkeypatch.setenv(SUBAGENT_ENV_VAR, arrived)
    assert is_subagent_process(), f"the child was handed {arrived!r}, which it does not read as a sub-agent role"
