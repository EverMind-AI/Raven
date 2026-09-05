"""Trajectory compression — session.jsonl → ~10K-token diagnostic summary.

Used by the judge LLM client (B3) when ``trajectory_format="compressed"``
is requested. The compressor is rule-based (no LLM call), so it's cheap
and deterministic.
"""

from .trajectory import (
    CompressorConfig,
    Event,
    TrajectoryCompressor,
    estimate_tokens,
    load_session_jsonl,
)

__all__ = [
    "CompressorConfig",
    "Event",
    "TrajectoryCompressor",
    "estimate_tokens",
    "load_session_jsonl",
]
