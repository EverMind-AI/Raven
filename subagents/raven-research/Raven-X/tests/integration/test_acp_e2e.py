"""End-to-end over the real process boundary: spawn ``raven acp`` and drive it.

Unit tests under ``tests/test_acp_*.py`` cover handler and spine correctness
in-process; this file guarantees the *process shell* doesn't regress -- the
CLI entry, ``claim_stdout``'s fd juggling, the stdin transport, the engine
factory building a real AgentLoop from a config file, and the one property
everything else stands on: **every byte the process puts on stdout is a
frame**.

No LLM is invoked: an empty prompt is answered ``end_turn`` without a turn,
and ``session/load`` replays from disk. The provider in the temp config is a
dummy that is never dialled.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from raven.acp import protocol
from raven.session.manager import SessionManager

REPO_ROOT = Path(__file__).resolve().parents[2]

STORED_KEY = "acp:20260825_000000_e2e"


def _write_config(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = {
        "providers": {"custom": {"apiBase": "http://127.0.0.1:9", "apiKey": "dummy-never-dialled"}},
        "agents": {
            "defaults": {
                "workspace": str(workspace),
                "model": "openai/dummy-model",
                "provider": "custom",
                "maxToolIterations": 2,
            }
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    manager = SessionManager(workspace)
    stored = manager.get_or_create(STORED_KEY)
    stored.add_message("user", "original question")
    stored.add_message("assistant", "clarifying question?")
    manager.save(stored)
    return path


def test_acp_subprocess_full_chain_and_stdout_purity(tmp_path):
    config = _write_config(tmp_path)
    requests = [
        protocol.request(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}),
        protocol.request(2, "session/new", {"cwd": str(tmp_path), "mcpServers": []}),
        protocol.request(3, "session/load", {"sessionId": STORED_KEY, "cwd": str(tmp_path), "mcpServers": []}),
        # An empty prompt is answered end_turn (plus one explanatory message
        # chunk) without running a turn, so the dummy provider is never dialled.
        protocol.request(4, "session/prompt", {"sessionId": STORED_KEY, "prompt": []}),
        protocol.request(5, "session/load", {"sessionId": "acp:never_existed", "mcpServers": []}),
    ]
    payload = b"".join(protocol.encode(r) for r in requests)

    proc = subprocess.run(
        [sys.executable, "-m", "raven", "acp", "--config", str(config)],
        input=payload,
        capture_output=True,
        timeout=150,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]

    # THE property: every stdout byte is consumable as newline-delimited JSON
    # frames. One stray log line here is a broken client in production.
    assert proc.stdout.endswith(b"\n"), proc.stdout[-200:]
    frames = [protocol.decode(line.decode("utf-8")) for line in proc.stdout.splitlines() if line.strip()]

    by_id = {f["id"]: f for f in frames if "id" in f and "method" not in f}
    init = by_id[1]["result"]
    assert init["agentInfo"]["name"] == "raven-x-research"
    caps = init["agentCapabilities"]
    assert caps["loadSession"] is True
    assert "resume" in caps["sessionCapabilities"]

    assert by_id[2]["result"]["sessionId"].startswith("acp:")
    assert by_id[3]["result"] == {}
    assert by_id[4]["result"] == {"stopReason": "end_turn"}
    assert by_id[5]["error"]["code"] == protocol.RESOURCE_NOT_FOUND

    updates = [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update" and f["params"]["sessionId"] == STORED_KEY
    ]
    # Two replayed by session/load, then the empty prompt's explanatory chunk.
    assert [u["sessionUpdate"] for u in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
        "agent_message_chunk",
    ]
    assert "no turn was run" in updates[2]["content"]["text"]
