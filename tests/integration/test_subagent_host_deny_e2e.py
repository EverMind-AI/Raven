"""A command the host denies stays denied once it is handed to Raven-Code.

Everything here is the shipped path except the model: this process is the
host, dispatching through raven's own ACP lane (``AcpAgentBackend``, the pool,
the approver), and the sub-agent is ``agents/raven-code/run.py --acp`` execing
``python -m raven acp``. The model is a loopback OpenAI-compatible stub that
has the sub-agent call ``exec`` with a curl aimed at a loopback sink, so a
sink hit is proof the command ran.

The control run, with no deny rule on the host, is what makes the denied run
evidence: it shows the rig reaches the command at all.

Memory is switched off in a copy of the product config, so a run cannot reach
an EverOS server on the machine; the copy carries no permissions block, as the
shipped file does not.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "agents" / "raven-code"


class _Stub:
    """The model and the sink, on one loopback port."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.command = ""
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def _send(self, body: bytes, ctype: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                stub.hits.append(self.path)
                self._send(b"SINK-HIT\n", "text/plain")

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                message, finish = stub.answer(body)
                if body.get("stream"):
                    frames = stub.chunks(message, finish)
                    payload = "".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n"
                    self._send(payload.encode(), "text/event-stream")
                    return
                reply = {
                    "id": "stub",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "stub-model",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
                self._send(json.dumps(reply).encode(), "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        tools = [((t or {}).get("function") or {}).get("name") for t in body.get("tools") or []]
        results = [m for m in body.get("messages") or [] if m.get("role") == "tool"]
        if "exec" in tools and not results:
            call = {
                "id": "call_1",
                "type": "function",
                "function": {"name": "exec", "arguments": json.dumps({"command": self.command})},
            }
            return {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        if results:
            return {"role": "assistant", "content": f"RESULT: {str(results[-1].get('content'))[:400]}"}, "stop"
        return {"role": "assistant", "content": "ok"}, "stop"

    @staticmethod
    def chunks(message: dict[str, Any], finish: str) -> list[dict[str, Any]]:
        common = {"id": "stub", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub-model"}
        calls = message.get("tool_calls")
        if calls:
            fn = calls[0]["function"]
            deltas = [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": calls[0]["id"],
                            "type": "function",
                            "function": {"name": fn["name"], "arguments": ""},
                        }
                    ],
                },
                {"tool_calls": [{"index": 0, "function": {"arguments": fn["arguments"]}}]},
            ]
        else:
            deltas = [{"role": "assistant", "content": message["content"]}]
        frames = [{**common, "choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
        frames.append({**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        frames.append(
            {**common, "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        )
        return frames


@pytest.fixture()
def stub():
    server = _Stub()
    yield server
    server.server.shutdown()


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """A host home and a HOME of the test's own, with no provider key inherited."""
    from raven.acp_client import permissions
    from raven.providers.registry import PROVIDERS

    home = tmp_path / "host-home"
    home.mkdir()
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("USERPROFILE", str(user))
    for spec in PROVIDERS:
        if getattr(spec, "env_key", None):
            monkeypatch.setenv(spec.env_key, "")
    permissions._host_policy.cache_clear()
    yield home
    permissions._host_policy.cache_clear()


async def _dispatch(home: Path, stub: _Stub, permissions: dict[str, Any] | None) -> str:
    from raven.acp_client.acp_agent import AcpAgentBackend
    from raven.acp_client.pool import close_pool

    stub.command = f"curl -s http://127.0.0.1:{stub.port}/hit"
    host: dict[str, Any] = {
        "providers": {
            "custom": {"apiKey": "sk-stub", "apiBase": f"http://127.0.0.1:{stub.port}/v1", "models": ["stub-model"]}
        },
        "agents": {"defaults": {"provider": "custom", "model": "stub-model"}},
    }
    if permissions is not None:
        host["permissions"] = permissions
    (home / "config.json").write_text(json.dumps(host), encoding="utf-8")

    product = json.loads((LAUNCHER / "config.json").read_text(encoding="utf-8"))
    assert "permissions" not in product
    product["memory"] = {"backend": None}
    product.get("plugins", {}).get("config", {}).pop("everos-memory", None)
    product_copy = home.parent / "raven-code-config.json"
    product_copy.write_text(json.dumps(product), encoding="utf-8")
    checkout = home.parent / "checkout"
    checkout.mkdir(exist_ok=True)

    backend = AcpAgentBackend(
        name="Raven-Code",
        command=f"{sys.executable} {LAUNCHER / 'run.py'} --acp --config {product_copy}",
        cwd=str(LAUNCHER),
        env={"CODE_API_KEY": ""},
        ready_timeout_ms=120000,
    )
    try:
        return await backend.run(f"Run exactly: {stub.command}", task_id="deny-e2e", workspace=checkout, executor=None)
    finally:
        await close_pool()


async def test_the_control_run_reaches_the_command(isolated, stub):
    reply = await _dispatch(isolated, stub, None)
    assert stub.hits == ["/hit"], reply


async def test_a_host_deny_rule_holds_inside_raven_code(isolated, stub):
    reply = await _dispatch(isolated, stub, {"mode": "ask", "tools": {"exec": {"curl *": "deny"}}})
    assert stub.hits == [], reply
    assert "deny rule" in reply, reply
