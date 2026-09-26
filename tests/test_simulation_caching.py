"""Cache breakpoints reach the gateway models measured to read them, and no other model."""

from experimental.curator.generation import run
from experimental.simulation import caching
from raven.providers import prompt_cache


def test_gpt_6_through_openrouter_keeps_its_breakpoints_and_other_models_are_unchanged(monkeypatch):
    from litellm.llms.openrouter.chat.transformation import OpenrouterConfig

    monkeypatch.setattr(prompt_cache, "accepts_cache_control", prompt_cache.accepts_cache_control)
    monkeypatch.setattr(run, "accepts_cache_control", run.accepts_cache_control)
    monkeypatch.setattr(
        OpenrouterConfig, "_supports_cache_control_in_content", OpenrouterConfig._supports_cache_control_in_content
    )
    caching.install()
    caching.install()
    accepts = prompt_cache.accepts_cache_control
    assert accepts("openrouter/openai/gpt-6-sol") and accepts("openai/gpt-6-sol", addressed_to="openrouter")
    assert run.accepts_cache_control is accepts
    assert accepts("openrouter/anthropic/claude-opus-5.5")
    assert not accepts("openai/gpt-6-sol") and not accepts("deepseek/deepseek-flash")
    assert not accepts("openrouter/openai/gpt-5.5")
    marked, _ = run.cached(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "m"}], None, "openrouter/openai/gpt-6-sol"
    )
    assert "cache_control" in str(marked[1])
    assert OpenrouterConfig()._supports_cache_control_in_content("openai/gpt-6-sol")
    assert not OpenrouterConfig()._supports_cache_control_in_content("openai/gpt-5.5")
