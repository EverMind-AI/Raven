"""Normalize reported usage for accounting and tracing without estimating money."""

from __future__ import annotations

import math
from typing import Any


def reported_cost(value: Any) -> float | None:
    """Accept a finite, nonnegative API amount in USD, including an explicit zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        amount = float(value)
    except OverflowError:
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


def token_count(value: Any) -> int | None:
    """Keep missing counters distinct from a reported zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Read adapter-normalized usage; prompt counts include cache unless declared otherwise."""
    usage = usage or {}
    prompt = token_count(usage.get("prompt_tokens")) or 0
    output = token_count(usage.get("completion_tokens")) or 0
    read = token_count(usage.get("cache_read_input_tokens"))
    write = token_count(usage.get("cache_creation_input_tokens"))
    cached = (read or 0) + (write or 0)
    includes_cache = usage.get("prompt_tokens_include_cache", True)
    fresh = max(0, prompt - cached) if includes_cache else prompt
    return {
        "input_tokens": fresh,
        "output_tokens": output,
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "total_tokens": token_count(usage.get("total_tokens")) or prompt + output + (0 if includes_cache else cached),
        "cost_usd": reported_cost(usage.get("cost_usd")),
    }


def responses_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Carry Responses API counters and reported USD cost into the common usage shape."""
    if not usage:
        return {}
    details = usage.get("input_tokens_details") or {}
    return {
        "prompt_tokens": token_count(usage.get("input_tokens")),
        "completion_tokens": token_count(usage.get("output_tokens")),
        "total_tokens": token_count(usage.get("total_tokens")),
        "cache_read_input_tokens": token_count(details.get("cached_tokens")),
        "cache_creation_input_tokens": token_count(details.get("cache_write_tokens")),
        "prompt_tokens_include_cache": True,
        "cost_usd": reported_cost(usage.get("cost")),
    }


def merge_usage(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge cumulative reports, preserving known fields across partial frames."""
    result = dict(current or {})
    for key, value in incoming.items():
        if isinstance(value, dict):
            previous = result.get(key)
            result[key] = merge_usage(previous if isinstance(previous, dict) else None, value)
        elif value is not None:
            result[key] = value
    return result
