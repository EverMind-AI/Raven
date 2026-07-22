"""Platform-specific scanners for cold-start import."""

from __future__ import annotations

import asyncio

from loguru import logger

from raven.importer.scanners.claude_code import ClaudeCodeScanner
from raven.importer.types import Platform, Scanner, ScanResult, SourceKind


def build_scanners() -> list[Scanner]:
    """Return all available scanner instances."""
    return [ClaudeCodeScanner()]


async def scan_all(
    scanners: list[Scanner] | None = None,
    *,
    platform_filter: Platform | None = None,
) -> list[ScanResult]:
    """Run all scanners concurrently and return aggregated results."""
    if scanners is None:
        scanners = build_scanners()
    if platform_filter:
        scanners = [s for s in scanners if s.platform == platform_filter]
    logger.info("scan started: {} scanner(s)", len(scanners))

    per_scanner = await asyncio.gather(*(s.scan() for s in scanners))

    results: list[ScanResult] = []
    for scanner, found in zip(scanners, per_scanner):
        logger.info("scan {}: {} results", scanner.platform.value, len(found))
        results.extend(found)

    mem = sum(1 for r in results if r.kind == SourceKind.MEMORY_FILE)
    conv = sum(1 for r in results if r.kind == SourceKind.CONVERSATION)
    logger.info("scan completed: {} results ({} memory_file, {} conversation)", len(results), mem, conv)
    return results


__all__ = ["ClaudeCodeScanner", "build_scanners", "scan_all"]
