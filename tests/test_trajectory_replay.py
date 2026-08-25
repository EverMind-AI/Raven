"""Tests for deterministic trajectory replay (``raven/trajectory/replay.py``).

Covers the recording loader, the request normalization/comparison, the
ReplayProvider feed (order, strict/warn divergence, exhaustion, streaming),
the ReplayToolRegistry (recorded results only — a side-effecting tool proves
nothing real executes), and the full ``run_replay`` drive, including a
record-then-replay round trip through the real tracer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from raven.trajectory.replay import (
    RecordedLLMCall,
    RecordedToolCall,
    Recording,
    ReplayProvider,
    ReplayState,
    ReplayToolRegistry,
    _normalize_text,
    compare_llm_request,
    load_recording,
    run_replay,
)

pytestmark = pytest.mark.asyncio


# ── bundle fixture helpers ─────────────────────────────────────────────


def _write_artifact(bundle: Path, name: str, payload) -> str:
    (bundle / "artifacts").mkdir(parents=True, exist_ok=True)
    rel = f"artifacts/{name}"
    (bundle / rel).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return rel


def _make_bundle(
    root: Path,
    *,
    llm_calls: list[tuple[dict | None, dict | None]] = (),
    tool_calls: list[tuple[dict, dict]] = (),
    turns: list[dict] = (),
    session_key: str = "cli:replay-test",
) -> Path:
    """Hand-build a minimal bundle: turn spans first (checkpoint + close),
    llm/tool spans interleaved in the given order."""
    bundle = root / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    spans: list[dict] = []

    def _span(name: str, span_id: str, attrs: dict) -> dict:
        return {
            "traceId": "trace-r",
            "spanId": span_id,
            "name": name,
            "startTime": "2026-08-20T10:00:00+00:00",
            "endTime": "2026-08-20T10:00:01+00:00",
            "attributes": {"attempt.id": "trace-r", "session.key": session_key, **attrs},
        }

    for i, turn in enumerate(turns):
        rel = _write_artifact(bundle, f"turn-in-{i}.json", turn)
        # Checkpoint record first (in_progress), close record later — replay
        # must dedupe by span id and keep the close.
        spans.append(_span("session.turn", f"turn-{i}", {"turn.input.artifact_path": rel, "turn.in_progress": True}))
    for i, (inp, out) in enumerate(llm_calls):
        attrs = {}
        if inp is not None:
            attrs["llm.input.artifact_path"] = _write_artifact(bundle, f"llm-in-{i}.json", inp)
        if out is not None:
            attrs["llm.output.artifact_path"] = _write_artifact(bundle, f"llm-out-{i}.json", out)
        spans.append(_span("llm.call", f"llm-{i}", attrs))
    for i, (inp, out) in enumerate(tool_calls):
        spans.append(
            _span(
                "tool.call",
                f"tool-{i}",
                {
                    "tool.input.artifact_path": _write_artifact(bundle, f"tool-in-{i}.json", inp),
                    "tool.output.artifact_path": _write_artifact(bundle, f"tool-out-{i}.json", out),
                },
            )
        )
    for i, turn in enumerate(turns):
        spans.append(
            _span(
                "session.turn",
                f"turn-{i}",
                {"turn.input.artifact_path": f"artifacts/turn-in-{i}.json", "turn.in_progress": False},
            )
        )

    (bundle / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    (bundle / "manifest.json").write_text(
        json.dumps({"format_version": 1, "attempt_id": "trace-r", "session_key": session_key}), encoding="utf-8"
    )
    return bundle


def _llm_output(content=None, tool_calls=(), finish_reason="stop", **extra) -> dict:
    return {
        "content": content,
        "output": content,
        "finish_reason": finish_reason,
        "tool_calls": list(tool_calls),
        "reasoning_content": extra.get("reasoning_content"),
        "thinking_blocks": extra.get("thinking_blocks"),
        "usage": extra.get("usage") or {},
    }


def _recording(llm_calls=(), tool_calls=(), turns=()) -> Recording:
    return Recording(
        bundle_dir=Path("."),
        manifest={},
        llm_calls=list(llm_calls),
        tool_calls=list(tool_calls),
        turns=list(turns),
    )


# ── load_recording ─────────────────────────────────────────────────────


async def test_load_recording_orders_dedupes_and_covers_skill_reads(tmp_path) -> None:
    """Spans dedupe by id (turn checkpoint + close = one turn); a skill.read
    retyped span still counts as a tool call because matching is by artifact
    key, not span name."""
    bundle = _make_bundle(
        tmp_path,
        llm_calls=[
            (None, _llm_output(content="first")),
            (None, _llm_output(content="second")),
        ],
        tool_calls=[({"name": "read_skill", "params": {"skill_id": "x"}}, {"result": "## x"})],
        turns=[{"content": "go", "channel": "cli", "chat_id": "direct"}],
    )
    # Retype the tool span the way semconv does for skill tools.
    lines = [json.loads(x) for x in (bundle / "spans.jsonl").read_text().splitlines()]
    for span in lines:
        if span["spanId"] == "tool-0":
            span["name"] = "skill.read"
    (bundle / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in lines), encoding="utf-8")

    rec = load_recording(bundle)

    assert [c.output["content"] for c in rec.llm_calls] == ["first", "second"]
    assert len(rec.tool_calls) == 1
    assert rec.tool_calls[0].name == "read_skill"
    assert rec.tool_calls[0].result == "## x"
    assert len(rec.turns) == 1
    assert rec.turns[0].content == "go"
    assert rec.turns[0].session_key == "cli:replay-test"


async def test_load_recording_rejects_non_bundle(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a trajectory bundle"):
        load_recording(tmp_path)


async def test_load_recording_rejects_traversal_before_reading_external_file(tmp_path, monkeypatch) -> None:
    bundle = _make_bundle(tmp_path, turns=[{"content": "go", "channel": "cli", "chat_id": "direct"}])
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"content": "host secret"}), encoding="utf-8")
    spans = [json.loads(line) for line in (bundle / "spans.jsonl").read_text().splitlines()]
    for span in spans:
        if span["spanId"] == "turn-0":
            span["attributes"]["turn.input.artifact_path"] = "../outside.json"
    (bundle / "spans.jsonl").write_text("".join(json.dumps(span) + "\n" for span in spans), encoding="utf-8")

    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path.resolve() == outside.resolve():
            pytest.fail("load_recording read a file outside the bundle")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    with pytest.raises(ValueError, match="escapes the bundle"):
        load_recording(bundle)


async def test_load_recording_rejects_symlinked_artifact_before_read(tmp_path, monkeypatch) -> None:
    bundle = _make_bundle(tmp_path, turns=[{"content": "go", "channel": "cli", "chat_id": "direct"}])
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"content": "host secret"}), encoding="utf-8")
    artifact = bundle / "artifacts" / "turn-in-0.json"
    artifact.unlink()
    artifact.symlink_to(outside)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path.resolve() == outside.resolve():
            pytest.fail("load_recording followed an artifact symlink outside the bundle")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    with pytest.raises(ValueError, match="escapes the bundle"):
        load_recording(bundle)


# ── normalization + comparison ─────────────────────────────────────────


async def test_normalize_masks_only_fence_nonces_and_the_clock_line() -> None:
    """Fence-marker nonces and the runtime clock line are masked; a date or an
    8-hex token inside ordinary content is NOT — that would swallow real
    divergences (e.g. `schedule 2026-08-21` vs `schedule 2027-09-22`)."""
    text = (
        "[BEGIN UNTRUSTED shell #a1b2c3d4 — until END tagged #a1b2c3d4]\n"
        "body kept verbatim, nonce-like #deadbeef too\n"
        "[END UNTRUSTED shell #a1b2c3d4]\n"
        "Current Time: 2026-08-21 09:15 (Friday) (CST)"
    )
    other_markers = text.replace("#a1b2c3d4", "#ffff0000").replace("(Friday) (CST)", "(Monday) (UTC)")
    assert _normalize_text(text) == _normalize_text(other_markers)
    assert "#deadbeef" in _normalize_text(text), "content nonlookalikes must stay"

    assert _normalize_text("schedule 2026-08-21") != _normalize_text("schedule 2027-09-22")
    assert _normalize_text("run at 09:15:22") != _normalize_text("run at 23:59:59")


async def test_compare_ignores_system_content_cache_control_and_nonces() -> None:
    recorded = {
        "model": "stub",
        "messages": [
            {"role": "system", "content": "recorded env: /home/rec/ws at 2026-08-20 10:00:00"},
            {"role": "user", "content": "go", "cache_control": {"type": "ephemeral"}},
            {"role": "tool", "content": "[BEGIN UNTRUSTED shell #11112222 x]ok[END #11112222]"},
        ],
        "tools": [{"type": "function", "function": {"name": "t1"}}],
    }
    live_messages = [
        {"role": "system", "content": "replay env: /tmp/other-ws at 2026-08-21 11:11:11"},
        {"role": "user", "content": "go"},
        {"role": "tool", "content": "[BEGIN UNTRUSTED shell #99998888 x]ok[END #99998888]"},
    ]
    assert compare_llm_request(recorded, live_messages, recorded["tools"], "stub") is None


async def test_compare_reports_the_first_mismatching_field() -> None:
    recorded = {
        "model": "stub",
        "messages": [{"role": "user", "content": "go"}],
        "tools": [{"type": "function", "function": {"name": "t1"}}],
    }
    mismatch = compare_llm_request(recorded, [{"role": "user", "content": "go"}], [], "other-model")
    assert mismatch.field == "model" and "other-model" in mismatch.detail
    assert (mismatch.expected, mismatch.actual) == ("stub", "other-model")

    mismatch = compare_llm_request(recorded, [], [], "stub")
    assert mismatch.field == "messages.length"
    assert (mismatch.expected, mismatch.actual) == (1, 0)

    mismatch = compare_llm_request(recorded, [{"role": "user", "content": "STOP"}], [], "stub")
    assert mismatch.field == "messages[0]" and "STOP" in mismatch.detail
    assert mismatch.expected == {"role": "user", "content": "go"}
    assert mismatch.actual == {"role": "user", "content": "STOP"}

    mismatch = compare_llm_request(recorded, [{"role": "user", "content": "go"}], [], "stub")
    assert mismatch.field == "tools" and "t1" in mismatch.detail
    assert (mismatch.expected, mismatch.actual) == (["t1"], [])


async def test_compare_preserves_assistant_thinking_blocks() -> None:
    recorded = {
        "model": "stub",
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "thinking_blocks": [{"type": "thinking", "thinking": "recorded"}],
            }
        ],
        "tools": [],
    }
    live = [{"role": "assistant", "content": None, "thinking_blocks": [{"type": "thinking", "thinking": "live"}]}]

    mismatch = compare_llm_request(recorded, live, [], "stub")

    assert mismatch is not None
    assert mismatch.field == "messages[0]"


# ── ReplayProvider ─────────────────────────────────────────────────────


async def test_provider_feeds_recorded_outputs_in_order() -> None:
    rec = _recording(
        llm_calls=[
            RecordedLLMCall(
                input=None,
                output=_llm_output(
                    content="plan",
                    tool_calls=[{"id": "c1", "name": "marker", "arguments": {"note": "hi"}}],
                    finish_reason="tool_calls",
                    reasoning_content="thinking",
                    thinking_blocks=[{"type": "thinking", "thinking": "structured"}],
                    usage={"prompt_tokens": 3},
                ),
            ),
            RecordedLLMCall(input=None, output=_llm_output(content="done")),
        ]
    )
    state = ReplayState(mode="strict")
    provider = ReplayProvider(rec, state)

    first = await provider.chat(messages=[{"role": "user", "content": "go"}])
    assert first.content == "plan"
    assert first.finish_reason == "tool_calls"
    assert first.reasoning_content == "thinking"
    assert first.thinking_blocks == [{"type": "thinking", "thinking": "structured"}]
    assert first.usage == {"prompt_tokens": 3}
    assert [(tc.name, tc.arguments) for tc in first.tool_calls] == [("marker", {"note": "hi"})]

    second = await provider.chat(messages=[])
    assert second.content == "done"
    assert state.llm_fed == 2
    assert state.divergences == []


async def test_provider_strict_halts_on_divergence() -> None:
    rec = _recording(
        llm_calls=[
            RecordedLLMCall(
                input={"model": "stub", "messages": [{"role": "user", "content": "recorded"}], "tools": []},
                output=_llm_output(content="reply"),
            ),
        ]
    )
    state = ReplayState(mode="strict")
    provider = ReplayProvider(rec, state)

    response = await provider.chat(messages=[{"role": "user", "content": "different"}], model="stub")

    assert response.finish_reason == "error"
    assert response.error_classification is not None
    assert response.error_classification.category == "replay_divergence"
    assert not response.error_classification.retryable
    assert state.halted
    assert state.llm_fed == 0
    assert len(state.divergences) == 1
    div = state.divergences[0]
    assert div.kind == "llm" and div.index == 0 and div.fatal
    assert div.field == "messages[0]"
    assert "recorded" in div.detail and "different" in div.detail
    # Structured expected/actual carry the raw values for regression checks.
    assert div.expected == {"role": "user", "content": "recorded"}
    assert div.actual == {"role": "user", "content": "different"}
    # The live request is captured verbatim even though the call diverged.
    assert state.llm_requests == [
        {"model": "stub", "stream": False, "messages": [{"role": "user", "content": "different"}], "tools": []}
    ]
    # Once halted, later calls are refused without consuming the recording.
    again = await provider.chat(messages=[])
    assert again.finish_reason == "error"
    assert len(state.llm_requests) == 1, "calls refused after the halt are not captured"


async def test_provider_warn_records_and_keeps_feeding() -> None:
    rec = _recording(
        llm_calls=[
            RecordedLLMCall(
                input={"model": "stub", "messages": [{"role": "user", "content": "recorded"}], "tools": []},
                output=_llm_output(content="reply-1"),
            ),
            RecordedLLMCall(input=None, output=_llm_output(content="reply-2")),
        ]
    )
    state = ReplayState(mode="warn")
    provider = ReplayProvider(rec, state)

    first = await provider.chat(messages=[{"role": "user", "content": "different"}], model="stub")
    second = await provider.chat(messages=[])

    assert (first.content, second.content) == ("reply-1", "reply-2")
    assert not state.halted
    assert len(state.divergences) == 1
    assert not state.divergences[0].fatal


async def test_provider_exhaustion_halts_even_in_warn_mode() -> None:
    state = ReplayState(mode="warn")
    provider = ReplayProvider(_recording(), state)

    response = await provider.chat(messages=[])

    assert response.finish_reason == "error"
    assert state.halted
    assert state.divergences[0].field == "exhausted"


async def test_provider_stream_reassembles_to_the_recorded_response() -> None:
    long_content = "x" * 200
    rec = _recording(
        llm_calls=[
            RecordedLLMCall(
                input=None,
                output=_llm_output(
                    content=long_content,
                    tool_calls=[{"id": "c1", "name": "marker", "arguments": {"note": "hi"}}],
                    finish_reason="length",
                    reasoning_content="because",
                    usage={"completion_tokens": 9},
                ),
                stream=True,
            ),
        ]
    )
    state = ReplayState(mode="strict")
    provider = ReplayProvider(rec, state)

    deltas = [d async for d in provider.chat_stream(messages=[])]

    assert len(deltas) > 2, "recorded output must be re-chunked, not replayed as one blob"
    assert "".join(d.content or "" for d in deltas) == long_content
    assert "".join(d.reasoning_content or "" for d in deltas) == "because"
    terminal = deltas[-1]
    assert terminal.finish_reason == "length"
    assert terminal.usage == {"completion_tokens": 9}
    calls = terminal.tool_call_delta["tool_calls"]
    assert calls[0]["function"]["name"] == "marker"
    assert json.loads(calls[0]["function"]["arguments"]) == {"note": "hi"}
    assert state.llm_streamed == 1


async def test_provider_flags_stream_mode_mismatch() -> None:
    """A call recorded on the streaming path but requested non-streaming (or
    vice versa) is a harness-behavior divergence in its own right."""
    rec = _recording(llm_calls=[RecordedLLMCall(input=None, output=_llm_output(content="x"), stream=True)])
    state = ReplayState(mode="strict")

    response = await ReplayProvider(rec, state).chat(messages=[])

    assert response.finish_reason == "error"
    assert state.halted
    assert state.divergences[0].field == "stream mode"

    rec = _recording(llm_calls=[RecordedLLMCall(input=None, output=_llm_output(content="x"), stream=False)])
    warn = ReplayState(mode="warn")
    deltas = [d async for d in ReplayProvider(rec, warn).chat_stream(messages=[])]

    assert "".join(d.content or "" for d in deltas) == "x"
    assert not warn.halted
    assert warn.divergences[0].field == "stream mode" and not warn.divergences[0].fatal


# ── ReplayToolRegistry ─────────────────────────────────────────────────


class _BombTool:
    """A registered tool whose execution would leave a visible side effect —
    proof the replay registry never dispatches."""

    name = "bomb"
    description = "must never run"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, marker: Path):
        self._marker = marker

    def to_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name}}

    async def execute(self, **_kwargs) -> str:
        self._marker.write_text("executed", encoding="utf-8")
        return "boom"


async def test_tool_registry_feeds_recorded_results_and_never_executes(tmp_path) -> None:
    marker = tmp_path / "side-effect"
    rec = _recording(tool_calls=[RecordedToolCall(name="bomb", params={"a": 1}, result="recorded output")])
    state = ReplayState(mode="strict")
    registry = ReplayToolRegistry(rec, state)
    registry.register(_BombTool(marker))

    result = await registry.execute("bomb", {"a": 1})

    assert result == "recorded output"
    assert not marker.exists(), "replay must never run real tool code"
    assert state.tool_fed == 1
    assert state.divergences == []


async def test_tool_registry_divergence_strict_vs_warn(tmp_path) -> None:
    calls = [RecordedToolCall(name="bomb", params={"a": 1}, result="recorded output")]

    strict = ReplayState(mode="strict")
    result = await ReplayToolRegistry(_recording(tool_calls=calls), strict).execute("other", {"a": 1})
    assert result.startswith("Error: replay halted")
    assert strict.halted and strict.divergences[0].field == "tool name"

    warn = ReplayState(mode="warn")
    result = await ReplayToolRegistry(_recording(tool_calls=calls), warn).execute("bomb", {"a": 2})
    assert result == "recorded output"
    assert not warn.halted
    assert warn.divergences[0].field == "tool params"
    assert warn.divergences[0].expected == {"a": 1}
    assert warn.divergences[0].actual == {"a": 2}
    assert warn.tool_requests == [{"name": "bomb", "params": {"a": 2}}]


async def test_tool_registry_exhaustion_halts(tmp_path) -> None:
    state = ReplayState(mode="warn")
    registry = ReplayToolRegistry(_recording(), state)

    result = await registry.execute("bomb", {})

    assert result.startswith("Error: replay halted")
    assert state.halted


async def test_tool_registry_serves_recorded_tool_definitions() -> None:
    tools = [{"type": "function", "function": {"name": "marker", "parameters": {}}}]
    rec = _recording(llm_calls=[RecordedLLMCall(input={"model": "m", "messages": [], "tools": tools}, output=None)])

    registry = ReplayToolRegistry(rec, ReplayState())

    assert registry.get_definitions() == tools


# ── run_replay over a hand-built bundle ────────────────────────────────


async def test_run_replay_drives_the_loop_and_executes_nothing(tmp_path, monkeypatch) -> None:
    """A recorded exec call replays from the recording: the loop runs both
    model iterations, the tool result is the recorded text, and no command
    executes. The replay run emits no spans even with tracing enabled."""
    from raven.tracing import spans as _spans

    pwned = tmp_path / "pwned"
    bundle = _make_bundle(
        tmp_path,
        llm_calls=[
            (
                None,
                _llm_output(
                    content="",
                    tool_calls=[{"id": "c1", "name": "exec", "arguments": {"command": f"touch {pwned}"}}],
                    finish_reason="tool_calls",
                ),
            ),
            (None, _llm_output(content="done")),
        ],
        tool_calls=[({"name": "exec", "params": {"command": f"touch {pwned}"}}, {"result": "Exit code: 0"})],
        turns=[{"content": "go", "channel": "cli", "chat_id": "direct"}],
    )
    traces = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(traces))
    _spans._store = None
    try:
        report = await run_replay(bundle, mode="warn")
    finally:
        _spans._store = None

    assert report.complete
    assert (report.turns_replayed, report.turns_recorded) == (1, 1)
    assert (report.llm_calls_replayed, report.llm_calls_recorded) == (2, 2)
    assert (report.tool_calls_replayed, report.tool_calls_recorded) == (1, 1)
    assert report.replies == ["done"]
    # The report exposes every live request for regression assertions.
    assert len(report.llm_requests) == 2
    assert report.llm_requests[0]["messages"][-1]["content"].endswith("go")
    assert report.tool_requests == [{"name": "exec", "params": {"command": f"touch {pwned}"}}]
    assert not pwned.exists(), "the recorded exec call must not run"
    assert not (traces / "logs" / "audit-spans.log").exists(), "a replay run must not emit spans"
    from raven.tracing import trace

    assert trace.enabled(), "suppression must not outlive the replay"


async def test_run_replay_flags_unconsumed_recording(tmp_path) -> None:
    """A harness that never asks for the rest of the recording diverged too:
    strict halts (exit path), warn records a non-fatal divergence."""

    def bundle_with_extra_call(root):
        return _make_bundle(
            root,
            llm_calls=[
                (None, _llm_output(content="done")),
                (None, _llm_output(content="never requested")),
            ],
            tool_calls=[({"name": "ghost", "params": {}}, {"result": "never requested"})],
            turns=[{"content": "go", "channel": "cli", "chat_id": "direct"}],
        )

    warn = await run_replay(bundle_with_extra_call(tmp_path / "w"), mode="warn")
    assert warn.complete
    assert {(d.kind, d.field, d.fatal) for d in warn.divergences} == {
        ("llm", "unconsumed", False),
        ("tool", "unconsumed", False),
    }

    strict = await run_replay(bundle_with_extra_call(tmp_path / "s"), mode="strict")
    assert strict.halted
    assert not strict.complete
    # First-divergence contract: the audit stops at the first fatal record,
    # so the leftover tool call is not reported as a second "first" divergence.
    assert [(d.kind, d.field, d.fatal) for d in strict.divergences] == [("llm", "unconsumed", True)]


async def test_run_replay_requires_recorded_turns(tmp_path) -> None:
    bundle = _make_bundle(tmp_path, llm_calls=[(None, _llm_output(content="x"))])
    with pytest.raises(ValueError, match="no recorded turn inputs"):
        await run_replay(bundle)


async def test_run_replay_rejects_unknown_mode(tmp_path) -> None:
    bundle = _make_bundle(tmp_path, turns=[{"content": "go", "channel": "cli", "chat_id": "d"}])
    with pytest.raises(ValueError, match="mode"):
        await run_replay(bundle, mode="loose")


# ── pre-attempt session history ────────────────────────────────────────


def _write_session(bundle: Path, records: list[dict]) -> None:
    lines = [json.dumps({"_type": "metadata", "key": "cli:replay-test"})]
    lines += [json.dumps(r) for r in records]
    (bundle / "session.jsonl").write_text("".join(x + "\n" for x in lines), encoding="utf-8")


async def test_pre_attempt_messages_cut_at_first_turn_content(tmp_path) -> None:
    from raven.trajectory.replay import _pre_attempt_messages

    bundle = _make_bundle(tmp_path, turns=[{"content": "go", "channel": "cli", "chat_id": "d"}])
    _write_session(
        bundle,
        [
            {"role": "user", "content": "earlier question", "timestamp": "2026-08-19T10:00:00"},
            {"role": "assistant", "content": "earlier answer", "timestamp": "2026-08-19T10:00:05"},
            {"role": "user", "content": "go", "timestamp": "2026-08-20T10:00:00"},
            {"role": "assistant", "content": "attempt answer", "timestamp": "2026-08-20T10:00:05"},
        ],
    )

    pre = _pre_attempt_messages(load_recording(bundle))

    assert [m["content"] for m in pre] == ["earlier question", "earlier answer"]


async def test_pre_attempt_messages_timestamp_fallback_and_no_anchor(tmp_path) -> None:
    from raven.trajectory.replay import _pre_attempt_messages

    bundle = _make_bundle(tmp_path, turns=[{"content": "go", "channel": "cli", "chat_id": "d"}])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["time_range"] = {"start": "2026-08-20T02:00:00+00:00"}
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # The stored user content differs from the turn input (no content match),
    # so the cut falls back to the attempt's start time.
    _write_session(
        bundle,
        [
            {"role": "user", "content": "earlier", "timestamp": "2026-08-19T10:00:00+00:00"},
            {"role": "user", "content": "inside the attempt", "timestamp": "2026-08-20T02:00:01+00:00"},
        ],
    )
    pre = _pre_attempt_messages(load_recording(bundle))
    assert [m["content"] for m in pre] == ["earlier"]

    # No content match and no usable timestamps -> seed nothing rather than
    # risk double-loading attempt messages.
    _write_session(bundle, [{"role": "user", "content": "unknowable"}])
    manifest.pop("time_range")
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert _pre_attempt_messages(load_recording(bundle)) == []


async def test_pre_attempt_messages_repeated_input_cuts_at_the_attempts_own_turn(tmp_path) -> None:
    """A user who said the same thing before the attempt (a bare "go") must not
    drag the cut to that earlier occurrence: the time window locates the
    attempt's own opening message, and everything before it is seeded."""
    from raven.trajectory.replay import _pre_attempt_messages

    bundle = _make_bundle(tmp_path, turns=[{"content": "go", "channel": "cli", "chat_id": "d"}])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["time_range"] = {"start": "2026-08-20T10:00:00+00:00", "end": "2026-08-20T10:01:00+00:00"}
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_session(
        bundle,
        [
            {"role": "user", "content": "go", "timestamp": "2026-08-19T09:00:00+00:00"},
            {"role": "assistant", "content": "old answer", "timestamp": "2026-08-19T09:00:05+00:00"},
            {"role": "user", "content": "go", "timestamp": "2026-08-20T10:00:30+00:00"},
            {"role": "assistant", "content": "result", "timestamp": "2026-08-20T10:00:35+00:00"},
        ],
    )

    pre = _pre_attempt_messages(load_recording(bundle))

    assert [m["content"] for m in pre] == ["go", "old answer"]


async def test_pre_attempt_messages_never_preloads_future_when_unlocatable(tmp_path) -> None:
    """Untimestamped legacy rows plus a transformed first input leave no
    reliable anchor: seed nothing — never the whole file (which would preload
    attempt-internal and post-attempt messages)."""
    from raven.trajectory.replay import _pre_attempt_messages

    bundle = _make_bundle(tmp_path, turns=[{"content": "go", "channel": "cli", "chat_id": "d"}])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["time_range"] = {"start": "2026-08-20T10:00:00+00:00"}
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_session(
        bundle,
        [
            {"role": "user", "content": "old question", "timestamp": "2026-08-19T09:00:00+00:00"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current but transformed", "timestamp": "2026-08-20T10:00:30+00:00"},
            {"role": "assistant", "content": "future attempt answer", "timestamp": "2026-08-20T10:00:35+00:00"},
        ],
    )
    assert _pre_attempt_messages(load_recording(bundle)) == []

    # A repeated input without any usable time anchor is ambiguous too.
    manifest.pop("time_range")
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_session(
        bundle,
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "go"},
        ],
    )
    assert _pre_attempt_messages(load_recording(bundle)) == []


# ── record → save → replay round trip through the real tracer ─────────


class _ScriptedProvider(LLMProvider):
    """Calls the marker tool once, then answers with a final text."""

    def __init__(self):
        super().__init__(api_key="test")
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="marker", arguments={"note": "hi"})],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="all done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


class _MarkerTool:
    name = "marker"
    description = "leaves a marker file"
    parameters = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}

    def __init__(self, marker: Path):
        self._marker = marker

    def to_schema(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def cast_params(self, params):
        return params

    def validate_params(self, params):
        return []

    timeout_seconds = None
    blocking_interaction = False
    truncation_hint = None
    incomplete_hint = None

    async def execute(self, note: str) -> str:
        self._marker.write_text(note, encoding="utf-8")
        return f"marker done: {note}"


async def test_end_to_end_record_save_replay_with_real_tracer(tmp_path, monkeypatch) -> None:
    """Record a real turn (real tracer, real ToolRegistry, side-effecting tool)
    → save the bundle → replay it strict. The replay reproduces the recorded
    reply with zero divergence, re-runs no tool, and emits no new spans."""
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest
    from raven.tracing import spans as _spans
    from raven.trajectory import store as tstore
    from raven.trajectory.bundle import collect_bundle

    traces = tmp_path / "traces"
    marker = tmp_path / "marker"
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(traces))
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    _spans._store = None
    try:
        loop = AgentLoop(
            provider=_ScriptedProvider(),
            workspace=tmp_path / "ws",
            model="stub",
            max_iterations=5,
            restrict_to_workspace=True,
        )
        loop.tools.register(_MarkerTool(marker))
        # Source identity and session key agree, as they do in every real
        # channel turn (the session key defaults to "<channel>:<chat_id>").
        result = await loop._process_message(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="e2e-replay", sender_id="user", chat_type=ChatType.DM),
                text="go",
            ),
            session_key="cli:e2e-replay",
        )
        assert result is not None and result[0] == "all done"
        assert marker.read_text(encoding="utf-8") == "hi", "recording must have run the real tool"

        attempt_id = next(iter(tstore.iter_spans(traces)))["attributes"]["attempt.id"]
        bundle = collect_bundle(attempt_id, state_dir=traces)
        marker.unlink()
        spans_before = (traces / "logs" / "audit-spans.log").read_text(encoding="utf-8")

        report = await run_replay(bundle, mode="strict")

        assert report.complete, [d.render() for d in report.divergences]
        assert report.divergences == []
        assert report.replies == ["all done"], "the replayed reply must match the recorded one"
        assert (report.llm_calls_replayed, report.llm_calls_recorded) == (2, 2)
        assert (report.tool_calls_replayed, report.tool_calls_recorded) == (1, 1)
        assert not marker.exists(), "replay must not re-run the marker tool"
        spans_after = (traces / "logs" / "audit-spans.log").read_text(encoding="utf-8")
        assert spans_after == spans_before, "a replay run must not append spans to the live store"
    finally:
        _spans._store = None


async def test_end_to_end_replay_after_harness_change_diverges(tmp_path, monkeypatch) -> None:
    """Same round trip, but the replayed turn input differs from the recording
    (standing in for changed harness behavior): strict mode halts at model
    call #1 and names the mismatching message."""
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest
    from raven.tracing import spans as _spans
    from raven.trajectory import store as tstore
    from raven.trajectory.bundle import collect_bundle

    traces = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(traces))
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    _spans._store = None
    try:
        loop = AgentLoop(
            provider=_ScriptedProvider(),
            workspace=tmp_path / "ws",
            model="stub",
            max_iterations=5,
            restrict_to_workspace=True,
        )
        loop.tools.register(_MarkerTool(tmp_path / "marker"))
        await loop._process_message(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="e2e-div", sender_id="user", chat_type=ChatType.DM),
                text="go",
            ),
            session_key="cli:e2e-div",
        )
        attempt_id = next(iter(tstore.iter_spans(traces)))["attributes"]["attempt.id"]
        bundle = collect_bundle(attempt_id, state_dir=traces)

        # Rewrite the recorded turn input so the live request diverges.
        for path in (bundle / "artifacts").iterdir():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("content") == "go" and "channel" in payload:
                payload["content"] = "go somewhere else"
                path.write_text(json.dumps(payload), encoding="utf-8")

        report = await run_replay(bundle, mode="strict")

        assert report.halted
        assert report.llm_calls_replayed == 0
        div = report.divergences[0]
        assert div.kind == "llm" and div.index == 0 and div.fatal
    finally:
        _spans._store = None


async def test_end_to_end_streamed_recording_replays_through_the_stream_path(tmp_path, monkeypatch) -> None:
    """A turn recorded on the streaming path (llm.stream=true) replays through
    ``_llm_call_stream`` — the stream aggregation itself re-runs — with zero
    divergence, and the report says how many calls streamed."""
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest
    from raven.tracing import spans as _spans
    from raven.trajectory import store as tstore
    from raven.trajectory.bundle import collect_bundle

    traces = tmp_path / "traces"
    marker = tmp_path / "marker"
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(traces))
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    _spans._store = None
    try:
        loop = AgentLoop(
            provider=_ScriptedProvider(),
            workspace=tmp_path / "ws",
            model="stub",
            max_iterations=5,
            restrict_to_workspace=True,
        )
        loop.tools.register(_MarkerTool(marker))
        streamed_tokens: list[str] = []

        async def collect(text: str) -> None:
            streamed_tokens.append(text)

        result = await loop._process_message(
            TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="e2e-stream", sender_id="user", chat_type=ChatType.DM),
                text="go",
            ),
            session_key="cli:e2e-stream",
            on_token_delta=collect,
        )
        assert result is not None and result[0] == "all done"
        recorded_spans = list(tstore.iter_spans(traces))
        assert any(s["attributes"].get("llm.stream") for s in recorded_spans), "recording must be streamed"

        attempt_id = recorded_spans[0]["attributes"]["attempt.id"]
        bundle = collect_bundle(attempt_id, state_dir=traces)
        marker.unlink()

        report = await run_replay(bundle, mode="strict")

        assert report.complete, [d.render() for d in report.divergences]
        assert report.divergences == []
        assert report.llm_calls_streamed == report.llm_calls_replayed == 2
        assert report.replies == ["all done"]
        assert not marker.exists(), "replay must not re-run the marker tool"
    finally:
        _spans._store = None


class _SequenceProvider(LLMProvider):
    """Answers each chat call with the next scripted response."""

    def __init__(self, responses):
        super().__init__(api_key="test")
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        response = self._responses[self.calls]
        self.calls += 1
        return response

    def get_default_model(self) -> str:
        return "stub"


async def test_end_to_end_replay_restores_pre_attempt_history(tmp_path, monkeypatch) -> None:
    """An attempt recorded mid-conversation carries the earlier turns as
    request history; the replay reseeds them from the bundle's session.jsonl,
    so a strict replay of just that attempt still matches — and the attempt's
    own messages are not double-loaded."""
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest
    from raven.tracing import spans as _spans
    from raven.trajectory import store as tstore
    from raven.trajectory.bundle import collect_bundle

    traces = tmp_path / "traces"
    marker = tmp_path / "marker"
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(traces))
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    _spans._store = None
    try:
        loop = AgentLoop(
            provider=_SequenceProvider(
                [
                    LLMResponse(content="ack first", finish_reason="stop"),
                    LLMResponse(
                        content="",
                        tool_calls=[ToolCallRequest(id="c1", name="marker", arguments={"note": "hi"})],
                        finish_reason="tool_calls",
                    ),
                    LLMResponse(content="all done", finish_reason="stop"),
                ]
            ),
            workspace=tmp_path / "ws",
            model="stub",
            max_iterations=5,
            restrict_to_workspace=True,
        )
        loop.tools.register(_MarkerTool(marker))

        def req(text: str) -> TurnRequest:
            return TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="e2e-hist", sender_id="user", chat_type=ChatType.DM),
                text=text,
            )

        await loop._process_message(req("first question"), session_key="cli:e2e-hist")
        await loop._process_message(req("now do the task"), session_key="cli:e2e-hist")

        # The second turn is its own single-turn attempt; bundle only it.
        last_span = list(tstore.iter_spans(traces))[-1]
        bundle = collect_bundle(last_span["attributes"]["attempt.id"], state_dir=traces)
        assert (bundle / "session.jsonl").is_file()
        marker.unlink()

        report = await run_replay(bundle, mode="strict")

        assert report.complete, [d.render() for d in report.divergences]
        assert report.divergences == []
        assert report.turns_recorded == 1, "the bundle must hold only the second turn"
        assert report.replies == ["all done"]
        assert not marker.exists()
    finally:
        _spans._store = None


async def test_end_to_end_replay_restores_history_when_first_input_repeats(tmp_path, monkeypatch) -> None:
    """The attempt's opening message repeats an earlier turn verbatim (a bare
    "go" twice): the history cut must land on the attempt's own occurrence,
    so a strict replay still reproduces the recording with zero divergence."""
    from raven.agent.loop import AgentLoop
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest
    from raven.tracing import spans as _spans
    from raven.trajectory import store as tstore
    from raven.trajectory.bundle import collect_bundle

    traces = tmp_path / "traces"
    marker = tmp_path / "marker"
    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(traces))
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    _spans._store = None
    try:
        loop = AgentLoop(
            provider=_SequenceProvider(
                [
                    LLMResponse(content="ack first", finish_reason="stop"),
                    LLMResponse(
                        content="",
                        tool_calls=[ToolCallRequest(id="c1", name="marker", arguments={"note": "hi"})],
                        finish_reason="tool_calls",
                    ),
                    LLMResponse(content="all done", finish_reason="stop"),
                ]
            ),
            workspace=tmp_path / "ws",
            model="stub",
            max_iterations=5,
            restrict_to_workspace=True,
        )
        loop.tools.register(_MarkerTool(marker))

        def req() -> TurnRequest:
            return TurnRequest(
                origin=Origin.USER,
                source=Source(channel="cli", chat_id="e2e-rep", sender_id="user", chat_type=ChatType.DM),
                text="go",
            )

        await loop._process_message(req(), session_key="cli:e2e-rep")
        await loop._process_message(req(), session_key="cli:e2e-rep")

        last_span = list(tstore.iter_spans(traces))[-1]
        bundle = collect_bundle(last_span["attributes"]["attempt.id"], state_dir=traces)
        marker.unlink()

        report = await run_replay(bundle, mode="strict")

        assert report.complete, [d.render() for d in report.divergences]
        assert report.divergences == []
        assert report.replies == ["all done"]
        assert not marker.exists()
    finally:
        _spans._store = None
