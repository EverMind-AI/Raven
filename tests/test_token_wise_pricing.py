"""Tests for raven.token_wise.pricing -- the arithmetic on top of a rate.

Where a rate comes from is ``raven.providers.rates`` and is tested in
``test_provider_rates.py``. What is asserted here is what the cost formula does
with one: the cache multipliers, the plan-billed abstention, and degrading to
None rather than reporting a number nobody was charged.
"""

from __future__ import annotations

import pytest

from raven.token_wise.pricing import estimate_cost_usd, reset_warning_cache


@pytest.fixture(autouse=True)
def _reset_warning_state():
    reset_warning_cache()
    yield
    reset_warning_cache()


def test_known_anthropic_model_returns_positive_cost():
    """Sonnet is in LiteLLM's DB; baseline cost should be > 0."""
    cost = estimate_cost_usd("anthropic/claude-sonnet-4-5", 1000, 500)
    assert cost is not None
    assert cost > 0


def test_unknown_model_returns_none():
    """A model no rate tier knows costs nothing reportable, not zero."""
    assert estimate_cost_usd("nonexistent-vendor/imaginary-model-9000", 100, 100) is None


def test_cache_read_is_cheaper_than_fresh_input():
    """1000 cache-read tokens should cost ~10% of 1000 fresh prompt tokens."""
    base = estimate_cost_usd("anthropic/claude-sonnet-4-5", 1000, 0)
    cached = estimate_cost_usd("anthropic/claude-sonnet-4-5", 0, 0, cache_read_tokens=1000)
    assert base is not None and cached is not None
    assert cached == pytest.approx(base * 0.1, rel=0.01)


def test_cache_write_more_expensive_than_fresh_input():
    """1000 cache-write tokens should cost ~125% of 1000 fresh prompt tokens."""
    base = estimate_cost_usd("anthropic/claude-sonnet-4-5", 1000, 0)
    cw = estimate_cost_usd("anthropic/claude-sonnet-4-5", 0, 0, cache_write_tokens=1000)
    assert base is not None and cw is not None
    assert cw == pytest.approx(base * 1.25, rel=0.01)


def test_zero_tokens_returns_zero_cost():
    assert estimate_cost_usd("anthropic/claude-sonnet-4-5", 0, 0) == 0.0


def test_combined_input_output_and_cache():
    """Integration: all five components add up correctly."""
    base = estimate_cost_usd("anthropic/claude-sonnet-4-5", 1000, 0)
    out = estimate_cost_usd("anthropic/claude-sonnet-4-5", 0, 1000)
    assert base is not None and out is not None
    full = estimate_cost_usd(
        "anthropic/claude-sonnet-4-5",
        input_tokens=1000,
        output_tokens=1000,
        cache_read_tokens=1000,
        cache_write_tokens=1000,
    )
    assert full is not None
    assert full == pytest.approx(base + out + base * 0.1 + base * 1.25, rel=0.01)


def test_unknown_model_warns_only_once():
    """Repeated estimates for the same unknown model must not flood the log."""
    import loguru

    seen: list[str] = []
    handler_id = loguru.logger.add(lambda m: seen.append(m), level="WARNING")
    try:
        for _ in range(3):
            estimate_cost_usd("ghost-vendor/never-heard-of", 10, 10)
    finally:
        loguru.logger.remove(handler_id)

    matching = [m for m in seen if "ghost-vendor/never-heard-of" in m]
    assert len(matching) == 1, f"Expected 1 warning, got {len(matching)}: {matching}"


def test_a_plan_billed_provider_reports_no_per_token_cost():
    """The subscription is the price, so no per-token figure describes the call.

    LiteLLM files these models at zero, which the rate ladder reads as "unknown"
    and would answer with the pay-as-you-go rate the user is not paying: $2.50 per
    million tokens for a Copilot seat.
    """
    for model in ("github_copilot/gpt-4o", "openai-codex/gpt-5.3-codex", "minimax-global/MiniMax-M3"):
        assert estimate_cost_usd(model, 1_000_000, 0) is None, model

    # The same vendor's metered API is unaffected: that one is per-token.
    assert estimate_cost_usd("minimax/MiniMax-M3", 1_000_000, 0) == pytest.approx(0.3)


def test_offline_context_neither_warns_nor_consumes_the_warning():
    """An offline replay prices nothing and must leave no trace: no unknown-model
    warning, no _WARNED_UNKNOWN mutation — the one-time warning still belongs to
    the next real call after the context exits."""
    import loguru

    from raven.providers.rates import rates_offline
    from raven.token_wise import pricing

    seen: list[str] = []
    handler_id = loguru.logger.add(lambda m: seen.append(m), level="WARNING")
    try:
        with rates_offline():
            assert estimate_cost_usd("ghost-vendor/offline-model", 10, 10) is None
        assert not [m for m in seen if "ghost-vendor/offline-model" in m]
        assert "ghost-vendor/offline-model" not in pricing._WARNED_UNKNOWN

        estimate_cost_usd("ghost-vendor/offline-model", 10, 10)
    finally:
        loguru.logger.remove(handler_id)

    assert len([m for m in seen if "ghost-vendor/offline-model" in m]) == 1
    assert "ghost-vendor/offline-model" in pricing._WARNED_UNKNOWN
