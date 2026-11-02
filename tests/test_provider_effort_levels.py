from __future__ import annotations

import pytest

from raven.providers import effort_levels


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
