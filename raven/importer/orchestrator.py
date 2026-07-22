"""Cold-start import orchestrator -- read, batch, store, track."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from raven.importer.state import ImportState
from raven.importer.types import ImportMessage, ImportSession, Scanner, ScanResult
from raven.memory_engine.backend import MemoryBackend

_BATCH_MSG_LIMIT = 100
_BATCH_CHAR_LIMIT = 30_000


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
    cancel_path: Path | None = None,
) -> ImportSummary:
    """Import pre-filtered scan results into the memory backend.

    The caller (CLI layer) is responsible for scanning, tier/platform
    filtering, and MemoryBackend lifecycle (start/stop).
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
        # session that fails mid-way will re-send already-accepted batches
        # on retry.  EverOS dedup is by session_id so duplicates are safe
        # (redundant extraction, no data loss).  Per-batch checkpoint is
        # deferred until full-conversation import is common enough to
        # justify the added state complexity.
        logger.info("[{}/{}] importing {}/{}", i + 1, total, platform, key)
        try:
            session = await scanner.read(result)
            await _feed_session(backend, session)
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


async def _feed_session(backend: MemoryBackend, session: ImportSession) -> None:
    if not session.messages:
        return
    all_dicts = [_to_store_dict(m) for m in session.messages]
    batch: list[dict[str, Any]] = []
    batch_chars = 0

    async def _flush(*, is_final: bool) -> None:
        nonlocal batch, batch_chars
        metadata: dict[str, Any] = {"is_final": is_final}
        _log_store_request(session.session_id, batch, metadata, batch_chars)
        await backend.store(session.session_id, batch, metadata=metadata)
        logger.debug("store completed: session_id={}", session.session_id)
        batch = []
        batch_chars = 0

    for msg_dict in all_dicts:
        msg_chars = len(msg_dict["content"])
        if batch and (len(batch) >= _BATCH_MSG_LIMIT or batch_chars + msg_chars > _BATCH_CHAR_LIMIT):
            await _flush(is_final=False)
        batch.append(msg_dict)
        batch_chars += msg_chars

    if batch:
        await _flush(is_final=True)


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
