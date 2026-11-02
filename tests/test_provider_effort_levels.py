from __future__ import annotations

import pytest

from raven.providers import codex_catalog, effort_levels


def _row(**flags):
    return {"supports_reasoning": True, "litellm_provider": "openai", **flags}


@pytest.fixture
def table(monkeypatch):
    def use(rows):
        monkeypatch.setattr(effort_levels, "_table", lambda: rows)
        effort_levels.reset()

    yield use
    effort_levels.reset()


def test_a_bare_model_key_is_found_through_its_provider(table):
    table({"gpt-5": _row()})
    assert effort_levels.levels_for("openai/gpt-5") == ("minimal", "low", "medium", "high")


def test_a_model_absent_from_the_table_has_no_levels(table):
    table({"gpt-5": _row()})
    assert effort_levels.levels_for("anthropic/claude-opus-4-5") == ()


def test_the_table_being_unreadable_is_not_a_model_answer(table):
    table(None)
    assert effort_levels.levels_for("anthropic/claude-opus-4-5") is None


def test_a_declared_list_wins_without_reading_the_table(table):
    table(None)
    assert effort_levels.levels_for("anything", ("high",)) == ("high",)


def test_minimal_and_low_are_allowed_unless_declared_false(table):
    table({"gpt-5": _row(supports_minimal_reasoning_effort=True)})
    assert effort_levels.levels_for("openai/gpt-5") == ("minimal", "low", "medium", "high")


def test_xhigh_and_max_are_opt_in(table):
    table(
        {
            "gpt-5.5-pro": _row(
                supports_minimal_reasoning_effort=False,
                supports_low_reasoning_effort=False,
                supports_xhigh_reasoning_effort=True,
            )
        }
    )
    assert effort_levels.levels_for("openai/gpt-5.5-pro") == ("medium", "high", "xhigh")


def test_a_route_that_declares_no_reasoning_has_no_levels(table):
    table({"openrouter/anthropic/claude-opus-4-5": {"supports_reasoning": False}})
    assert effort_levels.levels_for("openrouter/anthropic/claude-opus-4-5") == ()


def test_the_pinned_table_declares_the_models_the_spec_names():
    """The real 1.85.0 table, not a fixture: these are the values the design
    measured. A LiteLLM upgrade that moves them must fail here and be re-read."""
    effort_levels.reset()
    assert effort_levels.levels_for("openai/gpt-5") == ("minimal", "low", "medium", "high")
    assert effort_levels.levels_for("openai/gpt-5.5-pro") == ("medium", "high", "xhigh")
    assert effort_levels.levels_for("anthropic/claude-opus-4-5") == ("minimal", "low", "medium", "high")
    assert effort_levels.levels_for("anthropic/claude-opus-4-7") == (
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert effort_levels.levels_for("zai/glm-5") == ("minimal", "low", "medium", "high")
    assert effort_levels.levels_for("zhipu/glm-5.3-flash") == ()


def test_a_route_row_inherits_the_vendors_missing_flags(table):
    table(
        {
            "claude-opus-4-6": _row(supports_max_reasoning_effort=True, litellm_provider="anthropic"),
            "openrouter/anthropic/claude-opus-4.6": {
                "supports_reasoning": True,
                "supports_max_reasoning_effort": None,
            },
        }
    )
    assert "max" in effort_levels.levels_for("openrouter/anthropic/claude-opus-4.6")


def test_a_route_that_says_false_is_vetoed_even_when_the_vendor_says_true(table):
    table(
        {
            "claude-opus-4-5": _row(litellm_provider="anthropic"),
            "perplexity/anthropic/claude-opus-4-5": {"supports_reasoning": False},
        }
    )
    assert effort_levels.levels_for("perplexity/anthropic/claude-opus-4-5") == ()
    assert "high" in effort_levels.levels_for("anthropic/claude-opus-4-5")


def test_a_bare_row_from_an_unrelated_vendor_is_not_borrowed(table):
    table({"some-model": {**_row(), "litellm_provider": "somevendor"}})
    assert effort_levels.levels_for("bedrock/some-model") == ()


def test_an_unknown_provider_spelling_still_finds_its_row(table):
    table({"github_copilot/claude-opus-4.5": _row()})
    assert effort_levels.levels_for("github-copilot/claude-opus-4.5") == (
        "minimal",
        "low",
        "medium",
        "high",
    )


def test_a_model_with_no_registry_row_uses_its_route_vendor(table):
    table({"zai/glm-5": {**_row(), "litellm_provider": "zai"}})
    assert effort_levels.levels_for("bigmodel/glm-5") == ("minimal", "low", "medium", "high")


def test_an_empty_table_is_unreadable_not_a_model_answer(table):
    table({})
    assert effort_levels.levels_for("openai/gpt-5") is None


def test_a_route_silent_on_reasoning_keeps_its_own_level_flags(table):
    table(
        {
            "claude-opus-4-6": _row(supports_xhigh_reasoning_effort=True, litellm_provider="anthropic"),
            "openrouter/anthropic/claude-opus-4.6": {
                "supports_reasoning": None,
                "supports_xhigh_reasoning_effort": False,
            },
        }
    )
    assert "xhigh" not in effort_levels.levels_for("openrouter/anthropic/claude-opus-4.6")


def test_an_openrouter_alias_is_consulted(table):
    table({"openrouter/vendor/some-model": _row()})
    assert effort_levels.levels_for("vendor/some-model") == ("minimal", "low", "medium", "high")


def test_an_openai_codex_model_reads_the_account_catalogue(monkeypatch):
    calls = []

    def efforts(slug, *, refresh=False):
        calls.append((slug, refresh))
        return ("low", "medium", "high", "xhigh", "max")

    monkeypatch.setattr(codex_catalog, "account_efforts", efforts)
    assert effort_levels.levels_for("openai-codex/gpt-5.6-sol") == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert calls == [("gpt-5.6-sol", False)]


def test_the_account_catalogue_is_refreshed_only_when_asked(monkeypatch):
    calls = []

    def efforts(slug, *, refresh=False):
        calls.append(refresh)
        return ("high",)

    monkeypatch.setattr(codex_catalog, "account_efforts", efforts)
    assert effort_levels.levels_for("openai-codex/gpt-5.6-sol", refresh=True) == ("high",)
    assert calls == [True]


def test_a_cold_account_catalogue_is_unknown(monkeypatch):
    monkeypatch.setattr(codex_catalog, "account_efforts", lambda slug, *, refresh=False: None)
    assert effort_levels.levels_for("openai-codex/gpt-5.6-sol") is None


def test_an_unlisted_account_model_has_no_levels(monkeypatch):
    monkeypatch.setattr(codex_catalog, "account_efforts", lambda slug, *, refresh=False: ())
    assert effort_levels.levels_for("openai-codex/gpt-5.6-sol") == ()


def test_an_overlay_beats_the_account_catalogue(monkeypatch):
    def boom(slug, *, refresh=False):
        raise AssertionError("the overlay must short-circuit the catalogue")

    monkeypatch.setattr(codex_catalog, "account_efforts", boom)
    assert effort_levels.levels_for("openai-codex/gpt-5.6-sol", ("high",)) == ("high",)


def test_a_non_codex_model_does_not_read_the_catalogue(monkeypatch, table):
    table({"gpt-5": _row()})

    def boom(slug, *, refresh=False):
        raise AssertionError("only openai_codex models read the account catalogue")

    monkeypatch.setattr(codex_catalog, "account_efforts", boom)
    assert effort_levels.levels_for("openai/gpt-5") == ("minimal", "low", "medium", "high")
