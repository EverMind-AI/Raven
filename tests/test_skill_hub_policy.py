"""Unit tests for the install-time policy helpers (policy.py + audit.py)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from raven.skill_hub.audit import record_install, write_install_meta
from raven.skill_hub.policy import (
    SkillPolicy,
    is_blocked,
    lint_external_paths,
    normalize_blocklist,
    refuses_low_safety,
)


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


class TestLintExternalPaths:
    def test_empty_and_clean_bodies(self) -> None:
        assert lint_external_paths(None) == []
        assert lint_external_paths("") == []
        assert lint_external_paths("run scripts/db.py --db ./data.db") == []

    def test_own_dotdir_allowed(self) -> None:
        assert lint_external_paths("state lives in ~/.raven/skills") == []

    def test_foreign_dotdirs_flagged_deduped(self) -> None:
        body = (
            "python scripts/db.py --db ~/.openclaw/tag-memory/db.sqlite\n"
            "or $HOME/.openclaw/config.json, also /Users/bob/.claude/skills\n"
            "and /home/alice/.openclaw/x"
        )
        assert lint_external_paths(body) == ["~/.claude", "~/.openclaw"]


class TestSkillPolicy:
    def test_allows_clean_meta(self) -> None:
        policy = SkillPolicy.create()
        assert policy.refusal_for_detail({"slug": "ok", "score_safety": 0.9, "skill_md": "clean"}) is None

    def test_refuses_in_order_blocklist_first(self) -> None:
        policy = SkillPolicy.create(blocklist=["bad"])
        reason = policy.refusal_for_detail({"slug": "bad", "score_safety": 0.1})
        assert reason is not None and "blocklist" in reason

    def test_refuses_low_safety(self) -> None:
        policy = SkillPolicy.create(min_safety=0.7)
        reason = policy.refusal_for_detail({"slug": "x", "score_safety": 0.2})
        assert reason is not None and "score_safety" in reason

    def test_refuses_external_paths_in_body(self) -> None:
        policy = SkillPolicy.create()
        reason = policy.refusal_for_detail(
            {"slug": "tag-memory", "score_safety": 0.9, "skill_md": "write to ~/.openclaw/db"},
        )
        assert reason is not None and "~/.openclaw" in reason

    def test_extra_identifiers_hit_blocklist(self) -> None:
        policy = SkillPolicy.create(blocklist=["native-id"])
        reason = policy.refusal_for_detail({}, "native-id")
        assert reason is not None and "blocklist" in reason


class _FakeStdin(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestInstallSkipReason:
    def test_auto_allows(self) -> None:
        assert SkillPolicy.create().install_skip_reason("foo") is None

    def test_unknown_mode_behaves_like_auto(self) -> None:
        assert SkillPolicy.create(auto_install="sometimes").install_skip_reason("foo") is None

    def test_off_skips(self) -> None:
        reason = SkillPolicy.create(auto_install="off").install_skip_reason("foo")
        assert reason is not None
        assert "'off'" in reason and "foo" in reason

    def test_prompt_without_tty_behaves_like_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=False))
        reason = SkillPolicy.create(auto_install="prompt").install_skip_reason("foo")
        assert reason is not None and "prompt" in reason

    def test_prompt_tty_accepts_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
        monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
        assert SkillPolicy.create(auto_install="prompt").install_skip_reason("foo") is None

    def test_prompt_tty_declines_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
        monkeypatch.setattr("builtins.input", lambda _prompt="": "")
        reason = SkillPolicy.create(auto_install="prompt").install_skip_reason("foo")
        assert reason is not None and "declined" in reason


class TestWriteInstallMeta:
    def test_none_dir_is_noop(self) -> None:
        write_install_meta(None, slug="x", version="v1", trigger="use_skill")

    def test_writes_meta_into_skill_dir(self, tmp_path: Path) -> None:
        write_install_meta(str(tmp_path), slug="foo", version="v2", trigger="auto_inject")
        record = json.loads((tmp_path / ".install-meta.json").read_text(encoding="utf-8"))
        assert record["slug"] == "foo"
        assert record["version"] == "v2"
        assert record["source"] == "hub"
        assert record["trigger"] == "auto_inject"
        assert record["installed_at"]

    def test_first_install_wins(self, tmp_path: Path) -> None:
        write_install_meta(tmp_path, slug="foo", version="v2", trigger="auto_inject")
        write_install_meta(tmp_path, slug="foo", version="v2", trigger="use_skill")
        record = json.loads((tmp_path / ".install-meta.json").read_text(encoding="utf-8"))
        assert record["trigger"] == "auto_inject"

    def test_missing_dir_is_best_effort_noop(self, tmp_path: Path) -> None:
        write_install_meta(tmp_path / "ghost", slug="foo", version="v1", trigger="use_skill")
        assert not (tmp_path / "ghost").exists()


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
