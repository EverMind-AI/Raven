"""Unit tests for the grep and glob search tools.

Tests exercise both the ripgrep-backed path (bundled via ripgrep-bin, resolved
by ``resolve_rg``) and the pure-Python fallback (forced by patching
``resolve_rg`` to return None — patching shutil.which is no longer enough now
that a bundled binary outranks PATH).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import raven.agent.tools.file_search as fs
from raven.agent.tools.file_search import FindTool, GrepTool, _expand_braces
from raven.agent.tools.registry import ToolRegistry


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
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern=r"def hello")
    assert "app.py" in out
    assert "node_modules" not in out


async def test_grep_fallback_skips_binary(tree: Path, monkeypatch):
    (tree / "blob.bin").write_bytes(b"def hello\x00\x01binary")
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello")
    assert "blob.bin" not in out


async def test_grep_context(tree: Path, monkeypatch):
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)  # deterministic format
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="return 'world'", context=1)
    assert "def hello" in out  # context line above the match


async def test_grep_outside_allowed_dir(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = GrepTool(workspace=workspace, allowed_dir=workspace)
    out = await tool.execute(pattern="x", path="/etc")
    assert "Error" in out


# ── glob ────────────────────────────────────────────────────────────────


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
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello", glob="*.{py,md}")
    assert "app.py" in out
    assert "README.md" in out


@pytest.mark.skipif(fs.resolve_rg() is None, reason="ripgrep not resolvable; CI runs a with-rg leg")
async def test_grep_glob_brace_rg(tree: Path):
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello", glob="*.{py,md}")
    assert "app.py" in out
    assert "README.md" in out


async def test_grep_fallback_path_glob_is_explicit_error(tree: Path, monkeypatch):
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="hello", glob="src/*.py")
    assert out.startswith("Error")
    assert "ripgrep" in out


async def test_grep_fallback_deadline_cut_is_declared(tree: Path, monkeypatch):
    """A walk stopped at the deadline must never report a confident empty
    result — absence over a partial scan proves nothing."""
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    monkeypatch.setattr(fs, "_WALK_DEADLINE_S", -1.0)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="def hello")
    assert "results are incomplete" in out
    assert out != "No matches found."


async def test_grep_fallback_full_walk_keeps_plain_empty_message(tree: Path, monkeypatch):
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="zzz_no_such_symbol_zzz")
    assert out == "No matches found."


# ── rg resolution ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_rg_cache():
    """resolve_rg caches for the process lifetime; tests must not leak a
    resolution made under one monkeypatch into the next test. getattr because
    tests that replace resolve_rg with a plain lambda are still patched when
    this teardown runs (fixture teardown precedes monkeypatch undo)."""

    def clear() -> None:
        cache_clear = getattr(fs.resolve_rg, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()

    clear()
    yield
    clear()


def test_resolve_rg_env_override_wins(tmp_path: Path, monkeypatch):
    fake = tmp_path / "my-rg"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("RAVEN_RG", str(fake))
    assert fs.resolve_rg() == str(fake)


def test_resolve_rg_bad_override_falls_through(tmp_path: Path, monkeypatch):
    """A dangling RAVEN_RG must not brick grep — it warns and keeps resolving."""
    monkeypatch.setenv("RAVEN_RG", str(tmp_path / "missing-rg"))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "rg").write_text("#!/bin/sh\n")
    monkeypatch.setattr(fs.sysconfig, "get_path", lambda kind: str(scripts))
    assert fs.resolve_rg() == str(scripts / "rg")


def test_resolve_rg_bundled_outranks_path(tmp_path: Path, monkeypatch):
    """The binary raven ships must win over whatever the host has on PATH —
    host rg versions are exactly the variance bundling exists to remove."""
    monkeypatch.delenv("RAVEN_RG", raising=False)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "rg").write_text("#!/bin/sh\n")
    monkeypatch.setattr(fs.sysconfig, "get_path", lambda kind: str(scripts))
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/rg")
    assert fs.resolve_rg() == str(scripts / "rg")


def test_resolve_rg_path_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RAVEN_RG", raising=False)
    monkeypatch.setattr(fs.sysconfig, "get_path", lambda kind: str(tmp_path / "empty"))
    monkeypatch.setattr(shutil, "which", lambda _: "/host/rg")
    assert fs.resolve_rg() == "/host/rg"


# ── spill on truncation ─────────────────────────────────────────────────


@pytest.fixture
def spill_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ~/.raven/tool-output into the test tree."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / ".raven" / "tool-output"


async def test_grep_cap_overflow_spills_complete_result(tree: Path, spill_home: Path, monkeypatch):
    """Truncation must never lose evidence: the cut lines are recoverable from
    the spill file, so the model narrows with knowledge instead of blind."""
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    (tree / "many.py").write_text("".join(f"hit_{i} = {i}\n" for i in range(50)))
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern=r"hit_\d+", limit=10)
    assert "showing first 10 of 50" in out
    assert "saved to" in out
    spilled = next(spill_home.glob("grep-*.log"))
    content = spilled.read_text()
    assert "hit_49" in content  # the tail the cap dropped is all there
    assert content.count("hit_") == 50


async def test_grep_under_cap_does_not_spill(tree: Path, spill_home: Path, monkeypatch):
    monkeypatch.setattr(fs, "resolve_rg", lambda: None)
    tool = GrepTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern=r"def hello")
    assert "saved to" not in out
    assert not spill_home.exists() or not list(spill_home.glob("grep-*.log"))


async def test_find_cap_overflow_spills_complete_list(tree: Path, spill_home: Path):
    for i in range(20):
        (tree / f"gen_{i:02d}.py").write_text("x = 1\n")
    tool = FindTool(workspace=tree, allowed_dir=tree)
    out = await tool.execute(pattern="gen_*.py", limit=5)
    assert "showing first 5 of 20" in out
    assert "complete list saved to" in out
    spilled = next(spill_home.glob("find-*.log"))
    assert spilled.read_text().count("gen_") == 20


# ── glob rename + grep context cap ──────────────────────────────────────


def test_grep_context_schema_has_no_maximum():
    schema = GrepTool().parameters["properties"]["context"]
    assert "maximum" not in schema
    assert schema["minimum"] == 0


async def test_grep_context_30_passes_schema_and_executes(tree: Path):
    registry = ToolRegistry()
    registry.register(GrepTool(workspace=tree, allowed_dir=tree))
    result = str(await registry.execute("grep", {"pattern": "helper", "path": "src", "context": 30}))
    assert "Invalid parameters" not in result
    assert "util.py" in result


def test_find_tool_is_named_glob():
    tool = FindTool()
    assert tool.name == "glob"
    assert "find" in tool.aliases


async def test_registry_resolves_find_alias_to_glob(tree: Path):
    registry = ToolRegistry()
    tool = FindTool(workspace=tree, allowed_dir=tree)
    registry.register(tool)
    assert registry.get("find") is registry.get("glob") is tool
    assert registry.has("find")
    assert "find" in registry
    assert registry.canonical_name("find") == "glob"
    result = str(await registry.execute("find", {"pattern": "*.py"}))
    assert "app.py" in result


def test_wire_definitions_expose_only_glob(tree: Path):
    registry = ToolRegistry()
    registry.register(FindTool(workspace=tree, allowed_dir=tree))
    names = [d["function"]["name"] for d in registry.get_definitions()]
    assert names == ["glob"]


def test_unregister_by_legacy_name_removes_glob(tree: Path):
    registry = ToolRegistry()
    registry.register(FindTool(workspace=tree, allowed_dir=tree))
    registry.unregister("find")
    assert not registry.has("glob")
    assert not registry.has("find")
