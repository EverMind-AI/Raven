"""LLM usage normalization + best-effort cost.

Mirrors ``AgentLoop._build_usage_snapshot`` semantics (fresh-vs-total prompt
token convention differs by provider). Cost reuses raven's own pricing table
when importable; if raven moves it, cost degrades to ``None`` — never raises.
"""

from __future__ import annotations

from typing import Any


def normalize(usage: dict[str, Any] | None, model: str | None) -> dict[str, Any]:
    usage = usage or {}
    prompt_t = int(usage.get("prompt_tokens", 0) or 0)
    out_toks = int(usage.get("completion_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)

    # Normalize prompt tokens to *fresh* (non-cached): Anthropic reports
    # fresh-only, OpenRouter/LiteLLM report total (already including cache).
    if prompt_t >= cache_read + cache_write and (cache_read + cache_write) > 0:
        fresh = prompt_t - cache_read - cache_write
    else:
        fresh = prompt_t

    total = int(usage.get("total_tokens", 0) or 0) or (prompt_t + out_toks)
    return {
        "input_tokens": fresh,
        "output_tokens": out_toks,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": total,
        "cost_usd": _cost(model, fresh, out_toks, cache_read, cache_write),
        "raw": dict(usage),
    }


def _cost(model: str | None, fresh: int, out: int, cache_read: int, cache_write: int):
    if not model:
        return None
    try:
        from raven.token_wise.pricing import estimate_cost_usd

        return estimate_cost_usd(model, fresh, out, cache_read, cache_write)
    except Exception:  # noqa: BLE001 — pricing is best-effort; never break tracing
        return None
