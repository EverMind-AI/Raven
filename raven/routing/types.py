"""Data types for the EcoClaw-style model router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ── Benchmark data ─────────────────────────────────────────────────────────────


@dataclass
class ModelTaskScore:
    task_id: str
    score: float  # 0-100 percentage
    max_score: float


@dataclass
class ModelBenchmark:
    model: str  # PinchBench model ID  e.g. "anthropic/claude-sonnet-4"
    provider: str
    overall_score: float
    speed: float | None  # avg execution time in seconds (PinchBench records this)
    cost: float  # total USD cost for running the full 23-task benchmark
    task_scores: list[ModelTaskScore]
    submission_id: str


# ── Routing profiles ───────────────────────────────────────────────────────────

RoutingProfileName = Literal["best", "balanced", "eco"]


@dataclass(frozen=True)
class RoutingProfile:
    quality_weight: float
    cost_weight: float


# ── Classification ─────────────────────────────────────────────────────────────

TaskCategory = Literal[
    "sanity",
    "calendar",
    "stock",
    "blog",
    "tool_use",
    "summary",
    "events",
    "email",
    "memory",
    "files",
    "workflow",
    "clawdhub",
    "skill_search",
    "image_gen",
    "humanizer",
    "daily_summary",
    "email_triage",
    "email_search",
    "market_research",
    "spreadsheet",
    "eli5_pdf",
    "comprehension",
    "second_brain",
]


@dataclass
class ClassificationResult:
    category: TaskCategory
    similarity: float


# ── Selection ──────────────────────────────────────────────────────────────────


@dataclass
class ModelScore:
    model: str
    provider: str
    task_score: float
    cost_score: float
    composite_score: float


@dataclass
class SelectionResult:
    primary: ModelScore
    fallbacks: list[ModelScore]
    category: TaskCategory
    profile: RoutingProfileName
