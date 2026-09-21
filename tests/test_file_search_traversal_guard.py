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


def _expire_after_entries(monkeypatch, entries: int) -> None:
    """The first ``entries`` entries are within budget; the one after is past it.

    The walk reads the clock once for its deadline and once before each entry
    it yields, so the tick after those is what the deadline check sees.
    """
    ticks = itertools.chain([0.0] * (1 + entries), itertools.repeat(tree_walk.WALK_DEADLINE_S + 1))
    monkeypatch.setattr(tree_walk, "time", SimpleNamespace(monotonic=lambda: next(ticks)))


def _expire_after_first_directory(monkeypatch) -> None:
    """The root's two entries (``late/`` then ``a.txt``) fit; ``late/b.txt`` does not."""
    _expire_after_entries(monkeypatch, 2)


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
        yield str(base), "a.txt", False

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
    (tmp_path / "late" / "c.txt").write_text("", encoding="utf-8")
    # The root's three entries fit the budget; ``late/c.txt`` does not.
    _expire_after_entries(monkeypatch, 3)

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
        yield str(base), "a.py", False

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
        "src/lib/c.py",
        "src/lib",
        "src/lib/",
        "*/",
        "src/**/",
        "src/*/",
        "missing/*.py",
        "src/nope.py",
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
async def test_find_starts_at_the_patterns_literal_prefix(tmp_path, monkeypatch):
    """``src/**/*.py`` can match nothing outside ``src``, so the walk begins
    there: a sibling tree visited first used to spend the budget and turn a sure
    hit into a partial miss the advice to narrow the pattern could not fix."""
    _glob_fixture(tmp_path)
    for i in range(50):
        (tmp_path / "unrelated" / f"d{i}").mkdir(parents=True)
    opened: list[str] = []
    real_scandir = os.scandir

    def spy(path=".", *args, **kwargs):
        opened.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", spy)

    result = await FindTool().execute(pattern="src/**/*.py", path=str(tmp_path))

    assert set(result.splitlines()) == {"src/b.py", "src/lib/c.py"}
    assert not [p for p in opened if "unrelated" in p], opened
    assert str(tmp_path) not in opened, "the search root itself is never listed"


@pytest.mark.asyncio
async def test_find_with_a_literal_pattern_answers_without_a_walk(tmp_path, monkeypatch):
    _glob_fixture(tmp_path)
    monkeypatch.setattr(tree_walk, "walk", lambda *a, **k: pytest.fail("a pattern that names one path needs no walk"))

    assert (await FindTool().execute(pattern="src/lib/c.py", path=str(tmp_path))).splitlines() == ["src/lib/c.py"]
    assert (await FindTool().execute(pattern="src/lib/c.py/", path=str(tmp_path))) == "No files found matching pattern."
    assert (await FindTool().execute(pattern="src/nope.py", path=str(tmp_path))) == "No files found matching pattern."


@pytest.mark.asyncio
async def test_find_keeps_a_trailing_slashs_directory_only_meaning(tmp_path):
    _glob_fixture(tmp_path)

    assert set((await FindTool().execute(pattern="*/", path=str(tmp_path))).splitlines()) == {"src/", "tests/"}
    assert set((await FindTool().execute(pattern="src/**/", path=str(tmp_path))).splitlines()) == {"src/", "src/lib/"}


def _slow_clock(monkeypatch, *, per_entry_s: float):
    """A clock the caller's per-entry work advances, so the deadline check sees it."""
    now = [0.0]
    monkeypatch.setattr(tree_walk, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def spend():
        now[0] += per_entry_s

    return spend


@pytest.mark.asyncio
async def test_find_deadline_covers_the_work_done_per_entry(tmp_path, monkeypatch):
    """Eight matches in one flat directory, a stat that costs 30ms each and a
    50ms budget: the old once-per-directory check let all eight through with no
    notice, 240ms late. The check before every entry sees the stats."""
    for i in range(8):
        (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(tree_walk, "WALK_DEADLINE_S", 0.05)
    spend = _slow_clock(monkeypatch, per_entry_s=0.03)
    real_mtime = FindTool._mtime
    monkeypatch.setattr(FindTool, "_mtime", staticmethod(lambda p: (spend(), real_mtime(p))[1]))

    result = await FindTool().execute(pattern="*.txt", path=str(tmp_path))

    paths, notice = result.split("\n\n")
    assert len(paths.splitlines()) == 2, result
    assert notice.startswith("(PARTIAL result:")


@pytest.mark.asyncio
async def test_grep_fallback_deadline_covers_the_work_done_per_file(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search, "_resolve_rg", lambda: None)
    for i in range(8):
        (tmp_path / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(tree_walk, "WALK_DEADLINE_S", 0.05)
    spend = _slow_clock(monkeypatch, per_entry_s=0.03)
    real_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: (spend(), real_read(self))[1])

    result = await GrepTool().execute(pattern="needle", path=str(tmp_path), output_mode="files_with_matches")

    assert result.lower().startswith("warning: search incomplete")
    assert len([line for line in result.splitlines() if line.endswith(".txt")]) == 2


def test_the_walk_checks_the_deadline_before_every_entry(tmp_path, monkeypatch):
    for i in range(4):
        (tmp_path / f"f{i}").write_text("", encoding="utf-8")
    _expire_after_entries(monkeypatch, 2)

    seen = []
    with pytest.raises(TimeoutError):
        for _root, name, _is_dir in tree_walk.walk(tmp_path):
            seen.append(name)

    assert seen == ["f0", "f1"]


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
        yield str(base), "a.txt", False

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
