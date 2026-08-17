"""Unit tests for the grep and find search tools.

Tests exercise both the ripgrep-backed path (when rg is on PATH) and the
pure-Python fallback (forced by patching shutil.which to return None).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from raven.agent.tools.file_search import FindTool, GrepTool, _expand_braces


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\ndef hello():\n    return 'world'\n")
    (tmp_path / "src" / "util.py").write_text("def helper():\n    return 42\n")
    (tmp_path / "README.md").write_text("# Title\nhello there\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("def hello():\n    pass\n")
    return tmp_path


# ── grep ────────────────────────────────────────────────────────────────


async def test_grep_content_finds_match(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern=r"def hello")
    assert "app.py" in out
    assert "node_modules" not in out  # ignored dir


async def test_grep_no_match(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern=r"zzz_nonexistent")
    assert out == "No matches found."


async def test_grep_glob_filter(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello", glob="*.md")
    assert "README.md" in out
    assert "app.py" not in out


async def test_grep_files_with_matches(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern=r"def hello", output_mode="files_with_matches")
    assert "app.py" in out
    assert ":" not in out.split("\n")[0]  # path only, no line content


async def test_grep_count(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="def", output_mode="count")
    assert "app.py:1" in out
    assert "util.py:1" in out


async def test_grep_case_insensitive(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="HELLO", case_insensitive=True)
    assert "app.py" in out


async def test_grep_invalid_regex(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="(unclosed")
    assert "invalid regular expression" in out


async def test_grep_python_fallback(tree: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern=r"def hello")
    assert "app.py" in out
    assert "node_modules" not in out


async def test_grep_fallback_skips_binary(tree: Path, monkeypatch):
    (tree / "blob.bin").write_bytes(b"def hello\x00\x01binary")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello")
    assert "blob.bin" not in out


async def test_grep_context(tree: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)  # deterministic format
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="return 'world'", context=1)
    assert "def hello" in out  # context line above the match


async def test_grep_outside_allowed_dir(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = GrepTool(workspace=workspace, allowed_dir=workspace)
    out = await tool.execute(pattern="x", path="/etc")
    assert "Error" in out


# ── find ────────────────────────────────────────────────────────────────


async def test_find_basename_recursive(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="*.py")
    assert "src/app.py" in out
    assert "src/util.py" in out
    assert "node_modules" not in out  # ignored


async def test_find_path_pattern(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="src/*.py")
    assert "src/app.py" in out
    assert "README.md" not in out


async def test_find_no_match(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="*.rs")
    assert out == "No files found matching pattern."


async def test_find_limit(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="*.py", limit=1)
    assert "showing first 1 of 2" in out


async def test_find_sorted_by_recency(tree: Path):
    # util.py touched last → should sort first
    import os
    import time

    os.utime(tree / "src" / "util.py", (time.time() + 100, time.time() + 100))
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="*.py")
    lines = out.splitlines()
    assert lines[0].endswith("util.py")


# ── brace expansion ─────────────────────────────────────────────────────


def test_expand_braces_passthrough():
    assert _expand_braces("*.py") == ["*.py"]


def test_expand_braces_basic():
    assert _expand_braces("*.{ts,tsx}") == ["*.ts", "*.tsx"]


def test_expand_braces_multiple_groups():
    assert _expand_braces("{a,b}/{c,d}.py") == ["a/c.py", "a/d.py", "b/c.py", "b/d.py"]


def test_expand_braces_nested():
    assert _expand_braces("*.{p{y,yc},md}") == ["*.py", "*.pyc", "*.md"]


def test_expand_braces_single_element():
    assert _expand_braces("*.{py}") == ["*.py"]


def test_expand_braces_unbalanced_is_literal():
    assert _expand_braces("*.{py") == ["*.{py"]


def test_expand_braces_cap_aborts_before_full_expansion():
    with pytest.raises(ValueError, match="brace expansion"):
        _expand_braces("{a,b,c,d,e,f,g,h}{a,b,c,d,e,f,g,h}{a,b,c,d,e,f,g,h}")


async def test_find_brace_expansion(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="*.{py,md}")
    assert "src/app.py" in out
    assert "src/util.py" in out
    assert "README.md" in out
    assert "node_modules" not in out


async def test_find_brace_path_pattern(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="src/*.{py,md}")
    assert "src/app.py" in out
    assert "README.md" not in out


async def test_find_brace_nested(tree: Path):
    (tree / "src" / "app.pyc").write_text("bytecode")
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="*.{p{y,yc},md}")
    assert "src/app.py" in out
    assert "src/app.pyc" in out
    assert "README.md" in out


async def test_find_brace_dedup(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="{app,*}.py")
    assert out.splitlines().count("src/app.py") == 1


async def test_find_brace_unbalanced_is_literal(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="*.{py")
    assert out == "No files found matching pattern."


async def test_find_brace_cap_error(tree: Path):
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="{a,b,c,d,e,f,g,h}{a,b,c,d,e,f,g,h}{a,b,c,d,e,f,g,h}")
    assert out.startswith("Error")
    assert "brace expansion" in out


async def test_grep_glob_brace_fallback(tree: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello", glob="*.{py,md}")
    assert "app.py" in out
    assert "README.md" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed; CI runs a with-rg leg")
async def test_grep_glob_brace_rg(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello", glob="*.{py,md}")
    assert "app.py" in out
    assert "README.md" in out


async def test_grep_fallback_path_glob_is_explicit_error(tree: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello", glob="src/*.py")
    assert out.startswith("Error")
    assert "ripgrep" in out


async def test_grep_fallback_deadline_cut_is_declared(tree: Path, monkeypatch):
    """A walk stopped at the deadline must never report a confident empty
    result — absence over a partial scan proves nothing."""
    import raven.agent.tools.file_search as fs

    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr(fs, "_WALK_DEADLINE_S", -1.0)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="def hello")
    assert "results are incomplete" in out
    assert out != "No matches found."


async def test_grep_fallback_full_walk_keeps_plain_empty_message(tree: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="zzz_no_such_symbol_zzz")
    assert out == "No matches found."
