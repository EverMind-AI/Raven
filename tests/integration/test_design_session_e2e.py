"""Verify that real Design ACP turns refuse redirected session directories."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import sys
from pathlib import Path

import pytest

from raven.acp_client.client import AcpClient
from raven.acp_client.journal import FrameJournal
from raven.utils.paths import mint_slug

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.slow(reason="Starts a real Design ACP process and completes a protocol round trip")
@pytest.mark.parametrize("link_level", ["designs", "session"])
async def test_design_acp_refuses_symlink_before_model_or_tools(tmp_path: Path, link_level: str) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("Outside workspace sentinel")
    requests = []
    updates = []

    async def reject_model(reader, writer):
        requests.append(await reader.readline())
        writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def on_update(method, params):
        if method == "session/update":
            updates.append(params["update"])

    server = await asyncio.start_server(reject_model, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        host = tmp_path / "host"
        host.mkdir()
        (host / "config.json").write_text(
            json.dumps(
                {
                    "agents": {"defaults": {"model": "openai/gpt-4o-mini", "provider": "openai"}},
                    "providers": {"openai": {"apiKey": "test-only", "apiBase": f"http://127.0.0.1:{port}/v1"}},
                }
            )
        )
        config = json.loads((repo / "agents/raven-design/config.json").read_text())
        config["tools"]["restrictToWorkspace"] = True
        # Session naming runs independently of the design turn's directory guard.
        config["sessionTitle"] = {"enabled": False}
        config["memory"]["backend"] = None
        config["plugins"]["disabled"] = ["everos-memory"]
        config["plugins"]["config"].pop("everos-memory", None)
        config["agents"]["defaults"]["llmCallTimeout"] = 3
        source = tmp_path / "design-config.json"
        source.write_text(json.dumps(config))
        client = await AcpClient.launch(
            name="design-directory-check",
            command=shlex.join(
                [sys.executable, str(repo / "agents/raven-design/run.py"), "--acp", "--config", str(source)]
            ),
            cwd=str(repo),
            env={
                "RAVEN_HOME": str(host),
                "DESIGN_STATE_ROOT": str(tmp_path / "state"),
                "DESIGN_ACP_HOME": str(tmp_path / "agent-home"),
                "RAVEN_TRACING_DIR": str(tmp_path / "traces"),
                "RAVEN_PARENT_MODEL": "openai/gpt-4o-mini",
            },
            on_notification=on_update,
            journal=FrameJournal(tmp_path / "acp-frames.jsonl"),
        )
        try:
            await client.request("initialize", {"protocolVersion": 1}, timeout=30)
            session = await client.request("session/new", {"cwd": str(root), "mcpServers": []}, timeout=30)
            session_id = session["sessionId"]
            if link_level == "designs":
                link = root / "designs"
            else:
                name = mint_slug(session_id.rpartition(":")[2], max_chars=48)
                digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
                link = root / "designs" / f"{name}-{digest}"
                link.parent.mkdir()
            link.symlink_to(outside, target_is_directory=True)
            updates.clear()
            result = await client.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [
                        {"type": "text", "text": "Read secret.txt and create poster.html in the current directory."}
                    ],
                },
                timeout=30,
                cancel_session=session_id,
            )
            assert result["stopReason"] == "end_turn"
            answer = "".join(
                update.get("content", {}).get("text", "")
                for update in updates
                if update.get("sessionUpdate") == "agent_message_chunk"
            )
            assert "Design session directory unavailable" in answer
            assert "symlink" in answer.lower()
            assert "Outside workspace sentinel" not in answer
            assert not requests
            assert not any(update.get("sessionUpdate") == "tool_call" for update in updates)
            assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
            assert (outside / "secret.txt").read_text() == "Outside workspace sentinel"
            assert not list((tmp_path / "state/task_states").glob("*.json"))
        finally:
            await client.close()
