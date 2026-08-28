"""Model selector — mirrors EcoClaw's selector.ts.

Scores each model by composite(quality, cost) and returns the top choice
plus 2 fallbacks.
"""

from __future__ import annotations

from loguru import logger

from raven.routing.classifier import TASK_CATEGORIES
from raven.routing.fetcher import BenchmarkData
from raven.routing.profiles import ROUTING_PROFILES
from raven.routing.types import (
    ModelBenchmark,
    ModelScore,
    RoutingProfileName,
    SelectionResult,
    TaskCategory,
)

_warned_missing: set[str] = set()


def _get_task_score(model: ModelBenchmark, task_id: str) -> float | None:
    """Return the model's score for task_id, or None if no data exists.

    None means the model has never been benchmarked on this task and should
    be excluded from routing for it — using overall score as a proxy would
    misrepresent capability on tasks the model hasn't been tested on.
    """
    for ts in model.task_scores:
        if ts.task_id == task_id:
            return ts.score
    key = f"{model.model}:{task_id}"
    if key not in _warned_missing:
        _warned_missing.add(key)
        logger.warning(
            "No task-specific score for '{}' on '{}' — excluded from routing for this task",
            task_id,
            model.model,
        )
    return None


def _normalize(value: float, vmin: float, vmax: float, invert: bool) -> float:
    if vmax == vmin:
        return 1.0
    norm = (value - vmin) / (vmax - vmin)
    return 1.0 - norm if invert else norm


def _score_model_by_overall(
    model: ModelBenchmark,
    profile_name: RoutingProfileName,
    cost_min: float,
    cost_max: float,
) -> ModelScore:
    """Score a model using its overall benchmark score (last-resort fallback only)."""
    profile = ROUTING_PROFILES[profile_name]
    cost_score = _normalize(model.cost, cost_min, cost_max, invert=True)
    composite = profile.quality_weight * (model.overall_score / 100.0) + profile.cost_weight * cost_score
    return ModelScore(
        model=model.model,
        provider=model.provider,
        task_score=model.overall_score,
        cost_score=cost_score,
        composite_score=composite,
    )


def _score_model(
    model: ModelBenchmark,
    task_id: str,
    profile_name: RoutingProfileName,
    cost_min: float,
    cost_max: float,
) -> ModelScore | None:
    """Return a ModelScore, or None if the model has no data for this task."""
    task_score = _get_task_score(model, task_id)
    if task_score is None:
        return None
    profile = ROUTING_PROFILES[profile_name]
    cost_val = model.cost
    cost_score = _normalize(cost_val, cost_min, cost_max, invert=True)
    composite = profile.quality_weight * (task_score / 100.0) + profile.cost_weight * cost_score
    return ModelScore(
        model=model.model,
        provider=model.provider,
        task_score=task_score,
        cost_score=cost_score,
        composite_score=composite,
    )


def select_model(
    benchmark_data: BenchmarkData,
    category: TaskCategory,
    profile_name: RoutingProfileName,
) -> SelectionResult:
    """Return primary + 2 fallback models for the given category and profile."""
    # Filter valid models
    models = [m for m in benchmark_data.values() if m.cost is not None and m.cost > 0 and "/" in m.model]
    if not models:
        raise ValueError("No eligible models in benchmark data")

    task_id = TASK_CATEGORIES[category]

    costs = [m.cost for m in models]
    cost_min = min(costs)
    cost_max = max(costs)

    scored_raw = [_score_model(m, task_id, profile_name, cost_min, cost_max) for m in models]
    scored = sorted(
        [s for s in scored_raw if s is not None],
        key=lambda s: s.composite_score,
        reverse=True,
    )
    if not scored:
        # All models lack task-specific data — fall back to overall-score ranking
        logger.warning(
            "No models have task-specific data for '{}'; falling back to overall-score ranking",
            task_id,
        )
        scored = sorted(
            [_score_model_by_overall(m, profile_name, cost_min, cost_max) for m in models],
            key=lambda s: s.composite_score,
            reverse=True,
        )

    return SelectionResult(
        primary=scored[0],
        fallbacks=scored[1:3],
        category=category,
        profile=profile_name,
    )
