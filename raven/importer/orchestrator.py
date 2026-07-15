"""Cold-start import orchestrator -- read, batch, store, track."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
            state.mark_failed(platform, key, str(e))
            failed += 1
            errors.append(ImportFailure(platform, key, str(e)))
            logger.warning(
                "[{}/{}] failed to import {}/{}: {}",
                i + 1,
                total,
                platform,
                key,
                e,
            )
            if on_progress:
                on_progress(
                    ProgressEvent(
                        platform=platform,
                        source_key=key,
                        status="failed",
                        current=i + 1,
                        total=total,
                        error=str(e),
                    )
                )

    logger.info(
        "import finished: {} submitted, {} skipped, {} failed (of {} total)",
        submitted,
        skipped,
        failed,
        total,
    )
    return ImportSummary(
        total=total,
        submitted=submitted,
        skipped=skipped,
        failed=failed,
        errors=tuple(errors),
    )


async def _feed_session(backend: MemoryBackend, session: ImportSession) -> None:
    if not session.messages:
        return
    all_dicts = [_to_store_dict(m) for m in session.messages]
    metadata_base: dict[str, Any] = {
        "app_id": session.app_id,
        "project_id": session.project_id,
    }
    batch: list[dict[str, Any]] = []
    batch_chars = 0

    for msg_dict in all_dicts:
        msg_chars = len(msg_dict["content"])
        if batch and (len(batch) >= _BATCH_MSG_LIMIT or batch_chars + msg_chars > _BATCH_CHAR_LIMIT):
            await backend.store(
                session.session_id,
                batch,
                metadata={**metadata_base, "is_final": False},
            )
            batch = []
            batch_chars = 0
        batch.append(msg_dict)
        batch_chars += msg_chars

    if batch:
        await backend.store(
            session.session_id,
            batch,
            metadata={**metadata_base, "is_final": True},
        )


def _to_store_dict(msg: ImportMessage) -> dict[str, Any]:
    d: dict[str, Any] = {
        "role": msg.role,
        "content": msg.content,
        "sender_id": msg.sender_id,
        "timestamp": msg.timestamp,
    }
    if msg.tool_calls:
        d["tool_calls"] = list(msg.tool_calls)
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    return d


__all__ = ["ImportFailure", "ImportSummary", "ProgressEvent", "run_import"]
