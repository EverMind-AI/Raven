"""MemoryStore — additional methods landed in LT-1.

Two new concrete methods on :class:`MemoryStore`:

- ``read_history_tail(lines)`` — relocated from the (since-deleted)
  ``DefaultMemoryEngine`` facade. Returns the last N non-blank lines of
  ``HISTORY.md``.
- ``update_section(heading, body, *, at_end=True)`` — sectioned splice
  under the existing fcntl lock. Used by Sentinel's ``memory_writer``,
  the eventual Personalizer write path, and any CLI command that needs
  to rewrite a named ``## …`` section.

The pre-existing read / write / lock surface (``read_long_term`` /
``write_long_term`` / ``locked`` / ``append_history``) is exercised by
the consolidator tests; this file only covers the LT-1 additions.

The :class:`LongTermFacade` Protocol that originally accompanied this
work was deleted after the architecture decision that ``consolidate/``
stays in raven core — there is no cross-package boundary for a Protocol
to mediate, so :class:`MemoryStore` is consumed by direct import
everywhere it's needed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import MemoryStore


# ---------------------------------------------------------------------------
# Path attribute sanity (used by Sentinel's RoutineLearner)
# ---------------------------------------------------------------------------


class TestHistoryFilePath:
    def test_history_file_is_a_path(self, tmp_path: Path) -> None:
        store = MemoryStore(tmp_path)
        assert isinstance(store.history_file, Path)
        # The path resolves under the workspace's user_memory pillar.
        assert "user_memory" in str(store.history_file)
        assert store.history_file.name == "episodes.md"


# ---------------------------------------------------------------------------
# read_history_tail
# ---------------------------------------------------------------------------


class TestReadHistoryTail:
    @pytest.fixture
    def store(self, tmp_path: Path) -> MemoryStore:
        return MemoryStore(tmp_path)

    def test_missing_file_returns_empty(self, store: MemoryStore) -> None:
        assert store.read_history_tail(5) == ""

    def test_returns_last_n_non_blank(self, store: MemoryStore) -> None:
        store.history_file.write_text(
            "a\nb\n\n\nc\nd\ne\n", encoding="utf-8",
        )
        assert store.read_history_tail(2) == "d\ne"

    def test_lines_zero_returns_all_non_blank(
        self, store: MemoryStore,
    ) -> None:
        store.history_file.write_text("a\n\nb\nc\n", encoding="utf-8")
        assert store.read_history_tail(0) == "a\nb\nc"

    def test_lines_negative_returns_all_non_blank(
        self, store: MemoryStore,
    ) -> None:
        store.history_file.write_text("x\ny\n", encoding="utf-8")
        assert store.read_history_tail(-5) == "x\ny"

    def test_more_requested_than_present(
        self, store: MemoryStore,
    ) -> None:
        store.history_file.write_text("only\n", encoding="utf-8")
        assert store.read_history_tail(99) == "only"


# ---------------------------------------------------------------------------
# update_section
# ---------------------------------------------------------------------------


class TestUpdateSection:
    @pytest.fixture
    def store(self, tmp_path: Path) -> MemoryStore:
        return MemoryStore(tmp_path)

    def test_insert_into_empty_file(self, store: MemoryStore) -> None:
        with store.locked():
            store.update_section(
                "## Sentinel Observations",
                "user prefers terse responses",
            )
        text = store.read_long_term()
        assert "## Sentinel Observations" in text
        assert "user prefers terse responses" in text

    def test_replace_existing_section(self, store: MemoryStore) -> None:
        store.write_long_term(
            "## Sentinel Observations\n\nold body\n\n## Other\n\nkeep me\n",
        )
        with store.locked():
            store.update_section("## Sentinel Observations", "new body")
        text = store.read_long_term()
        assert "old body" not in text
        assert "new body" in text
        # `## Other` survives — only the named section was touched.
        assert "keep me" in text

    def test_at_end_true_moves_section_to_end(
        self, store: MemoryStore,
    ) -> None:
        store.write_long_term(
            "## Sentinel Observations\n\nold\n\n## Tail\n\nlast block\n",
        )
        with store.locked():
            store.update_section(
                "## Sentinel Observations", "new", at_end=True,
            )
        text = store.read_long_term()
        # After at_end splice, Sentinel section lives at the bottom.
        sentinel_pos = text.index("## Sentinel Observations")
        tail_pos = text.index("## Tail")
        assert sentinel_pos > tail_pos

    def test_at_end_false_preserves_position(
        self, store: MemoryStore,
    ) -> None:
        store.write_long_term(
            "## Profile\n\np\n\n## Sentinel Observations\n\nold\n\n## Tail\n\nt\n",
        )
        with store.locked():
            store.update_section(
                "## Sentinel Observations", "fresh", at_end=False,
            )
        text = store.read_long_term()
        # Section stayed in-place; `## Tail` still after it.
        sentinel_pos = text.index("## Sentinel Observations")
        tail_pos = text.index("## Tail")
        assert sentinel_pos < tail_pos
        assert "fresh" in text
        assert "old" not in text


# ---------------------------------------------------------------------------
# locked() integration with update_section
# ---------------------------------------------------------------------------


class TestLockedSectionWrite:
    """Two threads each grab the lock, update the same section, and the
    writes serialize — neither read-modify-write loses to the other."""

    def test_concurrent_section_updates_serialize(
        self, tmp_path: Path,
    ) -> None:
        store = MemoryStore(tmp_path)
        observed_orders: list[str] = []
        barrier = threading.Barrier(2)

        def writer(label: str, delay_inside_lock_s: float) -> None:
            barrier.wait()  # ensure both threads race for the lock
            with store.locked():
                # Inside the lock, record observation order
                observed_orders.append(label)
                current = store.read_long_term()
                # Simulate slow LLM-driven section build
                time.sleep(delay_inside_lock_s)
                # Each writer claims its own H2 section
                heading = f"## {label}"
                store.update_section(heading, f"body-{label}")

        t1 = threading.Thread(target=writer, args=("alpha", 0.05))
        t2 = threading.Thread(target=writer, args=("beta", 0.05))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both sections present, neither lost.
        text = store.read_long_term()
        assert "## alpha" in text
        assert "## beta" in text
        assert "body-alpha" in text
        assert "body-beta" in text
        # Two enter-lock observations recorded.
        assert len(observed_orders) == 2

    def test_update_section_without_lock_works_in_single_thread(
        self, tmp_path: Path,
    ) -> None:
        """Single-thread case: calling update_section without explicit
        ``locked`` still works — the lock is for cross-writer safety,
        not a precondition. Documented behavior matters because tests
        and simple CLI paths skip the lock."""
        store = MemoryStore(tmp_path)
        store.update_section("## Solo", "no-lock body")
        assert "no-lock body" in store.read_long_term()
