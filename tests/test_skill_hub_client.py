"""Unit tests for SkillHubClient bundle handling: nested-wrapper resolution
and lenient zip extraction (skip unsafe entries, hard-reject traversal)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from raven.skill_hub.client import SkillHubClient, SkillHubError


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


# ── _bundle_root ─────────────────────────────────────────────────────

def test_bundle_root_collapses_single_wrapper_dir(tmp_path: Path) -> None:
    # Hub zips wrap everything under one <skill>/ directory.
    inner = tmp_path / "tmux"
    (inner / "scripts").mkdir(parents=True)
    (inner / "SKILL.md").write_text("x")
    assert SkillHubClient._bundle_root(tmp_path) == inner


def test_bundle_root_flat_zip_stays_at_dest(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("x")
    (tmp_path / "scripts").mkdir()
    assert SkillHubClient._bundle_root(tmp_path) == tmp_path


def test_bundle_root_ignores_hidden_entries(tmp_path: Path) -> None:
    inner = tmp_path / "skill"
    inner.mkdir()
    (tmp_path / ".DS_Store").write_text("")
    assert SkillHubClient._bundle_root(tmp_path) == inner


# ── _safe_extract ────────────────────────────────────────────────────

def test_safe_extract_writes_allowed_files(tmp_path: Path) -> None:
    SkillHubClient._safe_extract(
        _zip({"SKILL.md": "body", "scripts/run.sh": "echo hi"}), tmp_path,
    )
    assert (tmp_path / "SKILL.md").read_text() == "body"
    assert (tmp_path / "scripts" / "run.sh").read_text() == "echo hi"


def test_safe_extract_skips_disallowed_not_fails(tmp_path: Path) -> None:
    # One stray file must not make the whole skill uninstallable.
    SkillHubClient._safe_extract(
        _zip({"SKILL.md": "body", "templates/weird.xyz": "junk"}), tmp_path,
    )
    assert (tmp_path / "SKILL.md").exists()
    assert not (tmp_path / "templates" / "weird.xyz").exists()


def test_safe_extract_allows_common_assets(tmp_path: Path) -> None:
    SkillHubClient._safe_extract(
        _zip({"logo.svg": "<svg/>", "page.html": "<html/>", "tool.js": "x"}),
        tmp_path,
    )
    assert (tmp_path / "logo.svg").exists()
    assert (tmp_path / "page.html").exists()
    assert (tmp_path / "tool.js").exists()


def test_safe_extract_rejects_path_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.md", "owned")
    with pytest.raises(SkillHubError, match="unsafe zip path"):
        SkillHubClient._safe_extract(buf.getvalue(), tmp_path)
