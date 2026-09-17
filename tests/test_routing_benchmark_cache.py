"""What BenchmarkCache.load() hands back on each of its four routes.

The class had no test. Its two refresh-success routes are the ones that decide
whether a router sees fresh numbers or yesterday's, and both used to read the
attribute back after the refresh had set it rather than using what the refresh
returned -- a shape that could only be spelled with a suppression.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from raven.routing import cache as cache_mod
from raven.routing.types import ModelBenchmark, ModelTaskScore


def _bench(model: str, score: float) -> ModelBenchmark:
    return ModelBenchmark(
        model=model,
        provider="acme",
        overall_score=score,
        speed=1.0,
        cost=1.0,
        task_scores=[ModelTaskScore(task_id="t", score=score, max_score=100.0)],
        submission_id=f"sub-{model}",
    )


def _write_cache(path: Path, data: dict[str, ModelBenchmark], fetched_at: float) -> None:
    raw = cache_mod._serialize(data)
    raw["fetched_at"] = fetched_at
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")


@pytest.fixture
def fresh_from_api(monkeypatch):
    """Whatever the API would return, without going near it."""
    data = {"acme/new": _bench("acme/new", 90.0)}

    async def _build():
        return data

    monkeypatch.setattr(cache_mod, "build_benchmark_data", _build)
    return data


async def test_no_cache_on_disk_serves_what_the_refresh_returned(tmp_path, fresh_from_api) -> None:
    cache = cache_mod.BenchmarkCache(cache_path=tmp_path / "benchmark-cache.json")

    assert await cache.load() == fresh_from_api


async def test_a_stale_cache_is_replaced_by_the_refresh_rather_than_served(tmp_path, fresh_from_api) -> None:
    """Stale means "try the API first"; the disk copy is the fallback, not the answer."""
    path = tmp_path / "benchmark-cache.json"
    _write_cache(path, {"acme/old": _bench("acme/old", 10.0)}, time.time() - cache_mod.CACHE_TTL_S - 1)
    cache = cache_mod.BenchmarkCache(cache_path=path)

    assert await cache.load() == fresh_from_api


async def test_a_stale_cache_is_served_when_the_refresh_raises(tmp_path, monkeypatch) -> None:
    """The contrast to the test above: only a failed refresh makes the disk copy
    the answer, and yesterday's numbers beat no numbers."""
    stale = {"acme/old": _bench("acme/old", 10.0)}
    path = tmp_path / "benchmark-cache.json"
    _write_cache(path, stale, time.time() - cache_mod.CACHE_TTL_S - 1)

    async def _fail():
        raise RuntimeError("the benchmark endpoint is down")

    monkeypatch.setattr(cache_mod, "build_benchmark_data", _fail)
    cache = cache_mod.BenchmarkCache(cache_path=path)

    loaded = await cache.load()

    assert list(loaded) == ["acme/old"]
