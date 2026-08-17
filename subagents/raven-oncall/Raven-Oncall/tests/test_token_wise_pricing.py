"""Tests for raven.token_wise.pricing."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from raven.token_wise import model_catalog_cache, pricing
from raven.token_wise.pricing import (
    _FALLBACK_PRICING,
    estimate_cost_usd,
    reset_warning_cache,
    resolve_context_window,
)

# The real fetch, captured before conftest's autouse guard stubs it to {}.
_REAL_FETCH = pricing._fetch_openrouter_models


@pytest.fixture(autouse=True)
def _reset_warning_state():
    reset_warning_cache()
    pricing._OPENROUTER_CACHE.clear()
    yield
    reset_warning_cache()
    pricing._OPENROUTER_CACHE.clear()


def _patch_openrouter(monkeypatch, handler):
    """Route pricing's real OpenRouter fetch through a MockTransport.

    Restores the real ``_fetch_openrouter_models`` (conftest stubs it to {} so
    no test hits the network by default), then mocks the httpx transport.
    Returns a counter dict whose ``["calls"]`` tracks network hits.
    """
    counter = {"calls": 0}

    def counting_handler(request):
        counter["calls"] += 1
        return handler(request)

    transport = httpx.MockTransport(counting_handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(pricing, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(pricing.httpx, "Client", client_factory)
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE_TIME", 0.0)
    return counter


def _models_response(models):
    return httpx.Response(200, content=json.dumps({"data": models}))


def test_known_anthropic_model_returns_positive_cost():
    """Sonnet is in LiteLLM's DB; baseline cost should be > 0."""
    cost = estimate_cost_usd("anthropic/claude-sonnet-4-5", 1000, 500)
    assert cost is not None
    assert cost > 0


def test_unknown_model_returns_none():
    """Models LiteLLM doesn't know about and we don't have fallback for → None."""
    cost = estimate_cost_usd("nonexistent-vendor/imaginary-model-9000", 100, 100)
    assert cost is None


def test_fallback_pricing_used_when_litellm_misses():
    """A model in our manual table should yield a finite cost even if LiteLLM lacks it."""
    model = next(iter(_FALLBACK_PRICING))
    p_rate, c_rate = _FALLBACK_PRICING[model]
    cost = estimate_cost_usd(model, 1000, 500)
    assert cost is not None
    # Should equal the fallback exactly (or be at least that much, if LiteLLM also has it).
    expected = 1000 * p_rate + 500 * c_rate
    assert cost == pytest.approx(expected, rel=0.01)


def test_cache_read_is_cheaper_than_fresh_input():
    """1000 cache-read tokens should cost ~10% of 1000 fresh prompt tokens."""
    base = estimate_cost_usd("anthropic/claude-sonnet-4-5", 1000, 0)
    cached = estimate_cost_usd("anthropic/claude-sonnet-4-5", 0, 0, cache_read_tokens=1000)
    assert base is not None and cached is not None
    # Cache read is 10% of base prompt rate.
    assert cached == pytest.approx(base * 0.1, rel=0.01)


def test_cache_write_more_expensive_than_fresh_input():
    """1000 cache-write tokens should cost ~125% of 1000 fresh prompt tokens."""
    base = estimate_cost_usd("anthropic/claude-sonnet-4-5", 1000, 0)
    cw = estimate_cost_usd("anthropic/claude-sonnet-4-5", 0, 0, cache_write_tokens=1000)
    assert base is not None and cw is not None
    assert cw == pytest.approx(base * 1.25, rel=0.01)


def test_zero_tokens_returns_zero_cost():
    cost = estimate_cost_usd("anthropic/claude-sonnet-4-5", 0, 0)
    assert cost == 0.0


def test_unknown_model_warns_only_once(caplog):
    """Repeated estimates for the same unknown model must not flood the log."""
    import loguru

    seen: list[str] = []
    handler_id = loguru.logger.add(lambda m: seen.append(m), level="WARNING")
    try:
        estimate_cost_usd("ghost-vendor/never-heard-of", 10, 10)
        estimate_cost_usd("ghost-vendor/never-heard-of", 10, 10)
        estimate_cost_usd("ghost-vendor/never-heard-of", 10, 10)
    finally:
        loguru.logger.remove(handler_id)

    matching = [m for m in seen if "ghost-vendor/never-heard-of" in m]
    assert len(matching) == 1, f"Expected 1 warning, got {len(matching)}: {matching}"


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
    expected = base + out + base * 0.1 + base * 1.25
    assert full == pytest.approx(expected, rel=0.01)


_DEEPSEEK_MODELS = [
    {
        "id": "deepseek/deepseek-v4-pro",
        "context_length": 163840,
        "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
    }
]


def test_openrouter_unmapped_model_yields_live_cost(monkeypatch):
    """A model LiteLLM doesn't map gets a non-zero cost from OpenRouter's API."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert cost is not None
    assert cost == pytest.approx(1000 * 0.0000005 + 500 * 0.0000015, rel=1e-9)


def test_openrouter_bare_alias_lookup(monkeypatch):
    """The bare model name (no vendor prefix) resolves via the double-keyed cache."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("openrouter/deepseek-v4-pro", 1000, 0)

    assert cost == pytest.approx(1000 * 0.0000005, rel=1e-9)


def test_openrouter_miss_degrades_to_none(monkeypatch):
    """An OpenRouter model absent from the /models table still degrades to None."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("openrouter/some/model-not-listed", 1000, 500)

    assert cost is None


def test_openrouter_offline_degrades_to_none(monkeypatch):
    """A network failure must never fabricate a rate — cost falls to None."""

    def boom(req):
        raise httpx.ConnectError("offline")

    _patch_openrouter(monkeypatch, boom)

    cost = estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert cost is None


def test_openrouter_response_cached_for_an_hour(monkeypatch):
    """The /models table is fetched once and reused across estimates."""
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)
    estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 10, 20)

    assert counter["calls"] == 1


def test_non_openrouter_unmapped_model_consults_catalog(monkeypatch):
    """Tier 2: any LiteLLM-miss model (not just openrouter/) consults the catalog,
    and degrades to None when absent."""
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("nonexistent-vendor/imaginary-model-9000", 100, 100)

    assert cost is None
    assert counter["calls"] == 1


_UNMAPPED_CATALOG = [
    {
        "id": "fakevendor/imaginary-priced-9000",
        "context_length": 163840,
        "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
    }
]


def test_non_openrouter_model_priced_via_catalog(monkeypatch):
    """Tier 2: a bare provider model LiteLLM misses is priced off the OpenRouter
    catalog, no openrouter/ prefix required. Uses a vendor LiteLLM does not know
    so Tier 1 genuinely misses and the catalog path is exercised."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_UNMAPPED_CATALOG))

    cost = estimate_cost_usd("fakevendor/imaginary-priced-9000", 1000, 500)

    assert cost == pytest.approx(1000 * 0.0000005 + 500 * 0.0000015, rel=1e-9)


def _patch_litellm_info(monkeypatch, fn):
    """Stub litellm.get_model_info (offline) — fn(model) returns a dict or raises."""
    import litellm

    monkeypatch.setattr(litellm, "get_model_info", fn)


def _litellm_miss(_model):
    raise Exception("This model isn't mapped yet")


def test_resolve_context_window_from_litellm_no_network(monkeypatch):
    """Tier 1: a LiteLLM-mapped model's window comes from LiteLLM, no OpenRouter hit."""
    _patch_litellm_info(monkeypatch, lambda m: {"max_input_tokens": 200000})
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("anthropic/claude-sonnet-4-5") == 200000
    assert counter["calls"] == 0


def test_resolve_context_window_from_openrouter_when_litellm_misses(monkeypatch):
    """An OpenRouter model LiteLLM lags on falls back to the live /models table."""
    _patch_litellm_info(monkeypatch, _litellm_miss)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("openrouter/deepseek/deepseek-v4-pro") == 163840
    assert resolve_context_window("openrouter/deepseek-v4-pro") == 163840


def test_resolve_context_window_non_openrouter_via_catalog(monkeypatch):
    """Tier 2: a bare provider model LiteLLM misses resolves via the OpenRouter catalog."""
    _patch_litellm_info(monkeypatch, _litellm_miss)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("deepseek/deepseek-v4-pro") == 163840


def test_resolve_context_window_unknown_returns_none(monkeypatch):
    """Unknown to both LiteLLM and the OpenRouter catalog resolves to None."""
    _patch_litellm_info(monkeypatch, _litellm_miss)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("openrouter/some/model-not-listed") is None


# --- Disk persistence of the OpenRouter catalog ---

_DEEPSEEK_PRICE = (0.0000005, 0.0000015)


def _disk_payload(fetched_at, *, prompt="0.0000005", completion="0.0000015", version=None):
    return {
        "version": model_catalog_cache.CACHE_VERSION if version is None else version,
        "fetched_at": fetched_at,
        "models": {
            "deepseek/deepseek-v4-pro": {
                "pricing": {"prompt": prompt, "completion": completion},
                "context_length": 163840,
            }
        },
    }


@pytest.fixture
def disk_cache(tmp_path, monkeypatch):
    """Point the OpenRouter disk cache at a temp file; never touch real ~/.raven."""
    path = tmp_path / "model-catalog.json"
    monkeypatch.setattr(model_catalog_cache, "_CACHE_PATH", path, raising=False)
    pricing._OPENROUTER_CACHE.clear()
    monkeypatch.setattr(pricing, "_OPENROUTER_CACHE_TIME", 0.0)
    return path


def test_cold_fetch_writes_disk_cache(monkeypatch, disk_cache):
    """A cold network fetch persists the catalog as a versioned envelope."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert disk_cache.exists()
    payload = json.loads(disk_cache.read_text(encoding="utf-8"))
    assert payload["version"] == model_catalog_cache.CACHE_VERSION
    assert payload["fetched_at"] > 0
    assert "deepseek/deepseek-v4-pro" in payload["models"]


def test_warm_disk_hit_skips_network(monkeypatch, disk_cache):
    """A fresh disk file hydrates the in-proc cache with zero network calls."""
    disk_cache.write_text(json.dumps(_disk_payload(time.time())), encoding="utf-8")
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 0
    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)


def test_expired_disk_triggers_refetch(monkeypatch, disk_cache):
    """A disk file older than the TTL is not served fresh — the catalog refetches."""
    stale_at = time.time() - (pricing._OPENROUTER_CACHE_TTL + 100)
    disk_cache.write_text(json.dumps(_disk_payload(stale_at, prompt="9", completion="9")), encoding="utf-8")
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 1
    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)


def test_version_mismatch_ignored(monkeypatch, disk_cache):
    """A file whose version differs from CACHE_VERSION is treated as a miss."""
    disk_cache.write_text(
        json.dumps(_disk_payload(time.time(), prompt="9", completion="9", version=999)),
        encoding="utf-8",
    )
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 1
    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)
    # The bad-version file is overwritten with a current-version envelope.
    assert json.loads(disk_cache.read_text(encoding="utf-8"))["version"] == model_catalog_cache.CACHE_VERSION


def test_corrupt_disk_degrades_to_network(monkeypatch, disk_cache):
    """An unparseable cache file degrades to a miss and falls through to network."""
    disk_cache.write_text("{ this is not valid json", encoding="utf-8")
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 1
    assert cost is not None
    # The corrupt file is replaced by a clean, parseable envelope.
    assert json.loads(disk_cache.read_text(encoding="utf-8"))["version"] == model_catalog_cache.CACHE_VERSION


def test_network_fail_falls_back_to_stale_disk(monkeypatch, disk_cache):
    """On a network failure with an empty in-proc cache, the stale disk file is served."""
    stale_at = time.time() - (pricing._OPENROUTER_CACHE_TTL + 100)
    disk_cache.write_text(json.dumps(_disk_payload(stale_at)), encoding="utf-8")

    def boom(req):
        raise httpx.ConnectError("offline")

    _patch_openrouter(monkeypatch, boom)

    cost = estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)


def test_disk_write_is_atomic(monkeypatch, disk_cache):
    """The write leaves no temp file behind and the cache file parses cleanly."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    estimate_cost_usd("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert list(disk_cache.parent.glob("*.tmp")) == []
    json.loads(disk_cache.read_text(encoding="utf-8"))
