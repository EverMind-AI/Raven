"""``providers.<name>.routing`` — pinning the OpenRouter upstream.

The capability existed only in a comment: ``LiteLLMProvider`` took ``extra_body``
and its source named this exact use, but ``make_provider`` reached it solely
through a Qwen-specific clause, so no config could ask for it. See
``ProviderConfig.routing`` for the measurement that motivated it.
"""

from __future__ import annotations

from raven.cli._helpers import make_provider
from raven.config.schema import Config

PIN = {"order": ["deepinfra/fp8"], "allow_fallbacks": False}


def _config(model: str, *, routing: dict | None = None) -> Config:
    cfg = Config()
    cfg.agents.defaults.model = model
    cfg.providers.openrouter.api_key = "test-key"
    if routing is not None:
        cfg.providers.openrouter.routing = routing
    return cfg


def test_routing_absent_leaves_the_request_body_untouched():
    """The default must be byte-identical to the pre-field behaviour."""
    provider = make_provider(_config("deepseek/deepseek-v4-flash"))
    assert provider.extra_body == {}


def test_routing_is_forwarded_as_the_provider_object():
    provider = make_provider(_config("deepseek/deepseek-v4-flash", routing=PIN))
    assert provider.extra_body["provider"] == PIN


def test_routing_and_the_qwen_clause_both_survive():
    """The reason this merges instead of assigning.

    Both are independent conditions on one request body; an assignment would let
    whichever branch ran last silently drop the other.
    """
    provider = make_provider(_config("qwen/qwen3-27b", routing=PIN))
    assert provider.extra_body["provider"] == PIN
    assert provider.extra_body["reasoning"] == {"enabled": False}


def test_qwen_clause_alone_is_unchanged_when_routing_is_unset():
    provider = make_provider(_config("qwen/qwen3-27b"))
    assert provider.extra_body == {"reasoning": {"enabled": False}}


def test_routing_on_a_non_openrouter_provider_is_not_silently_applied():
    """Ignored is acceptable; ignored without saying so is not."""
    cfg = Config()
    cfg.agents.defaults.model = "student"
    cfg.providers.custom.api_key = "test-key"
    cfg.providers.custom.api_base = "http://127.0.0.1:30000/v1"
    cfg.providers.custom.routing = PIN
    provider = make_provider(cfg)
    assert "provider" not in provider.extra_body
