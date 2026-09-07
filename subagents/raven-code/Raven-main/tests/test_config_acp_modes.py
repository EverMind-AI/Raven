"""The ``acp`` config block: the session modes an ACP client may switch to.

A mode is a named per-session operating profile switched over ACP
``session/set_mode``. The block is declarative -- what each mode moves -- and
the schema is strict about it: a knob a mode cannot move must fail loudly, or a
launcher rendering ``reasoningEfort`` would ship a mode that changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config.loader import load_config
from raven.config.schema import Config


def _write(path: Path, body: dict) -> Path:
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_no_acp_block_declares_no_modes() -> None:
    cfg = Config()
    assert cfg.acp.modes == {}
    assert cfg.acp.default_mode is None


def test_the_launcher_shape_round_trips(tmp_path: Path) -> None:
    """The exact block ``subagents/raven-code/run.py`` renders: camelCase keys,
    the baseline entry carrying no effort of its own."""
    cfg = load_config(
        _write(
            tmp_path / "config.json",
            {
                "acp": {
                    "defaultMode": "high",
                    "modes": {
                        "low": {"name": "Low", "description": "quick", "reasoningEffort": "low"},
                        "high": {"name": "High", "description": "the default"},
                        "max": {"name": "Max", "reasoningEffort": "max"},
                    },
                }
            },
        )
    )
    assert cfg.acp.default_mode == "high"
    assert list(cfg.acp.modes) == ["low", "high", "max"]
    assert cfg.acp.modes["low"].reasoning_effort == "low"
    assert cfg.acp.modes["high"].reasoning_effort is None
    assert cfg.acp.modes["high"].description == "the default"
    assert cfg.acp.modes["max"].description == ""


def test_snake_case_is_accepted_too() -> None:
    cfg = Config.model_validate({"acp": {"default_mode": "a", "modes": {"a": {"name": "A", "reasoning_effort": "x"}}}})
    assert cfg.acp.modes["a"].reasoning_effort == "x"


def test_a_misspelled_knob_inside_a_mode_is_refused(tmp_path: Path) -> None:
    """``extra="forbid"`` on the mode entry: a typo must not become a mode that
    silently changes nothing."""
    path = _write(
        tmp_path / "config.json",
        {"acp": {"modes": {"low": {"name": "Low", "reasoningEfort": "low"}}}},
    )
    with pytest.raises(ValueError, match="reasoningEfort"):
        load_config(path)


def test_a_mode_needs_a_name() -> None:
    with pytest.raises(ValueError, match="name"):
        Config.model_validate({"acp": {"modes": {"low": {"reasoningEffort": "low"}}}})


def test_a_misspelled_key_in_the_acp_block_is_refused() -> None:
    """The block itself is as strict as its entries. A misspelled ``defaultMode``
    read as "unset" would start every session in the first declared mode --
    ``low`` for the launcher's catalogue -- with nothing reporting the typo."""
    with pytest.raises(ValueError, match="defaultMod"):
        Config.model_validate(
            {
                "acp": {
                    "defaultMod": "high",
                    "modes": {"low": {"name": "Low", "reasoningEffort": "low"}, "high": {"name": "High"}},
                }
            }
        )
