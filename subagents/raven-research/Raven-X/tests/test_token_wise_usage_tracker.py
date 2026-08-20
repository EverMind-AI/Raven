"""Tests for raven.token_wise.usage_tracker.UsageTracker."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from raven.token_wise.base import UsageSnapshot
from raven.token_wise.usage_tracker import UsageTracker


def _snap(model="anthropic/claude-sonnet-4-5", session_key="sess1", **kwargs) -> UsageSnapshot:
    return UsageSnapshot(model=model, session_key=session_key, **kwargs)


async def test_accumulates_across_multiple_calls(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(input_tokens=100, output_tokens=50, estimated_cost_usd=0.001))
    await tracker.after_llm_call({}, _snap(input_tokens=200, output_tokens=75, estimated_cost_usd=0.002))

    snap = tracker.snapshot("sess1")
    assert snap.input_tokens == 300
    assert snap.output_tokens == 125
    assert snap.estimated_cost_usd == pytest.approx(0.003, rel=1e-6)


async def test_per_session_separation(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(session_key="A", input_tokens=10))
    await tracker.after_llm_call({}, _snap(session_key="B", input_tokens=20))
    await tracker.after_llm_call({}, _snap(session_key="A", input_tokens=5))

    assert tracker.snapshot("A").input_tokens == 15
    assert tracker.snapshot("B").input_tokens == 20


async def test_total_includes_all_sessions(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(session_key="A", input_tokens=10, estimated_cost_usd=0.5))
    await tracker.after_llm_call({}, _snap(session_key="B", input_tokens=20, estimated_cost_usd=1.5))
    total = tracker.snapshot()
    assert total.input_tokens == 30
    assert total.estimated_cost_usd == pytest.approx(2.0)


async def test_per_day_bucketing(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(input_tokens=100))
    today_acc = tracker.per_day[date.today()]
    assert today_acc.input_tokens == 100


async def test_persists_jsonl_to_disk(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, flush_every=1)
    await tracker.after_llm_call({}, _snap(input_tokens=42, output_tokens=7))
    path = tmp_path / f"usage-{date.today().isoformat()}.jsonl"
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["input_tokens"] == 42
    assert row["output_tokens"] == 7
    assert row["model"] == "anthropic/claude-sonnet-4-5"
    assert "ts" in row


async def test_buffered_flush_respects_flush_every(tmp_path: Path):
    """flush_every=3 should write nothing on calls 1 and 2, then flush all 3 on call 3."""
    tracker = UsageTracker(telemetry_dir=tmp_path, flush_every=3)
    path = tmp_path / f"usage-{date.today().isoformat()}.jsonl"

    await tracker.after_llm_call({}, _snap(input_tokens=1))
    await tracker.after_llm_call({}, _snap(input_tokens=2))
    assert not path.exists()

    await tracker.after_llm_call({}, _snap(input_tokens=3))
    assert path.exists()
    rows = path.read_text().splitlines()
    assert len(rows) == 3


async def test_close_flushes_remaining_buffer(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, flush_every=10)
    await tracker.after_llm_call({}, _snap(input_tokens=1))
    await tracker.after_llm_call({}, _snap(input_tokens=2))

    path = tmp_path / f"usage-{date.today().isoformat()}.jsonl"
    assert not path.exists()

    tracker.close()
    assert path.exists()
    assert len(path.read_text().splitlines()) == 2


async def test_disk_failure_does_not_crash(tmp_path: Path, caplog):
    """If the telemetry dir is unwritable, the tracker should warn and continue."""
    # Point telemetry at a path under a regular file (so mkdir fails cleanly).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    tracker = UsageTracker(telemetry_dir=blocker / "telemetry", flush_every=1)

    # Must not raise.
    await tracker.after_llm_call({}, _snap(input_tokens=1))
    # In-memory accumulator still works.
    assert tracker.snapshot().input_tokens == 1


async def test_persist_false_skips_disk_writes(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(input_tokens=99))
    assert not list(tmp_path.glob("*.jsonl"))
    # Accumulator still updated.
    assert tracker.snapshot().input_tokens == 99


async def test_tracker_is_no_op_in_before_hook(tmp_path: Path):
    """before_llm_call inherits the default pass-through; the tracker must not modify input."""
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function"}]
    out_msgs, out_tools, out_model = await tracker.before_llm_call(msgs, tools, "m")
    assert out_msgs is msgs
    assert out_tools is tools
    assert out_model == "m"


async def test_cache_tokens_accumulate(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(cache_read_tokens=1000, cache_write_tokens=200))
    await tracker.after_llm_call({}, _snap(cache_read_tokens=500, cache_write_tokens=0))
    snap = tracker.snapshot("sess1")
    assert snap.cache_read_tokens == 1500
    assert snap.cache_write_tokens == 200


async def test_snapshot_returns_copy_not_internal_reference(tmp_path: Path):
    """Mutating the returned snapshot must not affect the tracker's internal state."""
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(input_tokens=10))
    snap = tracker.snapshot("sess1")
    snap.input_tokens = 99999
    snap_again = tracker.snapshot("sess1")
    assert snap_again.input_tokens == 10
