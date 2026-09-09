"""Tests for the skill body ref-path resolver.

Used by both the active-skills render path and the router-hits post-gate
hydrate step — same helper, so a single test set covers both."""

from __future__ import annotations

from pathlib import Path

from raven.memory_engine.skill_forge.refs import resolve_refs


def _make_skill_dir(tmp_path: Path) -> Path:
    """Build a SKILL directory with all four bundled subdirs populated."""
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "assets").mkdir()
    (skill / "examples").mkdir()
    (skill / "references" / "CONFIG.md").write_text("config body")
    (skill / "scripts" / "run.sh").write_text("#!/bin/bash")
    (skill / "assets" / "logo.png").write_bytes(b"\x89PNG")
    return skill


# ----------------------------------------------------------------------
# {baseDir}/x substitution
# ----------------------------------------------------------------------


def test_resolve_basedir_existing_ref(tmp_path: Path) -> None:
    skill = _make_skill_dir(tmp_path)
    body = "Read {baseDir}/references/CONFIG.md for details."
    out, ok = resolve_refs(body, skill)
    assert str(skill) + "/references/CONFIG.md" in out
    assert "{baseDir}" not in out
    assert ok is True


def test_resolve_basedir_missing_ref_left_literal(tmp_path: Path) -> None:
    skill = _make_skill_dir(tmp_path)
    body = "Run {baseDir}/scripts/nope.sh"
    out, ok = resolve_refs(body, skill)
    # Missing target → literal placeholder preserved so the agent doesn't
    # waste a turn on a confident 404.
    assert "{baseDir}/scripts/nope.sh" in out
    assert ok is False


def test_resolve_bare_basedir_substitutes_dir(tmp_path: Path) -> None:
    skill = _make_skill_dir(tmp_path)
    body = "cd {baseDir} then run scripts."
    out, ok = resolve_refs(body, skill)
    assert f"cd {skill}" in out
    assert ok is True


# ----------------------------------------------------------------------
# Markdown link substitution
# ----------------------------------------------------------------------


def test_resolve_markdown_link_to_bundled(tmp_path: Path) -> None:
    skill = _make_skill_dir(tmp_path)
    body = "See [Config](./references/CONFIG.md) for setup."
    out, ok = resolve_refs(body, skill)
    assert f"[Config]({skill}/references/CONFIG.md)" in out
    assert ok is True


def test_resolve_markdown_link_keeps_fragment(tmp_path: Path) -> None:
    skill = _make_skill_dir(tmp_path)
    body = "See [Config](references/CONFIG.md#section-a) for setup."
    out, _ok = resolve_refs(body, skill)
    assert f"[Config]({skill}/references/CONFIG.md#section-a)" in out


def test_resolve_markdown_link_missing_target_untouched(tmp_path: Path) -> None:
    skill = _make_skill_dir(tmp_path)
    body = "See [Missing](references/nope.md)."
    out, ok = resolve_refs(body, skill)
    assert "[Missing](references/nope.md)" in out
    assert ok is False


def test_resolve_skips_code_fence(tmp_path: Path) -> None:
    skill = _make_skill_dir(tmp_path)
    body = "Use this snippet:\n```\n[Config](references/CONFIG.md)\n```\nReal ref: [Config](references/CONFIG.md)"
    out, ok = resolve_refs(body, skill)
    # Inside the fence: untouched.
    assert "```\n[Config](references/CONFIG.md)\n```" in out
    # Outside the fence: rewritten.
    assert f"Real ref: [Config]({skill}/references/CONFIG.md)" in out
    assert ok is True


# ----------------------------------------------------------------------
# No skill_dir (sqlite-only / db-imported skills)
# ----------------------------------------------------------------------


def test_no_skill_dir_strips_basedir_prefix() -> None:
    body = "Read {baseDir}/references/x.md and run {baseDir}/scripts/y.sh."
    out, ok = resolve_refs(body, None)
    # Strip the literal `{baseDir}/` so refs read as bare relatives —
    # better than handing the agent a placeholder it can't expand.
    assert "{baseDir}" not in out
    assert "references/x.md" in out
    assert "scripts/y.sh" in out
    assert ok is False


def test_empty_body_safe() -> None:
    out, ok = resolve_refs("", None)
    assert out == ""
    assert ok is False
