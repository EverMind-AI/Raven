"""Tests for per-model coding-identity prompt dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.context_engine.segments import identity_prompts, render


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("claude-opus-4-8", "anthropic"),
        ("anthropic/claude-sonnet-5", "anthropic"),
        ("gpt-5.6", "gpt"),
        ("gpt-4o-mini", "gpt"),
        ("codex-mini", "gpt"),
        ("gemini-2.5-pro", "gemini"),
        ("deepseek-v4-flash-0731", "deepseek"),
        ("qwen3.6-35B-A3B", "qwen"),
        ("kimi-k3", "kimi"),
        ("some-unknown-model", "default"),
        ("", "default"),
        (None, "default"),
    ],
)
def test_family_resolution(model, family):
    assert identity_prompts.resolve_family(model) == family


def test_resolution_is_case_insensitive():
    assert identity_prompts.resolve_family("CLAUDE-OPUS-4-8") == "anthropic"


def test_family_without_a_file_falls_back_to_default():
    """A family may be reserved in the table before its prompt is written."""
    family, text = identity_prompts.load_template("qwen3.6-35B-A3B")
    assert identity_prompts.resolve_family("qwen3.6-35B-A3B") == "qwen"
    if "qwen" not in identity_prompts.available_families():
        assert family == "default"
    assert text


def test_default_family_always_has_a_file():
    assert "default" in identity_prompts.available_families()


def test_anthropic_family_has_a_file():
    family, _ = identity_prompts.load_template("claude-opus-4-8")
    assert family == "anthropic"


# A model id that resolves to each family that actually ships a prompt file, so
# the render tests below cover every file rather than the default one twice.
_MODEL_FOR_FAMILY = {"default": "some-unknown-model", "anthropic": "claude-opus-4-8"}


def _model_ids_covering_every_family() -> list[str | None]:
    return [_MODEL_FOR_FAMILY[f] for f in identity_prompts.available_families()]


def test_every_family_with_a_file_is_covered_by_the_render_tests():
    """Guards the map above: a new prompt file must be added to it."""
    assert set(identity_prompts.available_families()) <= set(_MODEL_FOR_FAMILY)


def test_no_sentinel_survives_rendering():
    """A typo'd sentinel would otherwise ship literal '{{...}}' to the model."""
    for model in _model_ids_covering_every_family():
        rendered = render.identity_text(Path("/workspace"), model=model)
        assert "{{" not in rendered, model


def test_anthropic_render_substitutes_env():
    rendered = render.identity_text(Path("/workspace"), model="claude-opus-4-8")
    assert "{{" not in rendered
    assert "/workspace" in rendered
    assert "todowrite" in rendered


def test_default_render_has_no_todowrite_section():
    """default.txt is the pre-dispatch text verbatim; it predates the tool."""
    rendered = render.identity_text(Path("/workspace"), model="unknown-model")
    assert "# Task management" not in rendered


def test_shared_discipline_block_reaches_every_family():
    for model in _model_ids_covering_every_family():
        rendered = render.identity_text(Path("/workspace"), model=model)
        assert "Software Engineering Discipline" in rendered, model


def test_retired_assistant_behaviors_reach_every_family():
    """The clauses folded in from the retired assistant identity are
    model-agnostic, so a per-family prompt file may not drop them."""
    for model in _model_ids_covering_every_family():
        rendered = render.identity_text(Path("/workspace"), model=model)
        assert "# Platform Policy" in rendered, model
        assert "skills/{skill-name}/SKILL.md" in rendered, model
        assert "# Working discipline" in rendered, model
        assert "call the `ask_user` tool and wait for the answer" in rendered, model
        assert "the `#tag` is a random nonce" in rendered, model
