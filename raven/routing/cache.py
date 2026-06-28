"""Benchmark data cache — mirrors EcoClaw's cache.ts.

4-tier fallback:
  1. In-memory  (instant)
  2. Disk cache (< 6h old)
  3. Stale disk cache (API failed)
  4. Hardcoded snapshot.json
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from loguru import logger

from raven.routing.fetcher import BenchmarkData, build_benchmark_data
from raven.routing.types import ModelBenchmark, ModelTaskScore

CACHE_VERSION = 1
CACHE_TTL_S = 6 * 60 * 60  # 6 hours
_DEFAULT_CACHE_PATH = Path.home() / ".raven" / "routing" / "benchmark-cache.json"
_SNAPSHOT_PATH = Path(__file__).parent / "snapshot.json"


def _load_snapshot() -> BenchmarkData:
    """Load hardcoded fallback data from snapshot.json."""
    data: BenchmarkData = {}
    try:
        raw = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        for entry in raw.get("models", []):
            cost = entry.get("cost")
            if not cost or cost <= 0:
                continue
            task_scores = [
                ModelTaskScore(
                    task_id=t["taskId"],
                    score=t["score"],
                    max_score=t["maxScore"],
                )
                for t in entry.get("taskScores", [])
            ]
            data[entry["model"]] = ModelBenchmark(
                model=entry["model"],
                provider=entry.get("provider", ""),
                overall_score=entry.get("overallScore", 0.0),
                speed=entry.get("speed"),
                cost=cost,
                task_scores=task_scores,
                submission_id=entry.get("submissionId", ""),
            )
    except Exception as e:
        logger.error("Failed to load snapshot.json: {}", e)
    return data


def _serialize(data: BenchmarkData) -> dict:
    return {
        "version": CACHE_VERSION,
        "fetched_at": time.time(),
        "models": [
            {
                "model": b.model,
                "provider": b.provider,
                "overall_score": b.overall_score,
                "speed": b.speed,
                "cost": b.cost,
                "submission_id": b.submission_id,
                "task_scores": [
                    {"task_id": t.task_id, "score": t.score, "max_score": t.max_score}
                    for t in b.task_scores
                ],
            }
            for b in data.values()
        ],
    }


def _deserialize(raw: dict) -> tuple[BenchmarkData, float] | None:
    if raw.get("version") != CACHE_VERSION or not isinstance(raw.get("models"), list):
        return None
    data: BenchmarkData = {}
    for entry in raw["models"]:
        cost = entry.get("cost")
        if not cost or cost <= 0:
            continue
        task_scores = [
            ModelTaskScore(
                task_id=t["task_id"],
                score=t["score"],
                max_score=t["max_score"],
            )
            for t in entry.get("task_scores", [])
        ]
        data[entry["model"]] = ModelBenchmark(
            model=entry["model"],
            provider=entry.get("provider", ""),
            overall_score=entry.get("overall_score", 0.0),
            speed=entry.get("speed"),
            cost=cost,
            task_scores=task_scores,
            submission_id=entry.get("submission_id", ""),
        )
    return data, raw.get("fetched_at", 0.0)


class BenchmarkCache:
    """Thread-safe benchmark data cache with background refresh."""

    def __init__(self, cache_path: Path = _DEFAULT_CACHE_PATH):
        self._cache_path = cache_path
        self._data: BenchmarkData | None = None
        self._refresh_task: asyncio.Task | None = None

    async def load(self) -> BenchmarkData:
        """Return benchmark data, fetching/refreshing as needed."""
        # 1. In-memory hit
        if self._data is not None:
            return self._data

        # 2. Try disk cache
        cached = self._load_from_disk()
        if cached:
            data, fetched_at = cached
            age = time.time() - fetched_at
            if age < CACHE_TTL_S:
                self._data = data
                self._schedule_background_refresh()
                return self._data

            # Stale — try API first
            try:
                await self._do_refresh()
                return self._data  # type: ignore[return-value]
            except Exception:
                logger.warning("API refresh failed, using stale cache")
                self._data = data
                return self._data

        # 3. No cache — try API
        try:
            await self._do_refresh()
            return self._data  # type: ignore[return-value]
        except Exception:
            # 4. Fallback to snapshot
            logger.warning("API unavailable, falling back to snapshot.json")
            self._data = _load_snapshot()
            return self._data

    def _load_from_disk(self) -> tuple[BenchmarkData, float] | None:
        try:
            if not self._cache_path.exists():
                return None
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            return _deserialize(raw)
        except Exception:
            return None

    def _save_to_disk(self, data: BenchmarkData) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(_serialize(data), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to write benchmark cache: {}", e)

    async def _do_refresh(self) -> None:
        data = await build_benchmark_data()
        self._data = data
        self._save_to_disk(data)
        logger.info("Benchmark cache refreshed: {} models", len(data))

    def _schedule_background_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            return
        async def _bg():
            await asyncio.sleep(0.1)
            try:
                await self._do_refresh()
            except Exception:
                pass
        self._refresh_task = asyncio.create_task(_bg())

    def get_fallback(self) -> BenchmarkData:
        return _load_snapshot()
