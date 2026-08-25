"""Tests for raven.providers.rates -- what a model costs and how much it holds.

Split out of ``test_token_wise_pricing.py`` when the resolution ladder moved into
``raven.providers``: these exercise where a number comes from, while the cost
arithmetic on top of it stays with the module that does the arithmetic.
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx
import pytest

from raven.providers import model_catalog_cache, rates
from raven.providers.base import send_max_tokens
from raven.providers.litellm_setup import import_litellm
from raven.providers.rates import (
    _FALLBACK_PRICING,
    resolve_context_window,
    token_rates,
)

# Imported here, at collection, and not left to whichever test asks for it first:
# `_patch_openrouter` replaces `httpx.Client` process-wide, and LiteLLM builds
# clients on the way up. A first import under that patch leaves the module
# half-initialised, and every later `import litellm` -- including the ones inside
# these tests -- gets back the broken one with a circular-import AttributeError.
import_litellm()

# The real fetch, captured before conftest's autouse guard stubs it to {}.
_REAL_FETCH = rates._fetch_openrouter_models


@pytest.fixture(autouse=True)
def _reset_catalog_state():
    rates._OPENROUTER_CACHE.clear()
    yield
    rates._OPENROUTER_CACHE.clear()


@pytest.fixture
def minimax_row(monkeypatch):
    """Pin the one row the assertions below need and the offline table lacks.

    LiteLLM's bundled table carries no `minimax/MiniMax-M3` -- only the
    bedrock-prefixed MiniMax models -- so the figure these two read was coming
    from the table LiteLLM fetches remotely. Pinned here, what they exercise is
    the resolution ladder rather than what MiniMax published this morning.
    `minimax/` and not `minimax-global/`: `_candidates` maps the plan-billed
    prefix onto the direct one before the table is consulted.
    """
    litellm = import_litellm()
    monkeypatch.setattr(
        litellm, "model_cost", {**litellm.model_cost, "minimax/MiniMax-M3": {"max_input_tokens": 1_000_000}}
    )


def _patch_openrouter(monkeypatch, handler):
    """Route the real OpenRouter fetch through a MockTransport.

    Restores the real ``_fetch_openrouter_models`` (conftest stubs it to {} so no
    test hits the network by default), then mocks the httpx transport. Returns a
    counter dict whose ``["calls"]`` tracks network hits.
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

    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(rates.httpx, "Client", client_factory)
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", 0.0)
    return counter


def _models_response(models):
    return httpx.Response(200, content=json.dumps({"data": models}))


def _rate_cost(model, input_tokens, output_tokens):
    """Rates applied to token counts, so a tier can be asserted as a figure."""
    pair = token_rates(model, input_tokens, output_tokens)
    return None if pair is None else input_tokens * pair[0] + output_tokens * pair[1]


_DEEPSEEK_MODELS = [
    {
        "id": "deepseek/deepseek-v4-pro",
        "context_length": 163840,
        "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
    }
]


# --- The tier LiteLLM answers (see `rates.token_rates` for the order) ---


def test_the_suite_reads_litellms_table_offline():
    """The table has to be a fixed input, not whatever a vendor published today.

    LiteLLM fetches it at import unless this is set, and setting it after the
    import is too late, so the value is published from `conftest` before any test
    module loads. Asserted rather than assumed: without it a row appearing
    upstream answers a lookup the tests below arrange to miss, and they fail on
    numbers no commit here touched.
    """
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"


def test_a_litellm_mapped_model_is_priced_without_touching_the_network(monkeypatch):
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert token_rates("anthropic/claude-sonnet-4-5", 1000, 500) is not None
    assert counter["calls"] == 0


def test_a_model_no_tier_knows_has_no_rates():
    assert token_rates("nonexistent-vendor/imaginary-model-9000", 100, 100) is None


# --- The tier the bundled snapshot answers, keyed by provider ---


def test_a_vendor_litellm_does_not_price_is_answered_by_its_own_published_rate(monkeypatch):
    """The tier that exists so a direct route is not priced off a gateway's table.

    ``zai/glm-5.2`` was reported at OpenRouter's figure because the live catalogue
    happened to carry a model of that name. The snapshot is keyed by provider, so
    Z.ai's own published price is the one that answers.
    """
    from raven.providers.catalog import model_cost

    published = model_cost("zai/glm-5.2")
    if not published:
        pytest.skip("the snapshot no longer carries this model")

    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))
    pair = rates._try_snapshot_rates("zai/glm-5.2")

    assert pair == (published["input"] / 1e6, published["output"] / 1e6)
    assert counter["calls"] == 0, "the vendor's own row needs no live catalogue"


def test_the_snapshot_tier_is_silent_about_a_model_it_does_not_carry():
    assert rates._try_snapshot_rates("nonexistent-vendor/imaginary-model-9000") is None


# --- The tier OpenRouter's live catalogue answers, for ids naming it ---


def test_an_openrouter_model_litellm_lags_on_is_priced_live(monkeypatch):
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = _rate_cost("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert cost == pytest.approx(1000 * 0.0000005 + 500 * 0.0000015, rel=1e-9)


def test_a_bare_id_under_openrouter_resolves_to_the_same_row(monkeypatch):
    """Within OpenRouter's own namespace a bare id names the model the full one
    does, so the alias stays. What was removed is asking this table at all about a
    request that does not go to OpenRouter."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert _rate_cost("openrouter/deepseek-v4-pro", 1000, 0) == pytest.approx(1000 * 0.0000005, rel=1e-9)


def test_an_openrouter_model_absent_from_the_table_has_no_rates(monkeypatch):
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert token_rates("openrouter/some/model-not-listed", 1000, 500) is None


def test_a_network_failure_falls_through_to_the_bundled_copy(monkeypatch):
    """Offline, the ladder keeps going rather than inventing a number.

    The live tier degrades to nothing and the bundled snapshot answers, which is
    the whole reason a copy ships: a figure from the last refresh beats no figure
    at all for a total nobody can act on. Nothing is fabricated -- a model absent
    from every tier is still None.
    """
    from raven.providers.catalog import model_cost

    def boom(req):
        raise httpx.ConnectError("offline")

    _patch_openrouter(monkeypatch, boom)

    published = model_cost("openrouter/deepseek/deepseek-v4-pro")
    if published:
        assert token_rates("openrouter/deepseek/deepseek-v4-pro", 1000, 500) == (
            published["input"] / 1e6,
            published["output"] / 1e6,
        )
    assert token_rates("openrouter/nobody/has-heard-of-this", 1000, 500) is None


def test_the_gateway_price_wins_over_the_routers_copy(monkeypatch):
    """Who sends the request is not who bills for it.

    LiteLLM routes an ``openrouter/`` id and also carries a row for it, so its
    answer used to win -- but OpenRouter is the party charging, and the two
    disagree. Measured on the pinned LiteLLM: ``openrouter/z-ai/glm-4.6`` is
    filed at 0.40/1.75 per million where OpenRouter's own table says 0.50/2.00,
    so every such call was under-reported by a fifth.

    A model LiteLLM knows is used deliberately: with one it does not, tier 1
    misses and the old order would pass this too.
    """
    router = rates._try_litellm_rates("openrouter/openai/gpt-4.1", 1000, 500)
    assert router, "fixture needs a model LiteLLM prices under its openrouter id"

    gateway_prompt, gateway_completion = router[0] * 2, router[1] * 2
    _patch_openrouter(
        monkeypatch,
        lambda req: _models_response(
            [
                {
                    "id": "openai/gpt-4.1",
                    "pricing": {"prompt": str(gateway_prompt), "completion": str(gateway_completion)},
                }
            ]
        ),
    )
    rates._fetch_openrouter_models()  # tier 0 is cache-only; warm it as a real run does

    assert token_rates("openrouter/openai/gpt-4.1", 1000, 500) == (gateway_prompt, gateway_completion)


def test_a_direct_id_is_never_priced_from_the_gateways_table(monkeypatch):
    """The other half. Reading OpenRouter's row for an id that does not name it
    is what priced a self-hosted deployment at a hosted model's rate, and tier 0
    must not reintroduce it.
    """
    _patch_openrouter(
        monkeypatch,
        lambda req: _models_response([{"id": "openai/gpt-4.1", "pricing": {"prompt": "999", "completion": "999"}}]),
    )
    rates._fetch_openrouter_models()

    direct = token_rates("openai/gpt-4.1", 1000, 500)

    assert direct is not None
    assert direct != (999.0, 999.0)


def test_the_price_ladder_never_fetches_on_its_first_tier(monkeypatch):
    """Pricing runs after every call, inside the turn. Tier 0 reads a catalogue
    already in hand; making it fetch would put an HTTP round-trip on the path of
    every completion.
    """
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    # A model LiteLLM knows, so tier 1 answers and tiers 2+ never run.
    assert token_rates("openrouter/openai/gpt-4.1", 1000, 500) is not None

    assert counter["calls"] == 0


def test_the_live_table_is_fetched_once_and_reused(monkeypatch):
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    token_rates("openrouter/deepseek/deepseek-v4-pro", 1000, 500)
    token_rates("openrouter/deepseek/deepseek-v4-pro", 10, 20)

    assert counter["calls"] == 1


_UNMAPPED_CATALOG = [
    {
        "id": "fakevendor/imaginary-priced-9000",
        "context_length": 163840,
        "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
    }
]


def test_a_model_that_does_not_route_through_openrouter_never_reads_its_table(monkeypatch):
    """The bare-name hijack, asserted from the other side.

    OpenRouter's table used to answer for every id LiteLLM missed. It is a
    cross-vendor catalogue, so a self-hosted deployment or a direct vendor route
    was reported at somebody else's price -- and, worse, somebody else's context
    window. An id that does not name OpenRouter must not reach it at all.
    """
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_UNMAPPED_CATALOG))

    assert token_rates("fakevendor/imaginary-priced-9000", 1000, 500) is None
    assert counter["calls"] == 0, "the live catalogue was consulted for a non-OpenRouter id"


def test_the_hijacked_model_reports_neither_a_price_nor_a_window(monkeypatch):
    """The case this was filed for: a self-hosted vLLM borrowed a hosted model's
    numbers because the bare name matched. Unknown is the correct answer -- the
    caller keeps its configured window rather than trimming to a stranger's."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_UNMAPPED_CATALOG))

    assert token_rates("hosted_vllm/qwen3-32b") is None
    assert resolve_context_window("hosted_vllm/qwen3-32b") is None


# --- The manual table, last ---


def test_the_manual_table_answers_a_model_too_new_for_the_others():
    model = next(iter(_FALLBACK_PRICING))
    p_rate, c_rate = _FALLBACK_PRICING[model]

    assert _rate_cost(model, 1000, 500) == pytest.approx(1000 * p_rate + 500 * c_rate, rel=0.01)


# --- Context windows ---


def _patch_litellm_info(monkeypatch, fn):
    """Stub litellm.get_model_info (offline) -- fn(model) returns a dict or raises."""
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


def test_a_litellm_mapped_window_comes_from_litellm_with_no_network(monkeypatch):
    _patch_litellm_info(monkeypatch, lambda m: {"max_input_tokens": 200000})
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("anthropic/claude-sonnet-4-5") == 200000
    assert counter["calls"] == 0


def test_an_openrouter_window_falls_back_to_the_live_table(monkeypatch):
    _patch_litellm_info(monkeypatch, _litellm_miss)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("openrouter/deepseek/deepseek-v4-pro") == 163840
    assert resolve_context_window("openrouter/deepseek-v4-pro") == 163840


def test_a_direct_route_gets_no_window_from_the_gateways_table(monkeypatch):
    """The window half of the hijack. A direct ``deepseek/`` route is not an
    OpenRouter request, so OpenRouter's context length does not describe it."""
    _patch_litellm_blind(monkeypatch)
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("deepseek/deepseek-v4-pro") is None
    assert counter["calls"] == 0


def test_the_snapshot_is_not_a_window_source(monkeypatch):
    """A window sizes trimming, so it shapes the next request -- which is the line
    the catalogue snapshot is deliberately kept on the other side of. It carries
    labels and a price and no ``limit``, and this asserts it stays that way even
    for a model it fully describes."""
    from raven.providers.catalog import model_cost

    assert model_cost("zai/glm-5.2"), "premise changed; pick another snapshot-only model"
    _patch_litellm_blind(monkeypatch)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("zai/glm-5.2") is None


def test_a_window_unknown_to_every_source_is_none(monkeypatch):
    _patch_litellm_info(monkeypatch, _litellm_miss)
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("openrouter/some/model-not-listed") is None


# --- effective_context_window: explicit config > real window > fallback ---


def test_a_configured_window_wins_even_when_the_real_one_disagrees(monkeypatch):
    """A pin is an override, so it answers even when the model has a real window."""
    monkeypatch.setattr(rates, "resolve_context_window", lambda model, **kw: 200_000)

    assert rates.effective_context_window("anthropic/claude-sonnet-4-5", 40_000) == 40_000


def test_no_configured_window_falls_back_to_the_real_one(monkeypatch):
    monkeypatch.setattr(rates, "resolve_context_window", lambda model, **kw: 200_000)

    assert rates.effective_context_window("anthropic/claude-sonnet-4-5", None) == 200_000
    assert rates.effective_context_window("anthropic/claude-sonnet-4-5", 0) == 200_000


def test_neither_configured_nor_resolvable_uses_the_documented_default(monkeypatch):
    monkeypatch.setattr(rates, "resolve_context_window", lambda model, **kw: None)

    assert rates.effective_context_window("some/unknown-model", None) == rates.DEFAULT_CONTEXT_WINDOW_TOKENS


def test_effective_context_window_passes_allow_fetch_through(monkeypatch):
    """The construction-time caller's ``allow_fetch=False`` must reach the ladder."""
    seen: dict = {}
    monkeypatch.setattr(
        rates,
        "resolve_context_window",
        lambda model, **kw: seen.update(kw) or 200_000,
    )

    rates.effective_context_window("anthropic/claude-sonnet-4-5", None, allow_fetch=False)

    assert seen == {"allow_fetch": False}


# --- Models whose driver would start an interactive login ---

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
    token file on disk the lookup prints device codes to stdout and blocks --
    twice over, because the bare and the openrouter-prefixed candidate reach the
    same driver. session.create runs this before the first turn, so the symptom
    was a gateway that hung on opening.
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
    """
    import litellm

    asked: list[str] = []
    monkeypatch.setattr(litellm, "cost_per_token", _forbid(asked))
    _patch_openrouter(monkeypatch, lambda req: _models_response(_COPILOT_MODELS))

    token_rates("github_copilot/gpt-4.1", 1000, 100)

    assert not asked, f"a login-prompting model was handed to LiteLLM: {asked}"


def test_a_login_prompting_model_no_source_knows_is_never_asked(monkeypatch):
    """No row and no safe way to ask: degrade, do not prompt.

    Both lookups fall through to what they already do for an unknown model -- the
    caller keeps its configured window, and there are no rates. A read that runs
    every turn is not worth a login prompt.
    """
    import litellm

    asked: list[str] = []
    monkeypatch.setattr(litellm, "get_model_info", _forbid(asked))
    monkeypatch.setattr(litellm, "cost_per_token", _forbid(asked))
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    assert resolve_context_window("github_copilot/not-a-real-model") is None
    assert token_rates("github_copilot/not-a-real-model", 1000, 100) is None
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


def test_one_place_decides_whether_a_model_can_be_handed_to_litellm():
    """Both lookups reach the same authenticator, so both consult one answer.

    The first attempt at this guarded the metadata lookup only, and would have
    needed the same decision again for the pricing call -- and again for
    ``validate_environment``, which turned out to prompt as well.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "raven" / "providers" / "rates.py").read_text()
    tree = ast.parse(source)
    owner = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_may_prompt")
    allowed = range(owner.lineno, (owner.end_lineno or owner.lineno) + 1)
    offenders = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(source.splitlines(), 1)
        if "authenticator.py" in line and i not in allowed
    ]
    assert not offenders, "ask _may_prompt instead:\n" + "\n".join(offenders)


# --- Field reading and id candidates ---


def test_prose_in_a_numeric_field_is_not_read_as_a_number():
    """LiteLLM ships a self-documenting row whose numeric fields hold sentences."""
    from raven.providers.rates import _numeric

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
    from raven.providers.rates import _may_prompt

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
    assert rates._candidates("openai-codex/gpt-5.3-codex") == ["chatgpt/gpt-5.3-codex"]
    assert rates._candidates("minimax-global/MiniMax-M3") == ["minimax/MiniMax-M3"]
    assert rates._candidates("minimax-cn/MiniMax-M3") == ["minimax/MiniMax-M3"]

    # Everything else is asked as it routes, with the alias behind it.
    assert rates._candidates("deepseek/deepseek-chat") == [
        "deepseek/deepseek-chat",
        "openrouter/deepseek/deepseek-chat",
    ]
    assert rates._candidates("openrouter/anthropic/claude-opus-4.8") == ["openrouter/anthropic/claude-opus-4.8"]


# --- Plan billing ---


def test_plan_billing_is_declared_not_inferred_from_oauth():
    """OAuth is how you authenticate, not how you are charged -- Vertex is OAuth
    and metered, so the flag cannot stand in for the other."""
    from raven.providers.registry import PROVIDERS

    plan_billed = {spec.name for spec in PROVIDERS if spec.billing == "plan"}
    assert plan_billed == {"openai_codex", "github_copilot", "minimax_global", "minimax_cn"}


def test_a_plan_billed_provider_still_reports_a_window(minimax_row):
    """Occupancy is the measure that means something on a subscription, so the
    window resolves even where no per-token figure describes the call."""
    for model in ("github_copilot/gpt-4o", "openai-codex/gpt-5.3-codex", "minimax-global/MiniMax-M3"):
        assert resolve_context_window(model), f"{model}: window still expected"


def test_a_directly_routed_model_is_priced_as_the_vendor_prices_it():
    """The alias answers with OpenRouter's numbers, which a user routing straight
    to the vendor does not pay. Asked alias-first, this model reported half its
    window at half its price."""
    assert resolve_context_window("deepseek/deepseek-chat") == 131_072
    assert _rate_cost("deepseek/deepseek-chat", 1_000_000, 0) == pytest.approx(0.28)

    # Routed through the gateway, OpenRouter's own numbers are the right ones.
    assert resolve_context_window("openrouter/deepseek/deepseek-chat") == 65_536


def test_the_window_those_families_report_is_the_vendors_own(minimax_row):
    """Read from LiteLLM's table offline, so this is the number, not a default."""
    assert rates._try_litellm_context_window("openai-codex/gpt-5.3-codex") == 128_000
    assert rates._try_litellm_context_window("minimax-global/MiniMax-M3") == 1_000_000


# --- allow_import=False: a cheap caller must not pay LiteLLM's ~2-7s import ---
#
# SF11: ``AgentLoop.__init__`` resolves a construction-time window with
# ``allow_fetch=False`` before ``LazyProvider``'s background thread has had a
# chance to import LiteLLM. Reaching for the import here on the main thread
# defeats the whole point of deferring it. ``import_litellm()`` was already
# forced at module load (see the top of this file) so LiteLLM is always
# present in ``sys.modules`` for every other test below -- these two
# temporarily hide that key to exercise the "not yet imported" branch.


def test_try_litellm_context_window_allow_import_false_skips_the_import_when_absent(monkeypatch):
    monkeypatch.delitem(sys.modules, "litellm", raising=False)
    called = {"n": 0}
    real_import_litellm = import_litellm

    def _spy():
        called["n"] += 1
        return real_import_litellm()

    monkeypatch.setattr("raven.providers.litellm_setup.import_litellm", _spy)

    assert rates._try_litellm_context_window("openai-codex/gpt-5.3-codex", allow_import=False) is None
    assert called["n"] == 0


def test_try_litellm_context_window_allow_import_false_still_answers_once_imported():
    """Once LiteLLM is already imported the gate is free, and the answer must
    not differ from the ``allow_import=True`` (default) path."""
    assert "litellm" in sys.modules
    assert rates._try_litellm_context_window(
        "openai-codex/gpt-5.3-codex", allow_import=False
    ) == rates._try_litellm_context_window("openai-codex/gpt-5.3-codex")


def test_resolve_context_window_allow_fetch_false_also_forwards_allow_import_false(monkeypatch):
    """One flag, one layer of semantics: allow_fetch=False must reach the
    LiteLLM tier as allow_import=False, not just the OpenRouter tier."""
    seen = {}

    def _fake_litellm_tier(model, *, allow_import=True):
        seen["allow_import"] = allow_import
        return None

    monkeypatch.setattr(rates, "_try_litellm_context_window", _fake_litellm_tier)
    monkeypatch.setattr(rates, "_lookup_openrouter_entry", lambda model, *, allow_fetch=True: None)

    rates.resolve_context_window("openrouter/deepseek/deepseek-v4-pro", allow_fetch=False)

    assert seen["allow_import"] is False


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
    rates._OPENROUTER_CACHE.clear()
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", 0.0)
    return path


def test_cold_fetch_writes_disk_cache(monkeypatch, disk_cache):
    """A cold network fetch persists the catalog as a versioned envelope."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    token_rates("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert disk_cache.exists()
    payload = json.loads(disk_cache.read_text(encoding="utf-8"))
    assert payload["version"] == model_catalog_cache.CACHE_VERSION
    assert payload["fetched_at"] > 0
    assert "deepseek/deepseek-v4-pro" in payload["models"]


def test_warm_disk_hit_skips_network(monkeypatch, disk_cache):
    """A fresh disk file hydrates the in-proc cache with zero network calls."""
    disk_cache.write_text(json.dumps(_disk_payload(time.time())), encoding="utf-8")
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = _rate_cost("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 0
    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)


def test_expired_disk_triggers_refetch(monkeypatch, disk_cache):
    """A disk file older than the TTL is not served fresh -- the catalog refetches."""
    stale_at = time.time() - (rates._OPENROUTER_CACHE_TTL + 100)
    disk_cache.write_text(json.dumps(_disk_payload(stale_at, prompt="9", completion="9")), encoding="utf-8")
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = _rate_cost("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 1
    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)


def test_version_mismatch_ignored(monkeypatch, disk_cache):
    """A file whose version differs from CACHE_VERSION is treated as a miss."""
    disk_cache.write_text(
        json.dumps(_disk_payload(time.time(), prompt="9", completion="9", version=999)),
        encoding="utf-8",
    )
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = _rate_cost("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 1
    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)
    # The bad-version file is overwritten with a current-version envelope.
    assert json.loads(disk_cache.read_text(encoding="utf-8"))["version"] == model_catalog_cache.CACHE_VERSION


def test_valid_json_non_dict_cache_is_a_miss(monkeypatch, disk_cache):
    """A file holding valid JSON that is not a dict ([] / null / 42) is a miss.

    raw.get on a list raised AttributeError straight through
    resolve_context_window and token_rates -- the exact raise the loader's
    docstring promises never reaches the cost path.
    """
    disk_cache.write_text("[]", encoding="utf-8")
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = _rate_cost("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 1
    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)


def test_corrupt_disk_degrades_to_network(monkeypatch, disk_cache):
    """An unparseable cache file degrades to a miss and falls through to network."""
    disk_cache.write_text("{ this is not valid json", encoding="utf-8")
    counter = _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    cost = _rate_cost("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert counter["calls"] == 1
    assert cost is not None
    # The corrupt file is replaced by a clean, parseable envelope.
    assert json.loads(disk_cache.read_text(encoding="utf-8"))["version"] == model_catalog_cache.CACHE_VERSION


def test_network_fail_falls_back_to_stale_disk(monkeypatch, disk_cache):
    """On a network failure with an empty in-proc cache, the stale disk file is served."""
    stale_at = time.time() - (rates._OPENROUTER_CACHE_TTL + 100)
    disk_cache.write_text(json.dumps(_disk_payload(stale_at)), encoding="utf-8")

    def boom(req):
        raise httpx.ConnectError("offline")

    _patch_openrouter(monkeypatch, boom)

    cost = _rate_cost("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert cost == pytest.approx(1000 * _DEEPSEEK_PRICE[0] + 500 * _DEEPSEEK_PRICE[1], rel=1e-9)


def test_disk_write_is_atomic(monkeypatch, disk_cache):
    """The write leaves no temp file behind and the cache file parses cleanly."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DEEPSEEK_MODELS))

    token_rates("openrouter/deepseek/deepseek-v4-pro", 1000, 500)

    assert list(disk_cache.parent.glob("*.tmp")) == []
    json.loads(disk_cache.read_text(encoding="utf-8"))


# --- allow_fetch=False: construction / event-loop callers never touch the network ---
#
# ``AgentLoop.__init__`` and ``refresh_context_window`` resolve a window before
# there is a request to size, one of them inside the running event loop -- a
# synchronous ``httpx.Client`` there is a stall, not a slow answer. Both pass
# ``allow_fetch=False`` down to the fetch, which is asserted here to never build
# a client: whatever is cached, of any age, answers instead.


def _forbid_network_client(monkeypatch):
    """Restore the real fetch, then count real ``httpx.Client`` builds.

    Not a raise: ``_fetch_openrouter_models`` wraps the fetch in
    ``except Exception`` to degrade on a network failure, so a raise from here
    would be swallowed as "the network failed" and the test would pass for the
    wrong reason. A counter the caller asserts is 0 actually distinguishes
    "never touched the network" from "touched it and degraded".
    """
    counter = {"calls": 0}
    real_client = rates.httpx.Client

    def _counting_client(*args, **kwargs):
        counter["calls"] += 1
        return real_client(*args, **kwargs)

    monkeypatch.setattr(rates, "_fetch_openrouter_models", _REAL_FETCH)
    monkeypatch.setattr(rates.httpx, "Client", _counting_client)
    return counter


def test_allow_fetch_false_serves_an_expired_in_memory_cache(monkeypatch):
    """In-process cache of any age answers -- expired is still better than a stall."""
    counter = _forbid_network_client(monkeypatch)
    rates._OPENROUTER_CACHE.update(
        {
            "deepseek/deepseek-v4-pro": {
                "pricing": {"prompt": "0.0000005", "completion": "0.0000015"},
                "context_length": 163840,
            }
        }
    )
    monkeypatch.setattr(rates, "_OPENROUTER_CACHE_TIME", time.time() - rates._OPENROUTER_CACHE_TTL - 1000)

    assert rates._fetch_openrouter_models(allow_fetch=False) is rates._OPENROUTER_CACHE
    assert resolve_context_window("openrouter/deepseek/deepseek-v4-pro", allow_fetch=False) == 163840
    assert counter["calls"] == 0


def test_allow_fetch_false_falls_back_to_an_expired_disk_cache(monkeypatch, disk_cache):
    """Empty in-memory, a stale disk file answers and refills memory -- never network."""
    counter = _forbid_network_client(monkeypatch)
    stale_at = time.time() - (rates._OPENROUTER_CACHE_TTL + 100)
    disk_cache.write_text(json.dumps(_disk_payload(stale_at)), encoding="utf-8")

    assert resolve_context_window("openrouter/deepseek/deepseek-v4-pro", allow_fetch=False) == 163840
    assert rates._OPENROUTER_CACHE, "the disk hit should have refilled the in-process cache"
    assert counter["calls"] == 0


def test_allow_fetch_false_with_nothing_cached_is_none_and_never_hits_the_network(monkeypatch, disk_cache):
    """Cold everywhere: {} / None, never a synchronous request that could stall
    construction or the event loop for up to 10s."""
    counter = _forbid_network_client(monkeypatch)

    assert rates._fetch_openrouter_models(allow_fetch=False) == {}
    assert resolve_context_window("openrouter/deepseek/deepseek-v4-pro", allow_fetch=False) is None
    assert (
        rates.effective_context_window("openrouter/deepseek/deepseek-v4-pro", None, allow_fetch=False)
        == rates.DEFAULT_CONTEXT_WINDOW_TOKENS
    )
    assert counter["calls"] == 0


def test_allow_fetch_false_never_calls_fetch_with_a_keyword_it_may_not_accept(monkeypatch):
    """``resolve_context_window(..., allow_fetch=False)`` must not depend on
    ``_fetch_openrouter_models`` declaring ``allow_fetch``.

    ``conftest``'s autouse network guard binds ``rates._fetch_openrouter_models``
    to a zero-argument stub (``lambda: {}``) for every test by default. Calling
    that name with ``allow_fetch=False`` -- as the no-fetch branch of
    ``_lookup_openrouter_entry`` used to -- raises ``TypeError`` against that
    exact stub. This test reproduces the stub shape directly (not via the
    fixture, so it stays correct even if the fixture's own lambda changes) and
    asserts the no-fetch ladder still answers.
    """
    monkeypatch.setattr(rates, "_fetch_openrouter_models", lambda: {})

    assert resolve_context_window("openrouter/deepseek/deepseek-v4-pro", allow_fetch=False) is None
    assert (
        rates.effective_context_window("openrouter/deepseek/deepseek-v4-pro", None, allow_fetch=False)
        == rates.DEFAULT_CONTEXT_WINDOW_TOKENS
    )


# --- Output ceilings: a catalogue row that files a window as a ceiling ---


def _patch_table(monkeypatch, table: dict, *, info=_litellm_miss):
    import litellm

    monkeypatch.setattr(litellm, "model_cost", table)
    monkeypatch.setattr(litellm, "get_model_info", info)


def test_a_row_filing_its_window_as_the_ceiling_is_not_trusted(monkeypatch):
    """Measured on the pinned LiteLLM: 984 of 3040 rows carry
    ``max_output_tokens >= max_input_tokens``, and one of them is the id this
    repo documents as an example -- ``openrouter/anthropic/claude-sonnet-4.5``
    reports 1000000 for both where Anthropic's real ceiling is 64000.

    A request carrying that is refused outright, and the refusal is classified
    ``invalid_request``: not retryable, not fallback-worthy, not compressible.
    The turn dies. Every call, not an edge case.
    """
    _patch_table(monkeypatch, {"probe/big": {"max_input_tokens": 1000000, "max_output_tokens": 1000000}})

    assert rates.resolve_max_output_tokens("probe/big") == rates.DEFAULT_MAX_OUTPUT_TOKENS


def test_the_same_row_shape_is_rejected_at_the_second_lookup_too(monkeypatch):
    """The table is asked first and ``get_model_info`` second, and the bad rows
    answer both -- the documented id reports 1000000 from either. A guard on
    one tier alone leaves the other to hand back what it just rejected."""
    _patch_table(monkeypatch, {}, info=lambda m: {"max_input_tokens": 1000000, "max_output_tokens": 1000000})

    assert rates.resolve_max_output_tokens("probe/big") == rates.DEFAULT_MAX_OUTPUT_TOKENS


def test_a_small_suspicious_row_keeps_its_own_number(monkeypatch):
    """The rule rejects a row only when it is at least as large as what would
    replace it, which is what makes it safe rather than merely suspicious.

    Without that second condition, 252 rows (``4096/4096`` shapes among them)
    are raised past their real ceiling -- one refused request traded for
    another. Here the row is equally suspect and the fallback is larger, so
    the row stands.
    """
    _patch_table(monkeypatch, {"probe/small": {"max_input_tokens": 4096, "max_output_tokens": 4096}})

    assert rates.resolve_max_output_tokens("probe/small") == 4096


def test_a_row_whose_ceiling_sits_below_its_window_is_left_alone(monkeypatch):
    """The ordinary shape, and the majority of the table."""
    _patch_table(monkeypatch, {"probe/sane": {"max_input_tokens": 200000, "max_output_tokens": 64000}})

    assert rates.resolve_max_output_tokens("probe/sane") == 64000


def test_an_explicit_pin_is_still_the_caller_s_to_make(monkeypatch):
    """The escape hatch for a caller that really does want a long single answer,
    and the one the share bound points at. Bounded by the model, not the share.
    """
    _patch_table(monkeypatch, {"probe/roomy": {"max_input_tokens": 200_000, "max_output_tokens": 64_000}})

    assert send_max_tokens(None, "probe/roomy", pinned=64_000) == 64_000
    assert send_max_tokens(None, "probe/roomy", pinned=999_999) == 64_000


def test_the_share_no_longer_bounds_what_a_request_asks_for(monkeypatch):
    """The share bound moves back to the budget, which is the only side that
    needs it once requests stop volunteering a ceiling.

    It existed to keep two numbers addable: the prompt was allowed to grow into
    `window - reserved` while the request asked for the full ceiling, and the
    sum had to fit. A request that names no ceiling has nothing to add, so the
    reservation becomes a margin like LiteLLM's 0.75 and OpenClaw's 0.7 rather
    than a guarantee -- which is the posture every surveyed agent takes.
    """
    _patch_table(monkeypatch, {"probe/roomy": {"max_input_tokens": 200_000, "max_output_tokens": 64_000}})

    assert send_max_tokens(None, "probe/roomy") == 64_000, "the model's own ceiling, unbounded"


# --- Hyphen/dot version spellings (OpenRouter files what vendors hyphenate) ---


_DOTTED_MODELS = [
    {
        "id": "anthropic/claude-sonnet-4.5",
        "context_length": 200000,
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    },
    {
        "id": "meta-llama/llama-3.3-70b-instruct",
        "context_length": 131072,
        "pricing": {"prompt": "0.00000004", "completion": "0.00000012"},
    },
]


def test_a_hyphenated_version_finds_the_dotted_openrouter_row(monkeypatch):
    """Raven routes Anthropic's ``claude-sonnet-4-5``; OpenRouter files the same
    model as ``claude-sonnet-4.5``. Exact-key lookup missed, so the default model
    Raven itself recommends reported a cost of None on every turn."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DOTTED_MODELS))

    entry = rates._lookup_openrouter_entry("openrouter/anthropic/claude-sonnet-4-5")
    assert entry is not None
    assert entry["pricing"]["prompt"] == "0.000003"
    assert _rate_cost("openrouter/anthropic/claude-sonnet-4-5", 1000, 100) is not None


def test_only_one_boundary_is_dotted_at_a_time(monkeypatch):
    """``llama-3-3-70b`` is ``llama-3.3-70b``, never ``llama-3.3.70b`` -- so the
    variants are tried one digit boundary at a time rather than all at once."""
    _patch_openrouter(monkeypatch, lambda req: _models_response(_DOTTED_MODELS))

    entry = rates._lookup_openrouter_entry("openrouter/meta-llama/llama-3-3-70b-instruct")
    assert entry is not None
    assert entry["pricing"]["prompt"] == "0.00000004"
    assert rates._lookup_openrouter_entry("openrouter/meta-llama/llama-3-3-70b-nope") is None


def test_dotted_variants_are_a_fallback_not_a_rewrite():
    """No digit boundary, nothing to try; and the exact key is always preferred,
    so a wrong guess can only ever degrade to the None it replaced."""
    assert rates._dotted_version_variants("openai/gpt-4o-mini") == []
    assert rates._dotted_version_variants("anthropic/claude-sonnet-4-5") == ["anthropic/claude-sonnet-4.5"]
    # Only digit-to-digit boundaries count: the hyphen in "x-1" joins a letter
    # to a digit and is left alone.
    assert rates._dotted_version_variants("x-1-2-3") == ["x-1.2-3", "x-1-2.3", "x-1.2.3"]
