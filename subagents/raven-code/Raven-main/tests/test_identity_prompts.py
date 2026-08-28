"""Tests for per-model coding-identity prompt dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent import workdir
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
    _, family, text = identity_prompts.load_template("qwen3.6-35B-A3B")
    assert identity_prompts.resolve_family("qwen3.6-35B-A3B") == "qwen"
    if "qwen" not in identity_prompts.available_families():
        assert family == "default"
    assert text


def test_default_family_always_has_a_file():
    assert "default" in identity_prompts.available_families()


def test_anthropic_family_has_a_file():
    _, family, _ = identity_prompts.load_template("claude-opus-4-8")
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


def test_identity_distinguishes_workdir_from_agent_home(tmp_path):
    agent_home = tmp_path / "agent-home"
    checkout = tmp_path / "checkout"
    agent_home.mkdir()
    checkout.mkdir()

    with workdir.bind(checkout):
        rendered = render.identity_text(agent_home, model="claude-opus-4-8")

    assert f"Working directory: {checkout}" in rendered
    assert f"Agent home: {agent_home}" in rendered
    assert f"Custom skills: {agent_home}/skills/" in rendered
    assert f"Custom skills: {checkout}/skills/" not in rendered


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


# ---- domain axis ---------------------------------------------------------------


def _render_for_domain(monkeypatch: pytest.MonkeyPatch, domain: str, model: str | None = None) -> str:
    from raven.agent import profile as run_profile

    monkeypatch.setenv(run_profile.DOMAIN_ENV, domain)
    monkeypatch.setattr(run_profile, "_current", None)
    monkeypatch.setattr(run_profile, "_current", run_profile.resolve_profile())
    return render.identity_text(Path("/workspace"), model=model)


def test_data_domain_serves_its_own_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = _render_for_domain(monkeypatch, "data")
    assert "Data Analysis Methodology" in rendered
    assert "{{" not in rendered


def test_data_domain_drops_the_software_discipline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The coding discipline is not merely unhelpful here, it is wrong work."""
    rendered = _render_for_domain(monkeypatch, "data")
    assert "Software Engineering Discipline" not in rendered
    assert "the project's existing tests come first" not in rendered
    assert "DO NOT ADD ***ANY*** COMMENTS" not in rendered


def test_data_domain_keeps_the_domain_agnostic_clauses(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = _render_for_domain(monkeypatch, "data")
    assert "# Platform Policy" in rendered
    assert "# Working discipline" in rendered
    assert "the `#tag` is a random nonce" in rendered
    assert "`<system-reminder>`" in rendered


def test_coding_domain_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = _render_for_domain(monkeypatch, "coding")
    assert "Software Engineering Discipline" in rendered
    assert "Data Analysis Methodology" not in rendered


def test_a_domain_without_a_prompt_falls_back_to_coding() -> None:
    """Same rule as a missing family file: degrade, never raise."""
    domain, family, text = identity_prompts.load_template("claude-opus-4-8", "no-such-domain")
    assert domain == identity_prompts.DEFAULT_DOMAIN
    assert family == "default"
    assert text


def test_domain_family_beats_domain_default() -> None:
    """A family file inside the domain wins over that domain's default."""
    domain, family, _ = identity_prompts.load_template("claude-opus-4-8", "coding")
    assert (domain, family) == ("coding", "anthropic")


def test_data_prompt_carries_the_data_discipline_sentinel() -> None:
    """The block is substituted, not inlined -- the file must ask for it."""
    text = identity_prompts.prompt_path("data", "default").read_text(encoding="utf-8")
    assert "{{DATA_DISCIPLINE}}" in text
    assert "{{SE_DISCIPLINE}}" not in text


def test_bootstrap_files_are_coding_only(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """AGENTS.md states how to build *this repo*; a data workspace has no such thing."""
    from raven.agent import profile as run_profile

    (tmp_path / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    monkeypatch.setattr(run_profile, "_current", None)
    monkeypatch.setenv(run_profile.DOMAIN_ENV, "coding")
    monkeypatch.setattr(run_profile, "_current", run_profile.resolve_profile())
    assert "repo rules" in render.load_bootstrap_files(tmp_path)
    monkeypatch.setenv(run_profile.DOMAIN_ENV, "data")
    monkeypatch.setattr(run_profile, "_current", run_profile.resolve_profile())
    assert render.load_bootstrap_files(tmp_path) == ""
