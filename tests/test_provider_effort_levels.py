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


def test_a_prefixed_model_never_reaches_litellms_provider_resolver(table, monkeypatch):
    """``get_llm_provider`` starts a device-flow login for LiteLLM's OAuth
    spellings (``github_copilot/``, ``chatgpt/``); a prefixed id names its
    provider already, and the turn calls this on its own thread."""
    table({"github_copilot/gpt-4o": _row()})
    import litellm.utils as litellm_utils

    calls: list[str] = []

    def record(*, model, custom_llm_provider):
        calls.append(model)
        return {}

    monkeypatch.setattr(litellm_utils, "_get_potential_model_names", record)
    assert effort_levels.levels_for("github_copilot/gpt-4o") == ("minimal", "low", "medium", "high")
    assert calls == []
    assert effort_levels.levels_for("github_copilot/not-in-the-table") == ()
    assert calls == []


def test_a_bare_model_still_consults_litellms_resolver(table, monkeypatch):
    """The resolver's aliases are what a bare id has; only prefixed ids skip it."""
    table({"gpt-5": _row()})
    import litellm.utils as litellm_utils

    calls: list[str] = []

    def record(*, model, custom_llm_provider):
        calls.append(model)
        return {}

    monkeypatch.setattr(litellm_utils, "_get_potential_model_names", record)
    assert effort_levels.levels_for("some-bare-model") == ()
    assert calls == ["some-bare-model"]


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


def test_a_cold_account_catalogue_has_no_levels(monkeypatch):
    monkeypatch.setattr(codex_catalog, "account_efforts", lambda slug, *, refresh=False: ())
    assert effort_levels.levels_for("openai-codex/gpt-5.6-sol") == ()


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


def test_a_declared_word_outside_the_ladder_falls_back_to_the_table(table):
    """A hand-edited overlay is not validated at the file; only a word the
    build knows is a declaration, and a list of none states nothing."""
    table({"gpt-5": _row()})
    assert effort_levels.levels_for("gpt-5", ("High",)) == ("minimal", "low", "medium", "high")
    assert effort_levels.levels_for("gpt-5", ("high", "Turbo")) == ("high",)


def test_a_fallback_downgrades_to_the_highest_level_the_hop_declares():
    assert effort_levels.downgrade("max", ("low", "high")) == "high"
    assert effort_levels.downgrade("high", ("minimal", "low")) == "low"
    assert effort_levels.downgrade("minimal", ("medium", "high")) is None
    assert effort_levels.downgrade("high", None) == "high"


def test_an_overlays_last_alias_wins():
    """The model-label reader and the overlay writer already use the last row;
    the effort reader must not pick the first and disagree with them."""
    from raven.providers.overlay import overlay_for

    section = {
        "modelOverlay": {
            "gpt-5": {"reasoningEfforts": ["low"]},
            "openai/gpt-5": {"reasoningEfforts": ["high"]},
        }
    }
    assert overlay_for(section, "openai", "openai/gpt-5") == {"reasoningEfforts": ["high"]}


def test_declared_for_reads_raw_and_parsed_providers_alike(tmp_path):
    """The turn reads the live file's raw subtree and the RPC the parsed
    config; one lookup keeps their answers, including a legacy spelling."""
    import json

    from raven.config.live import LiveConfig
    from raven.config.loader import load_config
    from raven.providers.overlay import declared_for

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"providers": {"zhipu": {"modelOverlay": {"zai/glm-5": {"reasoningEfforts": ["high"]}}}}}),
        encoding="utf-8",
    )
    assert declared_for(LiveConfig(path).get("providers") or {}, "zai", "zai/glm-5") == ("high",)
    assert declared_for(load_config(path).providers, "zai", "zai/glm-5") == ("high",)
