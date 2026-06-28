"""Tests for ``_proactive_stack._migrate_legacy_feedback_log``."""

from __future__ import annotations

from pathlib import Path

from raven.cli._proactive_stack import _migrate_legacy_feedback_log


def test_no_op_when_legacy_absent(tmp_path: Path) -> None:
    legacy = tmp_path / "ws" / "sentinel_feedback.jsonl"
    new = tmp_path / "sentinel" / "feedback.jsonl"

    _migrate_legacy_feedback_log(legacy, new)

    assert not new.exists()
    assert not legacy.exists()


def test_simple_move_when_new_absent(tmp_path: Path) -> None:
    legacy = tmp_path / "ws" / "sentinel_feedback.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"signal": "dispatched"}\n', encoding="utf-8")
    new = tmp_path / "sentinel" / "feedback.jsonl"

    _migrate_legacy_feedback_log(legacy, new)

    assert not legacy.exists()
    assert new.read_text(encoding="utf-8") == '{"signal": "dispatched"}\n'


def test_merge_when_both_exist(tmp_path: Path) -> None:
    legacy = tmp_path / "ws" / "sentinel_feedback.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        '{"signal": "dispatched", "ts": "2026-01-01"}\n', encoding="utf-8",
    )
    new = tmp_path / "sentinel" / "feedback.jsonl"
    new.parent.mkdir(parents=True)
    new.write_text(
        '{"signal": "accepted", "ts": "2026-02-01"}\n', encoding="utf-8",
    )

    _migrate_legacy_feedback_log(legacy, new)

    assert not legacy.exists()
    merged = new.read_text(encoding="utf-8")
    assert '"ts": "2026-02-01"' in merged
    assert '"ts": "2026-01-01"' in merged
    assert merged.index('"2026-02-01"') < merged.index('"2026-01-01"'), \
        "appended legacy events come after existing ones"


def test_creates_new_parent_dir(tmp_path: Path) -> None:
    legacy = tmp_path / "ws" / "sentinel_feedback.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}\n", encoding="utf-8")
    new = tmp_path / "deep" / "nested" / "sentinel" / "feedback.jsonl"

    _migrate_legacy_feedback_log(legacy, new)

    assert new.exists()
    assert new.parent.is_dir()
