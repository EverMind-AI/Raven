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


def _patch_litellm_blind(monkeypatch):
    """Make LiteLLM miss for real: the price table is consulted before the ask.

    Stubbing ``get_model_info`` alone stopped being enough once the table is read
    first -- a model the table keys exactly is answered there and never reaches
    the OpenRouter tier, which is the point of reading it first.
    """
    import litellm

    monkeypatch.setattr(litellm, "model_cost", {})
    _patch_litellm_info(monkeypatch, _litellm_miss)


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
    _patch_litellm_blind(monkeypatch)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("deepseek/deepseek-v4-pro") == 163840


def test_resolve_context_window_unknown_returns_none(monkeypatch):
    """Unknown to both LiteLLM and the OpenRouter catalog resolves to None."""
    _patch_litellm_info(monkeypatch, _litellm_miss)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("openrouter/some/model-not-listed") is None


_COPILOT_MODELS = [
    {
        "id": "github_copilot/gpt-4.1",
        "pricing": {"prompt": "0.000002", "completion": "0.000008"},
        "context_length": 128000,
    }
]


def _forbid(recorder: list):
    """A stub that records the call and misses.

    Recorded rather than raised: both lookups swallow exceptions to move to the
    next candidate, so a probe that raises is caught and proves nothing. That is
    how the first version of these tests passed against the unfixed code.
    """

    def _stub(*args, **kwargs):
        recorder.append(kwargs.get("model") or (args[0] if args else "?"))
        raise Exception("unmapped")

    return _stub


def test_the_window_of_a_login_prompting_model_comes_from_the_table(monkeypatch):
    """Asking LiteLLM about a Copilot model starts a GitHub device flow.

    It resolves the model's credentials on the way to its metadata, so with no
    token file on disk one lookup printed six device codes to stdout and blocked
    for 410 seconds -- two three-attempt login cycles, because the bare and the
    openrouter-prefixed candidate reach the same driver. session.create runs this
    before the first turn, so the symptom was a gateway that hung on opening.
    """
    import litellm

    asked: list[str] = []
    monkeypatch.setattr(litellm, "get_model_info", _forbid(asked))
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("github_copilot/gpt-4.1") == 128000
    assert not asked, f"a login-prompting model was handed to LiteLLM: {asked}"
    assert counter["calls"] == 0


def test_the_rates_of_a_login_prompting_model_skip_litellm(monkeypatch):
    """The same hang, on the path that runs after every single call.

    ``cost_per_token`` resolves credentials too, so the cost estimate blocked on
    the same device flow. Fixing only the window lookup left this one, and
    answering "can this be handed to LiteLLM" separately in each place is what
    made the first attempt at this wrong.

    The estimate still lands: the live catalogue prices the model. Asking LiteLLM
    is what is skipped, not estimating.
    """
    import litellm

    asked: list[str] = []
    monkeypatch.setattr(litellm, "cost_per_token", _forbid(asked))
    _patch_openrouter(monkeypatch, lambda req: _models_response(_COPILOT_MODELS))

    cost = estimate_cost_usd("github_copilot/gpt-4.1", 1000, 100)
    assert cost is not None and cost > 0
    assert not asked, f"a login-prompting model was handed to LiteLLM: {asked}"


def test_a_login_prompting_model_the_table_does_not_price_is_never_asked(monkeypatch):
    """No row and no safe way to ask: degrade, do not prompt.

    Both lookups fall through to what they already do for an unknown model -- the
    caller keeps its configured window, and the cost estimate is None. A read that
    runs every turn is not worth a login prompt.
    """
    import litellm

    asked: list[str] = []
    monkeypatch.setattr(litellm, "get_model_info", _forbid(asked))
    monkeypatch.setattr(litellm, "cost_per_token", _forbid(asked))
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("github_copilot/not-a-real-model") is None
    assert estimate_cost_usd("github_copilot/not-a-real-model", 1000, 100) is None
    assert not asked, f"asked anyway: {asked}"


def test_reading_the_table_does_not_replace_asking_litellm(monkeypatch):
    """The ask does more than key normalization, so it stays for everything else.

    "anthropic/claude-sonnet-4-5" is absent from the table -- it keys that model
    bare -- and prefixed ids are the form Raven stores. Stripping the prefix to
    read the table instead looked free and was not: for an openrouter-prefixed
    candidate LiteLLM derives OpenRouter's own numbers, which are in no row, and
    stripping answered three MiniMax models with the direct figure instead.
    """
    import litellm

    assert "anthropic/claude-sonnet-4-5" not in getattr(litellm, "model_cost", {}), (
        "premise changed; this test proves nothing"
    )
    _patch_litellm_info(monkeypatch, lambda m: {"max_input_tokens": 200000})
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("anthropic/claude-sonnet-4-5") == 200000
    assert counter["calls"] == 0


def test_prose_in_a_numeric_field_is_not_read_as_a_number():
    """LiteLLM ships a self-documenting row whose numeric fields hold sentences."""
    from raven.token_wise.pricing import _numeric

    prose = {"max_input_tokens": "max input tokens, if the provider specifies it"}
    assert _numeric(prose, "max_input_tokens") is None
    assert _numeric({"max_input_tokens": 128000}, "max_input_tokens") == 128000
    assert _numeric({"max_tokens": 8192}, "max_input_tokens", "max_tokens") == 8192
    assert _numeric(None, "max_tokens") is None


def test_which_drivers_can_prompt_is_read_from_the_installed_litellm():
    """Derived, not snapshotted, so a LiteLLM bump cannot make it stale.

    A frozen list would need regenerating on every bump, and a stale one brings
    the hang back for the vendor it missed. The driver ships ``authenticator.py``
    or it does not.
    """
    from raven.token_wise.pricing import _may_prompt

    assert _may_prompt("github_copilot/gpt-4.1")
    assert _may_prompt("openrouter/github_copilot/gpt-4.1"), "any segment counts"
    assert _may_prompt("chatgpt/gpt-5.1")
    assert _may_prompt("gigachat/GigaChat-2-Max")
    for safe in ("openai/gpt-4o", "anthropic/claude-sonnet-4-5", "deepseek/deepseek-v4-pro", "gpt-4o"):
        assert not _may_prompt(safe), safe


def test_a_model_reached_by_region_or_subscription_is_looked_up_as_the_vendor_files_it():
    """Routing says where the request goes; the table is keyed by what the model
    is. Asking with the routing id missed every time, so a Codex or MiniMax OAuth
    model had no context window and no price at all."""
    assert pricing._candidates("openai-codex/gpt-5.3-codex") == ["chatgpt/gpt-5.3-codex"]
    assert pricing._candidates("minimax-global/MiniMax-M3") == ["minimax/MiniMax-M3"]
    assert pricing._candidates("minimax-cn/MiniMax-M3") == ["minimax/MiniMax-M3"]

    # Everything whose name is already LiteLLM's keeps the pair it always had.
    assert pricing._candidates("deepseek/deepseek-chat") == [
        "openrouter/deepseek/deepseek-chat",
        "deepseek/deepseek-chat",
    ]
    assert pricing._candidates("openrouter/anthropic/claude-opus-4.8") == ["openrouter/anthropic/claude-opus-4.8"]


def test_the_window_those_families_report_is_the_vendors_own():
    """Read from LiteLLM's table offline, so this is the number, not a default."""
    assert pricing._try_litellm_context_window("openai-codex/gpt-5.3-codex") == 128_000
    assert pricing._try_litellm_context_window("minimax-global/MiniMax-M3") == 1_000_000


def test_one_place_decides_whether_a_model_can_be_handed_to_litellm():
    """Both lookups reach the same authenticator, so both consult one answer.

    The first attempt at this guarded the metadata lookup only, and would have
    needed the same decision again for the pricing call -- and again for
    ``validate_environment``, which turned out to prompt as well.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "raven" / "token_wise" / "pricing.py").read_text()
    tree = ast.parse(source)
    owner = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_may_prompt")
    allowed = range(owner.lineno, (owner.end_lineno or owner.lineno) + 1)
    offenders = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(source.splitlines(), 1)
        if "authenticator.py" in line and i not in allowed
    ]
    assert not offenders, "ask _may_prompt instead:\n" + "\n".join(offenders)


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
