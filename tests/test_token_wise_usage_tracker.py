"""Tests for raven.token_wise.usage_tracker.UsageTracker."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from raven.contracts.llm_provider import ChatDelta, GenerationSettings
from raven.contracts.token_strategy import UsageSnapshot
from raven.providers import usage_record
from raven.providers.base import LLMProvider, LLMResponse
from raven.providers.lazy import LazyProvider
from raven.token_wise import usage_context
from raven.token_wise.registry import StrategyRegistry
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


async def test_the_row_carries_the_reasoning_count_and_why_the_call_ended(tmp_path: Path):
    """The row had a reasoning_tokens column that nothing ever filled, so every
    call read as "did not think"; and no field at all for how the call ended."""
    tracker = UsageTracker(telemetry_dir=tmp_path)
    await tracker.after_llm_call(
        {"content": "", "finish_reason": "tool_calls", "usage": {}},
        _snap(input_tokens=256, output_tokens=417, reasoning_tokens=235),
    )
    await tracker.after_llm_call({}, _snap(input_tokens=10))
    tracker.close()

    rows = [
        json.loads(line) for line in (tmp_path / f"usage-{date.today().isoformat()}.jsonl").read_text().splitlines()
    ]
    assert [row["reasoning_tokens"] for row in rows] == [235, 0]
    assert [row["finish_reason"] for row in rows] == ["tool_calls", None]
    assert tracker.snapshot("sess1").reasoning_tokens == 235


async def test_a_delegated_call_bills_the_turn_that_delegated_it(tmp_path: Path):
    """The recorder is the seam: a sub-agent's call reaches the delegating
    turn's scope, a turn's own call does not (its loop bills that one, and
    billing it here would double it), and a scope that has closed -- a
    delegation outliving the turn -- bills nobody."""
    from raven.token_wise import usage_context
    from raven.token_wise.turn_spend import TurnSpend

    tracker = UsageTracker(telemetry_dir=tmp_path, persist=False)
    spend = TurnSpend("parent")
    with spend.collecting():
        with usage_context.bind("parent"):
            await tracker.after_llm_call({}, UsageSnapshot(model="parent/model", cost_usd=0.5))
        with usage_context.bind("child", {"root_session_key": "parent"}):
            await tracker.after_llm_call({}, UsageSnapshot(model="child/model", cost_usd=0.02))
            await tracker.after_llm_call({}, UsageSnapshot(model="child/model", cost_usd=None))

    assert spend.cost_usd == pytest.approx(0.02)
    assert spend.cost_missing_calls == 1

    with usage_context.bind("child", {"root_session_key": "parent"}):
        await tracker.after_llm_call({}, UsageSnapshot(model="child/model", cost_usd=0.04))
    assert spend.cost_usd == pytest.approx(0.02), "the turn had ended; its total is final"


async def test_a_turn_with_no_session_key_bills_only_its_own_calls():
    """A loop run outside a session -- no key -- is not listed as a root, so no
    delegation can name it; the calls it makes itself still count."""
    from raven.token_wise import turn_spend

    spend = turn_spend.TurnSpend(None)
    with spend.collecting() as scope:
        assert scope is spend
        turn_spend.note_delegated(None, 0.5)
        turn_spend.note_delegated("", 0.5)
        spend.note(0.01)

    assert spend.cost_usd == pytest.approx(0.01)
    assert spend.cost_missing_calls == 0


async def test_two_overlapping_turns_of_one_session_each_bill_what_ran_under_them():
    """Two turns of one session can overlap -- a relay re-entering the
    conversation it was delegated from -- so each is billed for the delegations
    that ran while it did, and the first to close leaves the other listed."""
    from raven.token_wise import turn_spend

    outer = turn_spend.TurnSpend("s1")
    inner = turn_spend.TurnSpend("s1")
    with outer.collecting():
        with inner.collecting():
            turn_spend.note_delegated("s1", 0.5)
        turn_spend.note_delegated("s1", 0.25)

    assert outer.cost_usd == pytest.approx(0.75)
    assert inner.cost_usd == pytest.approx(0.5)


async def test_delegated_usage_reads_what_a_child_process_wrote_for_its_root(tmp_path: Path):
    """The one-shot summary's second source: an ACP sub-agent records its calls
    with its own tracker, under its own session, billed to the host's root. Read
    back through the real writer, not a hand-built file, so the reader cannot
    drift from the shape the writer actually produces."""
    from datetime import datetime, timedelta, timezone

    from raven.token_wise import usage_context
    from raven.token_wise.usage_tracker import delegated_usage

    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    child = UsageTracker(telemetry_dir=tmp_path)
    owner = {"root_session_key": "cli:root", "telemetry_dir": str(tmp_path)}
    with usage_context.bind("acp:child", owner):
        await child.after_llm_call({}, UsageSnapshot(model="m", input_tokens=100, output_tokens=10, cost_usd=0.25))
        await child.after_llm_call({}, UsageSnapshot(model="m", input_tokens=50, output_tokens=5, cost_usd=None))
        await child.record_tool_call("write_file", "t1")
    host = UsageTracker(telemetry_dir=tmp_path)
    with usage_context.bind("cli:root"):
        await host.after_llm_call({}, UsageSnapshot(model="m", input_tokens=999, output_tokens=999, cost_usd=9.0))
    with usage_context.bind("acp:other", {"root_session_key": "cli:elsewhere", "telemetry_dir": str(tmp_path)}):
        await child.after_llm_call({}, UsageSnapshot(model="m", input_tokens=7, output_tokens=7, cost_usd=1.0))

    got = delegated_usage("cli:root", since, telemetry_dir=tmp_path)

    # The root's own call is the caller's to count from its own tracker, the
    # other root's call is not this conversation's, and a tool row is not a call.
    assert got.calls == 2
    assert got.input_tokens == 150
    assert got.output_tokens == 15
    assert got.cost_usd == pytest.approx(0.25)
    assert got.cost_missing_calls == 1


async def test_in_process_subagent_usage_is_written_as_delegated_usage(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from raven.agent.subagent import activity
    from raven.token_wise import usage_context
    from raven.token_wise.usage_tracker import delegated_usage

    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    owner = {"root_session_key": "cli:root", "telemetry_dir": str(tmp_path)}
    with usage_context.bind("cli:root", owner), activity.collecting() as did:
        await activity.note_provider_usage(
            {"prompt_tokens": 40, "completion_tokens": 5, "cost_usd": 0.2},
            model="provider/model",
            session_key="cli:root",
            task_id="worker-1",
        )

    got = delegated_usage("cli:root", since, telemetry_dir=tmp_path)
    assert (did.tokens_in, did.tokens_out) == (40, 5)
    assert got.calls == 1
    assert got.input_tokens == 40
    assert got.output_tokens == 5
    assert got.cost_usd == pytest.approx(0.2)


def test_delegated_usage_keeps_to_its_window_and_to_reported_prices(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from raven.token_wise.usage_tracker import delegated_usage

    now = datetime.now(timezone.utc)
    rows = [
        {"ts": (now - timedelta(hours=2)).isoformat(), "schema_version": 2, "cost_usd": 5.0},
        {"ts": (now - timedelta(minutes=1)).isoformat(), "schema_version": 2, "cost_usd": 0.5},
        # A row from before providers reported a price carries a local estimate,
        # which is not a bill.
        {"ts": (now - timedelta(minutes=1)).isoformat(), "cost_usd": 3.0},
    ]
    path = tmp_path / f"usage-{date.today().isoformat()}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            base = {"model": "m", "session_key": "acp:c", "root_session_key": "cli:r", "input_tokens": 1}
            f.write(json.dumps({**base, **row}) + "\n")
        f.write('{"ts": "cut mid-wri')

    got = delegated_usage("cli:r", now - timedelta(minutes=5), until=now, telemetry_dir=tmp_path)

    assert got.calls == 2
    assert got.cost_usd == pytest.approx(0.5)
    assert got.cost_missing_calls == 1


# ---------------------------------------------------------------------------
# The provider seam: a call made outside the turn loop is billed, once.
# ---------------------------------------------------------------------------


_REPORTED = {"prompt_tokens": 120, "completion_tokens": 30}


class _DirectProvider(LLMProvider):
    """A provider called the way the heartbeat and the sentinel call one."""

    def __init__(self, reply: LLMResponse | None = None):
        super().__init__(api_key="test")
        self.reply = reply or LLMResponse(content="ok", usage=dict(_REPORTED))
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=None, temperature=0.7, **_kwargs):
        self.calls += 1
        return self.reply

    def get_default_model(self) -> str:
        return "direct/model"


class _SplitStreamProvider(_DirectProvider):
    """Streams usage and the finish reason on separate deltas, as some wires do."""

    async def chat_stream(self, messages, tools=None, model=None, **_kwargs):
        yield ChatDelta(content="hel")
        yield ChatDelta(content="lo", usage=dict(_REPORTED))
        yield ChatDelta(content=None, finish_reason="stop")


def _listening() -> UsageTracker:
    """A tracker the seam reports to for the length of one block."""
    return UsageTracker(persist=False)


async def test_a_direct_provider_call_is_billed_once_to_the_bound_session():
    """The retry ladder calls ``chat`` inside ``chat_with_retry``; the caller got
    one answer, so the usage file gets one row, carrying what the vendor
    reported, under the session the caller bound."""
    tracker = _listening()
    provider = _DirectProvider()

    with usage_record.bind(StrategyRegistry([tracker])), usage_context.bind("heartbeat"):
        await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="direct/model")

    assert provider.calls == 1
    assert tracker.total.calls == 1
    row = tracker.snapshot("heartbeat")
    assert (row.calls, row.input_tokens, row.output_tokens) == (1, 120, 30)


async def test_a_wrapped_provider_is_billed_at_the_outer_call_only():
    """``LazyProvider`` (and the resolving and per-model wrappers) delegate to an
    inner provider whose own entry points are instrumented too."""
    tracker = _listening()
    inner = _DirectProvider()
    lazy = LazyProvider(lambda: inner, "direct/model", GenerationSettings())

    with usage_record.bind(StrategyRegistry([tracker])):
        await lazy.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert inner.calls == 1
    assert tracker.total.calls == 1


async def test_a_call_its_caller_records_is_not_billed_twice():
    """The turn loop records its own calls with the turn's session and spend;
    inside ``recorded_by_caller`` the seam stays out of it."""
    tracker = _listening()
    provider = _DirectProvider()

    with usage_record.bind(StrategyRegistry([tracker])):
        with usage_record.recorded_by_caller():
            await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])
        await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert provider.calls == 2
    assert tracker.total.calls == 1, "only the call made outside the claim"


async def test_a_stream_over_the_non_streaming_fallback_is_one_row_not_two():
    """The base ``chat_stream`` answers through ``chat``; both are instrumented,
    and the call they make is still one call."""
    tracker = _listening()
    provider = _DirectProvider()

    with usage_record.bind(StrategyRegistry([tracker])):
        deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "hi"}])]

    assert deltas and provider.calls == 1
    assert tracker.total.calls == 1
    assert tracker.total.input_tokens == 120


async def test_a_stream_whose_usage_and_finish_arrive_apart_is_one_row():
    """Neither delta alone is the end of the call. Wrapped, the stream passes
    the inner provider's deltas through, and is still recorded once."""
    tracker = _listening()
    lazy = LazyProvider(lambda: _SplitStreamProvider(), "direct/model", GenerationSettings())

    with usage_record.bind(StrategyRegistry([tracker])):
        text = "".join([d.content or "" async for d in lazy.chat_stream(messages=[{"role": "user", "content": "hi"}])])

    assert text == "hello"
    assert tracker.total.calls == 1
    assert (tracker.total.input_tokens, tracker.total.output_tokens) == (120, 30)


async def test_an_error_that_reached_no_model_is_not_a_row():
    """Nothing was spent, and a row of zeros would read as a cheap call."""
    tracker = _listening()
    provider = _DirectProvider(LLMResponse(content="Error: connection refused", finish_reason="error"))

    with usage_record.bind(StrategyRegistry([tracker])):
        await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert tracker.total.calls == 0


async def test_a_sink_that_fails_does_not_fail_the_call():
    class _Broken:
        async def after_llm_call(self, _response, _usage):
            raise RuntimeError("disk full")

        def __len__(self) -> int:  # the seam reads only this one method
            return 1

    provider = _DirectProvider()

    with usage_record.bind(_Broken()):
        response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "ok"


async def test_a_call_is_billed_to_the_registry_the_caller_bound(tmp_path: Path):
    """The assembly installs its registry as the default, for the callers the
    loop never sees; a caller that binds a registry of its own is billed there
    instead, because a generation is built while the one it replaces may still
    be serving (see the swap test below)."""
    from raven.config.raven import TokenWiseConfig
    from raven.core.token_wise_stack import install_from_config

    provider = _DirectProvider()
    await provider.chat(messages=[{"role": "user", "content": "hi"}])

    default = install_from_config(TokenWiseConfig(), telemetry_dir=tmp_path)
    await provider.chat(messages=[{"role": "user", "content": "hi"}])
    default_tracker = default.get("usage_tracker")
    assert default_tracker is not None
    assert default_tracker.total.calls == 1, "the call before assembly went nowhere; the one after is billed"

    bound = _listening()
    with usage_record.bind(StrategyRegistry([bound])):
        await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert bound.total.calls == 1, "bound: the call is billed to the bound registry"
    assert default_tracker.total.calls == 1, "and not to the default as well"
    rows = [
        json.loads(line) for line in (tmp_path / f"usage-{date.today().isoformat()}.jsonl").read_text().splitlines()
    ]
    assert [r["model"] for r in rows] == ["direct/model"]


async def test_calls_made_in_tasks_spawned_under_a_bind_count_toward_it():
    """An Action may fan out (best-of-n with ``gather``); each task copies the
    context, so a counter held by value would stay at zero in the turn and the
    loop would write its own row over calls the seam already recorded."""
    import asyncio

    tracker = _listening()
    provider = _DirectProvider()

    with usage_record.bind(StrategyRegistry([tracker])):
        await asyncio.gather(*(provider.chat(messages=[{"role": "user", "content": "hi"}]) for _ in range(3)))
        assert usage_record.recorded_inbound() == 3

    assert tracker.total.calls == 3
    assert usage_record.recorded_inbound() == 0, "the count ends with its bind"


async def test_a_registry_swap_does_not_bill_one_call_to_both_registries(tmp_path: Path):
    """Generation N+1's registry is built while N may still be serving: a
    candidate that fails to assemble leaves N running, and a forced reload can
    overlap N's shutdown grace. A process-wide sink would have N's next call
    recorded by the seam into N+1's tracker, and then -- because N's loop goes on
    recording its own row -- into N's as well, so one physical call landed in the
    usage file twice."""
    from raven.config.raven import TokenWiseConfig
    from raven.core.token_wise_stack import install_from_config

    new_registry = install_from_config(TokenWiseConfig(), telemetry_dir=tmp_path)
    old_registry = install_from_config(TokenWiseConfig(), telemetry_dir=tmp_path)
    new_tracker = new_registry.get("usage_tracker")
    old_tracker = old_registry.get("usage_tracker")
    assert new_tracker is not None and old_tracker is not None
    provider = _DirectProvider()

    # N is serving: its loop bound its own registry around the turn.
    with usage_record.bind(old_registry):
        # N+1 is built mid-flight (its registry now exists, and a process-wide
        # install would have pointed the seam at it), and N+1's build then fails,
        # so N keeps serving with its own binding still in force.
        install_from_config(TokenWiseConfig(), telemetry_dir=tmp_path)
        await provider.chat(messages=[{"role": "user", "content": "hi"}])
        assert usage_record.recorded_inbound() == 1

    assert old_tracker.total.calls == 1, "the serving generation's tracker billed the call"
    assert new_tracker.total.calls == 0, "the candidate's tracker billed nothing"
