"""PinchBench API client.

Mirrors EcoClaw's fetcher.ts — fetches leaderboard + submission details
and builds a BenchmarkData dict keyed by model ID.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from raven.routing.types import ModelBenchmark, ModelTaskScore

API_BASE = "https://api.pinchbench.com/api"
FETCH_CONCURRENCY = 5
FETCH_TIMEOUT_S = 30.0

# type alias
BenchmarkData = dict[str, ModelBenchmark]


async def fetch_leaderboard(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    resp = await client.get(f"{API_BASE}/leaderboard", timeout=FETCH_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    return data.get("leaderboard", [])


async def fetch_submission(client: httpx.AsyncClient, submission_id: str) -> dict[str, Any]:
    url = f"{API_BASE}/submissions/{submission_id}"
    resp = await client.get(url, timeout=FETCH_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


async def fetch_latest_submission_id(client: httpx.AsyncClient, model: str) -> str | None:
    url = f"{API_BASE}/submissions"
    resp = await client.get(url, params={"model": model, "limit": 1}, timeout=FETCH_TIMEOUT_S)
    if not resp.is_success:
        return None
    data = resp.json()
    submissions = data.get("submissions", [])
    return submissions[0]["id"] if submissions else None


async def _fetch_one(
    client: httpx.AsyncClient,
    entry: dict[str, Any],
    sem: asyncio.Semaphore,
) -> dict[str, Any] | None:
    async with sem:
        try:
            latest_id = await fetch_latest_submission_id(client, entry["model"])
            if not latest_id:
                return None
            detail = await fetch_submission(client, latest_id)
            return {"entry": entry, "detail": detail, "submission_id": latest_id}
        except Exception as e:
            logger.warning("Failed to fetch submission for {}: {}", entry.get("model"), e)
            return None


async def build_benchmark_data() -> BenchmarkData:
    """Fetch leaderboard + submission details from PinchBench API.

    Returns a dict mapping model ID → ModelBenchmark.
    """
    async with httpx.AsyncClient() as client:
        leaderboard = await fetch_leaderboard(client)
        logger.info("PinchBench leaderboard: {} entries", len(leaderboard))

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        results = await asyncio.gather(
            *[_fetch_one(client, entry, sem) for entry in leaderboard],
        )

    data: BenchmarkData = {}

    for item in results:
        if item is None:
            continue
        entry: dict[str, Any] = item["entry"]
        detail: dict[str, Any] = item["detail"]
        submission_id: str = item["submission_id"]

        cost = entry.get("average_cost_usd")
        if cost is None or cost <= 0:
            continue

        tasks = detail.get("submission", {}).get("tasks", [])
        task_scores = [
            ModelTaskScore(
                task_id=t["task_id"],
                score=(t["score"] / (t["max_score"] or 1)) * 100,
                max_score=t["max_score"],
            )
            for t in tasks
        ]

        sum_score = sum(t["score"] for t in tasks)
        sum_max = sum(t["max_score"] for t in tasks)
        overall = (sum_score / sum_max * 100) if sum_max > 0 else 0.0

        benchmark = ModelBenchmark(
            model=entry["model"],
            provider=entry.get("provider", ""),
            overall_score=overall,
            speed=entry.get("average_execution_time_seconds"),
            cost=cost,
            task_scores=task_scores,
            submission_id=submission_id,
        )
        data[entry["model"]] = benchmark

    # Deduplicate: if multiple entries map to same model ID, keep higher cost
    seen: dict[str, str] = {}  # normalized_id → original model key
    for model_id, benchmark in list(data.items()):
        norm = model_id.lower()
        if norm in seen:
            existing_id = seen[norm]
            if benchmark.cost > data[existing_id].cost:
                del data[existing_id]
                seen[norm] = model_id
            else:
                del data[model_id]
        else:
            seen[norm] = model_id

    logger.info("Built benchmark data: {} models", len(data))
    return data
