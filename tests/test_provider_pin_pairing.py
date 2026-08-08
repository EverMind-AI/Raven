"""A subsystem pin is a model id; the credential comes from the provider.

``skill_forge.llm_gate_model`` and ``context.curator_model`` name a model
and nothing else. The provider they are used with is whichever one the
agent is on -- at construction as much as after a live ``/model`` switch.
``LiteLLMProvider._resolve_model`` takes the vendor prefix from the model
string while the request carries the instance's own ``api_key``, so a pin
naming another vendor sends one vendor's key to another's endpoint: the
gate 401s into its top-N fallback and the curator 401s into its
deterministic fallback, both behind a single warning line.

``serves_model`` is the question those holders now ask before honouring a
pin. It cannot conjure the other vendor's credential -- honouring a
cross-vendor pin would need the pin to carry its own provider -- so the
answer is to fall back rather than to mis-pair.
"""

from __future__ import annotations

import pytest

from raven.config.raven import ContextConfig
from raven.context_engine.segments.curator import CuratorSegmentBuilder
from raven.memory_engine.skill_forge.gate import LLMGateFilter
from raven.providers.litellm_provider import LiteLLMProvider


def _provider(name: str, model: str) -> LiteLLMProvider:
    # No api_key: constructing with one writes the vendor's env var, which
    # would leak into whatever test runs next in the same process.
    return LiteLLMProvider(default_model=model, provider_name=name)


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
    """No spec for the vendor means we cannot prove a mismatch. Answering True
    keeps today's behaviour instead of dropping a pin on a guess -- the same
    caution ``config.set model`` applies when it hands routing back to auto.
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


def test_curator_falls_back_to_the_agent_model_off_its_vendor(tmp_path) -> None:
    """``curator_model`` defaults to a Gemini id for everyone, so this is the
    default path on any direct-vendor setup -- not an exotic configuration.
    """
    builder = _curator(tmp_path, _provider("anthropic", "claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder.curator_model == ContextConfig().curator_model
    assert builder._effective_curator_model() == "anthropic/claude-opus-4-5"


def test_curator_keeps_the_pin_behind_a_gateway(tmp_path) -> None:
    builder = _curator(tmp_path, _provider("openrouter", "anthropic/claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder._effective_curator_model() == ContextConfig().curator_model


def test_curator_re_decides_after_a_switch(tmp_path) -> None:
    builder = _curator(tmp_path, _provider("openrouter", "anthropic/claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder._effective_curator_model() == ContextConfig().curator_model

    builder.set_provider(_provider("anthropic", "claude-opus-4-5"), "anthropic/claude-opus-4-5")
    assert builder._effective_curator_model() == "anthropic/claude-opus-4-5"
    assert builder.curator_model == ContextConfig().curator_model, "the pin itself never moves"


@pytest.mark.parametrize("pinned", ["gemini-2.5-flash", "gemini/gemini-2.5-flash"])
def test_a_gemini_pin_is_recognised_with_or_without_its_prefix(pinned: str) -> None:
    p = _provider("anthropic", "claude-opus-4-5")
    assert not p.serves_model(pinned)
