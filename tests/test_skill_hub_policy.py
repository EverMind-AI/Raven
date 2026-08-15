"""Unit tests for the install-time policy helpers (policy.py + audit.py)."""

from __future__ import annotations

import json
from pathlib import Path

from raven.skill_hub.audit import record_install
from raven.skill_hub.policy import is_blocked, normalize_blocklist, refuses_low_safety


class TestNormalizeBlocklist:
    def test_empty_and_none(self) -> None:
        assert normalize_blocklist(None) == frozenset()
        assert normalize_blocklist([]) == frozenset()
        assert normalize_blocklist(["", "  "]) == frozenset()

    def test_casefold_and_trim(self) -> None:
        assert normalize_blocklist([" Tag-Memory ", "OTHER"]) == frozenset({"tag-memory", "other"})


class TestIsBlocked:
    def test_empty_blocklist_never_blocks(self) -> None:
        assert not is_blocked(frozenset(), "anything")

    def test_matches_any_identifier_case_insensitively(self) -> None:
        bl = normalize_blocklist(["tag-memory"])
        assert is_blocked(bl, "Tag-Memory")
        assert is_blocked(bl, "other", "TAG-MEMORY")
        assert not is_blocked(bl, "fine-skill", None)

    def test_none_identifiers_ignored(self) -> None:
        assert not is_blocked(normalize_blocklist(["x"]), None, None)


class TestRefusesLowSafety:
    def test_missing_score_passes(self) -> None:
        assert not refuses_low_safety(None, 0.7)

    def test_low_score_refused(self) -> None:
        assert refuses_low_safety(0.2, 0.7)
        assert refuses_low_safety("0.69", 0.7)

    def test_at_or_above_bar_passes(self) -> None:
        assert not refuses_low_safety(0.7, 0.7)
        assert not refuses_low_safety(0.93, 0.7)

    def test_malformed_score_passes(self) -> None:
        assert not refuses_low_safety("n/a", 0.7)
        assert not refuses_low_safety({}, 0.7)


class TestRecordInstall:
    def test_none_path_is_noop(self) -> None:
        record_install(None, slug="x", trigger="use_skill")

    def test_appends_jsonl_and_creates_parent(self, tmp_path: Path) -> None:
        audit = tmp_path / "nested" / "installs.jsonl"
        record_install(audit, slug="a", version="v1", trigger="auto_inject", score_safety=0.9)
        record_install(audit, slug="b", trigger="use_skill", skill_dir="/tmp/b")
        lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").strip().splitlines()]
        assert [r["slug"] for r in lines] == ["a", "b"]
        assert lines[0]["trigger"] == "auto_inject"
        assert lines[0]["score_safety"] == 0.9
        assert lines[1]["dir"] == "/tmp/b"
        assert all(r["ts"] for r in lines)
