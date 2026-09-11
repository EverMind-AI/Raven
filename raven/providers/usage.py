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
        # Part of the output count, never added to it -- an accounting field
        # here, and the one number that says a silent turn still thought.
        "reasoning_tokens": token_count(usage.get("reasoning_tokens")) or 0,
        "total_tokens": token_count(usage.get("total_tokens")) or prompt + output + (0 if includes_cache else cached),
        "cost_usd": reported_cost(usage.get("cost_usd")),
    }


def responses_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Carry Responses API counters and reported USD cost into the common usage shape."""
    if not usage:
        return {}
    details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "prompt_tokens": token_count(usage.get("input_tokens")),
        "completion_tokens": token_count(usage.get("output_tokens")),
        "total_tokens": token_count(usage.get("total_tokens")),
        "cache_read_input_tokens": token_count(details.get("cached_tokens")),
        "cache_creation_input_tokens": token_count(details.get("cache_write_tokens")),
        "reasoning_tokens": token_count(output_details.get("reasoning_tokens")),
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


def image_usage(payload: dict[str, Any], protocol: str) -> dict[str, Any]:
    """Normalize image usage without inventing absent token counts or amounts."""
    raw = payload.get("usage")
    raw = raw if isinstance(raw, dict) else {}
    prompt_key, output_key, details_key = (
        ("prompt_tokens", "completion_tokens", "prompt_tokens_details")
        if protocol == "chat"
        else ("input_tokens", "output_tokens", "input_tokens_details")
    )
    # Image gateways may return Chat-style usage even on the Images endpoint.
    if prompt_key not in raw and "prompt_tokens" in raw:
        prompt_key, details_key = "prompt_tokens", "prompt_tokens_details"
    if output_key not in raw and "completion_tokens" in raw:
        output_key = "completion_tokens"
    details = raw.get(details_key)
    details = details if isinstance(details, dict) else {}
    prompt = token_count(raw.get(prompt_key))
    read = token_count(details.get("cached_tokens"))
    write = token_count(details.get("cache_write_tokens"))
    return {
        "input_tokens": max(0, prompt - (read or 0) - (write or 0)) if prompt is not None else None,
        "output_tokens": token_count(raw.get(output_key)),
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "cost_usd": reported_cost(raw.get("cost")),
    }
