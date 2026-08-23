"""Tests for raven.importer.state.ImportState."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.importer import ImportState


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "import_state.json"


@pytest.fixture
def state(state_path: Path) -> ImportState:
    return ImportState(path=state_path)


class TestImportState:
    def test_initial_state_empty(self, state: ImportState) -> None:
        assert not state.is_submitted("claude_code", "some-key")

    def test_mark_submitted(self, state: ImportState) -> None:
        state.mark_submitted("claude_code", "k1")
        assert state.is_submitted("claude_code", "k1")

    def test_mark_failed_not_submitted(self, state: ImportState) -> None:
        state.mark_failed("codex", "k2", "parse error")
        assert not state.is_submitted("codex", "k2")

    def test_failed_then_submitted(self, state: ImportState) -> None:
        state.mark_failed("hermes", "k3", "timeout")
        assert not state.is_submitted("hermes", "k3")
        state.mark_submitted("hermes", "k3")
        assert state.is_submitted("hermes", "k3")

    def test_persistence_across_instances(self, state_path: Path) -> None:
        s1 = ImportState(path=state_path)
        s1.mark_submitted("openclaw", "k4")

        s2 = ImportState(path=state_path)
        assert s2.is_submitted("openclaw", "k4")

    def test_get_progress_structure(self, state: ImportState) -> None:
        state.mark_submitted("claude_code", "a")
        state.mark_failed("codex", "b", "err")
        progress = state.get_progress()
        entries = progress["entries"]
        assert entries["claude_code:a"]["status"] == "submitted"
        assert entries["codex:b"]["status"] == "failed"
        assert entries["codex:b"]["error"] == "err"

    def test_corrupt_json_recovery(self, state_path: Path) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{invalid json", encoding="utf-8")

        s = ImportState(path=state_path)
        assert not s.is_submitted("any", "key")
        assert state_path.with_suffix(".json.corrupt").exists()

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        s = ImportState(path=tmp_path / "nonexistent" / "state.json")
        assert s.get_summary() == {"total": 0, "submitted": 0, "failed": 0}

    def test_atomic_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        deep_path = tmp_path / "a" / "b" / "import_state.json"
        s = ImportState(path=deep_path)
        s.mark_submitted("kimicode", "k5")
        assert deep_path.exists()

    def test_key_format(self, state: ImportState) -> None:
        state.mark_submitted("claude_code", "proj-memory")
        entries = state.get_progress()["entries"]
        assert "claude_code:proj-memory" in entries

    def test_set_total_and_get_summary(self, state: ImportState) -> None:
        state.set_total(5)
        state.mark_submitted("claude_code", "a")
        state.mark_submitted("claude_code", "b")
        state.mark_failed("codex", "c", "err")
        summary = state.get_summary()
        assert summary == {"total": 5, "submitted": 2, "failed": 1}

    def test_get_summary_without_set_total(self, state: ImportState) -> None:
        state.mark_submitted("claude_code", "a")
        state.mark_failed("codex", "b", "err")
        summary = state.get_summary()
        assert summary["total"] == 2
        assert summary["submitted"] == 1
        assert summary["failed"] == 1

    def test_meta_separated_from_entries(self, state: ImportState) -> None:
        state.set_total(10)
        state.mark_submitted("hermes", "x")
        summary = state.get_summary()
        assert summary["submitted"] == 1
        assert summary["total"] == 10
