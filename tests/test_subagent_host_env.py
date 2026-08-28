"""The host identity survives the login-shell environment boundary."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest

from raven.agent.acp.client import AcpClient
from raven.agent.subagent.backends import env as backend_env


async def test_acp_child_inherits_custom_raven_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host_home = tmp_path / "custom-home"
    host_home.mkdir()
    observed = tmp_path / "observed.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, pathlib, time\n"
        "pathlib.Path(os.environ['OUTPUT']).write_text(os.environ.get('RAVEN_HOME', '<missing>'))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RAVEN_HOME", str(host_home))
    monkeypatch.setattr(backend_env, "login_shell_env", lambda: {"PATH": os.environ["PATH"], "OUTPUT": str(observed)})

    client = await AcpClient.launch(
        name="env-probe",
        command=f"{shlex.quote(sys.executable)} {shlex.quote(str(child))}",
    )
    try:
        for _ in range(100):
            if observed.exists():
                break
            await asyncio.sleep(0.01)
        assert observed.read_text(encoding="utf-8") == str(host_home)
    finally:
        await client.close()
