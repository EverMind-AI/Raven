"""scan_all -- aggregation and per-scanner failure isolation."""

from __future__ import annotations

from pathlib import Path

from raven.importer.scanners import scan_all
from raven.importer.types import ImportSession, Platform, ScanResult, SourceKind


class _StubScanner:
    def __init__(
        self,
        platform: Platform,
        *,
        results: list[ScanResult] | None = None,
        fail: BaseException | None = None,
    ) -> None:
        self.platform = platform
        self._results = results or []
        self._fail = fail
        self.partial_failure: BaseException | None = None

    async def scan(self) -> list[ScanResult]:
        if self._fail is not None:
            raise self._fail
        return self._results

    async def read(self, result: ScanResult) -> ImportSession:
        raise NotImplementedError


def _result(platform: Platform, key: str) -> ScanResult:
    return ScanResult(
        source_key=key,
        platform=platform,
        kind=SourceKind.MEMORY_FILE,
        file_paths=(Path("/fake"),),
        estimated_size=1,
        mtime=1.0,
    )


async def test_results_from_all_scanners_are_aggregated() -> None:
    scanners = [
        _StubScanner(Platform.CLAUDE_CODE, results=[_result(Platform.CLAUDE_CODE, "a")]),
        _StubScanner(Platform.HERMES, results=[_result(Platform.HERMES, "b")]),
    ]
    results = await scan_all(scanners)
    assert {r.source_key for r in results} == {"a", "b"}


async def test_one_failing_scanner_does_not_cost_the_others_their_results() -> None:
    """A scanner that shells out fails for reasons unrelated to the other tools;
    propagating that would leave a user with an off-PATH hermes binary unable to
    import anything at all."""
    scanners = [
        _StubScanner(Platform.CLAUDE_CODE, results=[_result(Platform.CLAUDE_CODE, "a")]),
        _StubScanner(Platform.HERMES, fail=RuntimeError("hermes executable not found on PATH")),
    ]
    results = await scan_all(scanners)
    assert [r.source_key for r in results] == ["a"]


async def test_a_failure_is_reported_rather_than_becoming_a_silent_zero() -> None:
    seen: list[tuple[Platform, str]] = []
    scanners = [
        _StubScanner(Platform.CLAUDE_CODE, results=[_result(Platform.CLAUDE_CODE, "a")]),
        _StubScanner(Platform.HERMES, fail=RuntimeError("boom")),
    ]
    await scan_all(scanners, on_error=lambda p, e: seen.append((p, str(e))))
    assert seen == [(Platform.HERMES, "boom")]


async def test_platform_filter_skips_other_scanners_entirely() -> None:
    scanners = [
        _StubScanner(Platform.CLAUDE_CODE, fail=RuntimeError("must not run")),
        _StubScanner(Platform.HERMES, results=[_result(Platform.HERMES, "b")]),
    ]
    seen: list[Platform] = []
    results = await scan_all(scanners, platform_filter=Platform.HERMES, on_error=lambda p, _e: seen.append(p))
    assert [r.source_key for r in results] == ["b"]
    assert seen == []


async def test_a_partial_result_is_reported_as_well_as_kept() -> None:
    """A scanner may return less than everything on purpose -- hermes keeps its
    memory files when the CLI it needs for conversations is gone. Keeping the
    results silently would be the silent under-import the design warns about, so
    the reason travels the same path a total failure does."""
    scanner = _StubScanner(Platform.HERMES, results=[_result(Platform.HERMES, "user-md")])
    scanner.partial_failure = RuntimeError("hermes executable not found on PATH")
    seen: list[tuple[Platform, str]] = []
    results = await scan_all([scanner], on_error=lambda p, e: seen.append((p, str(e))))
    assert [r.source_key for r in results] == ["user-md"]
    assert seen == [(Platform.HERMES, "hermes executable not found on PATH")]


async def test_only_the_failing_platform_is_reported_not_the_healthy_one() -> None:
    seen: list[Platform] = []
    scanners = [
        _StubScanner(Platform.CLAUDE_CODE, results=[_result(Platform.CLAUDE_CODE, "a")]),
        _StubScanner(Platform.HERMES, fail=RuntimeError("boom")),
    ]
    await scan_all(scanners, on_error=lambda p, _e: seen.append(p))
    assert seen == [Platform.HERMES]
