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


def test_no_sentinel_survives_rendering():
    """A typo'd sentinel would otherwise ship literal '{{...}}' to the model."""
    for family in identity_prompts.available_families():
        _, template = identity_prompts.load_template(
            "claude-opus-4-8" if family == "anthropic" else "unknown-model"
        )
        rendered = render.identity_text(Path("/workspace"), profile="coding", model=None)
        assert "{{" not in rendered, family
        del template


def test_anthropic_render_substitutes_env():
    rendered = render.identity_text(Path("/workspace"), profile="coding", model="claude-opus-4-8")
    assert "{{" not in rendered
    assert "/workspace" in rendered
    assert "todowrite" in rendered


def test_default_render_has_no_todowrite_section():
    """default.txt is the pre-dispatch text verbatim; it predates the tool."""
    rendered = render.identity_text(Path("/workspace"), profile="coding", model="unknown-model")
    assert "# Task management" not in rendered


def test_assistant_profile_ignores_model():
    a = render.identity_text(Path("/workspace"), profile="assistant", model="claude-opus-4-8")
    b = render.identity_text(Path("/workspace"), profile="assistant", model="gpt-5.6")
    assert a == b


def test_shared_discipline_block_reaches_every_family():
    for model in (None, "claude-opus-4-8"):
        rendered = render.identity_text(Path("/workspace"), profile="coding", model=model)
        assert "Software Engineering Discipline" in rendered
