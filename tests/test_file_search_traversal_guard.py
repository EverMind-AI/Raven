"""grep/find guards against system-root traversal and unbounded os.walk.

A model that runs `grep <pat> /` (or find over /) would otherwise walk the
whole host — including slow mounts under /proc, /sys — and wedge the loop.
The registry timeout can't save this: os.walk is synchronous, so wait_for
cannot preempt it. The guard + in-walk deadline live inside the tool.
"""

from __future__ import annotations

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
    monkeypatch.setattr(file_search.shutil, "which", lambda *_a, **_k: None)
    (tmp_path / "a.txt").write_text("the needle is here\n", encoding="utf-8")

    result = await GrepTool().execute(pattern="needle", path=str(tmp_path))
    assert "needle" in result
    assert "a.txt" in result


@pytest.mark.asyncio
async def test_grep_walk_deadline_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setattr(file_search.shutil, "which", lambda *_a, **_k: None)
    # Deadline already in the past -> the walk bails before yielding any file.
    monkeypatch.setattr(file_search, "_WALK_DEADLINE_S", -1.0)
    (tmp_path / "a.txt").write_text("the needle is here\n", encoding="utf-8")

    result = await GrepTool().execute(pattern="needle", path=str(tmp_path))
    # Walk short-circuited -> the otherwise-matching file is not found.
    assert "needle" not in result
    assert "a.txt" not in result
