"""Tests for bug report completeness evaluation (`raven.trajectory.completeness`).

The unreplayable verdict is anchored to a real warn-mode replay probe, so most
samples assert both the completeness mapping and, where the distinction
matters, the underlying replay outcome on the same tree. All tests are
synchronous: the evaluator drives its own event loop via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from raven.trajectory import completeness as tcomp
from raven.trajectory.redact import RedactionReport
from raven.trajectory.replay import load_recording, run_replay, validate_recording

# ── bundle construction ────────────────────────────────────────────────


def _write_artifact(bundle: Path, name: str, payload) -> str:
    (bundle / "artifacts").mkdir(parents=True, exist_ok=True)
    rel = f"artifacts/{name}"
    (bundle / rel).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return rel


def _llm_output(content="ok", tool_calls=(), **extra) -> dict:
    return {
        "content": content,
        "finish_reason": extra.get("finish_reason", "stop"),
        "tool_calls": list(tool_calls),
        "reasoning_content": extra.get("reasoning_content"),
        "thinking_blocks": extra.get("thinking_blocks"),
        "usage": extra.get("usage") or {},
    }


def _llm_input(messages=None, tools=None, model="m") -> dict:
    return {"model": model, "messages": messages if messages is not None else [], "tools": tools or []}


def _bundle(
    root: Path,
    *,
    turns=("hi",),
    llm_calls=((_llm_input(), _llm_output()),),
    tool_calls=(),
    session_lines=({"role": "user", "content": "hi"},),
    session_included=True,
    manifest_extra=None,
    spans_extra=(),
    stream_flags=(),
) -> Path:
    """A hand-built sanitized-tree stand-in with checkpoint+close turn records."""
    bundle = root / "tree"
    bundle.mkdir(parents=True, exist_ok=True)
    spans: list[dict] = []

    def _span(name, span_id, attrs):
        return {
            "traceId": "trace-c",
            "spanId": span_id,
            "name": name,
            "startTime": "2026-08-20T10:00:00+00:00",
            "endTime": "2026-08-20T10:00:01+00:00",
            "attributes": {"attempt.id": "trace-c", "session.key": "cli:c", **attrs},
        }

    for i, turn in enumerate(turns):
        payload = turn if isinstance(turn, (dict, list)) else {"content": turn, "channel": "cli", "chat_id": "c"}
        rel = _write_artifact(bundle, f"turn-in-{i}.json", payload)
        spans.append(_span("session.turn", f"turn-{i}", {"turn.input.artifact_path": rel, "turn.in_progress": True}))
    for i, (inp, out) in enumerate(llm_calls):
        attrs = {}
        if inp is not None:
            attrs["llm.input.artifact_path"] = _write_artifact(bundle, f"llm-in-{i}.json", inp)
        if out is not None:
            attrs["llm.output.artifact_path"] = _write_artifact(bundle, f"llm-out-{i}.json", out)
        if i in stream_flags:
            attrs["llm.stream"] = True
        spans.append(_span("llm.call", f"llm-{i}", attrs))
    for i, (inp, out) in enumerate(tool_calls):
        attrs = {}
        if inp is not None:
            attrs["tool.input.artifact_path"] = _write_artifact(bundle, f"tool-in-{i}.json", inp)
        if out is not None:
            attrs["tool.output.artifact_path"] = _write_artifact(bundle, f"tool-out-{i}.json", out)
        spans.append(_span("tool.call", f"tool-{i}", attrs))
    for i, _turn in enumerate(turns):
        spans.append(
            _span(
                "session.turn",
                f"turn-{i}",
                {"turn.input.artifact_path": f"artifacts/turn-in-{i}.json", "turn.in_progress": False},
            )
        )
    spans.extend(spans_extra)

    (bundle / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    manifest = {
        "format_version": 1,
        "attempt_id": "trace-c",
        "session_key": "cli:c",
        "session_included": session_included and session_lines is not None,
        "missing_artifacts": [],
    }
    manifest.update(manifest_extra or {})
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if session_included and session_lines is not None:
        (bundle / "session.jsonl").write_text(
            "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in session_lines),
            encoding="utf-8",
        )
    return bundle


def _evaluate(tree, redaction=None):
    return tcomp.evaluate_completeness(tree, redaction)


# ── complete / degraded ────────────────────────────────────────────────


def test_complete_sample_probes_clean(tmp_path):
    tree = _bundle(tmp_path)
    status, reasons = _evaluate(tree)
    assert (status, reasons) == ("complete", [])
    report = asyncio.run(run_replay(tree, mode="warn"))
    assert not [d for d in report.divergences if d.fatal]


def test_turn_checkpoint_and_close_do_not_degrade(tmp_path):
    tree = _bundle(tmp_path)
    status, reasons = _evaluate(tree)
    assert status == "complete", reasons


def test_missing_llm_input_degrades_but_replays(tmp_path):
    tree = _bundle(tmp_path, llm_calls=((None, _llm_output()),))
    status, reasons = _evaluate(tree)
    assert status == "degraded"
    assert any("model call input" in r for r in reasons)
    report = asyncio.run(run_replay(tree, mode="warn"))
    assert not [d for d in report.divergences if d.fatal]


def test_missing_tool_input_degrades_but_replays(tmp_path):
    tree = _bundle(
        tmp_path,
        llm_calls=(
            (_llm_input(), _llm_output(content=None, tool_calls=[{"id": "t", "name": "x", "arguments": {}}])),
            (_llm_input(), _llm_output()),
        ),
        tool_calls=((None, {"result": "done"}),),
    )
    status, reasons = _evaluate(tree)
    assert status == "degraded"
    assert any("tool call input" in r for r in reasons)
    report = asyncio.run(run_replay(tree, mode="warn"))
    assert not [d for d in report.divergences if d.fatal]


def test_partially_unloadable_turn_degrades(tmp_path):
    tree = _bundle(tmp_path, turns=("hi", "again"), llm_calls=((_llm_input(), _llm_output()),) * 2)
    (tree / "artifacts" / "turn-in-1.json").unlink()
    status, reasons = _evaluate(tree)
    assert status == "degraded"
    assert any("turn input(s) could not be loaded" in r for r in reasons)


def test_session_not_included_degrades(tmp_path):
    tree = _bundle(tmp_path, session_included=False, session_lines=None)
    status, reasons = _evaluate(tree)
    assert status == "degraded"
    assert "the session conversation record is missing" in reasons


def test_session_declared_but_absent_degrades(tmp_path):
    tree = _bundle(tmp_path, manifest_extra={"session_included": True}, session_included=False, session_lines=None)
    status, reasons = _evaluate(tree)
    assert status == "degraded"
    assert "the session conversation record is missing" in reasons


def test_unreferenced_missing_artifacts_degrade(tmp_path):
    tree = _bundle(tmp_path, manifest_extra={"missing_artifacts": ["gone.png"]})
    status, reasons = _evaluate(tree)
    assert status == "degraded"
    assert any("referenced artifact(s) are missing" in r for r in reasons)


def test_skipped_binaries_degrade(tmp_path):
    tree = _bundle(tmp_path)
    redaction = RedactionReport(bundle_dir=tree, redacted_dir=tree, skipped_binaries=["b.bin"])
    status, reasons = _evaluate(tree, redaction)
    assert status == "degraded"
    assert any("non-UTF-8" in r for r in reasons)


# ── history cut ────────────────────────────────────────────────────────


def test_cut_at_zero_is_complete(tmp_path):
    tree = _bundle(tmp_path, session_lines=({"role": "user", "content": "hi"},))
    assert _evaluate(tree)[0] == "complete"


def test_unlocatable_cut_degrades_and_matches_replay_divergence(tmp_path):
    session = (
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "earlier"},
        {"role": "user", "content": "hi"},
    )
    recorded_messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "earlier"},
        {"role": "user", "content": "hi"},
    ]
    tree = _bundle(
        tmp_path, session_lines=session, llm_calls=((_llm_input(messages=recorded_messages), _llm_output()),)
    )

    status, reasons = _evaluate(tree)

    assert status == "degraded"
    assert tcomp.REASON_CUT_UNLOCATABLE in reasons
    report = asyncio.run(run_replay(tree, mode="warn"))
    llm_divergences = [d for d in report.divergences if d.kind == "llm"]
    assert llm_divergences and not any(d.fatal for d in llm_divergences)


# ── unreplayable: probe verdicts ───────────────────────────────────────


def test_no_turns_is_unreplayable(tmp_path):
    tree = _bundle(tmp_path, turns=(), session_lines=None, session_included=False)
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("no recorded turn inputs" in r for r in reasons)
    with pytest.raises(ValueError):
        asyncio.run(run_replay(tree, mode="warn"))


def test_missing_llm_output_is_fatal(tmp_path):
    tree = _bundle(tmp_path, llm_calls=((_llm_input(), None),))
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("missing output" in r for r in reasons)


def test_tool_result_not_string_is_fatal(tmp_path):
    tree = _bundle(
        tmp_path,
        llm_calls=(
            (_llm_input(), _llm_output(content=None, tool_calls=[{"id": "t", "name": "x", "arguments": {}}])),
            (_llm_input(), _llm_output()),
        ),
        tool_calls=(({"name": "x", "params": {}}, {"result": 42}),),
    )
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("missing output" in r for r in reasons)


@pytest.mark.parametrize(
    "shape",
    ["two-turns-one-call", "tool-call-without-record", "tool-record-without-followup"],
)
def test_control_flow_exhaustion_is_unreplayable(tmp_path, shape):
    if shape == "two-turns-one-call":
        tree = _bundle(tmp_path, turns=("hi", "again"), llm_calls=((_llm_input(), _llm_output()),))
    elif shape == "tool-call-without-record":
        tree = _bundle(
            tmp_path,
            llm_calls=(
                (_llm_input(), _llm_output(content=None, tool_calls=[{"id": "t", "name": "x", "arguments": {}}])),
            ),
        )
    else:
        tree = _bundle(
            tmp_path,
            llm_calls=(
                (_llm_input(), _llm_output(content=None, tool_calls=[{"id": "t", "name": "x", "arguments": {}}])),
            ),
            tool_calls=(({"name": "x", "params": {}}, {"result": "done"}),),
        )
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("exhausted" in r for r in reasons)


@pytest.mark.parametrize("recovery", ["empty-output", "thinking-only"])
def test_empty_response_recovery_consumes_more_recording(tmp_path, recovery):
    """AgentLoop retries/prefills after an empty response — one output is not enough."""
    output = (
        _llm_output(content=None)
        if recovery == "empty-output"
        else _llm_output(content=None, reasoning_content="thinking...")
    )
    tree = _bundle(tmp_path, llm_calls=((_llm_input(), output),))
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("exhausted" in r for r in reasons)


# ── unreplayable: explicit contract validation ─────────────────────────


@pytest.mark.parametrize(
    "case",
    [
        "tool-calls-entry",
        "messages-not-list",
        "tools-not-list",
        "content-not-string",
        "stream-reasoning-not-string",
        "time-range-not-object",
        "session-key-not-string",
    ],
)
def test_bad_payload_shapes_are_unreplayable_not_unknown(tmp_path, case):
    kwargs = {}
    if case == "tool-calls-entry":
        kwargs["llm_calls"] = ((_llm_input(), {**_llm_output(), "tool_calls": ["bad"]}),)
    elif case == "messages-not-list":
        kwargs["llm_calls"] = (({"model": "m", "messages": "bad"}, _llm_output()),)
    elif case == "tools-not-list":
        kwargs["llm_calls"] = (({"model": "m", "messages": [], "tools": "bad"}, _llm_output()),)
    elif case == "content-not-string":
        kwargs["llm_calls"] = ((_llm_input(), {**_llm_output(), "content": []}),)
    elif case == "stream-reasoning-not-string":
        kwargs["llm_calls"] = ((_llm_input(), {**_llm_output(), "reasoning_content": {}}),)
        kwargs["stream_flags"] = (0,)
    elif case == "time-range-not-object":
        kwargs["manifest_extra"] = {"time_range": [1, 2]}
    elif case == "session-key-not-string":
        pass
    tree = _bundle(tmp_path, **kwargs)
    if case == "session-key-not-string":
        lines = []
        for line in (tree / "spans.jsonl").read_text(encoding="utf-8").splitlines():
            span = json.loads(line)
            span["attributes"]["session.key"] = 123
            lines.append(json.dumps(span))
        (tree / "spans.jsonl").write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    status, reasons = _evaluate(tree)

    assert status == "unreplayable"
    assert any("violates the replay contract" in r for r in reasons)
    assert validate_recording(load_recording(tree))


# ── unreplayable: static-only defects ──────────────────────────────────


def test_corrupt_span_line_is_unreplayable(tmp_path):
    tree = _bundle(tmp_path)
    with (tree / "spans.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("corrupt and could not be parsed" in r for r in reasons)


def test_corrupt_session_line_is_unreplayable(tmp_path):
    tree = _bundle(tmp_path, session_lines=({"role": "user", "content": "hi"}, "[1, 2]"))
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert "the session record contains corrupt entries" in reasons


def test_unsupported_format_version_is_unreplayable(tmp_path):
    tree = _bundle(tmp_path, manifest_extra={"format_version": 99})
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("format version" in r for r in reasons)


def test_crashing_turn_payload_is_unreplayable(tmp_path):
    tree = _bundle(tmp_path, turns=(["bad"],))
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("could not be parsed for replay" in r for r in reasons)


def test_levels_merge_keeps_both_reason_sets(tmp_path):
    tree = _bundle(tmp_path, llm_calls=((None, None),), session_included=False, session_lines=None)
    status, reasons = _evaluate(tree)
    assert status == "unreplayable"
    assert any("missing output" in r for r in reasons)
    assert "the session conversation record is missing" in reasons


# ── probe exception attribution ────────────────────────────────────────


@pytest.mark.parametrize("exc", [OSError("disk full"), RuntimeError("loop already running")])
def test_unrecognized_probe_failures_propagate_for_unknown(tmp_path, monkeypatch, exc):
    """Environment/harness failures must not masquerade as evidence damage."""
    tree = _bundle(tmp_path)

    async def _boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(tcomp, "run_replay", _boom)
    with pytest.raises(type(exc)):
        _evaluate(tree)


def test_non_fatal_divergences_do_not_degrade(tmp_path):
    tree = _bundle(
        tmp_path,
        llm_calls=((_llm_input(), _llm_output()), (_llm_input(), _llm_output(content="never asked"))),
    )
    status, reasons = _evaluate(tree)
    assert status == "complete", reasons
    report = asyncio.run(run_replay(tree, mode="warn"))
    assert any(d.field == "unconsumed" for d in report.divergences)
