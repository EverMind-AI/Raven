"""PerModelProvider: routes calls to per-model endpoints, falls back for unknown models."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.config.schema import ModelEndpoint
from raven.providers.base import ErrorClassification, GenerationSettings, LLMResponse
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.per_model_provider import PerModelProvider
from raven.providers.prompt_cache import CACHE_CONTROL


def _fallback():
    fb = MagicMock()
    fb.get_default_model.return_value = "fallback-model"
    fb.generation = GenerationSettings()
    return fb


def _provider():
    eps = [
        ModelEndpoint(model="small", api_base="http://a/v1", api_key="KA"),
        ModelEndpoint(model="large", api_base="http://b/v1", api_key="KB"),
    ]
    return PerModelProvider(eps, fallback=_fallback())


def test_pick_routes_by_model():
    p = _provider()
    assert p._pick("small") is p._by_model["small"]
    assert p._pick("large") is p._by_model["large"]


def test_pick_unknown_and_none_use_fallback():
    p = _provider()
    assert p._pick("nope") is p._fallback
    assert p._pick(None) is p._fallback


def test_get_default_model_is_first_configured():
    assert _provider().get_default_model() == "small"


def test_default_falls_back_when_no_models():
    p = PerModelProvider([], fallback=_fallback())
    assert p.get_default_model() == "fallback-model"


def test_generation_propagates_to_sub_providers():
    # Sub-providers are built without generation settings; they must inherit the
    # fallback's (configured from AgentDefaults) so routed calls honor
    # timeout / temperature / max_tokens instead of the dataclass defaults.
    fb = _fallback()
    fb.generation = GenerationSettings(timeout=42.0, temperature=0.2, max_tokens=111)
    eps = [ModelEndpoint(model="small", api_base="http://a/v1", api_key="KA")]
    p = PerModelProvider(eps, fallback=fb)
    assert p.generation is fb.generation
    assert p._by_model["small"].generation is fb.generation
    assert p._by_model["small"].generation.timeout == 42.0


@pytest.mark.asyncio
async def test_chat_with_retry_dispatches_by_model():
    p = _provider()
    large_resp = LLMResponse(content="LARGE_RESP", finish_reason="stop")
    small_resp = LLMResponse(content="SMALL_RESP", finish_reason="stop")
    p._by_model["large"].chat_with_retry = AsyncMock(return_value=large_resp)
    p._by_model["small"].chat_with_retry = AsyncMock(return_value=small_resp)

    out = await p.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="large")

    assert out.content == "LARGE_RESP"
    p._by_model["large"].chat_with_retry.assert_awaited_once()
    assert p._by_model["large"].chat_with_retry.call_args.kwargs["model"] == "large"
    # fallback_models=[] so the sub-provider's own chain never re-tries a
    # *different* hop's endpoint on top of its own retry ladder.
    assert p._by_model["large"].chat_with_retry.call_args.kwargs["fallback_models"] == []
    p._by_model["small"].chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_with_retry_unknown_model_uses_fallback():
    fb = _fallback()
    fb.chat_with_retry = AsyncMock(return_value=LLMResponse(content="FB_RESP", finish_reason="stop"))
    p = PerModelProvider([ModelEndpoint(model="small", api_base="http://a/v1")], fallback=fb)

    out = await p.chat_with_retry(messages=[], model="other")

    assert out.content == "FB_RESP"
    fb.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_with_retry_dispatches_each_hop_to_its_own_endpoint(monkeypatch):
    # The whole point of per-model routing: a fallback hop has its own
    # endpoint (see _endpoint_provider), not the primary model's. Sending the
    # entire chain to one sub-provider (the old behavior) would mean every
    # fallback silently reused the primary's endpoint instead of its own.
    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        if kwargs["model"] == "openai/small":
            raise RuntimeError("503 service unavailable")
        message = MagicMock(content="ok-from-large", tool_calls=None)
        return MagicMock(choices=[MagicMock(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    p = _provider()
    # Zero the primary hop's own retry ladder so its exhaustion (a retryable
    # classification) stays fast, while still exercising that it retries on
    # its own endpoint before the chain moves to the next hop's.
    p._by_model["small"]._CHAT_RETRY_DELAYS = (0, 0, 0)

    resp = await p.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model="small",
        fallback_models=["large"],
    )

    assert resp.content == "ok-from-large"
    # The primary hop exhausts its own retry ladder (3 sleeping + 1 final)
    # entirely against its own endpoint before the chain moves on.
    calls = [(c["model"], c["api_base"], c["api_key"]) for c in seen]
    assert calls == [("openai/small", "http://a/v1", "KA")] * 4 + [("openai/large", "http://b/v1", "KB")]


@pytest.mark.asyncio
async def test_chat_with_retry_strips_cache_control_on_a_fallback_hop_that_cannot_read_it():
    """Mirrors ``LLMProvider.chat_with_retry``'s own guard: a breakpoint placed
    for the primary model's vendor must not reach a fallback hop whose vendor
    cannot read it -- here, a hop that resolves to ``fallback`` (e.g. Azure,
    which does not strip cache_control itself). Without this guard the marker
    is billed as an unrecognized block or refused outright by the second hop's
    wire."""
    p = _provider()
    error_resp = LLMResponse(
        content="boom",
        finish_reason="error",
        error_classification=ErrorClassification(category="server_error", should_fallback=True),
    )
    p._by_model["small"].chat_with_retry = AsyncMock(return_value=error_resp)

    seen: list[dict] = []

    async def fake_fallback_chat_with_retry(messages, tools=None, **kwargs):
        seen.append({"messages": messages, "tools": tools})
        return LLMResponse(content="FB_RESP", finish_reason="stop")

    p._fallback.chat_with_retry = AsyncMock(side_effect=fake_fallback_chat_with_retry)

    messages = [{"role": "system", "content": "sys", "cache_control": CACHE_CONTROL}]
    out = await p.chat_with_retry(messages=messages, model="small", fallback_models=["unrecognized-fallback-model"])

    assert out.content == "FB_RESP"
    assert seen[0]["messages"] == [{"role": "system", "content": "sys"}]


@pytest.mark.asyncio
async def test_chat_with_retry_skips_a_fallback_hop_its_sub_provider_cannot_serve():
    """Mirrors ``LLMProvider.chat_with_retry``'s ``can_serve`` guard: a
    per-model sub-provider picked for a later hop can be just as unable to
    serve it as the single-instance case that guard exists for -- e.g. the
    hop resolves to a vendor this sub-provider's credentials do not reach."""
    p = _provider()
    error_resp = LLMResponse(
        content="boom",
        finish_reason="error",
        error_classification=ErrorClassification(category="server_error", should_fallback=True),
    )
    p._by_model["small"].chat_with_retry = AsyncMock(return_value=error_resp)
    p._by_model["large"].can_serve = MagicMock(return_value=False)
    p._by_model["large"].chat_with_retry = AsyncMock()

    out = await p.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="small", fallback_models=["large"]
    )

    p._by_model["large"].chat_with_retry.assert_not_awaited()
    assert out is error_resp


def test_sub_providers_inherit_configured_model_overrides():
    # Routed models are served by their own sub-providers, built here rather
    # than by make_provider -- so the user's overrides have to be pushed down
    # the same way generation settings are.
    fb = _fallback()
    fb.model_overrides = {"small": {"top_p": 0.3}}
    p = PerModelProvider([ModelEndpoint(model="small", api_base="http://a/v1")], fallback=fb)

    assert p._by_model["small"].model_overrides == {"small": {"top_p": 0.3}}


def test_sub_providers_inherit_overrides_from_a_rotor_fallback():
    """``fallback`` in production is whatever ``make_provider`` built, and a
    multi-endpoint section builds an ``EndpointRotorProvider`` there, not a
    ``LiteLLMProvider`` -- the only class every other test in this module
    exercises. ``getattr(fallback, "model_overrides", None)`` silently read
    nothing back off a rotor before it gained the delegating property, and a
    routed model's overrides went missing on exactly the configs with several
    endpoints to rotate.
    """
    from raven.providers.endpoint_rotor import EndpointRotorProvider
    from raven.providers.endpoints import ResolvedEndpoint

    def make_inner(ep):
        return LiteLLMProvider(
            api_key=ep.api_key,
            api_base=ep.api_base,
            default_model="fb",
            provider_name="openrouter",
            model_overrides={"small": {"top_p": 0.3}},
        )

    rotor = EndpointRotorProvider(
        [ResolvedEndpoint(label="a", api_key="k1", api_base="http://a/v1", extra_headers=None)],
        make_inner,
        default_model="fb",
    )

    p = PerModelProvider([ModelEndpoint(model="small", api_base="http://a/v1")], fallback=rotor)

    assert p._by_model["small"].model_overrides == {"small": {"top_p": 0.3}}


def test_sub_providers_go_through_litellm():
    p = _provider()
    assert all(isinstance(sub, LiteLLMProvider) for sub in p._by_model.values())


@pytest.mark.asyncio
async def test_routed_models_stream_natively(monkeypatch):
    # The point of routing through LiteLLM: the old direct client had no
    # chat_stream, so routed models fell back to one delta carrying the whole
    # reply. Real chunks must now reach the caller one at a time.
    class _Stream:
        def __aiter__(self):
            return self

        def __init__(self):
            self._chunks = iter(["Hel", "lo", "!"])

        async def __anext__(self):
            try:
                text = next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration
            delta = MagicMock(content=text, tool_calls=None, reasoning_content=None)
            return MagicMock(choices=[MagicMock(delta=delta, finish_reason=None)], usage=None)

    async def fake_acompletion(**kwargs):
        return _Stream()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    p = _provider()
    chunks = [d.content async for d in p.chat_stream(messages=[{"role": "user", "content": "hi"}], model="small")]

    assert chunks == ["Hel", "lo", "!"]


def test_endpoint_without_api_base_is_rejected():
    # ModelEndpoint.api_base defaults to "", and LiteLLM would then fall back to
    # api.openai.com and ship this endpoint's key there. Fail loudly instead.
    with pytest.raises(ValueError, match="api_base"):
        PerModelProvider([ModelEndpoint(model="m1")], fallback=_fallback())


@pytest.mark.asyncio
async def test_model_name_keeps_its_own_provider_prefix(monkeypatch):
    # A routed model may itself be prefixed (e.g. "anthropic/claude-x"). The
    # generic gateway prefix is added on top and LiteLLM strips it back off, so
    # the endpoint still receives the original name.
    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        message = MagicMock(content="ok", tool_calls=None)
        return MagicMock(choices=[MagicMock(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    endpoint = ModelEndpoint(model="anthropic/claude-x", api_base="http://a/v1", api_key="KA")
    p = PerModelProvider([endpoint], fallback=_fallback())
    await p.chat(messages=[{"role": "user", "content": "hi"}], model="anthropic/claude-x")

    assert seen[0]["model"] == "openai/anthropic/claude-x"


def test_building_endpoints_leaves_litellm_api_base_unset(monkeypatch):
    # Building a provider must not pin the process-global litellm.api_base:
    # with several endpoints in one process the last one built would otherwise
    # become the default base for every caller that omits api_base.
    from raven.providers.litellm_provider import litellm

    monkeypatch.setattr(litellm, "api_base", None)
    _provider()
    assert litellm.api_base is None


@pytest.mark.asyncio
async def test_each_endpoint_sends_its_own_api_base(monkeypatch):
    # Each sub-provider carries its own api_base / api_key per call, so routed
    # calls reach the endpoint that serves that model.
    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        message = MagicMock(content="ok", tool_calls=None)
        choice = MagicMock(message=message, finish_reason="stop")
        return MagicMock(choices=[choice], usage=None)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    p = _provider()
    for model in ("small", "large"):
        await p.chat(messages=[{"role": "user", "content": "hi"}], model=model)

    # Assert the (model, base, key) triple per call: a {base: key} dict would
    # still pass with the model-to-endpoint mapping swapped.
    assert [(c["model"], c["api_base"], c["api_key"]) for c in seen] == [
        ("openai/small", "http://a/v1", "KA"),
        ("openai/large", "http://b/v1", "KB"),
    ]


def test_building_knn_endpoints_leaves_the_process_environment_alone(monkeypatch) -> None:
    """Several endpoints in one process must not fight over a shared env var.

    Each endpoint's provider is built with ``provider_name="custom"``, and
    ``custom`` is a gateway -- so it takes ``_setup_env``'s branch that *overwrites*
    an existing variable rather than the one that defers to it. The only thing
    stopping N endpoints from each writing their own key into one variable, last
    one winning, is that the custom spec declares no ``env_key``.

    That is a single empty field holding up a correctness property, and nothing
    else asserts it: filling it in with "OPENAI_API_KEY" looks entirely
    reasonable, and every other test would stay green.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "PRE-EXISTING")

    fallback = LiteLLMProvider(api_key="FALLBACK", default_model="fb", provider_name="openrouter")
    endpoints = [
        ModelEndpoint(model="small", api_key="KEY-SMALL", api_base="http://small/v1"),
        ModelEndpoint(model="large", api_key="KEY-LARGE", api_base="http://large/v1"),
    ]

    provider = PerModelProvider(endpoints, fallback)

    assert os.environ["OPENAI_API_KEY"] == "PRE-EXISTING", "an endpoint key leaked into the process environment"
    # Each endpoint still carries its own credentials, per call rather than per
    # process -- which is what made one process able to hold several at all.
    assert provider._by_model["small"].api_key == "KEY-SMALL"
    assert provider._by_model["large"].api_key == "KEY-LARGE"


def test_endpoint_providers_are_built_with_unparsed_reasoning_disabled():
    """Should-fix 9 repro: ``_endpoint_provider`` builds every knn-routed
    endpoint with ``provider_name="custom"`` for its api_base/api_key shape
    alone -- not as a claim that the backend behind it is a self-hosted
    inference server without a reasoning parser. Without the explicit
    override, a front-loaded big vendor routed this way would have its
    ordinary content clipped at a stray ``</think>``.
    """
    p = _provider()
    assert p._by_model["small"].emits_unparsed_reasoning() is False
    assert p._by_model["large"].emits_unparsed_reasoning() is False


@pytest.mark.asyncio
async def test_routed_endpoint_does_not_cut_ordinary_content_at_a_stray_think_tag(monkeypatch):
    """The evaluator's repro: a knn-routed endpoint's ordinary reply happened
    to contain a bare ``</think>``, and the ``provider_name="custom"``-derived
    guess (before ``unparsed_reasoning=False`` was wired in) read that as an
    unparsed reasoning leak and cut the reply in half.
    """

    async def fake_acompletion(**kwargs):
        message = MagicMock(
            content="the widget's </think> hinge broke",
            tool_calls=None,
            reasoning_content=None,
            thinking_blocks=None,
        )
        return MagicMock(choices=[MagicMock(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)

    p = _provider()
    resp = await p.chat(messages=[{"role": "user", "content": "hi"}], model="small")

    assert resp.content == "the widget's </think> hinge broke"
    assert resp.reasoning_content is None


def test_the_custom_spec_declares_no_env_var_to_write() -> None:
    """States the field the test above depends on, so a change to it fails here
    with the reason rather than somewhere unrelated."""
    from raven.providers.registry import find_by_name

    spec = find_by_name("custom")
    assert spec is not None
    assert spec.is_gateway is True, "if custom stopped being a gateway, re-derive which _setup_env branch applies"
    assert spec.env_key == "", "custom must name no env var: knn builds one provider per endpoint under this spec"
    assert spec.env_extras == ()
