"""grep/find guards against system-root traversal and unbounded os.walk.

A model that runs `grep <pat> /` (or find over /) would otherwise walk the
whole host — including slow mounts under /proc, /sys — and wedge the loop.
The registry timeout can't save this: os.walk is synchronous, so wait_for
cannot preempt it. The guard + in-walk deadline live inside the tool.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.agent.tools import file_search
from raven.agent.tools.file_search import FindTool, GrepTool


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
    monkeypatch.setattr(file_search, "_WALK_DEADLINE_S", -1.0)
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
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    late = tmp_path / "late"
    late.mkdir()
    (late / "b.txt").write_text("needle\n", encoding="utf-8")
    ticks = iter([0.0, 0.0, file_search._WALK_DEADLINE_S + 1])
    monkeypatch.setattr(file_search, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    result = await GrepTool().execute(pattern="needle", path=str(tmp_path), output_mode=output_mode)

    assert expected in result
    assert "b.txt" not in result
    assert "search incomplete" in result.lower()
    assert "No matches found" not in result
