"""Unit tests for PendingDecisionStore + MemoryStore.read_history_since (MS1)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.proactive_engine.sentinel.executor.pending_decision import PendingDecisionStore
from raven.proactive_engine.sentinel.types import PendingDecision, TaskOption


# ── helpers ───────────────────────────────────────────────────────────


def _opt(option_id: str, **overrides) -> TaskOption:
    base = dict(
        id=option_id,
        title=f"title for {option_id}",
        why="just because",
        type="ad_hoc",
        exec_kind="reply",
        exec_payload={"prompt": "do the thing"},
        source="history",
        priority="medium",
        created_at_ms=1000,
    )
    base.update(overrides)
    return TaskOption(**base)


def _decision(
    *,
    decision_id: str = "dec_test",
    channel: str = "feishu",
    to: str = "ou_xxx",
    created_at_ms: int = 1_700_000_000_000,
    ttl_min: int = 60,
    options: list[TaskOption] | None = None,
) -> PendingDecision:
    return PendingDecision(
        decision_id=decision_id,
        channel=channel,
        to=to,
        created_at_ms=created_at_ms,
        ttl_min=ttl_min,
        options=options or [_opt("opt_a"), _opt("opt_b")],
    )


# ── PendingDecisionStore ──────────────────────────────────────────────


def test_put_then_get_recent_returns_decision(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    d = _decision()
    store.put(d)

    got = store.get_recent("feishu", "ou_xxx", now_ms=d.created_at_ms + 60_000)
    assert got is not None
    assert got.decision_id == "dec_test"
    assert len(got.options) == 2
    assert got.options[0].id == "opt_a"


def test_get_recent_misses_other_address(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    store.put(_decision(channel="feishu", to="ou_xxx"))

    assert store.get_recent("feishu", "ou_other", now_ms=1_700_000_060_000) is None
    assert store.get_recent("cli", "ou_xxx", now_ms=1_700_000_060_000) is None


def test_get_recent_skips_expired(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    d = _decision(created_at_ms=1_000_000_000_000, ttl_min=60)
    store.put(d)

    # 60 min after creation = exactly expired (boundary inclusive)
    expired_now = d.created_at_ms + 60 * 60_000
    assert store.get_recent(d.channel, d.to, now_ms=expired_now) is None
    # 1 min before expiry → still live
    live_now = d.created_at_ms + 59 * 60_000
    assert store.get_recent(d.channel, d.to, now_ms=live_now) is not None


def test_put_supersedes_prior_live_decision_on_same_address(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    older = _decision(decision_id="dec_old", created_at_ms=1_700_000_000_000)
    newer = _decision(decision_id="dec_new", created_at_ms=1_700_000_001_000)
    store.put(older)
    store.put(newer)

    got = store.get_recent("feishu", "ou_xxx", now_ms=1_700_000_002_000)
    assert got is not None
    assert got.decision_id == "dec_new"
    # The older one is gone (superseded), not just shadowed:
    all_active = store.all_active(now_ms=1_700_000_002_000)
    assert len(all_active) == 1
    assert all_active[0].decision_id == "dec_new"


def test_put_does_not_supersede_consumed_decisions(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    older = _decision(decision_id="dec_old", created_at_ms=1_700_000_000_000)
    store.put(older)
    store.mark_consumed("dec_old", picked_option_id="opt_a",
                        consumed_at_ms=1_700_000_000_500)

    newer = _decision(decision_id="dec_new", created_at_ms=1_700_000_001_000)
    store.put(newer)

    # Both should still be in the file (consumed=True kept for audit), but
    # only the new one shows up as live.
    raw = store._store.load()
    assert {d["decision_id"] for d in raw["decisions"]} == {"dec_old", "dec_new"}
    live = store.all_active(now_ms=1_700_000_002_000)
    assert [d.decision_id for d in live] == ["dec_new"]


def test_mark_consumed_records_pick(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    d = _decision(decision_id="dec_x")
    store.put(d)

    ok = store.mark_consumed("dec_x", picked_option_id="opt_b",
                             consumed_at_ms=1_700_000_000_500)
    assert ok is True

    # Subsequent get_recent ignores it
    assert store.get_recent(d.channel, d.to, now_ms=1_700_000_001_000) is None

    raw = store._store.load()["decisions"]
    assert len(raw) == 1
    assert raw[0]["consumed"] is True
    assert raw[0]["picked_option_id"] == "opt_b"
    assert raw[0]["consumed_at_ms"] == 1_700_000_000_500


def test_mark_consumed_idempotent_returns_false_second_time(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    d = _decision(decision_id="dec_x")
    store.put(d)

    assert store.mark_consumed("dec_x", "opt_a", consumed_at_ms=1) is True
    # Second mark on already-consumed decision returns False (loser of race)
    assert store.mark_consumed("dec_x", "opt_b", consumed_at_ms=2) is False
    raw = store._store.load()["decisions"]
    # Original pick stays — second call did NOT overwrite
    assert raw[0]["picked_option_id"] == "opt_a"


def test_mark_consumed_returns_false_for_missing(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    assert store.mark_consumed("dec_nope", "opt_a", consumed_at_ms=1) is False


def test_sweep_expired_removes_old_unconsumed(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    # Put both close in time so put()'s opportunistic expiry-cleanup leaves
    # them both. Different (channel, to) so put() of "fresh" doesn't
    # supersede "old".
    old = _decision(decision_id="dec_old", created_at_ms=1_000_000_000_000,
                    ttl_min=60)
    fresh = _decision(decision_id="dec_fresh",
                      created_at_ms=1_000_000_002_000,  # 2s after old
                      channel="feishu", to="ou_other")
    store.put(old)
    store.put(fresh)

    # Sweep at a time where old has just expired (60min+1s after old's
    # creation) but fresh is still alive (~59.98min lifetime).
    sweep_at = 1_000_000_000_000 + 60 * 60_000 + 1_000
    removed = store.sweep_expired(now_ms=sweep_at)
    assert removed == 1

    live = store.all_active(now_ms=sweep_at)
    assert [d.decision_id for d in live] == ["dec_fresh"]


def test_serialization_roundtrip_preserves_options(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    d = _decision(options=[
        _opt("opt_a", type="routine_confirm", exec_kind="routine_confirm",
             exec_payload={"routine_id": "dow1-h09-meeting", "make_cron": True,
                           "cron_expr": "0 9 * * 1"},
             source="routine", priority="high"),
        _opt("opt_b", exec_kind="spawn",
             exec_payload={"task_description": "research X",
                           "max_iterations": 5}),
    ])
    store.put(d)

    got = store.get_recent("feishu", "ou_xxx", now_ms=d.created_at_ms + 100)
    assert got is not None
    assert got.options[0].type == "routine_confirm"
    assert got.options[0].exec_payload["cron_expr"] == "0 9 * * 1"
    assert got.options[1].exec_kind == "spawn"
    assert got.options[1].exec_payload["max_iterations"] == 5


def test_malformed_decision_in_file_is_silently_dropped(tmp_path: Path):
    store = PendingDecisionStore(tmp_path / "pending.json")
    # Hand-poke malformed entry alongside a good one
    good = _decision(decision_id="dec_good")
    store.put(good)

    raw_state = store._store.load()
    raw_state["decisions"].append({"decision_id": "dec_bad"})  # missing required fields
    store._store.update(lambda s: raw_state)

    # Sweep + read should not crash, malformed entry is ignored (gracefully
    # dropped during sweep).
    sweep_removed = store.sweep_expired(now_ms=good.created_at_ms + 100)
    assert sweep_removed >= 1
    live = store.all_active(now_ms=good.created_at_ms + 100)
    assert [d.decision_id for d in live] == ["dec_good"]


def test_cross_process_visibility_via_fcntl(tmp_path: Path):
    """Two store instances on the same path read each other's writes —
    JsonStateStore.locked() ensures consistency."""
    path = tmp_path / "pending.json"
    a = PendingDecisionStore(path)
    b = PendingDecisionStore(path)

    a.put(_decision(decision_id="dec_from_a"))
    got = b.get_recent("feishu", "ou_xxx", now_ms=1_700_000_000_500)
    assert got is not None
    assert got.decision_id == "dec_from_a"

    b.mark_consumed("dec_from_a", "opt_a", consumed_at_ms=1_700_000_000_600)
    assert a.get_recent("feishu", "ou_xxx", now_ms=1_700_000_000_700) is None


# ── MemoryStore.read_history_since ────────────────────────────────────


def test_read_history_since_filters_by_timestamp(tmp_path: Path):
    ws = tmp_path / "ws"
    store = MemoryStore(ws)

    # Helper: epoch-ms for a local datetime
    def to_ms(s: str) -> int:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp() * 1000)

    store.append_history("[2026-05-06 10:00] old entry one")
    store.append_history("[2026-05-07 09:30] middle entry")
    store.append_history("[2026-05-08 08:00] fresh entry today")

    # since 2026-05-07 00:00 → drop the 5/6 one, keep 5/7 and 5/8
    result = store.read_history_since(to_ms("2026-05-07 00:00"))
    assert "old entry one" not in result
    assert "middle entry" in result
    assert "fresh entry today" in result

    # since 2026-05-08 00:00 → only today's
    result_today = store.read_history_since(to_ms("2026-05-08 00:00"))
    assert "old entry one" not in result_today
    assert "middle entry" not in result_today
    assert "fresh entry today" in result_today


def test_read_history_since_drops_unstamped_paragraphs(tmp_path: Path):
    ws = tmp_path / "ws"
    store = MemoryStore(ws)
    store.append_history("[2026-05-08 08:00] valid stamp")
    store.append_history("no stamp at all — should be dropped")
    store.append_history("[bad-stamp-format] also dropped")

    result = store.read_history_since(0)  # since epoch — keep everything stamped
    assert "valid stamp" in result
    assert "no stamp at all" not in result
    assert "bad-stamp-format" not in result


def test_read_history_since_missing_file_returns_empty(tmp_path: Path):
    ws = tmp_path / "ws"
    store = MemoryStore(ws)
    # No history written yet
    assert store.read_history_since(0) == ""


def test_read_history_since_preserves_multi_line_paragraph(tmp_path: Path):
    ws = tmp_path / "ws"
    store = MemoryStore(ws)
    store.append_history("[2026-05-08 08:00] line one\nline two\nline three")

    result = store.read_history_since(0)
    assert "line one" in result
    assert "line two" in result
    assert "line three" in result
