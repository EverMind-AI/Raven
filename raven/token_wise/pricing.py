"""What a call cost, given what it used.

Used by ``UsageTracker`` and ``BudgetAlerter``. Returning a consistent cost from
one place prevents drift between "what we tracked" and "what we budgeted".

The rates themselves are a fact about the provider's catalogue, so they come from
``raven.providers.rates``; what lives here is the arithmetic on top of them --
including Anthropic's ephemeral cache pricing, which is a billing rule rather
than a rate:

    cache read  -> 10% of prompt rate
    cache write -> 125% of prompt rate (ephemeral 5-min TTL)

Non-Anthropic providers (no cache support) pass ``cache_read_tokens=0``,
``cache_write_tokens=0`` and the function collapses to the standard formula.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Iterator

from loguru import logger

from raven.providers.rates import is_plan_billed, token_rates

# Track which unknown models we've already warned about so we log once each.
_WARNED_UNKNOWN: set[str] = set()

# Rate lookups can reach the network (the LiteLLM catalog, the OpenRouter model
# table). An offline context — a trajectory replay feeding recorded responses,
# say — has no use for a cost figure and must not gate on DNS or a fetch
# timeout, so it suppresses pricing wholesale instead of trusting every lookup
# layer to stay local.
_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar("pricing_suppressed", default=False)

__all__ = ["estimate_cost_usd", "pricing_suppressed", "reset_warning_cache"]


@contextlib.contextmanager
def pricing_suppressed() -> Iterator[None]:
    """Inside this context, :func:`estimate_cost_usd` returns None immediately."""
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Estimate USD cost for a single LLM call. Returns None for unknown models.

    ``input_tokens`` is fresh (non-cache) prompt tokens. Anthropic's
    ``usage.input_tokens`` already excludes cache tokens, so pass it
    through untouched.

    A plan-billed provider returns None as well. The subscription is the price,
    so no per-token figure describes this call: LiteLLM files those models at
    zero, which the rate ladder reads as "unknown" and would answer with the
    pay-as-you-go rate the user is not paying -- $2.50 per million for a Copilot
    seat. Callers already degrade on None; the tokens are still counted.
    """
    if _SUPPRESSED.get():
        return None
    if is_plan_billed(model):
        return None

    rates = token_rates(model, input_tokens, output_tokens)
    if rates is None:
        if model not in _WARNED_UNKNOWN:
            logger.warning("pricing: unknown model '{}', cost estimate = None", model)
            _WARNED_UNKNOWN.add(model)
        return None

    prompt_rate, completion_rate = rates
    return (
        input_tokens * prompt_rate
        + output_tokens * completion_rate
        + cache_read_tokens * prompt_rate * 0.1
        + cache_write_tokens * prompt_rate * 1.25
    )


def reset_warning_cache() -> None:
    """Clear the set of models we've already logged an 'unknown' warning for.

    Only useful for tests -- production code should let warnings land once.
    """
    _WARNED_UNKNOWN.clear()
