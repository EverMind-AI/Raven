"""Unit tests for prompts_loader.

Verifies:
- YAML files load to {system, user} dicts
- Placeholder substitution works on drafted prompts
- Missing placeholders raise KeyError (fail fast)
- Literal braces (escaped {{ / }}) survive rendering as single { / }
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_RUNNERS = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "proactivity_eval"
    / "runners"
)
if str(_RUNNERS) not in sys.path:
    sys.path.insert(0, str(_RUNNERS))

import prompts_loader  # noqa: E402

PROMPTS_DIR = _RUNNERS / "prompts"


def test_load_raven_prompt_has_both_keys():
    p = prompts_loader.load_prompt(PROMPTS_DIR / "raven_agent.yaml")
    assert set(p.keys()) == {"system", "user"}
    assert "Raven" in p["system"]
    assert "{obs_block}" in p["user"]
    assert "{synth_block}" in p["user"]


def test_load_hermes_prompt_has_both_keys():
    p = prompts_loader.load_prompt(PROMPTS_DIR / "hermes_agent.yaml")
    assert set(p.keys()) == {"system", "user"}
    assert "Hermes" in p["system"]
    assert "{obs_block}" in p["user"]
    assert "{synth_block}" in p["user"]


def test_render_substitutes_placeholders():
    p = prompts_loader.load_prompt(PROMPTS_DIR / "raven_agent.yaml")
    rendered = prompts_loader.render(
        p,
        obs_block="[t=1.0] User opens a file.",
        synth_block="",
    )
    assert "[t=1.0] User opens a file." in rendered["user"]
    assert "{obs_block}" not in rendered["user"]
    assert "{synth_block}" not in rendered["user"]


def test_escaped_braces_become_literal():
    """Literal {{ / }} in template should render as single { / }."""
    p = prompts_loader.load_prompt(PROMPTS_DIR / "raven_agent.yaml")
    rendered = prompts_loader.render(p, obs_block="x", synth_block="")
    # JSON template example in system prompt uses escaped braces
    assert '"should_help"' in rendered["system"]
    assert "{{" not in rendered["system"]  # no doubled braces in output


def test_missing_placeholder_raises():
    p = prompts_loader.load_prompt(PROMPTS_DIR / "raven_agent.yaml")
    with pytest.raises(KeyError):
        prompts_loader.render(p, obs_block="x")  # missing synth_block


def test_nonexistent_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        prompts_loader.load_prompt(tmp_path / "nope.yaml")


def test_malformed_yaml_missing_system(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("user: hello")
    with pytest.raises(KeyError, match="system"):
        prompts_loader.load_prompt(bad)


def test_yaml_not_mapping(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list")
    with pytest.raises(ValueError, match="mapping"):
        prompts_loader.load_prompt(bad)
