"""Tests for the cassette minimizer (``raven/trajectory/cassette.py``).

Covers the minimize round trip (a cassette replays identically to its source
bundle), junk-span/field dropping, the never-truncate payload policy, the
session slice (including the full-copy fallback when the slice would reseed
differently), redaction of the result, and replayability validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.trajectory.cassette import minimize_bundle
from raven.trajectory.redact import KnownSecret
from raven.trajectory.replay import _pre_attempt_messages, load_recording, run_replay

pytestmark = pytest.mark.asyncio


# ── bundle fixture helpers ─────────────────────────────────────────────


def _write_artifact(bundle: Path, name: str, payload) -> str:
    (bundle / "artifacts").mkdir(parents=True, exist_ok=True)
    rel = f"artifacts/{name}"
    (bundle / rel).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return rel


def _span(name: str, span_id: str, attrs: dict) -> dict:
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": "trace-c",
        "spanId": span_id,
        "name": name,
        "startTime": "2026-08-20T10:00:00+00:00",
        "endTime": "2026-08-20T10:00:01+00:00",
        "status": {"code": "OK"},
        "attributes": {"attempt.id": "trace-c", "session.key": "cli:cassette-test", **attrs},
    }


def _llm_input(user_content: str = "go") -> dict:
    return {
        "model": "stub",
        "messages": [
            {"role": "system", "content": "machine-specific prompt: /home/recorder/ws " + "x" * 4000},
            {"role": "user", "content": user_content},
        ],
        "tools": [{"type": "function", "function": {"name": "marker", "parameters": {"type": "object"}}}],
        "temperature": 0.7,
        "max_tokens": 4096,
    }


def _llm_output(content: str = "done") -> dict:
    return {
        "content": content,
        "finish_reason": "stop",
        "tool_calls": [],
        "usage": {"prompt_tokens": 3},
        "reasoning_content": None,
        "provider_request_id": "req-123",
    }


def _make_bundle(root: Path, *, time_range: dict | None = None, tool_result: str = "Exit code: 0") -> Path:
    """One turn, one llm call, one tool call, plus junk replay never consumes:
    a memory span, an unreferenced artifact, and span envelope extras."""
    bundle = root / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    spans = [
        _span(
            "session.turn",
            "turn-0",
            {
                "turn.input.artifact_path": _write_artifact(
                    bundle, "turn-in-0.json", {"content": "go", "channel": "cli", "chat_id": "d", "media": []}
                ),
                "turn.in_progress": True,
            },
        ),
        _span(
            "llm.call",
            "llm-0",
            {
                "llm.input.artifact_path": _write_artifact(bundle, "llm-in-0.json", _llm_input()),
                "llm.output.artifact_path": _write_artifact(bundle, "llm-out-0.json", _llm_output()),
                "llm.duration_ms": 1234,
            },
        ),
        _span(
            "tool.call",
            "tool-0",
            {
                "tool.input.artifact_path": _write_artifact(
                    bundle, "tool-in-0.json", {"name": "exec", "params": {"command": "ls"}, "caller": "loop"}
                ),
                "tool.output.artifact_path": _write_artifact(
                    bundle, "tool-out-0.json", {"result": tool_result, "display": "rich stuff"}
                ),
            },
        ),
        _span(
            "memory.recall",
            "mem-0",
            {"memory.query.artifact_path": _write_artifact(bundle, "mem-0.json", {"junk": "y" * 8000})},
        ),
        _span(
            "session.turn",
            "turn-0",
            {"turn.input.artifact_path": "artifacts/turn-in-0.json", "turn.in_progress": False},
        ),
    ]
    (bundle / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    manifest = {
        "format_version": 1,
        "attempt_id": "trace-c",
        "session_key": "cli:cassette-test",
        "time_range": time_range or {"start": "2026-08-20T10:00:00+00:00", "end": "2026-08-20T10:00:01+00:00"},
        "raven_version": "0.0-test",
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "verdicts.jsonl").write_text(
        json.dumps({"attempt_id": "trace-c", "status": "fail", "source": "user"}) + "\n", encoding="utf-8"
    )
    return bundle


def _write_session(bundle: Path, records: list[dict]) -> None:
    lines = [json.dumps({"_type": "metadata", "key": "cli:cassette-test"})]
    lines += [json.dumps(r) for r in records]
    (bundle / "session.jsonl").write_text("".join(x + "\n" for x in lines), encoding="utf-8")


# ── minimization surface ───────────────────────────────────────────────


async def test_minimize_keeps_only_the_replay_surface(tmp_path) -> None:
    """Junk spans/artifacts/fields are dropped; the recording loaded from the
    cassette carries the same calls and turns as the original."""
    bundle = _make_bundle(tmp_path)
    report = minimize_bundle(bundle, tmp_path / "cassette", secrets=[])

    original, cassette = load_recording(bundle), load_recording(tmp_path / "cassette")
    assert len(cassette.llm_calls) == len(original.llm_calls) == 1
    assert len(cassette.tool_calls) == len(original.tool_calls) == 1
    assert cassette.llm_calls[0].output == {
        k: v for k, v in original.llm_calls[0].output.items() if k != "provider_request_id"
    }
    assert cassette.tool_calls[0].result == original.tool_calls[0].result
    assert [t.content for t in cassette.turns] == ["go"]
    assert cassette.turns[0].session_key == "cli:cassette-test"

    # 5 records, but the turn's checkpoint+close dedupe to one span: 4 source spans.
    assert report.span_count == 3 and report.source_span_count == 4
    spans = [json.loads(x) for x in (tmp_path / "cassette" / "spans.jsonl").read_text().splitlines()]
    assert {s["name"] for s in spans} == {"session.turn", "llm.call", "tool.call"}
    assert all("startTime" not in s and "status" not in s for s in spans)
    assert not (tmp_path / "cassette" / "artifacts" / "mem-0.json").exists()
    assert not (tmp_path / "cassette" / "verdicts.jsonl").exists()

    llm_in = json.loads((tmp_path / "cassette" / "artifacts" / "llm-in-0.json").read_text())
    assert set(llm_in) == {"model", "messages", "tools"}, "non-consumed request fields must be dropped"
    assert llm_in["messages"][0] == {"role": "system", "content": "<system prompt: not compared>"}
    assert llm_in["messages"][1] == {"role": "user", "content": "go"}, "non-system messages stay verbatim"
    tool_in = json.loads((tmp_path / "cassette" / "artifacts" / "tool-in-0.json").read_text())
    assert set(tool_in) == {"name", "params"}

    manifest = json.loads((tmp_path / "cassette" / "manifest.json").read_text())
    assert manifest["attempt_id"] == "trace-c"
    assert manifest["minimized"] == {
        "source_span_count": 4,
        "span_count": 3,
        "artifact_count": 5,
        "llm_calls": 1,
        "tool_calls": 1,
        "turns": 1,
        "session": "omitted",
    }
    assert report.cassette_bytes < report.original_bytes


async def test_minimize_round_trip_replays_identically(tmp_path) -> None:
    """The regression contract: replaying the cassette gives the same result
    as replaying the source bundle."""
    bundle = _make_bundle(tmp_path)
    minimize_bundle(bundle, tmp_path / "cassette", secrets=[])

    original = await run_replay(bundle, mode="warn")
    cassette = await run_replay(tmp_path / "cassette", mode="warn")

    assert (cassette.complete, cassette.replies) == (original.complete, original.replies)
    assert [d.render() for d in cassette.divergences] == [d.render() for d in original.divergences]
    assert (cassette.llm_calls_replayed, cassette.tool_calls_replayed) == (
        original.llm_calls_replayed,
        original.tool_calls_replayed,
    )


async def test_minimize_never_truncates_payloads(tmp_path) -> None:
    """A huge recorded payload survives whole — truncation would silently
    change what the divergence comparison sees."""
    long_result = "line\n" * 40_000
    bundle = _make_bundle(tmp_path, tool_result=long_result)

    minimize_bundle(bundle, tmp_path / "cassette", secrets=[])

    assert load_recording(tmp_path / "cassette").tool_calls[0].result == long_result


async def test_minimize_redacts_the_cassette_and_keeps_the_original(tmp_path) -> None:
    secret = "sk-cassette-fake-9f8e7d6c5b4a3f2e1d0c"
    bundle = _make_bundle(tmp_path, tool_result=f"token={secret}")

    report = minimize_bundle(
        bundle, tmp_path / "cassette", secrets=[KnownSecret("config.providers.stub.api_key", secret)]
    )

    cassette_text = (tmp_path / "cassette" / "artifacts" / "tool-out-0.json").read_text()
    assert secret not in cassette_text
    assert "[REDACTED:config.providers.stub.api_key]" in cassette_text
    assert (tmp_path / "cassette" / "redaction.json").is_file()
    assert secret in (bundle / "artifacts" / "tool-out-0.json").read_text(), "the source bundle is untouched"
    assert sum(report.redaction.exact.values()) >= 1


async def test_minimize_replaces_a_stale_destination(tmp_path) -> None:
    """Re-minimizing over a previous cassette swaps it whole (no stale files);
    an empty directory is also acceptable as the destination."""
    bundle = _make_bundle(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    minimize_bundle(bundle, empty, secrets=[])
    assert (empty / "manifest.json").is_file()

    dest = tmp_path / "cassette"
    minimize_bundle(bundle, dest, secrets=[])
    (dest / "stale.txt").write_text("old", encoding="utf-8")

    minimize_bundle(bundle, dest, secrets=[])

    assert not (dest / "stale.txt").exists()
    assert (dest / "manifest.json").is_file()


async def test_minimize_never_deletes_a_non_cassette_destination(tmp_path) -> None:
    """The swap-in step must not become an `rm -rf` on an arbitrary directory:
    an existing destination that is not an (empty dir or) old cassette is
    refused, and so is any destination containing the source bundle."""
    bundle = _make_bundle(tmp_path)
    precious = tmp_path / "workdir"
    precious.mkdir()
    (precious / "notes.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(ValueError, match="not a cassette; refusing to delete"):
        minimize_bundle(bundle, precious, secrets=[])
    assert (precious / "notes.txt").read_text(encoding="utf-8") == "keep me"

    with pytest.raises(ValueError, match="must not contain the source bundle"):
        minimize_bundle(bundle, tmp_path, secrets=[])
    assert (bundle / "manifest.json").is_file(), "the source bundle must survive"

    with pytest.raises(ValueError, match="must be outside the source bundle"):
        minimize_bundle(bundle, bundle / "cassette", secrets=[])


async def test_minimize_rejects_escaping_artifact_references(tmp_path) -> None:
    """A span's artifact reference is recorded data: one pointing outside
    artifacts/ must fail the pack, not read foreign files into the cassette
    or write outside the staging tree. The escaping targets exist (the real
    attack exfiltrates a live file), so only the reference check stops them."""
    cases = [("case-updir", "../evil.json"), ("case-dotdot", "artifacts/../evil.json")]
    for name, ref in cases:
        bundle = _make_bundle(tmp_path / name)
        evil = (bundle / ref).resolve()
        evil.write_text(json.dumps({"content": "foreign secret", "channel": "cli", "chat_id": "d"}), encoding="utf-8")
        spans = [json.loads(x) for x in (bundle / "spans.jsonl").read_text().splitlines()]
        for span in spans:
            if span["spanId"] == "turn-0":
                span["attributes"]["turn.input.artifact_path"] = ref
        (bundle / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")

        with pytest.raises(ValueError, match="escapes the bundle"):
            minimize_bundle(bundle, bundle.parent / "cassette", secrets=[])
        assert not (bundle.parent / "cassette").exists()
        assert "foreign secret" in evil.read_text(encoding="utf-8"), "the outside file must be untouched"


async def test_minimize_rejects_symlinked_artifact_escaping_the_bundle(tmp_path) -> None:
    outside = tmp_path / "host-secret.json"
    outside.write_text(json.dumps({"result": "host secret"}), encoding="utf-8")
    bundle = _make_bundle(tmp_path)
    victim = bundle / "artifacts" / "tool-out-0.json"
    victim.unlink()
    victim.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes the bundle"):
        minimize_bundle(bundle, tmp_path / "cassette", secrets=[])
    assert not (tmp_path / "cassette").exists()


async def test_minimize_rejects_unreplayable_bundles(tmp_path) -> None:
    bundle = _make_bundle(tmp_path)
    (bundle / "artifacts" / "llm-out-0.json").unlink()
    with pytest.raises(ValueError, match="not fully replayable.*llm call #1 has no llm.output"):
        minimize_bundle(bundle, tmp_path / "cassette", secrets=[])

    bundle2 = _make_bundle(tmp_path / "second")
    (bundle2 / "artifacts" / "tool-in-0.json").unlink()
    with pytest.raises(ValueError, match="tool call #1 has no tool.input"):
        minimize_bundle(bundle2, tmp_path / "cassette2", secrets=[])

    with pytest.raises(ValueError, match="not a trajectory bundle"):
        minimize_bundle(tmp_path / "nowhere", tmp_path / "cassette3", secrets=[])


# ── session slice ──────────────────────────────────────────────────────


async def test_minimize_slices_session_to_history_plus_opening(tmp_path) -> None:
    """Only the pre-attempt records plus the attempt's opening row survive;
    the slice reseeds exactly what the full file did."""
    bundle = _make_bundle(tmp_path)
    _write_session(
        bundle,
        [
            {"role": "user", "content": "earlier question", "timestamp": "2026-08-19T10:00:00+00:00"},
            {"role": "assistant", "content": "earlier answer", "timestamp": "2026-08-19T10:00:05+00:00"},
            {"role": "user", "content": "go", "timestamp": "2026-08-20T10:00:00+00:00"},
            {"role": "assistant", "content": "attempt answer", "timestamp": "2026-08-20T10:00:01+00:00"},
            {"role": "user", "content": "later question", "timestamp": "2026-08-21T09:00:00+00:00"},
        ],
    )

    report = minimize_bundle(bundle, tmp_path / "cassette", secrets=[])

    assert report.session == "sliced"
    lines = [json.loads(x) for x in (tmp_path / "cassette" / "session.jsonl").read_text().splitlines()]
    assert lines[0]["_type"] == "metadata"
    assert [m.get("content") for m in lines[1:]] == ["earlier question", "earlier answer", "go"]
    original_pre = _pre_attempt_messages(load_recording(bundle))
    assert _pre_attempt_messages(load_recording(tmp_path / "cassette")) == original_pre
    assert [m["content"] for m in original_pre] == ["earlier question", "earlier answer"]


async def test_minimize_omits_session_without_usable_history(tmp_path) -> None:
    """No session file, an unlocatable cut, and an attempt-opens-the-session
    cut all yield a cassette without session.jsonl."""
    bundle = _make_bundle(tmp_path / "none")
    report = minimize_bundle(bundle, tmp_path / "none" / "cassette", secrets=[])
    assert report.session == "omitted"
    assert not (tmp_path / "none" / "cassette" / "session.jsonl").exists()

    opens = _make_bundle(tmp_path / "opens")
    _write_session(opens, [{"role": "user", "content": "go", "timestamp": "2026-08-20T10:00:00+00:00"}])
    report = minimize_bundle(opens, tmp_path / "opens" / "cassette", secrets=[])
    assert report.session == "omitted"
    assert not (tmp_path / "opens" / "cassette" / "session.jsonl").exists()

    unlocatable = _make_bundle(tmp_path / "unloc", time_range={"start": None, "end": None})
    _write_session(unlocatable, [{"role": "user", "content": "go"}, {"role": "user", "content": "go"}])
    report = minimize_bundle(unlocatable, tmp_path / "unloc" / "cassette", secrets=[])
    assert report.session == "omitted"
    assert not (tmp_path / "unloc" / "cassette" / "session.jsonl").exists()


async def test_minimize_falls_back_to_full_session_copy_when_slice_reseeds_differently(tmp_path) -> None:
    """Pathological timestamps: the original file locates the cut by unique
    content (a record after the cut has no timestamp), but the slice — fully
    timestamped and all before time_range.start — would reseed nothing. The
    fallback ships the whole file so the cassette replays like the bundle."""
    bundle = _make_bundle(tmp_path)
    _write_session(
        bundle,
        [
            {"role": "user", "content": "earlier", "timestamp": "2026-08-20T09:00:00+00:00"},
            {"role": "user", "content": "go", "timestamp": "2026-08-20T09:30:00+00:00"},
            {"role": "assistant", "content": "attempt answer"},
        ],
    )
    original_pre = _pre_attempt_messages(load_recording(bundle))
    assert [m["content"] for m in original_pre] == ["earlier"]

    report = minimize_bundle(bundle, tmp_path / "cassette", secrets=[])

    assert report.session == "copied"
    assert (tmp_path / "cassette" / "session.jsonl").read_text() == (bundle / "session.jsonl").read_text()
    assert _pre_attempt_messages(load_recording(tmp_path / "cassette")) == original_pre
