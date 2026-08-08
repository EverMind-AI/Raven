"""A subsystem pin is a model id; the credential comes from the provider.

``skill_forge.llm_gate_model`` and ``context.curator_model`` name a model
and nothing else. The provider they are used with is whichever one the
agent is on -- at construction as much as after a live ``/model`` switch.
``LiteLLMProvider._resolve_model`` takes the vendor prefix from the model
string while the request carries the instance's own ``api_key``, so a pin
naming another vendor sends one vendor's key to another's endpoint. The
gate logs one warning and returns its top-N fallback; the curator's slow
path turns the failure into ``finish_reason="error"`` content and drops
to the deterministic plan without logging anything at all.

``serves_model`` is the question those holders now ask before honouring a
pin. It cannot conjure the other vendor's credential -- honouring a
cross-vendor pin would need the pin to carry its own provider -- so the
answer is to fall back rather than to mis-pair.
"""

from __future__ import annotations

import os

import pytest

from raven.config.raven import ContextConfig
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.memory_engine.skill_forge.gate import LLMGateFilter
from raven.providers.litellm_provider import LiteLLMProvider


@pytest.fixture(autouse=True)
def _restore_env():
    """Constructing a keyed provider writes the vendor's env var
    (``_setup_env``); keep that inside this module.
    """
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def _provider(name: str, model: str) -> LiteLLMProvider:
    # A key is part of the premise: with none, LiteLLM resolves one per vendor
    # from the environment and there is no single credential to mis-pair.
    return LiteLLMProvider(api_key="test-key", default_model=model, provider_name=name)


# ---------------------------------------------------------------------------
# serves_model
# ---------------------------------------------------------------------------


def test_a_provider_serves_its_own_vendor() -> None:
    p = _provider("anthropic", "claude-opus-4-5")
    assert p.serves_model("anthropic/claude-opus-4-5")
    assert p.serves_model("claude-sonnet-4-5")


def test_a_provider_does_not_serve_another_vendor() -> None:
    p = _provider("anthropic", "claude-opus-4-5")
    assert not p.serves_model("openai/gpt-5-mini")
    assert not p.serves_model("gemini-2.5-flash")


def test_a_gateway_serves_whatever_it_is_handed() -> None:
    """The vendor in the id is upstream of the gateway, which prefixes the id
    and bills its own key -- so a cross-vendor pin is normal there, and this
    change must not touch it.
    """
    p = _provider("openrouter", "anthropic/claude-opus-4-5")
    assert p.serves_model("openai/gpt-5-mini")
    assert p.serves_model("gemini-2.5-flash")


def test_an_unclassifiable_id_is_left_alone() -> None:
    """No spec for the vendor means we cannot prove a mismatch, and dropping a
    pin the user set on a guess is the worse error.

    Note this is the opposite call from ``config.set model``, which sends an
    unclassifiable id to ``provider="auto"`` rather than keep the current
    provider's key on it (tui_rpc/methods/config.py). It can: it is choosing
    the agent's own provider and has the whole config to choose from. Here
    there is only one provider and the choice is keep-or-drop.
    """
    p = _provider("anthropic", "claude-opus-4-5")
    assert p.serves_model("mistral/mistral-large")
    assert p.serves_model("")


def test_the_base_default_keeps_todays_behaviour() -> None:
    """Providers that choose model and credential together (OAuth, Azure) do
    not override, so nothing changes for them.
    """

    class _Fixed:
        def get_default_model(self) -> str:
            return "x"

    from raven.providers.base import LLMProvider

    assert LLMProvider.serves_model(_Fixed(), "anything/at-all") is True


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_gate_keeps_a_pin_its_provider_serves() -> None:
    gate = LLMGateFilter(_provider("openai", "gpt-5-mini"), model="openai/gpt-5-mini")
    assert gate._effective_model() == "openai/gpt-5-mini"


def test_gate_drops_a_pin_its_provider_cannot_serve() -> None:
    gate = LLMGateFilter(_provider("anthropic", "claude-opus-4-5"), model="openai/gpt-5-mini")
    assert gate._effective_model() is None
    # The pin itself is untouched: switch back to a provider that serves it
    # and it is honoured again.
    assert gate._model == "openai/gpt-5-mini"
    gate.set_provider(_provider("openai", "gpt-5-mini"), "openai/gpt-5-mini")
    assert gate._effective_model() == "openai/gpt-5-mini"


def test_gate_warns_once_per_provider() -> None:
    gate = LLMGateFilter(_provider("anthropic", "claude-opus-4-5"), model="openai/gpt-5-mini")
    gate._effective_model()
    assert gate._pin_warned
    gate._effective_model()
    gate.set_provider(_provider("anthropic", "claude-opus-4-5"), "claude-opus-4-5")
    assert not gate._pin_warned, "a new provider is a new question, so it may warn again"


def test_an_unpinned_gate_is_unaffected() -> None:
    gate = LLMGateFilter(_provider("anthropic", "claude-opus-4-5"))
    assert gate._effective_model() is None
    assert not gate._pin_warned


# ---------------------------------------------------------------------------
# The curator
# ---------------------------------------------------------------------------


def _curator(workspace, provider: LiteLLMProvider, model: str) -> CuratorSegmentBuilder:
    return CuratorSegmentBuilder(
        workspace=workspace,
        config=ContextConfig(),
        provider=provider,
        model=model,
        context_window_tokens=8192,
        get_tool_definitions=lambda: [],
    )


def test_curator_skips_the_slow_path_off_its_vendor(tmp_path) -> None:
    """``curator_model`` defaults to a Gemini id for everyone, so this is the
    default path on any direct-vendor setup -- not an exotic configuration.
    False means the deterministic plan, not the agent's model: the slow path
    is up to ``max_steps`` tool-calling requests, and redirecting it would
    turn a knob documented "small and fast" into a dozen Opus-class calls.
    """
    builder = _curator(tmp_path, _provider("anthropic", "claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder.curator_model == ContextConfig().curator_model
    assert builder._curator_model_is_servable() is False


def test_curator_keeps_the_pin_behind_a_gateway(tmp_path) -> None:
    builder = _curator(tmp_path, _provider("openrouter", "anthropic/claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder._curator_model_is_servable() is True


def test_curator_re_decides_after_a_switch(tmp_path) -> None:
    builder = _curator(tmp_path, _provider("openrouter", "anthropic/claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder._curator_model_is_servable() is True

    builder.set_provider(_provider("anthropic", "claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder._curator_model_is_servable() is False
    assert builder.curator_model == ContextConfig().curator_model, "the pin itself never moves"


@pytest.mark.parametrize("pinned", ["gemini-2.5-flash", "gemini/gemini-2.5-flash"])
def test_a_gemini_pin_is_recognised_with_or_without_its_prefix(pinned: str) -> None:
    p = _provider("anthropic", "claude-opus-4-5")
    assert not p.serves_model(pinned)


def test_a_keyless_provider_never_drops_a_pin() -> None:
    """github_copilot runs with no api_key: LiteLLM then resolves one per
    vendor from the environment, so a cross-vendor pin was working and must
    not be dropped.
    """
    p = LiteLLMProvider(default_model="claude-opus-4-5", provider_name="anthropic")
    assert not p.api_key
    assert p.serves_model("gemini-2.5-flash")


def test_a_custom_endpoint_is_unclassifiable_not_mismatched() -> None:
    """An OpenAI-compatible host with no provider_name: guessing its vendor
    from the default model's keywords would classify the host as whoever that
    model belongs to.
    """
    p = LiteLLMProvider(api_key="test-key", api_base="https://example.invalid/v1", default_model="qwen3-30b")
    assert p.serves_model("gemini-2.5-flash")


def test_lazy_provider_answers_from_the_provider_it_builds() -> None:
    """The TUI's agent provider is a LazyProvider, so answering from the proxy
    would answer for no credential at all and leave the whole check inert on
    the primary surface.
    """
    from raven.providers.base import GenerationSettings
    from raven.providers.lazy import LazyProvider

    real = _provider("anthropic", "claude-opus-4-5")
    lazy = LazyProvider(lambda: real, "claude-opus-4-5", GenerationSettings())

    assert lazy.serves_model("gemini-2.5-flash") is False
    assert lazy.serves_model("claude-sonnet-4-5") is True


def test_per_model_provider_asks_whichever_endpoint_the_id_routes_to() -> None:
    from raven.providers.per_model_provider import PerModelProvider

    fallback = _provider("anthropic", "claude-opus-4-5")
    routed = PerModelProvider([], fallback)

    # No routed endpoints, so everything rides the fallback and gets its answer.
    assert routed.serves_model("openai/gpt-5-mini") is False
    assert routed.serves_model("claude-sonnet-4-5") is True


def test_the_gate_sends_the_effective_model_not_the_raw_pin() -> None:
    """Guards the call site, not just the helper: passing self._model here
    would leave every helper test green.
    """
    import asyncio

    from raven.memory_engine.skill_forge.types import RouterHit

    sent: dict[str, object] = {}

    class _Recording(LiteLLMProvider):
        async def chat_with_retry(self, **kwargs):
            sent["model"] = kwargs.get("model")
            raise RuntimeError("stop here; the model is what matters")

    provider = _Recording(api_key="test-key", default_model="claude-opus-4-5", provider_name="anthropic")
    gate = LLMGateFilter(provider, model="openai/gpt-5-mini")
    hit = RouterHit(qualified_id="local/s1", name="s1", content="body", score=0.9)

    asyncio.run(gate.filter("task", [hit]))
    assert sent["model"] is None, "the unservable pin must not reach the provider"


@pytest.mark.asyncio
async def test_the_curator_really_skips_the_slow_path(tmp_path) -> None:
    """Guards the call site: asserting only on ``_curator_model_is_servable``
    would stay green with the guard removed from ``build``.
    """
    from raven.context_engine.base import AssembledPrefix, AssemblyContext
    from raven.memory_engine import TokenBudget

    builder = _curator(tmp_path, _provider("anthropic", "claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder._curator_model_is_servable() is False

    entered = False

    async def _slow_path(state, turn_id):
        nonlocal entered
        entered = True
        return None

    builder._slow_path = _slow_path

    # A history far past fast_path_threshold, so the fast path cannot be what
    # keeps the slow path out.
    messages = [{"role": "user", "content": "x" * 4000} for _ in range(40)]
    ctx = AssemblyContext(
        session_key="s",
        current_message="hi",
        media=None,
        channel=None,
        chat_id=None,
        session_messages=messages,
        budget=TokenBudget(100_000, 4_000, 2_000, 1_000, 4_000),
        prefix=AssembledPrefix(system_prefix="sys", user_message={"role": "user", "content": "hi"}, tool_defs=[]),
    )

    seg = await builder.build(ctx)

    assert entered is False, "an unservable pin must not reach the slow path"
    assert seg is not None
    assert seg.meta["path"] == "fallback"
