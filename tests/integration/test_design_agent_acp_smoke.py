"""Exercise the installed Raven-Design launcher through real ACP frames."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_design_launcher_completes_handshake_and_session_creation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "RAVEN_HOME": str(tmp_path / "raven-home"),
            "DESIGN_API_KEY": "dummy-e2e-key",
            "DESIGN_STATE_ROOT": str(tmp_path / "state"),
        }
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": str(project), "mcpServers": []},
        },
    ]

    result = subprocess.run(
        [
            sys.executable,
            "agents/raven-design/run.py",
            "--acp",
            "--config",
            "agents/raven-design/config.json",
        ],
        input="".join(json.dumps(request) + "\n" for request in requests).encode(),
        capture_output=True,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        timeout=60,
        check=False,
    )

    frames = [json.loads(line) for line in result.stdout.decode().splitlines() if line.strip()]
    answers = {frame["id"]: frame for frame in frames if "id" in frame}
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert answers[1]["result"]["protocolVersion"] == 1
    assert answers[2]["result"]["sessionId"].startswith("acp:")
    assert any(frame.get("method") == "session/update" for frame in frames)
