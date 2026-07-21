"""Unit tests for the deep_research tool (MiroThinker, SSE).

Hermetic: an httpx.MockTransport feeds a canned SSE chunk stream, so no real
network or key is touched. The chunk shape mirrors a real MiroThinker stream
(delta.content pieces, a final chunk carrying finish_reason + usage; the
content is a self-contained answer with a References section).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools import deep_research as dr_mod
from raven.agent.tools.deep_research import (
    DeepResearchManager,
    DeepResearchOfferTool,
    DeepResearchTool,
    _extract_output_text,
    deep_research_mode,
)
from raven.config.schema import DeepResearchToolConfig

ANSWER = (
    "## Answer\n\nLangChain leads [1].\n\n### References\n[1] LangChain. <https://github.com/langchain-ai/langchain>\n"
)
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
    body = b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\ndata: [DONE]\n\n'
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


# ── async (channel) transport: Responses API + background delivery (2b) ──


def _responses_handler(answer: str = ANSWER, poll_status: str = "completed"):
    """MockTransport handler for the Responses API: POST submit -> 202 with id;
    GET poll -> the given status (completed carries a multi-item output)."""

    def handler(req):
        if req.method == "POST":
            return httpx.Response(202, json={"id": "resp_x", "status": "in_progress", "output": []})
        if poll_status == "completed":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "content": []},
                        {"type": "message", "content": [{"type": "output_text", "text": answer}]},
                    ],
                },
            )
        return httpx.Response(200, json={"status": poll_status, "output": []})

    return handler


def _async_tool(tmp_path: Path, submit) -> DeepResearchTool:
    cfg = DeepResearchToolConfig(api_key="sk-test")
    mgr = DeepResearchManager(cfg, workspace=tmp_path)
    if submit is not None:
        mgr.set_submit(submit)
    tool = DeepResearchTool(cfg, workspace=tmp_path, manager=mgr)
    tool.set_context("weixin", "chat1", "weixin:chat1")
    tool._mgr = mgr  # test handle
    return tool


async def test_async_mode_returns_ack_then_delivers_verbatim(tmp_path: Path, monkeypatch):
    captured: list = []
    tool = _async_tool(tmp_path, submit=lambda req: captured.append(req))
    _patch(monkeypatch, _responses_handler())

    ack = json.loads(await tool.execute(query="q"))
    assert ack["status"] == "started"  # immediate ack, does not block for the research
    assert "content" not in ack  # no answer inline — it is delivered later

    await tool._mgr._active["weixin:chat1"]  # let the background poller finish

    assert len(captured) == 1
    req = captured[0]
    assert req.deliver_text == ANSWER  # delivered verbatim, not rewritten
    assert req.conversation == "weixin:chat1"
    assert req.source.channel == "weixin" and req.source.chat_id == "chat1"
    assert (tmp_path / "deep_research").is_dir()  # report also saved to disk


async def test_async_guard_one_research_per_conversation(tmp_path: Path):
    tool = _async_tool(tmp_path, submit=lambda req: None)

    async def _never() -> None:
        await asyncio.Event().wait()

    t = asyncio.create_task(_never())
    tool._mgr._active["weixin:chat1"] = t  # simulate an in-flight research
    try:
        ack = json.loads(await tool.execute(query="q"))
        assert ack["status"] == "busy"  # refused, no second task
    finally:
        t.cancel()


async def test_async_failure_releases_guard_and_delivers_error(tmp_path: Path, monkeypatch):
    captured: list = []
    tool = _async_tool(tmp_path, submit=lambda req: captured.append(req))
    _patch(monkeypatch, _responses_handler(poll_status="failed"))

    ack = json.loads(await tool.execute(query="q"))
    assert ack["status"] == "started"
    await tool._mgr._active["weixin:chat1"]
    await asyncio.sleep(0)  # let the done-callback run

    assert len(captured) == 1 and "failed" in captured[0].deliver_text.lower()
    assert "weixin:chat1" not in tool._mgr._active  # guard released despite the failure


async def test_async_not_wired_falls_back_to_sse(tmp_path: Path, monkeypatch):
    # Manager present but submit not wired (non-gateway) -> can_deliver False ->
    # the tool takes the synchronous SSE path, never the async ack.
    tool = _async_tool(tmp_path, submit=None)
    tool.set_context("cli", "direct", "cli:direct")
    _patch(monkeypatch, lambda req: httpx.Response(200, content=_sse(ANSWER)))
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "ok" and result["content"] == ANSWER


async def test_async_report_write_failure_still_delivers_answer(tmp_path: Path, monkeypatch):
    # A disk/permission failure saving the local report must NOT discard the real
    # answer we already have — deliver it anyway, don't report a false failure.
    captured: list = []
    tool = _async_tool(tmp_path, submit=lambda req: captured.append(req))
    _patch(monkeypatch, _responses_handler())

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(dr_mod, "_write_report_file", _boom)

    await tool.execute(query="q")
    await tool._mgr._active["weixin:chat1"]

    assert len(captured) == 1
    assert captured[0].deliver_text == ANSWER  # answer delivered, not a "failed" message


# ── offer stand-in on an unconfigured deploy + registration mode ──


class _ScriptBroker:
    """Fake QuestionBroker: returns scripted answers in order, recording the
    prompts/choices it was shown (so tests can assert what the user saw)."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.choices_seen: list[list[str]] = []
        self.prompts_seen: list[str] = []

    async def await_question(self, cid, *, prompt, choices, default="", timeout_s=600.0):
        self.prompts_seen.append(prompt)
        self.choices_seen.append(choices)
        return self._script.pop(0)


def _offer(broker=None, *, cid: str = "cli:direct") -> DeepResearchOfferTool:
    tool = DeepResearchOfferTool()
    if broker is not None:
        tool.set_broker(broker)
        tool.set_context("cli", "direct", cid)
    return tool


async def test_offer_no_broker_falls_back_to_regular():
    tool = DeepResearchOfferTool()
    tool.set_context("cli", "direct", "cli:direct")  # cid set, but no broker
    assert await tool.execute(query="q") == dr_mod._USE_REGULAR


async def test_offer_no_cid_falls_back_to_regular():
    tool = DeepResearchOfferTool()
    tool.set_broker(_ScriptBroker([dr_mod._MODE_DEEP]))  # broker set, but no context
    assert await tool.execute(query="q") == dr_mod._USE_REGULAR


async def test_offer_regular_choice_uses_regular():
    broker = _ScriptBroker([dr_mod._MODE_REGULAR])
    assert await _offer(broker).execute(query="q") == dr_mod._USE_REGULAR
    assert len(broker.choices_seen) == 1


async def test_offer_freeform_answer_is_regular():
    broker = _ScriptBroker(["something the user typed"])
    assert await _offer(broker).execute(query="q") == dr_mod._USE_REGULAR


async def test_offer_deep_then_regular_meanwhile():
    # Deep on an unconfigured deploy never runs the engine. It shows setup steps
    # via a 2nd broker prompt (seen by the user directly, not model-relayed) and
    # asks about a regular search meanwhile; "yes" -> regular.
    broker = _ScriptBroker([dr_mod._MODE_DEEP, dr_mod._MEANWHILE_REGULAR])
    assert await _offer(broker).execute(query="q") == dr_mod._USE_REGULAR
    assert len(broker.choices_seen) == 2  # deep/regular, then meanwhile yes/no
    assert "raven deep-research enable" in broker.prompts_seen[1]  # command shown to the user


async def test_offer_deep_then_wait_stops():
    # "No, I'll set it up" -> tell the model to stop, not answer with regular.
    broker = _ScriptBroker([dr_mod._MODE_DEEP, dr_mod._MEANWHILE_WAIT])
    assert await _offer(broker).execute(query="q") == dr_mod._SETUP_STOP


async def test_offer_deep_then_freeform_meanwhile_is_regular():
    # Only an explicit "set it up" waits; a free-form answer to the meanwhile
    # question falls to regular (symmetric with the first ask), never stuck waiting.
    broker = _ScriptBroker([dr_mod._MODE_DEEP, "uh, whatever"])
    assert await _offer(broker).execute(query="q") == dr_mod._USE_REGULAR


# ── working tool also asks deep-vs-regular (product: deep is slow + paid) ──


async def test_real_tool_regular_choice_skips_engine(tmp_path: Path, monkeypatch):
    # Even configured, picking regular must NOT hit the engine.
    def _boom(*_a, **_k):
        raise AssertionError("engine must not run when the user picks regular")

    monkeypatch.setattr(dr_mod.httpx, "AsyncClient", _boom)
    tool = DeepResearchTool(DeepResearchToolConfig(api_key="sk-test"), workspace=tmp_path)
    tool.set_broker(_ScriptBroker([dr_mod._MODE_REGULAR]))
    tool.set_context("cli", "direct", "cli:direct")
    assert await tool.execute(query="q") == dr_mod._USE_REGULAR


async def test_real_tool_deep_choice_runs_engine(tmp_path: Path, monkeypatch):
    _patch(monkeypatch, lambda req: httpx.Response(200, content=_sse(ANSWER)))
    tool = DeepResearchTool(DeepResearchToolConfig(api_key="sk-test"), workspace=tmp_path)
    tool.set_broker(_ScriptBroker([dr_mod._MODE_DEEP]))
    tool.set_context("cli", "direct", "cli:direct")
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "ok" and result["content"] == ANSWER


async def test_real_tool_no_broker_runs_engine(tmp_path: Path, monkeypatch):
    # raven agent (no broker): can't ask -> honour the model's choice and run.
    _patch(monkeypatch, lambda req: httpx.Response(200, content=_sse(ANSWER)))
    tool = DeepResearchTool(DeepResearchToolConfig(api_key="sk-test"), workspace=tmp_path)
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "ok" and result["content"] == ANSWER


async def test_real_tool_broker_but_no_context_uses_regular(tmp_path: Path, monkeypatch):
    # A concurrent turn can be handed a just-promoted real tool before its per-turn
    # context is wired (broker set, cid empty). It must NOT run the paid engine
    # unasked -- fall back to regular.
    def _boom(*_a, **_k):
        raise AssertionError("engine must not run without the deep/regular ask")

    monkeypatch.setattr(dr_mod.httpx, "AsyncClient", _boom)
    tool = DeepResearchTool(DeepResearchToolConfig(api_key="sk-test"), workspace=tmp_path)
    tool.set_broker(_ScriptBroker([]))  # broker present, but set_context never called -> no cid
    assert await tool.execute(query="q") == dr_mod._USE_REGULAR


async def test_real_tool_run_capped_by_timeout(tmp_path: Path, monkeypatch):
    # blocking_interaction removes the registry timeout, so execute() must cap the
    # engine run itself: a hung run resolves to a timeout result, not forever.
    tool = DeepResearchTool(DeepResearchToolConfig(api_key="sk-test"), workspace=tmp_path)
    tool.timeout_seconds = 0.05  # instance override; no broker -> ask is skipped

    async def _hang(_query):
        await asyncio.Event().wait()

    monkeypatch.setattr(tool, "_run_engine", _hang)
    result = json.loads(await tool.execute(query="q"))
    assert result["status"] == "timeout"


class _StubProvider:
    """Minimal provider: AgentLoop construction reads the default model but the
    turn path is never exercised in these tests."""

    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
        raise NotImplementedError


def _offer_loop(tmp_path: Path) -> AgentLoop:
    """A loop that starts unconfigured, so the offer stand-in is registered."""
    return AgentLoop(provider=_StubProvider(), workspace=tmp_path, deep_research_config=DeepResearchToolConfig())


def test_promote_swaps_offer_for_real_when_key_appears(tmp_path: Path, monkeypatch):
    import raven.config.update_tools as ut

    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _offer_loop(tmp_path)
    assert isinstance(loop.tools.get("deep_research"), DeepResearchOfferTool)
    # A key now on disk (as if `raven deep-research enable` ran in another process).
    monkeypatch.setattr(
        ut,
        "get_deep_research",
        lambda **_kw: {"api_key": "sk", "api_base": "", "model": ""},
    )
    loop._maybe_promote_deep_research()
    assert isinstance(loop.tools.get("deep_research"), DeepResearchTool)
    assert loop.deep_research_manager is not None


def test_promote_noop_when_still_unconfigured(tmp_path: Path, monkeypatch):
    import raven.config.update_tools as ut

    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _offer_loop(tmp_path)
    monkeypatch.setattr(
        ut,
        "get_deep_research",
        lambda **_kw: {"api_key": "", "api_base": "", "model": ""},
    )
    loop._maybe_promote_deep_research()
    assert isinstance(loop.tools.get("deep_research"), DeepResearchOfferTool)  # unchanged
    assert loop.deep_research_manager is None


def test_promote_inherits_gateway_submit_handle(tmp_path: Path, monkeypatch):
    # A gateway wires the async-delivery handle at startup, before any key exists
    # (manager is None then). A later promotion must inherit it so a channel keeps
    # the async path instead of a blocking synchronous run.
    import raven.config.update_tools as ut

    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _offer_loop(tmp_path)
    loop.set_deep_research_submit(lambda _req: None)
    monkeypatch.setattr(
        ut,
        "get_deep_research",
        lambda **_kw: {"api_key": "sk", "api_base": "", "model": ""},
    )
    loop._maybe_promote_deep_research()
    assert loop.deep_research_manager is not None
    assert loop.deep_research_manager.can_deliver()  # async delivery wired, not sync fallback


def test_promoted_tool_inherits_broker(tmp_path: Path, monkeypatch):
    # The broker is wired once at startup onto the offer stand-in; a tool built
    # later by promotion must inherit it, else it skips the ask and runs the paid
    # engine unprompted -- defeating the whole deep-vs-regular gate.
    import raven.config.update_tools as ut

    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _offer_loop(tmp_path)
    broker = _ScriptBroker([dr_mod._MODE_REGULAR])
    loop.set_deep_research_broker(broker)  # startup wiring (onto the offer stand-in)
    monkeypatch.setattr(ut, "get_deep_research", lambda **_kw: {"api_key": "sk", "api_base": "", "model": ""})
    loop._maybe_promote_deep_research()
    tool = loop.tools.get("deep_research")
    assert isinstance(tool, DeepResearchTool)
    assert tool._broker is broker  # inherited -> will ask, not silently run


def test_set_deep_research_broker_applies_to_current_tool(tmp_path: Path, monkeypatch):
    # The immediate-apply path (onto the tool registered right now), independent of
    # promotion: startup wiring must reach the offer stand-in too.
    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _offer_loop(tmp_path)
    broker = _ScriptBroker([])
    loop.set_deep_research_broker(broker)
    assert loop.tools.get("deep_research")._broker is broker


def test_promote_survives_unreadable_config(tmp_path: Path, monkeypatch):
    # A corrupt config.json must not fail the turn: promotion runs every turn in
    # offer mode, so an unreadable config just skips promotion (the tool stays the
    # offer stand-in) instead of raising up through _process_message.
    import raven.config.update_tools as ut
    from raven.config.loader import ConfigReadError

    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _offer_loop(tmp_path)

    def _boom(**_kw):
        raise ConfigReadError("bad json")

    monkeypatch.setattr(ut, "get_deep_research", _boom)
    loop._maybe_promote_deep_research()  # must not raise
    assert isinstance(loop.tools.get("deep_research"), DeepResearchOfferTool)  # unchanged


def test_both_deep_research_variants_accept_broker():
    # The CLI double-bind loop over ("ask_user", "deep_research") wires whichever
    # variant is registered; both ask the user deep-vs-regular, so both take a broker.
    assert hasattr(DeepResearchOfferTool, "set_broker")
    assert hasattr(DeepResearchTool, "set_broker")


def test_deep_research_mode_two_states(monkeypatch):
    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    assert deep_research_mode(DeepResearchToolConfig(api_key="sk")) == "real"
    assert deep_research_mode(DeepResearchToolConfig()) == "offer"  # unconfigured -> offer
    # An env key counts as configured: register the working tool.
    monkeypatch.setenv("MIROTHINKER_API_KEY", "sk-env")
    assert deep_research_mode(DeepResearchToolConfig()) == "real"


def test_extract_output_text_concatenates_message_output_only():
    body = {
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "ignore"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "A"}, {"type": "refusal", "text": "X"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "B"}]},
        ]
    }
    assert _extract_output_text(body) == "AB"
