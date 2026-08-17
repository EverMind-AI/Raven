"""Tests pinning AgentLoop's spine-native ``run_turn(req, emit)`` output behavior.

run_turn fans the agent's output (streamed token deltas, reasoning, tool events,
notices, media) onto a single ``emit`` and returns a TurnOutcome. These pin that
observable behavior per category, plus origin gating and metadata reconstruction.
Driven against a real AgentLoop with only the LLM provider + sandbox edges faked
(never the output path itself).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.tools.base import Tool
from raven.providers.base import LLMResponse, StreamDelta, ToolCallRequest
from raven.sandbox import SandboxInitError
from raven.spine.events import MediaOut as EvMediaOut
from raven.spine.events import Notice as EvNotice
from raven.spine.events import NoticeKind, ToolPhase
from raven.spine.events import Reasoning as EvReasoning
from raven.spine.events import StreamDelta as EvStreamDelta
from raven.spine.events import Text as EvText
from raven.spine.events import ToolEvent as EvToolEvent
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


@dataclass
class _Reply:
    channel: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)


class _FakeTool(Tool):
    """Minimal no-sandbox tool so a tool-call turn can dispatch + fire events."""

    @property
    def name(self) -> str:
        return "faketool"

    @property
    def description(self) -> str:
        return "characterization fake tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        return "tool-ran"


class _FakeDeepResearch(Tool):
    """Fake deep_research: on execute it drives its stream callback with a progress
    line then the finished answer, so run_turn's inline routing can be pinned
    without real HTTP."""

    def __init__(self) -> None:
        self._cb = None

    @property
    def name(self) -> str:
        return "deep_research"

    @property
    def description(self) -> str:
        return "fake deep research"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def set_stream_callback(self, cb) -> None:
        self._cb = cb

    async def execute(self, query, **kwargs) -> str:
        if self._cb is not None:
            await self._cb("progress", "searching the web...")
            await self._cb("answer", "ANSWER-BODY")
        return '{"status": "ok", "delivered": true}'


class _FakeChatProvider:
    """Non-streaming path: ``chat_with_retry`` returns scripted LLMResponses."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._i = 0

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return r

    def get_default_model(self) -> str:
        return "fake/model"


class _FakeStreamProvider:
    """Yields scripted stream chunks via ``chat_stream`` (the streaming path that
    ``run(req, emit)`` takes when emitting StreamDelta tokens)."""

    def __init__(self, chunks: list[StreamDelta]) -> None:
        self._chunks = chunks

    async def chat_stream(self, **kwargs):
        for chunk in self._chunks:
            yield chunk

    def get_default_model(self) -> str:
        return "fake/model"


class _FakeStreamToolProvider:
    """``chat_stream`` yields a fresh scripted chunk-list per call, so a
    tool-call iteration works under run() (call 1 -> tool_call_delta, call 2 ->
    final content). run() always wires on_token_delta, so every turn streams."""

    def __init__(self, scripts: list[list[StreamDelta]]) -> None:
        self._scripts = scripts
        self._i = 0

    async def chat_stream(self, **kwargs):
        script = self._scripts[min(self._i, len(self._scripts) - 1)]
        self._i += 1
        for chunk in script:
            yield chunk

    def get_default_model(self) -> str:
        return "fake/model"


class _EmitCollector:
    """Records every RunnerEvent run() emits, in order."""

    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, ev) -> None:
        self.events.append(ev)


def _drain() -> list:
    return []


def _req(text: str, *, media=(), origin: Origin = Origin.USER) -> TurnRequest:
    return TurnRequest(
        origin=origin,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text=text,
        media=media,
    )


def _stub_edges(loop: AgentLoop) -> None:
    """No-op the sandbox/MCP bring-up so a text-only turn runs without a VM."""

    async def _noop() -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop


async def test_help_slash_returns_command_list_at_outbound_layer(tmp_path):
    # Slash exit (:1294) pinned at the _process_message reply layer
    # (media-capable; the old str-returning path would project metadata away).
    loop = AgentLoop(provider=_FakeChatProvider([]), workspace=tmp_path)
    _stub_edges(loop)

    out = await loop._process_message(_req("/help"))

    assert out is not None
    content, _media = out
    assert "Raven commands" in content


async def test_hook_short_circuit_preserves_media_at_outbound_layer(tmp_path):
    # MediaOut category: a before_user_inbound short-circuit (:1199) returns a
    # reply that can carry media. Pinned at the reply layer —
    # the old str-returning path would project media away, so collapse-drops-media would
    # be untestable otherwise.
    async def _decision(req: TurnRequest):
        return _Reply(
            channel=req.source.channel,
            chat_id=req.source.chat_id,
            content="short",
            media=["/tmp/x.png"],
        )

    loop = AgentLoop(
        provider=_FakeChatProvider([]),
        workspace=tmp_path,
        decision_consumer=_decision,
    )
    _stub_edges(loop)

    out = await loop._process_message(_req("hi"))

    assert out is not None
    content, media = out
    assert media == ["/tmp/x.png"]  # media survives the short-circuit return
    assert content == "short"


# ── run(req, emit) collapse — emit sequence per category ────────────


async def test_run_streams_then_dissolves_main_response(tmp_path):
    # Streaming main response: each non-empty chunk -> emit(StreamDelta); the
    # return dissolves (b2) -> no trailing Text. Usage rides TurnOutcome.
    chunks = [
        StreamDelta(content="Hel"),
        StreamDelta(content="lo", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
    ]
    loop = AgentLoop(provider=_FakeStreamProvider(chunks), workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    outcome = await loop.run_turn(_req("hi"), sink, _drain)

    assert [type(e).__name__ for e in sink.events] == ["StreamDelta", "StreamDelta"]
    assert [e.delta for e in sink.events] == ["Hel", "lo"]
    assert not any(isinstance(e, EvText) for e in sink.events)  # dissolved, no double
    assert outcome.usage.total_tokens == 5
    assert outcome.explicit_reply is True


async def test_run_emits_reasoning_then_stream(tmp_path):
    chunks = [
        StreamDelta(content=None, reasoning_content="think"),
        StreamDelta(content="answer"),
    ]
    loop = AgentLoop(provider=_FakeStreamProvider(chunks), workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    await loop.run_turn(_req("hi"), sink, _drain)

    assert isinstance(sink.events[0], EvReasoning) and sink.events[0].content == "think"
    assert any(isinstance(e, EvStreamDelta) and e.delta == "answer" for e in sink.events)
    assert not any(isinstance(e, EvText) for e in sink.events)


async def test_run_tool_call_emits_tool_events_and_notice(tmp_path):
    # Tool-call turn under run(): emit(ToolEvent start) + emit(Notice tool_hint) +
    # emit(ToolEvent complete), then the final answer streams + dissolves.
    # The ToolEvent schema: start carries tool_call_id/name/
    # arguments, complete carries tool_call_id/result_preview/truncated.
    provider = _FakeStreamToolProvider(
        [
            [
                StreamDelta(
                    content=None,
                    tool_call_delta={
                        "tool_calls": [{"index": 0, "id": "t1", "function": {"name": "faketool", "arguments": "{}"}}]
                    },
                )
            ],
            [StreamDelta(content="done")],
        ]
    )
    loop = AgentLoop(provider=provider, workspace=tmp_path)
    _stub_edges(loop)
    loop.tools.register(_FakeTool())
    sink = _EmitCollector()

    await loop.run_turn(_req("hi"), sink, _drain)

    tool_events = [e for e in sink.events if isinstance(e, EvToolEvent)]
    start = next(e for e in tool_events if e.phase == ToolPhase.START)
    assert start.tool_call_id == "t1" and start.name == "faketool" and start.arguments == {}
    complete = next(e for e in tool_events if e.phase == ToolPhase.COMPLETE)
    assert complete.tool_call_id == "t1" and complete.result_preview == "tool-ran"
    assert complete.truncated is False
    # The tool-call hint rides NoticeKind.TOOL_HINT (kept distinct from PROGRESS so
    # an outlet gates it on send_tool_hints), not merged into PROGRESS.
    assert any(isinstance(e, EvNotice) and e.kind is NoticeKind.TOOL_HINT for e in sink.events)
    assert any(isinstance(e, EvStreamDelta) and e.delta == "done" for e in sink.events)
    assert not any(isinstance(e, EvText) for e in sink.events)  # streamed final dissolves


# ── deep_research inline streaming: run_turn's _route_deep_research (2a) ──


def _dr_stream_provider():
    return _FakeStreamToolProvider(
        [
            [
                StreamDelta(
                    content=None,
                    tool_call_delta={
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "d1",
                                "function": {"name": "deep_research", "arguments": '{"query": "q"}'},
                            }
                        ]
                    },
                )
            ],
            [StreamDelta(content="ok done")],
        ]
    )


def _dr_chat_provider():
    return _FakeChatProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="d1", name="deep_research", arguments={"query": "q"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="", finish_reason="stop"),
        ]
    )


async def test_inline_tool_stream_routes_progress_to_reasoning_and_answer_to_stream(tmp_path):
    # TUI path (stream=True): deep_research progress -> Reasoning (thinking.delta),
    # the finished answer -> StreamDelta (token.delta). Pins _route_deep_research.
    loop = AgentLoop(provider=_dr_stream_provider(), workspace=tmp_path)
    _stub_edges(loop)
    loop.tools.register(_FakeDeepResearch())
    sink = _EmitCollector()

    await loop.run_turn(_req("q"), sink, _drain, stream=True, inline_tool_stream=True)

    deltas = [e.delta for e in sink.events if isinstance(e, EvStreamDelta)]
    assert any(isinstance(e, EvReasoning) and e.content == "searching the web..." for e in sink.events)
    # Answer streams as a delta AND the model's coda still flows after it (the
    # `if not streamed` boundary must not swallow the coda once the answer set it).
    assert "ANSWER-BODY" in deltas
    assert "ok done" in deltas


async def test_inline_tool_stream_answer_as_text_when_not_streaming(tmp_path):
    # CLI/REPL path (stream=False): answer -> Text so a non-streaming outlet renders it.
    loop = AgentLoop(provider=_dr_chat_provider(), workspace=tmp_path)
    _stub_edges(loop)
    loop.tools.register(_FakeDeepResearch())
    sink = _EmitCollector()

    await loop.run_turn(_req("q"), sink, _drain, stream=False, inline_tool_stream=True)

    assert any(isinstance(e, EvReasoning) and e.content == "searching the web..." for e in sink.events)
    assert any(isinstance(e, EvText) and e.content == "ANSWER-BODY" for e in sink.events)


async def test_no_inline_tool_stream_leaves_deep_research_callback_unset(tmp_path):
    # Without inline_tool_stream (gateway/channels), the callback is never wired, so
    # the tool emits nothing inline -> no progress/answer routing.
    loop = AgentLoop(provider=_dr_chat_provider(), workspace=tmp_path)
    _stub_edges(loop)
    loop.tools.register(_FakeDeepResearch())
    sink = _EmitCollector()

    await loop.run_turn(_req("q"), sink, _drain, stream=False, inline_tool_stream=False)

    assert not any(isinstance(e, EvReasoning) and e.content == "searching the web..." for e in sink.events)
    assert not any(isinstance(e, EvText) and e.content == "ANSWER-BODY" for e in sink.events)


async def test_inject_message_merged_before_next_iteration(tmp_path):
    # BusyPolicy.INJECT: a message injected mid-turn is drained at the next
    # iteration's top and appended as a user message before that LLM call.
    class _RecordingStreamToolProvider:
        def __init__(self, scripts):
            self._scripts = scripts
            self._i = 0
            self.calls: list[list[dict]] = []

        async def chat_stream(self, **kwargs):
            self.calls.append(list(kwargs.get("messages") or []))
            script = self._scripts[min(self._i, len(self._scripts) - 1)]
            self._i += 1
            for chunk in script:
                yield chunk

        def get_default_model(self) -> str:
            return "fake/model"

    provider = _RecordingStreamToolProvider(
        [
            [
                StreamDelta(
                    content=None,
                    tool_call_delta={
                        "tool_calls": [{"index": 0, "id": "t1", "function": {"name": "faketool", "arguments": "{}"}}]
                    },
                )
            ],
            [StreamDelta(content="done")],
        ]
    )
    loop = AgentLoop(provider=provider, workspace=tmp_path)
    _stub_edges(loop)
    loop.tools.register(_FakeTool())

    injects: list[list] = [[], [_req("also check the logs")]]
    n = 0

    def _drain_inject() -> list:
        nonlocal n
        out = injects[n] if n < len(injects) else []
        n += 1
        return out

    await loop.run_turn(_req("start"), _EmitCollector(), _drain_inject, stream=True)

    assert len(provider.calls) >= 2  # the tool call drove a second iteration
    second = provider.calls[1]
    assert any(m.get("role") == "user" and "also check the logs" in str(m.get("content", "")) for m in second), (
        f"injected message not merged into the second iteration: {second}"
    )


async def test_run_slash_emits_text_not_streamed(tmp_path):
    # /help is an early return (no LLM stream) -> streamed=False -> emit(Text).
    loop = AgentLoop(provider=_FakeChatProvider([]), workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    outcome = await loop.run_turn(_req("/help"), sink, _drain)

    texts = [e for e in sink.events if isinstance(e, EvText)]
    assert len(texts) == 1 and "Raven commands" in texts[0].content
    assert not any(isinstance(e, EvStreamDelta) for e in sink.events)
    assert outcome.explicit_reply is True


async def test_run_short_circuit_emits_media_before_text(tmp_path):
    # MediaOut category: a hook short-circuit returns media + content. MediaOut is
    # independent of the stream and precedes Text (G-MEDIA-2(a) order).
    async def _decision(req: TurnRequest):
        return _Reply(
            channel=req.source.channel,
            chat_id=req.source.chat_id,
            content="short",
            media=["/tmp/x.png"],
        )

    loop = AgentLoop(
        provider=_FakeChatProvider([]),
        workspace=tmp_path,
        decision_consumer=_decision,
    )
    _stub_edges(loop)
    sink = _EmitCollector()

    await loop.run_turn(_req("hi"), sink, _drain)

    kinds = [type(e).__name__ for e in sink.events]
    assert kinds == ["MediaOut", "Text"]  # media first, then text
    assert sink.events[0].media[0].path == "/tmp/x.png"
    assert sink.events[1].content == "short"


async def test_run_propagates_sandbox_error_not_error_string(tmp_path):
    # Unlike the old string-returning path (returned "[Sandbox error]"), run() lets the exception
    # propagate so the lane turns it into TurnFailed.
    loop = AgentLoop(provider=_FakeChatProvider([]), workspace=tmp_path)

    async def _boom() -> None:
        raise SandboxInitError("test: sandbox down")

    loop._start_executor = _boom
    sink = _EmitCollector()

    with pytest.raises(SandboxInitError):
        await loop.run_turn(_req("hi"), sink, _drain)

    assert not any(isinstance(e, EvText) for e in sink.events)


async def test_run_propagates_mid_turn_error_not_sorry_text(tmp_path):
    # N-TURNFAILED second source: a mid-turn provider error propagates out of
    # _process_message (the "Sorry" catch lives in _dispatch, the bus wrapper run()
    # does not use) -> run() re-raises -> the lane makes TurnFailed, not a Text.
    class _BoomStreamProvider:
        async def chat_stream(self, **kwargs):
            raise RuntimeError("mid-turn boom")
            yield  # unreachable; makes this an async generator

        def get_default_model(self) -> str:
            return "fake/model"

    loop = AgentLoop(provider=_BoomStreamProvider(), workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    with pytest.raises(RuntimeError):
        await loop.run_turn(_req("hi"), sink, _drain)

    assert not any(isinstance(e, EvText) for e in sink.events)


def _message_tool_call(arguments: str) -> StreamDelta:
    return StreamDelta(
        content=None,
        tool_call_delta={
            "tool_calls": [{"index": 0, "id": "m1", "function": {"name": "message", "arguments": arguments}}]
        },
    )


async def test_run_message_tool_text_streams_and_dissolves(tmp_path):
    # The message tool's reply routes through on_token -> StreamDelta (b2), then
    # _process_message returns None -> no trailing Text. explicit_reply is still
    # True (the agent did reply via the tool).
    provider = _FakeStreamToolProvider(
        [
            [_message_tool_call('{"content": "hi via tool"}')],
            [StreamDelta(content="")],  # second iteration: nothing more, finish
        ]
    )
    loop = AgentLoop(provider=provider, workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    outcome = await loop.run_turn(_req("hi"), sink, _drain)

    assert any(isinstance(e, EvStreamDelta) and e.delta == "hi via tool" for e in sink.events)
    assert not any(isinstance(e, EvText) for e in sink.events)  # tool reply dissolves
    assert outcome.explicit_reply is True


async def test_run_message_tool_media_is_not_dropped(tmp_path):
    # Regression guard: a message-tool reply carrying media must emit MediaOut
    # (media is independent of the token stream). The tool path returns None from
    # _process_message, so the return boundary never sees it — _route_to_stream
    # must emit the media itself, matching what the bus path delivers.
    provider = _FakeStreamToolProvider(
        [
            [_message_tool_call('{"content": "see this", "media": ["/tmp/pic.png"]}')],
            [StreamDelta(content="")],
        ]
    )
    loop = AgentLoop(provider=provider, workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    outcome = await loop.run_turn(_req("hi"), sink, _drain)

    media = [e for e in sink.events if isinstance(e, EvMediaOut)]
    assert media and media[0].media[0].path == "/tmp/pic.png"
    assert any(isinstance(e, EvStreamDelta) and e.delta == "see this" for e in sink.events)
    assert outcome.explicit_reply is True


# ── stream=False (REPL assembly, canon Q2-D): reply is one Text, no StreamDelta ──


async def test_run_stream_false_main_reply_is_one_text(tmp_path):
    # build_repl wires stream=False -> non-streaming chat_with_retry -> the reply
    # is one Text (CliOutlet renders it), never a StreamDelta.
    provider = _FakeChatProvider([LLMResponse(content="full reply", finish_reason="stop")])
    loop = AgentLoop(provider=provider, workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    outcome = await loop.run_turn(_req("hi"), sink, _drain, stream=False)

    texts = [e for e in sink.events if isinstance(e, EvText)]
    assert len(texts) == 1 and texts[0].content == "full reply"
    assert not any(isinstance(e, EvStreamDelta) for e in sink.events)
    assert outcome.explicit_reply is True


async def test_run_stream_false_message_tool_emits_text(tmp_path):
    # The message-tool reply under stream=False must emit Text, not StreamDelta —
    # else a non-streaming outlet (CliOutlet) would eat the delta and the REPL
    # would go silent for tool replies.
    provider = _FakeChatProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="m1", name="message", arguments={"content": "hi via tool"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="", finish_reason="stop"),
        ]
    )
    loop = AgentLoop(provider=provider, workspace=tmp_path)
    _stub_edges(loop)
    sink = _EmitCollector()

    outcome = await loop.run_turn(_req("hi"), sink, _drain, stream=False)

    assert any(isinstance(e, EvText) and e.content == "hi via tool" for e in sink.events)
    assert not any(isinstance(e, EvStreamDelta) for e in sink.events)
    assert outcome.explicit_reply is True


# ── origin gates the user-inbound hook (engagement); magic-key fallback ──


def _hook_loop(tmp_path):
    """An AgentLoop whose decision_consumer short-circuits with 'hook-fired' when
    the user-inbound hook runs; otherwise the (streaming) LLM reply 'llm' streams.
    So the hook firing vs being skipped is observable from the output."""

    async def _decision(req: TurnRequest):
        return _Reply(
            channel=req.source.channel,
            chat_id=req.source.chat_id,
            content="hook-fired",
        )

    loop = AgentLoop(
        provider=_FakeStreamProvider([StreamDelta(content="llm")]),
        workspace=tmp_path,
        decision_consumer=_decision,
    )
    _stub_edges(loop)
    return loop


async def test_run_turn_user_origin_fires_user_inbound_hook(tmp_path):
    sink = _EmitCollector()
    await _hook_loop(tmp_path).run_turn(_req("hi", origin=Origin.USER), sink, _drain)
    # USER -> hook fires -> short-circuits, the LLM is never reached.
    assert any(isinstance(e, EvText) and e.content == "hook-fired" for e in sink.events)
    assert not any(isinstance(e, EvStreamDelta) for e in sink.events)


async def test_run_turn_cron_origin_fires_hook_like_user(tmp_path):
    # D-ENGAGE replicate: cron is NOT proactive-suppressed -> hook fires (as today).
    sink = _EmitCollector()
    await _hook_loop(tmp_path).run_turn(_req("tick", origin=Origin.CRON), sink, _drain)
    assert any(isinstance(e, EvText) and e.content == "hook-fired" for e in sink.events)


async def test_run_turn_sentinel_origin_suppresses_user_inbound_hook(tmp_path):
    sink = _EmitCollector()
    await _hook_loop(tmp_path).run_turn(_req("nudge", origin=Origin.SENTINEL), sink, _drain)
    # SENTINEL -> hook suppressed -> proceeds to the LLM (no short-circuit).
    assert any(isinstance(e, EvStreamDelta) and e.delta == "llm" for e in sink.events)
    assert not any(isinstance(e, EvText) and e.content == "hook-fired" for e in sink.events)


async def test_run_turn_subagent_origin_suppresses_user_inbound_hook(tmp_path):
    sink = _EmitCollector()
    await _hook_loop(tmp_path).run_turn(_req("result", origin=Origin.SUBAGENT), sink, _drain)
    assert any(isinstance(e, EvStreamDelta) and e.delta == "llm" for e in sink.events)
    assert not any(isinstance(e, EvText) and e.content == "hook-fired" for e in sink.events)


async def _process_via_chat(loop, msg):
    # _process_message with no callbacks -> non-streaming chat_with_retry path.
    return await loop._process_message(msg)


async def test_process_message_origin_none_plain_fires_hook(tmp_path):
    # The legacy path passed origin=None; the safe default
    # (origin not in _SKIP_*) treats it as a user inbound and fires the hook.
    async def _decision(req: TurnRequest):
        return _Reply(
            channel=req.source.channel,
            chat_id=req.source.chat_id,
            content="hook-fired",
        )

    loop = AgentLoop(
        provider=_FakeChatProvider([LLMResponse(content="llm", finish_reason="stop")]),
        workspace=tmp_path,
        decision_consumer=_decision,
    )
    _stub_edges(loop)
    out = await _process_via_chat(loop, _req("hi"))
    assert out is not None
    content, _media = out
    assert content == "hook-fired"


async def test_run_turn_reconstructs_metadata_from_source_extras(tmp_path):
    # channel metadata rides Source.extras; run_turn reconstructs it into
    # the turn metadata so consumers (here _set_tool_context, which reads
    # message_id for reply threading) still see it.
    loop = AgentLoop(
        provider=_FakeChatProvider([LLMResponse(content="ok", finish_reason="stop", tool_calls=[])]),
        workspace=tmp_path,
    )
    _stub_edges(loop)
    seen: dict = {}
    real = loop._set_tool_context

    def _spy(channel, chat_id, message_id=None, session_key=None):
        seen["message_id"] = message_id
        return real(channel, chat_id, message_id, session_key=session_key)

    loop._set_tool_context = _spy

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(
            channel="tg",
            chat_id="c",
            sender_id="u",
            chat_type=ChatType.DM,
            extras={"message_id": "m1"},
        ),
        text="hi",
    )
    await loop.run_turn(req, _EmitCollector(), _drain, stream=False)
    assert seen.get("message_id") == "m1"  # extras -> metadata -> _set_tool_context


# ── deliver_text: verbatim background delivery, skips the model (2b) ──


async def test_run_turn_deliver_text_emits_verbatim_skips_model_and_indexes(tmp_path):
    # A background task pushes a finished result back via deliver_text: run_turn
    # emits it as one Text verbatim, records it to the session, and hands it to
    # the backend index — and never touches the model.
    class _NoCallProvider:
        async def chat_stream(self, **kwargs):
            raise AssertionError("model must not be called for a deliver_text turn")
            yield  # unreachable; keeps this an async generator

        async def chat_with_retry(self, **kwargs):
            raise AssertionError("model must not be called for a deliver_text turn")

        def get_default_model(self) -> str:
            return "fake/model"

    stored: list = []

    class _FakeBackend:
        async def store(self, key, messages) -> None:
            stored.append((key, messages))

    loop = AgentLoop(provider=_NoCallProvider(), workspace=tmp_path)
    _stub_edges(loop)
    loop.backend = _FakeBackend()
    sink = _EmitCollector()

    req = TurnRequest(
        origin=Origin.SUBAGENT,
        source=Source(channel="weixin", chat_id="c", sender_id="deep_research", chat_type=ChatType.DM),
        text="",
        conversation="weixin:c",
        deliver_text="FULL REPORT [1]",
    )
    outcome = await loop.run_turn(req, sink, _drain, stream=False)

    texts = [e for e in sink.events if isinstance(e, EvText)]
    assert len(texts) == 1 and texts[0].content == "FULL REPORT [1]"
    assert not any(isinstance(e, EvStreamDelta) for e in sink.events)  # not streamed through the model
    assert outcome.explicit_reply is True

    session = loop.sessions.get_or_create("weixin:c")
    assert any(m.get("role") == "assistant" and m.get("content") == "FULL REPORT [1]" for m in session.messages)
    assert stored and stored[0][0] == "weixin:c"
    assert stored[0][1][0]["content"] == "FULL REPORT [1]"


async def test_run_turn_deliver_text_persists_before_emit(tmp_path):
    # save-then-send order: if the session save fails, nothing was emitted, so
    # the user is never left with a delivered message that no turn recorded.
    loop = AgentLoop(provider=_FakeChatProvider([]), workspace=tmp_path)
    _stub_edges(loop)

    def _boom(_session) -> None:
        raise RuntimeError("save failed")

    loop.sessions.save = _boom
    sink = _EmitCollector()

    req = TurnRequest(
        origin=Origin.SUBAGENT,
        source=Source(channel="weixin", chat_id="c", sender_id="deep_research", chat_type=ChatType.DM),
        text="",
        conversation="weixin:c",
        deliver_text="REPORT",
    )
    with pytest.raises(RuntimeError):
        await loop.run_turn(req, sink, _drain, stream=False)
    assert not any(isinstance(e, EvText) for e in sink.events)  # save failed -> nothing delivered


async def test_run_turn_wires_deep_research_delivery_routing(tmp_path):
    # _set_tool_context routes the deep_research tool's delivery context so the
    # async transport knows which conversation to push the finished answer to.
    class _FakeDRRouting(Tool):
        def __init__(self) -> None:
            self.ctx: tuple | None = None

        @property
        def name(self) -> str:
            return "deep_research"

        @property
        def description(self) -> str:
            return "fake deep research (routing)"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}, "required": []}

        def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
            self.ctx = (channel, chat_id, session_key)

        async def execute(self, **kwargs) -> str:
            return "{}"

    loop = AgentLoop(
        provider=_FakeChatProvider([LLMResponse(content="ok", finish_reason="stop")]),
        workspace=tmp_path,
    )
    _stub_edges(loop)
    dr = _FakeDRRouting()
    loop.tools.register(dr)

    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="weixin", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="hi",
        conversation="weixin:c",
    )
    await loop.run_turn(req, _EmitCollector(), _drain, stream=False)
    assert dr.ctx == ("weixin", "c", "weixin:c")


async def test_run_turn_empty_extras_reconstructs_empty_metadata(tmp_path):
    # No-regression: a host source carries no extras -> metadata={} ->
    # _set_tool_context sees message_id=None.
    loop = AgentLoop(
        provider=_FakeChatProvider([LLMResponse(content="ok", finish_reason="stop", tool_calls=[])]),
        workspace=tmp_path,
    )
    _stub_edges(loop)
    seen: dict = {"message_id": "sentinel"}

    def _spy(channel, chat_id, message_id=None, session_key=None):
        seen["message_id"] = message_id

    loop._set_tool_context = _spy
    await loop.run_turn(_req("hi"), _EmitCollector(), _drain, stream=False)
    assert seen["message_id"] is None  # empty extras -> metadata={} -> no message_id
