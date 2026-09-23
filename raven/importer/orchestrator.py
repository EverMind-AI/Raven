"""Cold-start import orchestrator -- read, batch, store, track."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.contracts.memory import MemoryBackend
from raven.importer.state import ImportState
from raven.importer.types import ImportMessage, ImportSession, Scanner, ScanResult

# Both bounds decide where batch boundaries fall, and EverOS derives its
# message_id from (session_id, timestamp_ms, index-within-batch), so those
# boundaries are part of the id: two messages sharing a millisecond collide,
# and one is dropped, if they land at the same index in different batches.
# Ten: EverOS extracts on every add, and that cost is superlinear in the
# message count -- against a real service a 15-message batch took 12s and a
# 52-message batch 24s, while a batch of 100 ran past the six-minute
# extraction budget and failed every memory-file source. With a slower
# extraction model, batches of 50 took 2.4-7.4 minutes and six of seven
# memory-file sources died on that same budget. Ten is the maintainer's
# call: a batch that finishes well inside the budget on any model matters
# more than the fixed cost of about 7s that every add carries -- which is
# also why it is not one message per add.
_BATCH_MSG_LIMIT = 10
_BATCH_CHAR_LIMIT = 30_000

# A batch the memory service refuses is sent again before its source is given
# up on. The refusals seen against a real service were transient -- a rate
# limit at the extraction provider, an answer the extractor could not parse, a
# slow answer past the budget -- and a source that fails on one of them takes
# every message behind it down with it, then the next source runs straight
# into the same wall. Three retries with these waits cover a rate-limit window
# of a couple of minutes; the wait polls the stop file so a stop lands in it.
_STORE_RETRY_BACKOFF_S: tuple[float, ...] = (30.0, 60.0, 120.0)


@dataclass(frozen=True)
class ImportFailure:
    """One failed import unit."""

    platform: str
    source_key: str
    error: str


@dataclass(frozen=True)
class ImportSummary:
    """Aggregate result of a run_import call."""

    total: int
    submitted: int
    skipped: int
    failed: int
    errors: tuple[ImportFailure, ...]
    cancelled: bool = False


@dataclass(frozen=True)
class ProgressEvent:
    """Progress notification emitted once per ScanResult."""

    platform: str
    source_key: str
    status: str
    current: int
    total: int
    error: str | None = None


async def run_import(
    items: Sequence[tuple[Scanner, ScanResult]],
    backend: MemoryBackend,
    state: ImportState,
    *,
    on_progress: Callable[[ProgressEvent], None] | None = None,
    on_batch: Callable[[str, str, int, int], None] | None = None,
    cancel_path: Path | None = None,
) -> ImportSummary:
    """Import pre-filtered scan results into the memory backend.

    The caller (CLI layer) is responsible for scanning, tier/platform
    filtering, and MemoryBackend lifecycle (start/stop). ``on_batch(platform,
    source_key, sent, total)`` reports, per source, how many of its messages
    have landed so far: a large source is many batches and many minutes, and
    the per-source counts alone stand still for all of them.
    """
    total = len(items)
    logger.info("import started: {} items", total)

    submitted = 0
    skipped = 0
    failed = 0
    errors: list[ImportFailure] = []

    for i, (scanner, result) in enumerate(items):
        if cancel_path is not None and cancel_path.exists():
            logger.info("import cancelled by user after {}/{} items", i, total)
            break

        platform = result.platform.value
        key = result.source_key

        if state.is_submitted(platform, key):
            skipped += 1
            logger.info(
                "[{}/{}] skipping {}/{} (already submitted)",
                i + 1,
                total,
                platform,
                key,
            )
            if on_progress:
                on_progress(
                    ProgressEvent(
                        platform=platform,
                        source_key=key,
                        status="skipped",
                        current=i + 1,
                        total=total,
                    )
                )
            continue

        # NOTE: checkpoint is per source unit, not per batch. A multi-batch
        # session that fails mid-way re-sends already-accepted batches on
        # retry. EverOS dedupes by message_id, but only across what is still
        # in its unprocessed buffer -- a batch it has already extracted has
        # left that buffer, so it is extracted again into a fresh memcell:
        # a duplicate memory, and there is no endpoint to delete it. Accepted
        # because the cost is tokens and duplicate entries rather than lost
        # data. Per-batch checkpoint is deferred until full-conversation
        # import is common enough to justify the added state complexity.
        logger.info("[{}/{}] importing {}/{}", i + 1, total, platform, key)
        try:
            session = await scanner.read(result)
            if not await _feed_session(
                backend,
                session,
                cancel_path=cancel_path,
                on_batch=(lambda sent, count: on_batch(platform, key, sent, count)) if on_batch else None,
            ):
                # Stopped between two batches: the source is neither done nor
                # failed, so it keeps no entry and a later run sends it whole.
                logger.info("[{}/{}] import cancelled inside {}/{}", i + 1, total, platform, key)
                break
            state.mark_submitted(platform, key)
            submitted += 1
            logger.info(
                "[{}/{}] imported {}/{} ({} messages)",
                i + 1,
                total,
                platform,
                key,
                len(session.messages),
            )
            if on_progress:
                on_progress(
                    ProgressEvent(
                        platform=platform,
                        source_key=key,
                        status="submitted",
                        current=i + 1,
                        total=total,
                    )
                )
        except Exception as e:
            err_msg = repr(e) if not str(e) else str(e)
            state.mark_failed(platform, key, err_msg)
            failed += 1
            errors.append(ImportFailure(platform, key, err_msg))
            logger.warning(
                "[{}/{}] failed to import {}/{}: {}",
                i + 1,
                total,
                platform,
                key,
                err_msg,
            )
            if on_progress:
                on_progress(
                    ProgressEvent(
                        platform=platform,
                        source_key=key,
                        status="failed",
                        current=i + 1,
                        total=total,
                        error=err_msg,
                    )
                )

    cancelled = cancel_path is not None and cancel_path.exists()
    logger.info(
        "import finished: {} submitted, {} skipped, {} failed (of {} total){}",
        submitted,
        skipped,
        failed,
        total,
        " [cancelled]" if cancelled else "",
    )
    return ImportSummary(
        total=total,
        submitted=submitted,
        skipped=skipped,
        failed=failed,
        errors=tuple(errors),
        cancelled=cancelled,
    )


class MemoryWriteDroppedError(RuntimeError):
    """A batch was not written, and the backend said so rather than raising.

    Raised here rather than returned so the per-source loop keeps deciding what
    a failure means: it already marks the source failed, records the reason,
    and moves on. Only the signal was lost when the backend stopped raising --
    the policy around it was, and stays, correct.
    """


async def _feed_session(
    backend: MemoryBackend,
    session: ImportSession,
    *,
    cancel_path: Path | None = None,
    on_batch: Callable[[int, int], None] | None = None,
) -> bool:
    """Store the session in batches. Returns False when a stop request arrived
    between two batches or during a retry wait, leaving the rest unsent; a long
    conversation is many batches, and a stop that waited for the whole source
    was not a stop. A batch the backend refuses is retried on
    ``_STORE_RETRY_BACKOFF_S`` before the source counts as failed."""
    if not session.messages:
        return True
    all_dicts = [_to_store_dict(m) for m in session.messages]
    batch: list[dict[str, Any]] = []
    batch_chars = 0
    sent = 0
    if on_batch:
        on_batch(0, len(all_dicts))

    def _cancelled() -> bool:
        return cancel_path is not None and cancel_path.exists()

    async def _flush(*, is_final: bool) -> bool:
        nonlocal batch, batch_chars, sent
        # bulk: nothing waits on an import write; the backend budgets it as extraction.
        metadata: dict[str, Any] = {"is_final": is_final, "bulk": True}
        _log_store_request(session.session_id, batch, metadata, batch_chars)
        for attempt, wait in enumerate((*_STORE_RETRY_BACKOFF_S, None), start=1):
            try:
                landed = await backend.store(session.session_id, batch, metadata=metadata)
                reason = "memory service did not accept a batch"
            except Exception as exc:
                landed, reason = False, (str(exc) or repr(exc))
            if landed is not False:
                break
            if wait is None:
                raise MemoryWriteDroppedError(
                    f"{reason} for {session.session_id} after {attempt} attempts; source left unsubmitted"
                )
            logger.warning(
                "batch for {} not accepted ({}); retrying in {}s ({}/{})",
                session.session_id,
                reason,
                int(wait),
                attempt,
                len(_STORE_RETRY_BACKOFF_S),
            )
            if not await _pause(wait, _cancelled):
                return False
        logger.debug("store completed: session_id={}", session.session_id)
        sent += len(batch)
        if on_batch:
            on_batch(sent, len(all_dicts))
        batch = []
        batch_chars = 0
        return True

    for msg_dict in all_dicts:
        msg_chars = len(msg_dict["content"])
        if batch and (len(batch) >= _BATCH_MSG_LIMIT or batch_chars + msg_chars > _BATCH_CHAR_LIMIT):
            if _cancelled():
                return False
            if not await _flush(is_final=False):
                return False
        batch.append(msg_dict)
        batch_chars += msg_chars

    if batch:
        if _cancelled():
            return False
        if not await _flush(is_final=True):
            return False
    return True


async def _pause(seconds: float, cancelled: Callable[[], bool]) -> bool:
    """Wait out a retry backoff a second at a time, so a stop lands inside it.

    Returns False when the stop arrived."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        if cancelled():
            return False
        remaining = deadline - loop.time()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(1.0, remaining))


def _log_store_request(
    session_id: str,
    batch: list[dict[str, Any]],
    metadata: dict[str, Any],
    batch_chars: int,
) -> None:
    logger.debug(
        "store request: session_id={}, metadata={}, messages={}, total_chars={}",
        session_id,
        metadata,
        len(batch),
        batch_chars,
    )
    for i, msg in enumerate(batch):
        content = msg["content"]
        if len(content) > 200:
            content = content[:200] + f"...(truncated, {len(msg['content'])} chars)"
        entry: dict[str, Any] = {
            "role": msg["role"],
            "content": content,
            "timestamp": msg["timestamp"],
        }
        if "tool_calls" in msg:
            entry["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            entry["tool_call_id"] = msg["tool_call_id"]
        logger.debug("store messages[{}]: {}", i, entry)


def _to_store_dict(msg: ImportMessage) -> dict[str, Any]:
    d: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
        "timestamp": msg.timestamp,
    }
    if msg.tool_calls:
        d["tool_calls"] = list(msg.tool_calls)
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    return d


__all__ = ["ImportFailure", "ImportSummary", "ProgressEvent", "run_import"]
