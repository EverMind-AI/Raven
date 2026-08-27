"""MemoryStore extensions for attention.md / behaviors.md (P1 file layer)."""

from __future__ import annotations

from pathlib import Path

from raven.memory_engine.consolidate.consolidator import MemoryStore


def test_paths_under_user_memory_root(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.attention_file == tmp_path / "user_memory" / "attention.md"
    assert store.behaviors_file == tmp_path / "user_memory" / "behaviors.md"
    assert store.behaviors_offsets_path == (tmp_path / "user_memory" / ".behaviors_offsets.json")


def test_lock_paths_are_siblings(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.attention_lock_path == store.attention_file.with_suffix(".md.lock")
    assert store.behaviors_lock_path == store.behaviors_file.with_suffix(".md.lock")


def test_locks_are_independent_of_memory_lock(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.attention_lock_path != store.memory_lock_path
    assert store.behaviors_lock_path != store.memory_lock_path
    assert store.attention_lock_path != store.behaviors_lock_path


def test_locked_attention_creates_lock_file_on_first_use(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path)
    with store.locked_attention():
        assert store.attention_lock_path.exists()


def test_locked_behaviors_creates_lock_file_on_first_use(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path)
    with store.locked_behaviors():
        assert store.behaviors_lock_path.exists()


def test_locked_attention_is_reentrant_across_calls(tmp_path: Path) -> None:
    """Acquiring + releasing then acquiring again must succeed without hang."""
    store = MemoryStore(tmp_path)
    with store.locked_attention():
        pass
    with store.locked_attention():
        pass
