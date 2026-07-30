"""Platform-specific scanners for cold-start import."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger

from raven.importer.scanners.claude_code import ClaudeCodeScanner
from raven.importer.scanners.hermes import HermesScanner
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind


def build_scanners() -> list[Scanner]:
    """Return all available scanner instances."""
    return [ClaudeCodeScanner(), HermesScanner()]


async def scan_all(
    scanners: list[Scanner] | None = None,
    *,
    platform_filter: Platform | None = None,
    on_error: Callable[[Platform, BaseException], None] | None = None,
) -> list[ScanResult]:
    """Run all scanners concurrently and return aggregated results.

    One platform failing must not cost the others theirs. A scanner that has to
    shell out -- hermes does, to enumerate conversations -- fails for reasons
    that say nothing about whether the user's other tools are importable, and
    letting that propagate would leave someone whose hermes binary is off PATH
    unable to import anything at all. ``on_error`` is how a caller surfaces the
    failure instead of it becoming a silent zero: the CLI prints it, since the
    logger is file-only during a run.
    """
    if scanners is None:
        scanners = build_scanners()
    if platform_filter:
        scanners = [s for s in scanners if s.platform == platform_filter]
    logger.info("scan started: {} scanner(s)", len(scanners))

    per_scanner = await asyncio.gather(*(s.scan() for s in scanners), return_exceptions=True)

    results: list[ScanResult] = []
    for scanner, found in zip(scanners, per_scanner):
        # KeyboardInterrupt and SystemExit propagate out of the gather rather
        # than arriving as results, so a Ctrl-C is never mistaken for a platform
        # that failed to scan. CancelledError does arrive here and is not an
        # Exception, which is why the check is the wider BaseException.
        if isinstance(found, BaseException):
            logger.warning("scan {} failed: {}", scanner.platform.value, found)
            if on_error is not None:
                on_error(scanner.platform, found)
            continue
        logger.info("scan {}: {} results", scanner.platform.value, len(found))
        results.extend(found)
        # A scanner may deliberately return less than everything -- hermes keeps
        # its memory files when the CLI it needs for conversations is missing --
        # and that reason must travel the same path a total failure does, or it
        # reaches only the log, which is silenced during scan and file-only
        # during run.
        partial = getattr(scanner, "partial_failure", None)
        if partial is not None and on_error is not None:
            on_error(scanner.platform, partial)

    mem = sum(1 for r in results if r.kind == SourceKind.MEMORY_FILE)
    conv = sum(1 for r in results if r.kind == SourceKind.CONVERSATION)
    logger.info("scan completed: {} results ({} memory_file, {} conversation)", len(results), mem, conv)
    return results


__all__ = ["ClaudeCodeScanner", "HermesScanner", "build_scanners", "scan_all"]
