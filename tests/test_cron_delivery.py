"""Unit tests for ``resolve_cron_delivery`` — the trigger-time delivery
resolver added in the cron config refactor (stage B).

Covers all four shape combinations:
  - non-ephemeral channel (per-job pass-through)
  - ephemeral channel + ``["*"]`` (broadcast all enabled)
  - ephemeral channel + specific list (intersect with enabled)
  - ephemeral channel + edge cases (empty config / no overlap / no
    recent session per target)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.proactive_engine.schedulers.cron.tool import (
    DeliveryTarget,
    is_ephemeral_channel,
    resolve_cron_delivery,
)
from raven.session.manager import SessionManager


@pytest.fixture
def populated_sessions(tmp_path: Path) -> SessionManager:
    """SessionManager with three pre-seeded channel sessions so
    find_most_recent_chat_id returns deterministic chat_ids."""
    sessions = tmp_path / "sessions"
    for ch, chat_id in [
        ("telegram", "6608552652"),
        ("feishu", "ou_ae6f5d330ca2665cd921bab323b893f9"),
        ("whatsapp", "220487145754747@lid"),
    ]:
        (sessions / ch).mkdir(parents=True, exist_ok=True)
        path = sessions / ch / f"{chat_id}.jsonl"
        path.write_text(
            json.dumps({"_type": "metadata", "key": f"{ch}:{chat_id}"}) + "\n",
            encoding="utf-8",
        )
    return SessionManager(tmp_path)


# ─────────────────────────────────────────────────────────────────────
# is_ephemeral_channel
# ─────────────────────────────────────────────────────────────────────


def test_is_ephemeral_cli_not_in_enabled():
    assert is_ephemeral_channel("cli", {"telegram", "feishu"}) is True


def test_is_ephemeral_tui_not_in_enabled():
    assert is_ephemeral_channel("tui", {"telegram", "feishu"}) is True


def test_is_ephemeral_telegram_in_enabled():
    assert is_ephemeral_channel("telegram", {"telegram", "feishu"}) is False


def test_is_ephemeral_empty_enabled_set():
    """When no channels are enabled, every channel is ephemeral."""
    assert is_ephemeral_channel("telegram", set()) is True


# ─────────────────────────────────────────────────────────────────────
# resolve_cron_delivery — pass-through for non-ephemeral
# ─────────────────────────────────────────────────────────────────────


def test_non_ephemeral_passthrough(populated_sessions):
    """Real channel (telegram) → direct pass-through, no broadcast,
    no warning."""
    targets, warnings = resolve_cron_delivery(
        channel="telegram",
        chat_id="6608552652",
        forward_channels=["*"],  # irrelevant for non-ephemeral
        enabled_channels={"telegram", "feishu"},
        session_manager=populated_sessions,
    )
    assert targets == [DeliveryTarget(channel="telegram", chat_id="6608552652")]
    assert warnings == []


def test_non_ephemeral_ignores_forward_channels(populated_sessions):
    """forward_channels has no effect on real channels."""
    targets, _ = resolve_cron_delivery(
        channel="feishu",
        chat_id="ou_explicit",
        forward_channels=["telegram"],
        enabled_channels={"telegram", "feishu"},
        session_manager=populated_sessions,
    )
    assert targets == [DeliveryTarget(channel="feishu", chat_id="ou_explicit")]


# ─────────────────────────────────────────────────────────────────────
# resolve_cron_delivery — ephemeral broadcast
# ─────────────────────────────────────────────────────────────────────


def test_ephemeral_broadcast_all(populated_sessions):
    """forward_channels=['*'] expands to every enabled channel."""
    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["*"],
        enabled_channels={"telegram", "feishu"},
        session_manager=populated_sessions,
    )
    by_channel = {t.channel: t.chat_id for t in targets}
    assert by_channel == {
        "telegram": "6608552652",
        "feishu": "ou_ae6f5d330ca2665cd921bab323b893f9",
    }
    assert warnings == []


def test_ephemeral_specific_subset(populated_sessions):
    """forward_channels=['telegram'] restricts to that one channel."""
    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["telegram"],
        enabled_channels={"telegram", "feishu"},
        session_manager=populated_sessions,
    )
    assert targets == [DeliveryTarget(channel="telegram", chat_id="6608552652")]
    assert warnings == []


def test_ephemeral_multiple_specific(populated_sessions):
    """forward_channels=['telegram', 'feishu'] → both."""
    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["telegram", "feishu"],
        enabled_channels={"telegram", "feishu"},
        session_manager=populated_sessions,
    )
    channels = sorted(t.channel for t in targets)
    assert channels == ["feishu", "telegram"]
    assert warnings == []


def test_tui_treated_same_as_cli(populated_sessions):
    """channel='tui' is also ephemeral and goes through forward_channels."""
    targets, _ = resolve_cron_delivery(
        channel="tui",
        chat_id="default",
        forward_channels=["telegram"],
        enabled_channels={"telegram", "feishu"},
        session_manager=populated_sessions,
    )
    assert targets == [DeliveryTarget(channel="telegram", chat_id="6608552652")]


# ─────────────────────────────────────────────────────────────────────
# resolve_cron_delivery — edge cases (warnings, no delivery)
# ─────────────────────────────────────────────────────────────────────


def test_ephemeral_empty_forward_channels_warns(populated_sessions):
    """forward_channels=[] → no targets, warning surfaced."""
    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=[],
        enabled_channels={"telegram", "feishu"},
        session_manager=populated_sessions,
    )
    assert targets == []
    assert any("no forward_channels configured" in w for w in warnings)


def test_ephemeral_no_overlap_warns(populated_sessions):
    """forward_channels=['xyz'] but enabled={'telegram'} → no overlap warning."""
    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["xyz"],
        enabled_channels={"telegram"},
        session_manager=populated_sessions,
    )
    assert targets == []
    assert any("no overlap" in w for w in warnings)


def test_ephemeral_skip_when_no_session(tmp_path: Path):
    """When SessionManager has no session for a forward target, that target
    is skipped with a warning — other targets still deliver."""
    # Sessions dir has only telegram, no feishu
    sessions = tmp_path / "sessions"
    (sessions / "telegram").mkdir(parents=True)
    (sessions / "telegram" / "6608552652.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "telegram:6608552652"}) + "\n",
        encoding="utf-8",
    )
    session_mgr = SessionManager(tmp_path)

    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["*"],
        enabled_channels={"telegram", "feishu"},
        session_manager=session_mgr,
    )
    # telegram delivers, feishu skipped
    by_channel = {t.channel: t.chat_id for t in targets}
    assert by_channel == {"telegram": "6608552652"}
    assert any("feishu" in w and "no recent session" in w for w in warnings)


def test_ephemeral_no_session_manager_skips_everything(tmp_path: Path):
    """Without a session_manager (REPL-only path), no chat_id can be
    resolved → all forward targets skipped."""
    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["telegram"],
        enabled_channels={"telegram"},
        session_manager=None,
    )
    assert targets == []
    assert any("no recent session" in w for w in warnings)


# ─────────────────────────────────────────────────────────────────────
# Minted tui chat_id fan-out invariant
# ─────────────────────────────────────────────────────────────────────


def _seed_minted_tui(tmp_path: Path, chat_id: str, updated_at: str) -> None:
    tui_dir = tmp_path / "sessions" / "tui"
    tui_dir.mkdir(parents=True, exist_ok=True)
    (tui_dir / f"{chat_id}.jsonl").write_text(
        json.dumps({
            "_type": "metadata",
            "key": f"tui:{chat_id}",
            "updated_at": updated_at,
            "metadata": {},
        }) + "\n",
        encoding="utf-8",
    )


def test_cron_fanout_to_tui_resolves_minted_chat_id(tmp_path: Path):
    """Fan-out to tui with two minted sessions: newer updated_at wins.

    Invariant: find_most_recent_chat_id("tui") returns the bare minted
    chat_id (not the full session key), and resolve_cron_delivery forms
    DeliveryTarget(channel="tui", chat_id=<bare_minted_id>).
    """
    older_id = "20260610_100000_aaa111"
    newer_id = "20260610_120000_bbb222"
    # Seed the newer session FIRST so file mtime order contradicts
    # updated_at order — pins that recency follows updated_at, not mtime.
    _seed_minted_tui(tmp_path, newer_id, "2026-06-10T12:00:00")
    _seed_minted_tui(tmp_path, older_id, "2026-06-10T10:00:00")

    session_mgr = SessionManager(tmp_path)

    resolved = session_mgr.find_most_recent_chat_id("tui")
    assert resolved == newer_id

    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["tui"],
        enabled_channels={"tui"},
        session_manager=session_mgr,
    )

    assert warnings == []
    assert len(targets) == 1
    assert targets[0].channel == "tui"
    assert targets[0].chat_id == newer_id


def test_cron_fanout_to_tui_single_minted_session(tmp_path: Path):
    """Fan-out to tui with one minted session returns its bare chat_id."""
    only_id = "20260610_090000_ccc333"
    _seed_minted_tui(tmp_path, only_id, "2026-06-10T09:00:00")

    session_mgr = SessionManager(tmp_path)
    targets, warnings = resolve_cron_delivery(
        channel="cli",
        chat_id="direct",
        forward_channels=["tui"],
        enabled_channels={"tui"},
        session_manager=session_mgr,
    )

    assert warnings == []
    assert targets == [DeliveryTarget(channel="tui", chat_id=only_id)]
