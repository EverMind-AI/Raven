"""On native Windows, a Connect's checks pass and leave no directory behind.

Windows will not remove a directory that a live process has as its working
directory. The ping hands its agent a fresh one to launch in, and the handshake
hands it one as the session's, where an adapter may start a worker of its own
-- the Claude Agent SDK starts ``claude.exe`` there. Each was removed while the
agent still ran, so both checks failed with ``WinError 32`` for an agent that
had answered, and left the directory in the temp folder.

The agent here is the same shape: a batch file, an interpreter behind it, and a
worker it starts in the session's directory and waits for before answering.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from raven.acp_client.capabilities import verify_agent
from raven.agent.subagent.probe import ping_agent
from raven.config.schema import ThirdPartyAcpSubagentConfig

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to remove a live process's working directory"),
]

_AGENT = """\
import json, subprocess, sys
workers = []

def send(frame):
    sys.stdout.write(json.dumps(frame) + '\\n')
    sys.stdout.flush()

for line in sys.stdin:
    frame = json.loads(line)
    method, rid, params = frame.get('method'), frame.get('id'), frame.get('params') or {}
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': 1, 'agentCapabilities': {}, 'authMethods': []}})
    elif method == 'session/new':
        worker = subprocess.Popen(
            [sys.executable, '-c', 'import time; print(flush=True); time.sleep(120)'],
            cwd=params['cwd'], stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        worker.stdout.readline()
        workers.append(worker)
        send({'jsonrpc': '2.0', 'id': rid, 'result': {'sessionId': f's{len(workers)}'}})
    elif method == 'session/prompt':
        chunk = {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'PONG'}}
        send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': params['sessionId'], 'update': chunk}})
        send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
    elif rid is not None:
        send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'method not found'}})
"""


async def test_a_connect_passes_and_leaves_no_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    (tmp_path / "agent.py").write_text(_AGENT, encoding="utf-8")
    launcher = tmp_path / "agent.cmd"
    launcher.write_bytes(f'@echo off\r\n"{sys.executable}" "%~dp0agent.py" %*\r\n'.encode())
    cfg = ThirdPartyAcpSubagentConfig(
        name="windows-chain", command=subprocess.list2cmdline([str(launcher)]), ready_timeout_ms=30000
    )

    pinged = await ping_agent(cfg)
    snapshot = await verify_agent(cfg)

    assert pinged.ok, pinged.detail
    assert snapshot.status == "ready", snapshot.detail
    assert list(root.iterdir()) == []
