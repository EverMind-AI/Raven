"""Unit tests for the deep_research tool (MiroThinker, SSE).

Hermetic: an httpx.MockTransport feeds a canned SSE chunk stream, so no real
network or key is touched. The chunk shape mirrors a real MiroThinker stream
(delta.content pieces, a final chunk carrying finish_reason + usage; the
content is a self-contained answer with a References section).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from raven.agent.tools import deep_research as dr_mod
from raven.agent.tools.deep_research import DeepResearchTool
from raven.config.schema import DeepResearchToolConfig

ANSWER = "## Answer\n\nLangChain leads [1].\n\n### References\n[1] LangChain. <https://github.com/langchain-ai/langchain>\n"
USAGE = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


def _sse(content: str, finish_reason: str = "stop", usage: dict | None = USAGE) -> bytes:
    """Build an SSE body: content split into delta chunks + a final finish chunk."""
    lines = []

    def emit(obj: dict) -> None:
        lines.append(f"data: {json.dumps(obj)}\n\n")

    emit({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    lines.append(": heartbeat\n\n")  # keep-alive comment the server sends on idle; must be skipped
    for piece in (content[i : i + 20] for i in range(0, len(content), 20)):
        emit({"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
    final: dict = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    if usage is not None:
        final["usage"] = usage
    emit(final)
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _patch(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(dr_mod.httpx, "AsyncClient", factory)


@pytest.fixture
def tool(tmp_path: Path) -> DeepResearchTool:
    return DeepResearchTool(DeepResearchToolConfig(api_key="sk-test"), workspace=tmp_path)


async def test_ok_accumulates_full_content(tool: DeepResearchTool, tmp_path: Path, monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(200, content=_sse(ANSWER)))
    result = json.loads(await tool.execute(query="who leads"))

    assert result["status"] == "ok"
    # Content is the full answer, reconstructed verbatim from the chunk stream.
    assert result["content"] == ANSWER
    assert result["usage"]["total_tokens"] == 150
    # No summary / no sources fields in the contract.
    assert set(result) == {"status", "content", "report_ref", "usage"}

    # Report persisted to disk; ref points at it; full content preserved there.
    ref = Path(result["report_ref"])
    assert ref.is_file() and ref.parent == tmp_path / "deep_research"
    assert ANSWER in ref.read_text()


async def test_cancelled_finish_is_timeout(tool: DeepResearchTool, monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(200, content=_sse("partial", finish_reason="cancelled")))
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "timeout"


async def test_empty_stream_no_report(tool: DeepResearchTool, monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(200, content=_sse("", usage=None)))
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "ok"
    assert result["report_ref"] is None
    assert result["content"] == "(empty response)"


async def test_http_error_reported_not_raised(tool: DeepResearchTool, monkeypatch):
    body = {"error": {"code": "insufficient_balance", "message": "out of credits", "type": "billing"}}
    _patch(monkeypatch, lambda req: httpx.Response(402, json=body))
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "error"
    assert "out of credits" in result["content"]
    assert result["report_ref"] is None


async def test_timeout_reported_not_raised(tool: DeepResearchTool, monkeypatch):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    _patch(monkeypatch, handler)
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "timeout"


async def test_finish_none_is_error(tool: DeepResearchTool, monkeypatch):
    # Stream ends (via [DONE]) without ever sending a finish_reason chunk — abnormal.
    body = (
        b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        b"data: [DONE]\n\n"
    )
    _patch(monkeypatch, lambda req: httpx.Response(200, content=body))
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "error"


async def test_http_error_non_json_body(tool: DeepResearchTool, monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(503, text="upstream boom"))
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "error"
    assert "boom" in result["content"] or "503" in result["content"]


async def test_no_key_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    tool = DeepResearchTool(DeepResearchToolConfig(), workspace=tmp_path)
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "error"
    assert "no API key" in result["content"]


async def test_stream_callback_delivers_answer_and_returns_receipt(tool: DeepResearchTool, monkeypatch):
    # A streaming surface wires a callback: progress streams, the answer is
    # delivered via the callback, and execute() returns a compact receipt (no
    # full content) so the model cannot re-emit/rewrite it.
    events: list[tuple[str, str]] = []

    async def cb(kind: str, text: str) -> None:
        events.append((kind, text))

    tool.set_stream_callback(cb)
    # Step churn: thinking is skipped; consecutive same-action collapses; only a
    # change to a new action type emits. Here -> search, fetch, search = 3 lines.
    def step(kind: str) -> bytes:
        return f'data: {{"choices":[{{"index":0,"delta":{{"reasoning_steps":[{{"type":"{kind}"}}]}},"finish_reason":null}}]}}\n\n'.encode()

    body = b"".join(
        step(k) for k in ("thinking", "web_search", "web_search", "thinking", "fetch_url_content", "web_search")
    )
    body += _sse(ANSWER)  # role + heartbeat + content deltas + finish + [DONE]
    _patch(monkeypatch, lambda req: httpx.Response(200, content=body))

    result = json.loads(await tool.execute(query="q"))

    # Compact receipt: no full content, but flags delivery + points at the report.
    assert result["status"] == "ok"
    assert "content" not in result
    assert result["delivered"] is True
    assert result["report_ref"]

    progress = [t for k, t in events if k == "progress"]
    assert progress == ["searching the web...", "reading a page...", "searching the web..."]
    assert "thinking..." not in progress  # thinking is not surfaced
    # The finished answer was delivered verbatim via the callback.
    answer = next(t for k, t in events if k == "answer")
    assert answer == ANSWER


async def test_no_callback_returns_full_content(tool: DeepResearchTool, monkeypatch):
    # Without a wired callback (e.g. a channel), execute() falls back to the full
    # structured result so the answer is not lost.
    _patch(monkeypatch, lambda req: httpx.Response(200, content=_sse(ANSWER)))
    result = json.loads(await tool.execute(query="q"))
    assert result["content"] == ANSWER
    assert "delivered" not in result


def test_is_configured_gate(monkeypatch):
    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    assert DeepResearchTool.is_configured(DeepResearchToolConfig()) is False
    assert DeepResearchTool.is_configured(DeepResearchToolConfig(api_key="sk")) is True
    monkeypatch.setenv("MIROTHINKER_API_KEY", "sk-env")
    assert DeepResearchTool.is_configured(DeepResearchToolConfig()) is True
