"""The tree-walking tools cannot wedge the gateway on a tree the model names.

``grep``, ``find`` and ``list_dir`` all take a directory, and a model will
name a home directory. A system root is refused outright; anything else is
walked with the noise directories pruned as it goes, under a wall-clock
deadline that turns a huge tree into a partial result that says so, and off
the event loop. The registry's ``wait_for`` ceiling cannot preempt synchronous
code, so all three bounds live inside the walk.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.tools import file_search, tree_walk
from raven.agent.tools.file_search import FindTool, GrepTool
from raven.agent.tools.filesystem import ListDirTool


def _expire_after_first_directory(monkeypatch) -> None:
    """The first directory is within budget; every directory after it is past it."""
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(tree_walk.WALK_DEADLINE_S + 1))
    monkeypatch.setattr(tree_walk, "time", SimpleNamespace(monotonic=lambda: next(ticks)))


def _tree_with_a_late_match(root: Path) -> None:
    (root / "a.txt").write_text("needle\n", encoding="utf-8")
    late = root / "late"
    late.mkdir()
    (late / "b.txt").write_text("needle\n", encoding="utf-8")


# --- refusing a system root ---------------------------------------------------


@pytest.mark.asyncio
async def test_grep_refuses_system_root():
    result = await GrepTool().execute(pattern="anything", path="/")
    assert "refusing to search" in result
    assert "system root" in result


@pytest.mark.asyncio
async def test_find_refuses_system_root():
    result = await FindTool().execute(pattern="*.py", path="/")
    assert "refusing to search" in result
    assert "system root" in result


# --- grep's pure-Python fallback ---------------------------------------------------


@pytest.mark.asyncio
async def test_grep_normal_search_still_works(tmp_path, monkeypatch):
    # Force the pure-Python os.walk fallback so the deadline path is exercised.
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    (tmp_path / "a.txt").write_text("the needle is here\n", encoding="utf-8")

    result = await GrepTool().execute(pattern="needle", path=str(tmp_path))
    assert "needle" in result
    assert "a.txt" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("output_mode", ["content", "files_with_matches", "count"])
async def test_grep_walk_deadline_short_circuits(tmp_path, monkeypatch, output_mode):
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    # Deadline already in the past -> the walk bails before yielding any file.
    monkeypatch.setattr(tree_walk, "WALK_DEADLINE_S", -1.0)
    (tmp_path / "a.txt").write_text("the needle is here\n", encoding="utf-8")

    result = await GrepTool().execute(pattern="needle", path=str(tmp_path), output_mode=output_mode)
    # Walk short-circuited -> the otherwise-matching file is not found.
    assert "needle" not in result
    assert "a.txt" not in result
    assert "search incomplete" in result.lower()
    assert "No matches found" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_mode", "expected"),
    [("content", "a.txt:1:needle"), ("files_with_matches", "a.txt"), ("count", "a.txt:1")],
)
async def test_grep_walk_deadline_preserves_partial_matches(tmp_path, monkeypatch, output_mode, expected):
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    _tree_with_a_late_match(tmp_path)
    _expire_after_first_directory(monkeypatch)

    result = await GrepTool().execute(pattern="needle", path=str(tmp_path), output_mode=output_mode)

    assert expected in result
    assert "b.txt" not in result
    assert "search incomplete" in result.lower()
    assert "No matches found" not in result


@pytest.mark.asyncio
async def test_grep_fallback_walk_runs_off_the_event_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    release = threading.Event()

    def blocking_walk(base, **_):
        release.wait(2.0)
        yield str(base), [], ["a.txt"]

    monkeypatch.setattr(tree_walk, "walk", blocking_walk)
    started = time.monotonic()
    search = asyncio.ensure_future(GrepTool().execute(pattern="needle", path=str(tmp_path)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    turnaround = time.monotonic() - started
    release.set()
    result = await search

    assert "a.txt:1:needle" in result
    assert turnaround < 1.0, f"the event loop was held for {turnaround:.2f}s"


# --- find ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_never_enters_a_noise_directory(tmp_path, monkeypatch):
    """Pruning happens during the walk, not by filtering afterwards: the
    directory where most of a home tree's inodes live is never even opened."""
    (tmp_path / "keep.py").write_text("", encoding="utf-8")
    noise = tmp_path / "node_modules" / "pkg"
    noise.mkdir(parents=True)
    (noise / "drop.py").write_text("", encoding="utf-8")
    opened: list[str] = []
    real_scandir = os.scandir

    def spy(path=".", *args, **kwargs):
        opened.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", spy)

    result = await FindTool().execute(pattern="*.py", path=str(tmp_path))

    assert result.splitlines() == ["keep.py"]
    assert not [p for p in opened if "node_modules" in p], opened


@pytest.mark.asyncio
async def test_find_walk_deadline_returns_what_it_found_and_says_so(tmp_path, monkeypatch):
    _tree_with_a_late_match(tmp_path)
    _expire_after_first_directory(monkeypatch)

    result = await FindTool().execute(pattern="*.txt", path=str(tmp_path))

    assert result.startswith("a.txt\n")
    assert "b.txt" not in result
    assert "PARTIAL result" in result and "traversal budget" in result
    assert "No files found" not in result


@pytest.mark.asyncio
async def test_find_walk_deadline_before_any_match_is_not_a_clean_miss(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(tree_walk, "WALK_DEADLINE_S", -1.0)

    result = await FindTool().execute(pattern="*.txt", path=str(tmp_path))

    assert "a.txt" not in result
    assert "PARTIAL result" in result and "traversal budget" in result
    assert result != "No files found matching pattern."


@pytest.mark.asyncio
async def test_find_reports_the_limit_and_the_deadline_in_one_notice(tmp_path, monkeypatch):
    """Both conditions ride the one parenthesised trailer after the blank line,
    which is the shape the raven-code glob tool strips before merging."""
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    (tmp_path / "late").mkdir()
    _expire_after_first_directory(monkeypatch)

    result = await FindTool().execute(pattern="*", path=str(tmp_path), limit=1)

    paths, notice = result.split("\n\n")
    assert len(paths.splitlines()) == 1
    assert notice.startswith("(showing first 1 of 3 results; PARTIAL result:") and notice.endswith(")")


@pytest.mark.asyncio
async def test_find_walk_runs_off_the_event_loop(tmp_path, monkeypatch):
    """A walk that takes its time must not take the loop with it: while the
    worker thread is inside the walk, the loop keeps turning."""
    release = threading.Event()

    def blocking_walk(base, **_):
        release.wait(2.0)
        yield str(base), [], ["a.py"]

    monkeypatch.setattr(tree_walk, "walk", blocking_walk)
    started = time.monotonic()
    search = asyncio.ensure_future(FindTool().execute(pattern="*.py", path=str(tmp_path)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The walk blocks its thread for two seconds; a loop it does not block is
    # back here in microseconds. The second is the margin for a slow runner.
    turnaround = time.monotonic() - started
    release.set()
    result = await search

    assert result.splitlines() == ["a.py"]
    assert turnaround < 1.0, f"the event loop was held for {turnaround:.2f}s"


def _glob_fixture(root: Path) -> None:
    for rel in ("a.py", "README.md", ".hidden.py", "src/b.py", "src/lib/c.py", "src/lib/c.pyi", "tests/a.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "d.py").write_text("", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern",
    [
        "*.py",
        "**/*.py",
        "src/*.py",
        "src/**/*.py",
        "a.py",
        "**/lib/c.py",
        "[ab].py",
        "?.py",
        "*.md",
        "lib",
        "**/lib",
        ".*",
    ],
)
async def test_find_matches_what_path_glob_matched(tmp_path, pattern):
    """The rewrite answers exactly what ``Path.glob`` answered for the same
    pattern, with the noise directories removed -- ``Path.glob`` is the oracle."""
    _glob_fixture(tmp_path)
    expr = pattern if "/" in pattern else f"**/{pattern}"
    expected = set()
    for p in tmp_path.glob(expr):
        rel = p.relative_to(tmp_path)
        if not any(part in tree_walk.IGNORE_DIRS for part in rel.parts):
            expected.add(rel.as_posix() + ("/" if p.is_dir() else ""))

    result = await FindTool().execute(pattern=pattern, path=str(tmp_path))

    got = set() if result == "No files found matching pattern." else set(result.splitlines())
    assert got == expected


@pytest.mark.asyncio
async def test_find_trailing_double_star_matches_files_beneath_the_prefix(tmp_path):
    _glob_fixture(tmp_path)

    result = await FindTool().execute(pattern="src/**", path=str(tmp_path))

    assert set(result.splitlines()) == {"src/", "src/b.py", "src/lib/", "src/lib/c.py", "src/lib/c.pyi"}


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["../*.py", "/etc/*", "", "a**b"])
async def test_find_refuses_a_pattern_that_reaches_outside_or_cannot_be_read(tmp_path, pattern):
    (tmp_path / "a.py").write_text("", encoding="utf-8")

    result = await FindTool().execute(pattern=pattern, path=str(tmp_path))

    assert result.startswith("Error running find: ")


# --- list_dir --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dir_recursive_lists_what_rglob_listed_in_the_same_order(tmp_path):
    _glob_fixture(tmp_path)
    expected = [
        f"{p.relative_to(tmp_path)}/" if p.is_dir() else str(p.relative_to(tmp_path))
        for p in sorted(tmp_path.rglob("*"))
        if not any(part in tree_walk.IGNORE_DIRS for part in p.relative_to(tmp_path).parts)
    ]

    result = await ListDirTool().execute(path=str(tmp_path), recursive=True)

    assert result.splitlines() == expected
    assert "node_modules" not in result


@pytest.mark.asyncio
async def test_list_dir_recursive_never_enters_a_noise_directory(tmp_path, monkeypatch):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "keep.txt").write_text("", encoding="utf-8")
    opened: list[str] = []
    real_scandir = os.scandir

    def spy(path=".", *args, **kwargs):
        opened.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", spy)

    result = await ListDirTool().execute(path=str(tmp_path), recursive=True)

    assert result.splitlines() == ["keep.txt"]
    assert not [p for p in opened if "node_modules" in p], opened


@pytest.mark.asyncio
async def test_list_dir_recursive_deadline_returns_what_it_found_and_says_so(tmp_path, monkeypatch):
    _tree_with_a_late_match(tmp_path)
    _expire_after_first_directory(monkeypatch)

    result = await ListDirTool().execute(path=str(tmp_path), recursive=True)

    assert result.startswith("a.txt\nlate/\n")
    assert "b.txt" not in result
    assert "PARTIAL result" in result and "traversal budget" in result
    assert "is empty" not in result


@pytest.mark.asyncio
async def test_list_dir_recursive_deadline_before_anything_is_not_an_empty_directory(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(tree_walk, "WALK_DEADLINE_S", -1.0)

    result = await ListDirTool().execute(path=str(tmp_path), recursive=True)

    assert "is empty" not in result
    assert "PARTIAL result" in result and "traversal budget" in result


@pytest.mark.asyncio
async def test_list_dir_recursive_walk_runs_off_the_event_loop(tmp_path, monkeypatch):
    release = threading.Event()

    def blocking_walk(base, **_):
        release.wait(2.0)
        yield str(base), [], ["a.txt"]

    monkeypatch.setattr(tree_walk, "walk", blocking_walk)
    started = time.monotonic()
    listing = asyncio.ensure_future(ListDirTool().execute(path=str(tmp_path), recursive=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    turnaround = time.monotonic() - started
    release.set()
    result = await listing

    assert result.splitlines() == ["a.txt"]
    assert turnaround < 1.0, f"the event loop was held for {turnaround:.2f}s"


@pytest.mark.asyncio
async def test_list_dir_flat_listing_is_unchanged(tmp_path):
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()

    result = await ListDirTool().execute(path=str(tmp_path))

    assert result.splitlines() == ["📄 a.txt", "📁 src"]
    assert await ListDirTool().execute(path=str(tmp_path / "src")) == f"Directory {tmp_path / 'src'} is empty"
