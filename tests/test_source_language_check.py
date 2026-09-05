"""The source-language gate: a two-way pin table, the passes, and the contract.

The pin table drives one accept case per signed zone and one reject case per
revoked boundary, so widening or narrowing the gate flips a named row. The
contract test parses the exemption zones out of AGENTS.md section 1.3 and
holds them equal to the gate's own list, so the rulebook and the enforcement
cannot drift apart silently.

The CJK samples are built from escape sequences so this file is itself pure
ASCII: the gate's virgin-file rule would otherwise flag the very test that
pins it whenever a range adds this file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from scripts import check_source_language

REPO = Path(__file__).resolve().parent.parent

CJK_SAMPLE = "\u57fa\u4e8e\u8c03\u7814\u5236\u4f5c"
CJK_OTHER = "\u7b2c\u4e8c\u53e5"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "gate@test.invalid")
    _git(repo, "config", "user.name", "gate")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _gate_verdict(tmp_path: Path, monkeypatch, path: str, text: str) -> int:
    """Run the gate over a range that adds exactly one CJK-carrying file."""
    repo = _repo(tmp_path)
    _write(repo, "raven/agent/existing.py", "x = 1\n")
    base = _commit(repo, "base")
    _write(repo, path, text)
    _commit(repo, "add candidate")
    monkeypatch.chdir(repo)
    return check_source_language.main([f"{base}..HEAD"])


# --- the pin table: one accept row per signed zone, one reject row per ----------
# --- revoked boundary, so a zone added or removed flips a named case ------------

ACCEPTED = (
    ("plugins-dist/ppt-engine/raven_ppt/marks.py", 'MARK = "{cjk}"\n'),
    ("tests/test_ppt_engine_marks.py", 'EXPECT = "{cjk}"\n'),
    ("plugins-dist/design-engine/raven_design/skills/palette.py", 'NAME = "{cjk}"\n'),
    ("tests/test_design_engine_palette.py", 'EXPECT = "{cjk}"\n'),
    ("raven/i18n/zh_extra.py", 'WORD = "{cjk}"\n'),
    ("raven/templates/prompts/zh/planner.txt", "{cjk}\n"),
    ("raven/agent/notes.md", "# {cjk}\n"),
    ("tests/test_i18n_zh_lexicon.py", 'import raven.i18n\n\nFIXTURE = "{cjk}"\n'),
)

REJECTED = (
    ("i18n/zh.json", '{{"label": "{cjk}"}}\n'),
    ("ui-tui/src/new.ts", 'const label = "{cjk}";\n'),
    ("ui-web/src/new.js", 'const label = "{cjk}";\n'),
    ("benchmarks/new.py", 'CASE = "{cjk}"\n'),
    ("demos/new.py", 'DEMO = "{cjk}"\n'),
    ("subagents/x/new.ts", 'const label = "{cjk}";\n'),
    ("docs/new.py", 'DOC = "{cjk}"\n'),
    ("tests/test_agent_notes.py", 'NOTE = "{cjk}"\n'),
)


@pytest.mark.parametrize(("path", "template"), ACCEPTED, ids=[row[0] for row in ACCEPTED])
def test_a_signed_zone_addition_passes(tmp_path, monkeypatch, path, template) -> None:
    assert _gate_verdict(tmp_path, monkeypatch, path, template.format(cjk=CJK_SAMPLE)) == 0


@pytest.mark.parametrize(("path", "template"), REJECTED, ids=[row[0] for row in REJECTED])
def test_a_revoked_boundary_addition_fails(tmp_path, monkeypatch, path, template) -> None:
    assert _gate_verdict(tmp_path, monkeypatch, path, template.format(cjk=CJK_SAMPLE)) == 1


# --- the fixture proviso: the tests/test_i18n_ prefix holds only with proof -----


def test_a_fixture_without_the_zh_import_loses_the_exemption(tmp_path, monkeypatch, capsys) -> None:
    result = _gate_verdict(tmp_path, monkeypatch, "tests/test_i18n_zh_orphan.py", f'FIXTURE = "{CJK_SAMPLE}"\n')

    captured = capsys.readouterr()
    assert result == 1
    assert "tests/test_i18n_" in captured.err
    assert "raven.i18n" in captured.err


def test_a_fixture_importing_zh_machinery_via_from_keeps_the_exemption(tmp_path, monkeypatch) -> None:
    text = f'from raven.i18n import zh_lexicon\n\nFIXTURE = "{CJK_SAMPLE}"\n'

    assert _gate_verdict(tmp_path, monkeypatch, "tests/test_i18n_zh_forms.py", text) == 0


# --- the passes: virgin file, relocation, carrier -------------------------------


def test_virgin_file_cjk_addition_fails_naming_file_and_line(tmp_path, monkeypatch, capsys) -> None:
    repo = _repo(tmp_path)
    _write(repo, "raven/agent/existing.py", "x = 1\n")
    base = _commit(repo, "base")
    _write(repo, "raven/agent/fresh.py", f'TITLE = "{CJK_SAMPLE}"\n')
    _commit(repo, "add cjk")
    monkeypatch.chdir(repo)

    result = check_source_language.main([f"{base}..HEAD"])

    captured = capsys.readouterr()
    assert result == 1
    assert "Non-English source additions are not allowed in PRs" in captured.err
    assert "raven/agent/fresh.py:1" in captured.err
    assert CJK_SAMPLE in captured.err


def test_relocation_of_an_existing_run_passes(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _write(repo, "raven/agent/old_home.py", f'TITLE = "{CJK_SAMPLE}"\n')
    base = _commit(repo, "base")
    (repo / "raven/agent/old_home.py").unlink()
    _write(repo, "raven/agent/new_home.py", f'TITLE = "{CJK_SAMPLE}"\n')
    _commit(repo, "relocate")
    monkeypatch.chdir(repo)

    assert check_source_language.main([f"{base}..HEAD"]) == 0


def test_existing_bilingual_carrier_edit_passes(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _write(repo, "raven/agent/carrier.py", f'OLD = "{CJK_SAMPLE}"\n')
    base = _commit(repo, "base")
    _write(repo, "raven/agent/carrier.py", f'OLD = "{CJK_SAMPLE}"\nNEW = "{CJK_OTHER}"\n')
    _commit(repo, "extend carrier")
    monkeypatch.chdir(repo)

    assert check_source_language.main([f"{base}..HEAD"]) == 0


def test_a_file_new_in_the_range_never_counts_as_a_carrier(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _write(repo, "raven/agent/existing.py", "x = 1\n")
    base = _commit(repo, "base")
    _write(repo, "raven/agent/fresh.py", f'A = "{CJK_SAMPLE}"\n')
    _commit(repo, "first cjk")
    _write(repo, "raven/agent/fresh.py", f'A = "{CJK_SAMPLE}"\nB = "{CJK_OTHER}"\n')
    _commit(repo, "second cjk")
    monkeypatch.chdir(repo)

    assert check_source_language.main([f"{base}..HEAD"]) == 1


def test_main_skips_without_range(capsys) -> None:
    result = check_source_language.main([])

    captured = capsys.readouterr()
    assert result == 0
    assert "No commit range detected; skipping source language check" in captured.out


# --- the contract: AGENTS.md section 1.3 and the gate read the same list --------

ZONE_BULLET = re.compile(r"^  - `([^`]+)`", re.MULTILINE)


def _section_1_3() -> str:
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    _, marker, after = text.partition("Source language: English, with named exemption zones")
    assert marker, "AGENTS.md lost its source-language section"
    section, _, _ = after.partition("\n### ")
    return section


def test_agents_md_zone_bullets_equal_the_gate_prefixes() -> None:
    zones = {bullet.rstrip("*") for bullet in ZONE_BULLET.findall(_section_1_3())}

    assert zones == set(check_source_language.EXEMPT_PREFIXES)


def test_agents_md_writes_down_the_suffix_and_fixture_rules_the_gate_applies() -> None:
    section = _section_1_3()

    assert "`*.md`" in section
    assert "`tests/test_i18n_*`" in section
    assert "`raven.i18n`" in section
    assert "`raven_ppt`" in section
    assert "`raven_design`" in section
    assert check_source_language.I18N_FIXTURE_PREFIX in check_source_language.EXEMPT_PREFIXES
