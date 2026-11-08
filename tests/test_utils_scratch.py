"""A temporary directory an agent worked in goes once the agent has, and never takes a verdict with it."""

from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest
from loguru import logger

from raven.utils import scratch
from raven.utils.scratch import ScratchDir
from tests._windows_dirs import scratch_root, windows_removal


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_root(tmp_path, monkeypatch)


async def test_the_directory_and_what_was_left_in_it_go_when_the_block_ends(_root: Path) -> None:
    async with ScratchDir("raven_check_") as path:
        assert path.parent == _root and path.name.startswith("raven_check_")
        (path / "nested").mkdir()
        (path / "nested" / "left.txt").write_text("x", encoding="utf-8")

    assert not path.exists()
    assert list(_root.iterdir()) == []


async def test_a_read_only_file_left_in_it_does_not_keep_the_directory() -> None:
    """Windows will not delete a read-only file, and git marks its objects so.

    An agent that ran ``git init`` where it was put leaves some. POSIX deletes
    them regardless, so this case only bites on Windows.
    """
    async with ScratchDir("raven_check_") as path:
        kept = path / "object"
        kept.write_text("x", encoding="utf-8")
        kept.chmod(stat.S_IREAD)

    assert not path.exists()


async def test_a_directory_still_in_use_is_tried_again_until_the_process_lets_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ending a process tree returns before the last of it has exited, so a first try can be refused."""
    made = ScratchDir("raven_check_")
    asked: list[Path] = []

    def holding(path: Path) -> bool:
        asked.append(path)
        return len(asked) < 3

    refused = windows_removal(monkeypatch, holding)

    await made.remove()

    assert refused == [made.path, made.path]
    assert not made.path.exists()


async def test_the_tries_are_spread_over_the_time_a_tree_takes_to_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tries made back to back all land inside the exit they are there to wait out."""
    monkeypatch.setattr(scratch, "_REMOVE_PAUSE_S", 0.05)
    made = ScratchDir("raven_check_")
    first_try: list[float] = []

    def holding(path: Path) -> bool:
        first_try.append(first_try[0] if first_try else time.monotonic())
        return time.monotonic() < first_try[0] + 0.08

    refused = windows_removal(monkeypatch, holding)

    await made.remove()

    assert refused, "the first try was not refused, so this tested nothing"
    assert not made.path.exists()


async def test_a_directory_that_stays_in_use_is_left_and_logged_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller reports an agent's answer, and a removal failure raised in its place says the agent failed."""
    made = ScratchDir("raven_check_")
    refused = windows_removal(monkeypatch, lambda path: path == made.path)
    warnings: list[str] = []
    sink = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
    try:
        await made.remove()
    finally:
        logger.remove(sink)

    assert len(refused) == scratch._REMOVE_ATTEMPTS
    assert made.path.is_dir()
    assert len(warnings) == 1
    assert str(made.path) in warnings[0]
