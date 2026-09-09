"""Unit tests for the grep and find search tools.

Tests exercise both the bundled ripgrep path and the pure-Python fallback
(forced by disabling binary resolution).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.tools import file_search
from raven.agent.tools.file_search import FindTool, GrepTool


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
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern=r"def hello")
    assert "app.py" in out
    assert "node_modules" not in out  # ignored dir


async def test_grep_no_match(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern=r"zzz_nonexistent")
    assert out == "No matches found."


async def test_grep_glob_filter(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="hello", glob="*.md")
    assert "README.md" in out
    assert "app.py" not in out


async def test_grep_files_with_matches(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern=r"def hello", output_mode="files_with_matches")
    assert "app.py" in out
    assert ":" not in out.split("\n")[0]  # path only, no line content


async def test_grep_count(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="def", output_mode="count")
    assert "app.py:1" in out
    assert "util.py:1" in out


async def test_grep_case_insensitive(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="HELLO", case_insensitive=True)
    assert "app.py" in out


async def test_grep_invalid_regex(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="(unclosed")
    assert "invalid regular expression" in out


async def test_grep_python_fallback(tree: Path, monkeypatch):
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern=r"def hello")
    assert "app.py" in out
    assert "node_modules" not in out


async def test_grep_fallback_skips_binary(tree: Path, monkeypatch):
    (tree / "blob.bin").write_bytes(b"def hello\x00\x01binary")
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="hello")
    assert "blob.bin" not in out


async def test_grep_context(tree: Path, monkeypatch):
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)  # deterministic format
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="return 'world'", context=1)
    assert "def hello" in out  # context line above the match


async def test_grep_outside_allowed_dir(tmp_path: Path):
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    tool = GrepTool(workspace=workspace, allowed_dirs=(workspace,))
    out = await tool.execute(pattern="x", path="/etc")
    assert "Error" in out


async def test_grep_uses_bundled_rg_without_path(tree: Path, monkeypatch):
    monkeypatch.setenv("PATH", "")

    def unexpected_fallback(*args, **kwargs):
        pytest.fail("The installed rg must work without its scripts directory on PATH")

    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    monkeypatch.setattr(tool, "_run_python", unexpected_fallback)
    out = await tool.execute(pattern="def hello")
    assert "src/app.py:2:def hello():" in out
    assert "node_modules" not in out
    assert await tool.execute(pattern="zzz_nonexistent") == "No matches found."


@pytest.mark.parametrize("system_rg", ["/system/rg", None])
def test_rg_resolution_without_bundled_binary(tmp_path, monkeypatch, system_rg):
    monkeypatch.setattr(file_search, "sysconfig", SimpleNamespace(get_path=lambda _: str(tmp_path)))
    monkeypatch.setattr(file_search.shutil, "which", lambda _: system_rg)
    assert file_search._resolve_rg() == system_rg


@pytest.mark.parametrize("output_mode", ["content", "files_with_matches", "count"])
async def test_grep_completed_fallback_no_match(tree: Path, monkeypatch, output_mode):
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="zzz_nonexistent", output_mode=output_mode)
    assert out == "No matches found."


# ── find ────────────────────────────────────────────────────────────────


async def test_find_basename_recursive(tree: Path):
    tool = FindTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="*.py")
    assert "src/app.py" in out
    assert "src/util.py" in out
    assert "node_modules" not in out  # ignored


async def test_find_path_pattern(tree: Path):
    tool = FindTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="src/*.py")
    assert "src/app.py" in out
    assert "README.md" not in out


async def test_find_no_match(tree: Path):
    tool = FindTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="*.rs")
    assert out == "No files found matching pattern."


async def test_find_limit(tree: Path):
    tool = FindTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="*.py", limit=1)
    assert "showing first 1 of 2" in out


async def test_find_sorted_by_recency(tree: Path):
    # util.py touched last → should sort first
    import os
    import time

    os.utime(tree / "src" / "util.py", (time.time() + 100, time.time() + 100))
    tool = FindTool(workspace=tree, allowed_dirs=(tree,))
    out = await tool.execute(pattern="*.py")
    lines = out.splitlines()
    assert lines[0].endswith("util.py")
