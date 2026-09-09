"""Reported LLM usage shared with the agent accounting path."""

from __future__ import annotations

from typing import Any

from raven.providers.usage import normalize_usage


def normalize(usage: dict[str, Any] | None, model: str | None) -> dict[str, Any]:
    return {**normalize_usage(usage), "raw": dict(usage or {})}
