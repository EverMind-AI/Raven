"""Embedding-based prompt classifier — mirrors EcoClaw's classifier.ts.

23 categories map 1:1 to PinchBench task IDs.
Classification = nearest-neighbour search against pre-computed embeddings.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from raven.routing.types import ClassificationResult, TaskCategory

if TYPE_CHECKING:
    pass

# ── 23 categories → task IDs ───────────────────────────────────────────────────

TASK_CATEGORIES: dict[TaskCategory, str] = {
    "sanity": "task_00_sanity",
    "calendar": "task_01_calendar",
    "stock": "task_02_stock",
    "blog": "task_03_blog",
    "tool_use": "task_04_weather",
    "summary": "task_05_summary",
    "events": "task_06_events",
    "email": "task_07_email",
    "memory": "task_08_memory",
    "files": "task_09_files",
    "workflow": "task_10_workflow",
    "clawdhub": "task_11_clawdhub",
    "skill_search": "task_12_skill_search",
    "image_gen": "task_13_image_gen",
    "humanizer": "task_14_humanizer",
    "daily_summary": "task_15_daily_summary",
    "email_triage": "task_16_email_triage",
    "email_search": "task_17_email_search",
    "market_research": "task_18_market_research",
    "spreadsheet": "task_19_spreadsheet_summary",
    "eli5_pdf": "task_20_eli5_pdf_summary",
    "comprehension": "task_21_openclaw_comprehension",
    "second_brain": "task_22_second_brain",
}

_TASK_TO_CATEGORY: dict[str, TaskCategory] = {v: k for k, v in TASK_CATEGORIES.items()}

_EMBEDDING_DATA_PATH = Path(__file__).parent / "embedding_data.json"

# ── Embedding API ──────────────────────────────────────────────────────────────

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
EMBEDDING_MODEL = "text-embedding-3-small"


async def fetch_embedding(
    text: str,
    api_key: str,
    model: str = EMBEDDING_MODEL,
) -> list[float]:
    """Call OpenRouter /embeddings and return the vector."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{OPENROUTER_API_BASE}/embeddings",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"input": text, "model": model},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity — handles dimension mismatches by truncating."""
    length = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(length))
    norm_a = math.sqrt(sum(x * x for x in a[:length]))
    norm_b = math.sqrt(sum(x * x for x in b[:length]))
    denom = norm_a * norm_b
    return dot / denom if denom > 0 else 0.0


# ── Pre-computed embeddings ────────────────────────────────────────────────────


def _load_embedding_data() -> list[dict] | None:
    """Load pre-computed task embeddings from embedding_data.json."""
    if not _EMBEDDING_DATA_PATH.exists():
        return None
    try:
        raw = json.loads(_EMBEDDING_DATA_PATH.read_text(encoding="utf-8"))
        return raw.get("tasks", [])
    except Exception as e:
        logger.warning("Failed to load embedding_data.json: {}", e)
        return None


# ── Classifier ─────────────────────────────────────────────────────────────────


class PromptClassifier:
    """Classify a prompt into one of 23 PinchBench task categories."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._tasks: list[dict] | None = None  # lazy-loaded

    def _get_tasks(self) -> list[dict] | None:
        if self._tasks is None:
            self._tasks = _load_embedding_data()
        return self._tasks

    async def classify(self, prompt: str) -> ClassificationResult:
        """Return the best-matching task category for a prompt.

        Falls back to "sanity" on any failure.
        """
        tasks = self._get_tasks()
        if not tasks:
            logger.warning(
                "Embedding data missing — run `python -m raven.routing.generate_embeddings`. Falling back to 'sanity'."
            )
            return ClassificationResult(category="sanity", similarity=0.0)

        try:
            query_vec = await fetch_embedding(prompt, self._api_key)
        except Exception as e:
            logger.warning("Embedding API failed ({}), falling back to 'sanity'", e)
            return ClassificationResult(category="sanity", similarity=0.0)

        best_category: TaskCategory = "sanity"
        best_sim = float("-inf")

        for task in tasks:
            task_vec: list[float] = task.get("embedding", [])
            if not task_vec:
                continue
            sim = cosine_similarity(query_vec, task_vec)
            if sim > best_sim:
                best_sim = sim
                task_id: str = task.get("task_id", "")
                best_category = _TASK_TO_CATEGORY.get(task_id, "sanity")

        logger.debug("Classified prompt → {} (similarity={:.3f})", best_category, best_sim)
        return ClassificationResult(category=best_category, similarity=best_sim)
