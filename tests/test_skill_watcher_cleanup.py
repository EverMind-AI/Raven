"""The suite must not accumulate SKILL.md watcher threads.

``LocalSkillCatalog`` starts one by default, and its daemon thread holds a
strong reference back to the catalog through the ``on_change`` bound method, so
nothing collects it once the test that built it returns. Measured on this tree
before the cleanup existed: six test files, 172 tests, 81 live watcher threads
left behind -- 81 of the 82 threads in the process. Across the whole suite a
worker carried hundreds, each holding its own inotify handles.

The catalog documents the rule (short-lived consumers pass
``start_watcher=False``) and every short-lived *production* consumer follows it;
a test is a short-lived consumer that never did.
"""

from __future__ import annotations

import threading
from pathlib import Path

from tests.conftest import stopping_skill_watchers


def _live_watchers() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "SkillFileWatcher" and t.is_alive())


def _catalog(workspace: Path):
    from raven.memory_engine.skill_forge import LocalSkillCatalog

    (workspace / "skills").mkdir(parents=True, exist_ok=True)
    return LocalSkillCatalog(workspace)


def test_a_catalogs_watcher_is_stopped_when_the_block_ends(tmp_path: Path) -> None:
    """The contract the autouse fixture rests on.

    Asserted from both sides: the watcher really runs inside the block, so a
    passing test cannot mean "no watcher was ever started", which is the shape
    an absent-state assertion would accept.
    """
    before = _live_watchers()

    with stopping_skill_watchers():
        catalog = _catalog(tmp_path)
        assert catalog.start_file_watcher() is False, "the catalog starts its own watcher"
        assert _live_watchers() == before + 1

    assert _live_watchers() == before


def test_every_watcher_in_the_block_is_stopped_not_just_the_last(tmp_path: Path) -> None:
    # One test can build several catalogs, and the leak is per catalog.
    before = _live_watchers()

    with stopping_skill_watchers():
        for i in range(3):
            _catalog(tmp_path / f"ws{i}")
        assert _live_watchers() == before + 3

    assert _live_watchers() == before


def test_the_block_restores_the_real_start_method(tmp_path: Path) -> None:
    """The tracker patches ``SkillFileWatcher.start``; leaving it patched would
    make every later test run through a wrapper the suite never asked for."""
    from raven.memory_engine.skill_local.watcher import SkillFileWatcher

    original = SkillFileWatcher.start
    with stopping_skill_watchers():
        assert SkillFileWatcher.start is not original
    assert SkillFileWatcher.start is original


def test_a_watcher_that_never_started_is_not_an_error(tmp_path: Path) -> None:
    # `start()` returns False for a missing root or a missing watchfiles; the
    # tracker must not then try to stop something that was never running.
    from raven.memory_engine.skill_local.watcher import SkillFileWatcher

    with stopping_skill_watchers():
        watcher = SkillFileWatcher(
            roots=[tmp_path / "absent"], on_change=lambda _s: None, resolve_source=lambda _p: None
        )
        assert watcher.start() is False
