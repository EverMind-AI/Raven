"""Tests for raven.token_wise.usage_tracker.UsageTracker."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from raven.contracts.token_strategy import UsageSnapshot
from raven.token_wise.usage_tracker import UsageTracker


def _snap(model="anthropic/claude-sonnet-4-5", session_key="sess1", **kwargs) -> UsageSnapshot:
    return UsageSnapshot(model=model, session_key=session_key, **kwargs)


async def test_accumulates_across_multiple_calls(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(input_tokens=100, output_tokens=50, cost_usd=0.001))
    await tracker.after_llm_call({}, _snap(input_tokens=200, output_tokens=75, cost_usd=0.002))

    snap = tracker.snapshot("sess1")
    assert snap.input_tokens == 300
    assert snap.output_tokens == 125
    assert snap.cost_usd == pytest.approx(0.003, rel=1e-6)


async def test_per_session_separation(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(session_key="A", input_tokens=10))
    await tracker.after_llm_call({}, _snap(session_key="B", input_tokens=20))
    await tracker.after_llm_call({}, _snap(session_key="A", input_tokens=5))

    assert tracker.snapshot("A").input_tokens == 15
    assert tracker.snapshot("B").input_tokens == 20


async def test_total_includes_all_sessions(tmp_path: Path):
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    await tracker.after_llm_call({}, _snap(session_key="A", input_tokens=10, cost_usd=0.5))
    await tracker.after_llm_call({}, _snap(session_key="B", input_tokens=20, cost_usd=1.5))
    total = tracker.snapshot()
    assert total.input_tokens == 30
    assert total.cost_usd == pytest.approx(2.0)


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


async def test_a_plan_billed_call_adds_tokens_but_no_money(tmp_path: Path):
    """Summing it as zero would read as "these calls were free"."""
    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)

    await tracker.after_llm_call({}, _snap(input_tokens=100, cost_usd=None))

    snap = tracker.snapshot("sess1")
    assert snap.input_tokens == 100
    assert snap.cost_usd is None

    await tracker.after_llm_call({}, _snap(input_tokens=10, cost_usd=0.25))

    snap = tracker.snapshot("sess1")
    assert snap.input_tokens == 110
    assert snap.cost_usd == pytest.approx(0.25)


def test_a_plan_billed_model_gets_no_cost_on_its_snapshot() -> None:
    """The snapshot is where a fabricated zero would enter the pipeline.

    Everything downstream treats the field as optional -- the status bar renders
    it only when it is a number -- so collapsing None here is what would put a
    price on a subscription.
    """
    from types import SimpleNamespace

    from raven.agent.loop.main import AgentLoop

    response = SimpleNamespace(usage={"prompt_tokens": 100, "completion_tokens": 20})

    plan = AgentLoop._build_usage_snapshot(response, "github_copilot/gpt-4o", "sess1")
    metered = AgentLoop._build_usage_snapshot(response, "deepseek/deepseek-chat", "sess1")

    assert plan.input_tokens == 100
    assert plan.cost_usd is None
    assert metered.cost_usd is None


async def test_unknown_and_zero_survive_persistence_and_mixed_totals(tmp_path):
    tracker = UsageTracker(telemetry_dir=tmp_path)
    await tracker.after_llm_call({}, _snap(input_tokens=10))
    assert tracker.snapshot().cost_usd is None
    await tracker.after_llm_call({}, _snap(input_tokens=20, cost_usd=0, cache_read_tokens=0, cache_write_tokens=0))
    await tracker.after_llm_call({}, _snap(input_tokens=30, cost_usd=0.25, cache_read_tokens=15))
    total = tracker.snapshot()
    assert total.cost_usd == 0.25
    assert total.calls == 3
    assert total.cost_missing_calls == 1
    assert total.cache_read_tokens == 15
    assert total.cache_read_missing_calls == 1
    assert total.cache_write_tokens == 0
    assert total.cache_write_missing_calls == 2
    rows = [
        json.loads(line) for line in (tmp_path / f"usage-{date.today().isoformat()}.jsonl").read_text().splitlines()
    ]
    assert [row["cost_usd"] for row in rows] == [None, 0, 0.25]
    assert all(row["schema_version"] == 2 and "estimated_cost_usd" not in row for row in rows)


@pytest.mark.parametrize("includes_cache,expected", [(False, 100), (True, 70)])
def test_snapshot_uses_protocol_convention_not_token_inequality(includes_cache, expected):
    from types import SimpleNamespace

    from raven.agent.loop.main import AgentLoop
    from raven.observability.usage import normalize

    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 2,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 10,
        "prompt_tokens_include_cache": includes_cache,
    }
    snap = AgentLoop._build_usage_snapshot(SimpleNamespace(usage=usage), "model", "session")
    assert snap.input_tokens == expected
    assert normalize(usage, "model")["input_tokens"] == expected
    assert snap.cost_usd is None


async def test_concurrent_image_usage_keeps_turn_ownership(tmp_path, monkeypatch):
    import asyncio

    from raven.token_wise import usage_context

    monkeypatch.delenv("RAVEN_USAGE_ROOT_SESSION", raising=False)
    tracker = UsageTracker(telemetry_dir=tmp_path)

    async def record(key):
        with usage_context.bind(key):
            await asyncio.sleep(0)
            await tracker.after_llm_call({}, UsageSnapshot(model="image", cost_usd=0.2))

    await asyncio.gather(record("task-a"), record("task-b"))
    rows = [json.loads(line) for line in next(tmp_path.glob("usage-*.jsonl")).read_text().splitlines()]
    assert {(row["session_key"], row["root_session_key"]) for row in rows} == {
        ("task-a", "task-a"),
        ("task-b", "task-b"),
    }
    assert usage_context.session_key() is None


async def test_buffered_delegated_usage_keeps_each_sessions_owner_and_destination(tmp_path):
    import asyncio

    from raven.token_wise import usage_context

    tracker = UsageTracker(telemetry_dir=tmp_path / "local", flush_every=10)

    async def record(name):
        owner = {"root_session_key": name, "telemetry_dir": str(tmp_path / name)}
        with usage_context.bind("child-" + name, owner):
            await asyncio.sleep(0)
            with usage_context.bind("tool-" + name):
                assert usage_context.delegation()["root_session_key"] == name
                await tracker.after_llm_call({}, UsageSnapshot(model="image", cost_usd=0.1))

    await asyncio.gather(record("a"), record("b"))
    assert usage_context.root_session_key() is None
    assert usage_context.telemetry_dir() is None
    tracker.close()
    for name in ("a", "b"):
        rows = [json.loads(line) for line in next((tmp_path / name).glob("usage-*.jsonl")).read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["root_session_key"] == name
        assert rows[0]["session_key"] == "tool-" + name
        assert "_telemetry_dir" not in rows[0]
    assert not (tmp_path / "local").exists()
