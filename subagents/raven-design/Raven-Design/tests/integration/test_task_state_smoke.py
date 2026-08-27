"""Full-process Task State smoke: real CLI + a local mock Responses endpoint.

Drives ``python -m raven agent -m`` as a subprocess against a stdlib HTTP
server speaking the OpenAI Responses SSE wire format. Only the network LLM is
mocked; config loading, provider construction, the Agent Loop, the
``update_task_state`` tool, the sidecar store, projection, and session
persistence are all the real implementation.

Covers the cross-module contract in one pass:

- the tool call mutates the sidecar (revision 0 -> 1 -> 2);
- the refreshed ``<task_state>`` snapshot is projected into the next LLM
  request after each mutation (delivery, not just storage);
- the persisted Session JSONL stays free of transient projections;
- an unfinished item appends the completion notice to the final reply.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sse(events: list[dict]) -> bytes:
    frames = [f"data: {json.dumps(event)}\n\n" for event in events]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def _completed(response_id: str) -> dict:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    }


def _tool_call_events(response_id: str, call_id: str, arguments: dict) -> list[dict]:
    payload = json.dumps(arguments)
    item = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": "update_task_state",
        "arguments": payload,
    }
    return [
        {"type": "response.output_item.added", "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": item["id"], "delta": payload},
        {"type": "response.output_item.done", "item": item},
        _completed(response_id),
    ]


_INITIALIZE = {
    "operations": [
        {
            "operation": "initialize",
            "state": {
                "goal": "Ship the Task State smoke",
                "requirements": ["Keep the sidecar consistent"],
                "items": [
                    {"title": "Initialize state", "status": "in_progress"},
                    {"title": "Verify projection", "status": "pending"},
                ],
            },
        }
    ]
}

_COMPLETE_FIRST = {"operations": [{"operation": "complete", "item_number": 1}]}


class _MockResponsesServer:
    """Scripted Responses endpoint.

    Agent-turn requests (those carrying the ``update_task_state`` tool) are
    answered from the script in order; auxiliary LLM calls the runtime makes
    around the turn (classifiers, summaries) get a bare text reply so the
    script indexes only the loop iterations under test.
    """

    def __init__(self):
        self.agent_requests: list[dict] = []
        self.scripts = [
            _tool_call_events("resp_1", "call_1", _INITIALIZE),
            _tool_call_events("resp_2", "call_2", _COMPLETE_FIRST),
            [
                {"type": "response.output_text.delta", "delta": "Stopping here for review."},
                _completed("resp_3"),
            ],
        ]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                request = json.loads(body)
                if "update_task_state" in json.dumps(request.get("tools") or []):
                    outer.agent_requests.append(request)
                    index = min(len(outer.agent_requests), len(outer.scripts)) - 1
                    events = outer.scripts[index]
                else:
                    events = [
                        {"type": "response.output_text.delta", "delta": "ok"},
                        _completed("resp_aux"),
                    ]
                data = _sse(events)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def mock_responses():
    server = _MockResponsesServer()
    try:
        yield server
    finally:
        server.stop()


def test_task_state_smoke_full_process(tmp_path: Path, mock_responses: _MockResponsesServer) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openai/task-smoke"}},
                "providers": {
                    "openai": {
                        "api_key": "sk-test",
                        "api_base": f"http://127.0.0.1:{mock_responses.port}/v1",
                        "protocol": "responses",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "raven",
            "agent",
            "--config",
            str(config_path),
            "--workspace",
            str(workspace),
            "--no-markdown",
            "-m",
            "Run the smoke task",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert len(mock_responses.agent_requests) == 3, [json.dumps(r)[:200] for r in mock_responses.agent_requests]

    first, second, third = (json.dumps(r) for r in mock_responses.agent_requests)
    assert "Revision: 0" in first and "Not initialized" in first
    assert "Revision: 1" in second, second[:2000]
    assert "Revision: 2" in third, third[:2000]

    sidecars = list((workspace / "task_states").glob("*.json"))
    assert len(sidecars) == 1, sidecars
    record = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert record["revision"] == 2
    statuses = [item["status"] for item in record["state"]["items"]]
    assert statuses == ["completed", "pending"]

    session_files = list((workspace / "sessions").rglob("*.jsonl"))
    assert session_files, "the turn must persist a session"
    for path in session_files:
        assert "<task_state>" not in path.read_text(encoding="utf-8"), path

    assert "This task is not complete" in result.stdout, result.stdout
