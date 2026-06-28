"""Bundled-file reference resolution in ``load_skills_for_context``.

Covers three mechanisms and their false-positive guards:
- markdown-link rewrite to absolute paths (existence-checked),
- the ``{baseDir}`` per-ref substitution,
- the directory-resolution header hint.
"""
from pathlib import Path

import pytest

from raven.memory_engine.skill_forge import LocalSkillCatalog
from raven.memory_engine.skill_local.types import SkillMeta


@pytest.fixture
def svc(tmp_path: Path) -> LocalSkillCatalog:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    return LocalSkillCatalog(workspace, builtin_skills_dir=builtin, start_watcher=False)


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """A skill directory with a few bundled files actually on disk."""
    d = tmp_path / "skills" / "demo"
    (d / "references").mkdir(parents=True)
    (d / "scripts").mkdir()
    (d / "assets").mkdir()
    (d / "examples").mkdir()
    (d / "references" / "EXISTS.md").write_text("x")
    (d / "references" / "GUIDE.md").write_text("x")
    (d / "references" / "sub").mkdir()
    (d / "references" / "sub" / "deep.md").write_text("x")
    (d / "scripts" / "run.sh").write_text("#!/bin/sh")
    (d / "assets" / "logo.svg").write_text("x")
    (d / "examples" / "demo.py").write_text("x")
    return d


def _meta(skill_dir: Path, body: str, name: str = "demo") -> SkillMeta:
    return SkillMeta(
        id=0, name=name, description="", path=skill_dir / "SKILL.md",
        content=body, source="t",
    )


def render(svc: LocalSkillCatalog, skill_dir: Path, body: str) -> str:
    return svc.load_skills_for_context([_meta(skill_dir, body)], max_inject=1)


# ── markdown-link rewrite (B) ────────────────────────────────────────

def test_md_link_existing_becomes_absolute(svc, skill_dir):
    out = render(svc, skill_dir, "see [Guide](references/GUIDE.md)")
    assert f"({skill_dir}/references/GUIDE.md)" in out


def test_md_link_missing_left_unchanged(svc, skill_dir):
    out = render(svc, skill_dir, "see [X](references/MISSING.md)")
    assert "(references/MISSING.md)" in out
    assert str(skill_dir) + "/references/MISSING.md" not in out


def test_md_link_external_url_untouched(svc, skill_dir):
    out = render(svc, skill_dir, "see [site](https://example.com/x)")
    assert "(https://example.com/x)" in out


def test_md_link_non_whitelisted_dir_untouched(svc, skill_dir):
    out = render(svc, skill_dir, "see [d](docs/intro.md)")
    assert "(docs/intro.md)" in out


def test_md_link_dotslash_prefix(svc, skill_dir):
    out = render(svc, skill_dir, "see [G](./references/EXISTS.md)")
    assert f"({skill_dir}/references/EXISTS.md)" in out


def test_md_link_nested_path(svc, skill_dir):
    out = render(svc, skill_dir, "see [D](references/sub/deep.md)")
    assert f"({skill_dir}/references/sub/deep.md)" in out


def test_md_link_anchor_and_query_preserved(svc, skill_dir):
    out = render(svc, skill_dir, "[a](references/GUIDE.md#sec) [b](references/GUIDE.md?v=1)")
    assert f"({skill_dir}/references/GUIDE.md#sec)" in out
    assert f"({skill_dir}/references/GUIDE.md?v=1)" in out


def test_md_link_all_bundled_dirs(svc, skill_dir):
    out = render(
        svc, skill_dir,
        "[s](scripts/run.sh) [a](assets/logo.svg) [e](examples/demo.py)",
    )
    assert f"({skill_dir}/scripts/run.sh)" in out
    assert f"({skill_dir}/assets/logo.svg)" in out
    assert f"({skill_dir}/examples/demo.py)" in out


def test_md_link_in_code_fence_not_rewritten(svc, skill_dir):
    out = render(svc, skill_dir, "```md\n[Guide](references/GUIDE.md)\n```")
    assert "(references/GUIDE.md)" in out
    assert f"{skill_dir}/references/GUIDE.md" not in out


# ── bare / shell refs are NOT touched (rely on the header hint) ───────

def test_bare_ref_in_prose_untouched(svc, skill_dir):
    out = render(svc, skill_dir, "see references/GUIDE.md for details")
    assert "references/GUIDE.md for details" in out
    assert f"{skill_dir}/references/GUIDE.md for details" not in out


def test_bash_dotslash_ref_untouched(svc, skill_dir):
    out = render(svc, skill_dir, "run `./scripts/run.sh` now")
    assert "./scripts/run.sh" in out


# ── directory header hint (C) ────────────────────────────────────────

def test_dir_header_present_for_pathset(svc, skill_dir):
    out = render(svc, skill_dir, "see references/GUIDE.md")
    assert f"**Skill directory**: `{skill_dir}`" in out
    assert "resolve under this directory" in out


def test_dir_header_absent_for_db_only(svc, skill_dir):
    meta = SkillMeta(id=0, name="d", description="", path=Path("sqlite://t/d"),
                     content="see references/GUIDE.md", source="t")
    out = svc.load_skills_for_context([meta], max_inject=1)
    assert "Skill directory" not in out


def test_dir_header_absent_when_dir_missing(svc, tmp_path):
    gone = tmp_path / "gone" / "SKILL.md"
    meta = SkillMeta(id=0, name="g", description="", path=gone,
                     content="[g](references/GUIDE.md)\nsee references/x.md", source="t")
    out = svc.load_skills_for_context([meta], max_inject=1)
    assert "Skill directory" not in out
    assert f"{gone.parent}/references/GUIDE.md" not in out  # no 404 abs path


# ── {baseDir} per-ref substitution ───────────────────────────────────

def test_basedir_existing_ref_resolves(svc, skill_dir):
    out = render(svc, skill_dir, "cat {baseDir}/scripts/run.sh")
    assert f"{skill_dir}/scripts/run.sh" in out


def test_basedir_missing_ref_left_literal_no_404(svc, skill_dir):
    out = render(svc, skill_dir, "ok {baseDir}/scripts/run.sh missing {baseDir}/references/NOPE.md")
    assert f"{skill_dir}/scripts/run.sh" in out          # existing → abs
    assert "{baseDir}/references/NOPE.md" in out         # missing → literal
    assert f"{skill_dir}/references/NOPE.md" not in out  # never a 404 abs path


def test_basedir_bare_resolves_to_dir(svc, skill_dir):
    out = render(svc, skill_dir, "files live under {baseDir} here")
    assert f"under {skill_dir} here" in out


def test_basedir_all_broken_no_header(svc, skill_dir):
    out = render(svc, skill_dir, "see {baseDir}/references/NOPE.md only")
    assert "Skill directory" not in out
    assert "{baseDir}/references/NOPE.md" in out


# ── misc ─────────────────────────────────────────────────────────────

def test_multi_skill_each_own_dir(svc, tmp_path):
    d1 = tmp_path / "s1"; (d1 / "references").mkdir(parents=True)
    (d1 / "references" / "A.md").write_text("a")
    d2 = tmp_path / "s2"; (d2 / "references").mkdir(parents=True)
    (d2 / "references" / "B.md").write_text("b")
    m1 = SkillMeta(id=1, name="s1", description="", path=d1 / "SKILL.md",
                   content="[a](references/A.md)", source="t")
    m2 = SkillMeta(id=2, name="s2", description="", path=d2 / "SKILL.md",
                   content="[b](references/B.md)", source="t")
    out = svc.load_skills_for_context([m1, m2], max_inject=5)
    assert f"{d1}/references/A.md" in out
    assert f"{d2}/references/B.md" in out


def test_empty_body_skipped(svc, skill_dir):
    assert render(svc, skill_dir, "") == ""
