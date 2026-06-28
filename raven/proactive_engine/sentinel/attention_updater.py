"""AttentionUpdater — runs a list of ``AttentionProducer`` instances on
each Sentinel tick and writes ``user_memory/attention.md`` once under
the dedicated fcntl lock.

Two-phase execution so slow producers (LLM calls in P4-C) don't hold
the attention.md lock:

1. **Compute (outside lock)**: For each producer, call
   ``should_run(now)`` then ``compute_body(now)``. Async-friendly so
   awaitable producers can be interleaved by the event loop.
2. **Splice (under lock)**: Read attention.md once, run
   :func:`upsert_section` for every (header, body) pair, write the
   merged text back if anything changed.

Compare-and-skip: when the new merged text equals the existing on-disk
content, no write happens — useful on cold ticks where no producer's
state has changed.

Per-producer failures (``should_run`` or ``compute_body`` raising) are
logged and the section is skipped; one bad producer does NOT block
the others.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator, Sequence, TYPE_CHECKING

from loguru import logger

from raven.memory_engine.consolidate.attention import upsert_section

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.proactive_engine.sentinel.attention_producers import (
        AttentionProducer,
    )


class AttentionUpdater:
    """Orchestrator over a list of :class:`AttentionProducer` instances."""

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        producers: "Sequence[AttentionProducer]",
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.producers = list(producers)
        self._now_fn = now_fn or datetime.now

    async def update(self) -> dict[str, bool]:
        """Refresh every producer's section. Returns a per-section dict
        ``{header: was_changed}``. ``False`` covers both "producer
        skipped via should_run" and "produced same body as on disk"."""
        now = self._now_fn()
        # Phase 1: compute (outside lock).
        bodies: dict[str, str] = {}
        for p in self.producers:
            header = p.SECTION_HEADER
            try:
                if not p.should_run(now):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AttentionProducer {} should_run failed: {}", header, exc,
                )
                continue
            try:
                body = await p.compute_body(now)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "AttentionProducer {} compute_body failed: {}", header, exc,
                )
                continue
            bodies[header] = body
        # Phase 2: splice (under lock).
        return self._splice_and_write(bodies)

    def _splice_and_write(self, bodies: dict[str, str]) -> dict[str, bool]:
        if not bodies:
            return {}
        with self._locked():
            attention_file = self.memory_store.attention_file
            current = (
                attention_file.read_text(encoding="utf-8")
                if attention_file.exists() else ""
            )
            new_text = current
            changed: dict[str, bool] = {}
            for header, body in bodies.items():
                proposed = upsert_section(new_text, header, body)
                changed[header] = proposed != new_text
                new_text = proposed
            if new_text == current:
                return {k: False for k in bodies}
            try:
                attention_file.parent.mkdir(parents=True, exist_ok=True)
                attention_file.write_text(new_text, encoding="utf-8")
            except OSError as exc:
                logger.warning("AttentionUpdater write failed: {}", exc)
                return {k: False for k in bodies}
            n_changed = sum(1 for v in changed.values() if v)
            logger.debug(
                "AttentionUpdater: refreshed {} of {} sections in {}",
                n_changed, len(bodies), attention_file,
            )
            return changed

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.memory_store.locked_attention():
            yield


__all__ = ["AttentionUpdater"]
