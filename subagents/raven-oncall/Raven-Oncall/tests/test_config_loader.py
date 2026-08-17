"""Tests for ``raven.config.loader.load_config``.

Covers the migrations that drop / relocate retired blocks from old
configs, plus the default-config fallback path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config.loader import load_config


def _write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    """No file → default Config — loader must not raise."""
    cfg = load_config(tmp_path / "does_not_exist.json")
    # AgentDefaults no longer carries the everos field;
    # check a stable default instead.
    assert cfg.agents.defaults.max_tool_iterations == 40


def test_legacy_everos_block_silently_dropped(tmp_path: Path) -> None:
    """Old configs may still carry ``agents.defaults.everos``. The
    migration strips it so model_validate doesn't reject the file."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {
                "defaults": {
                    "everos": {"enabled": True, "enableSkill": True},
                },
            },
        },
    )
    cfg = load_config(p)
    assert not hasattr(cfg.agents.defaults, "everos")


def test_legacy_everos_skill_light_relocated_under_agents_defaults(
    tmp_path: Path,
) -> None:
    """Old configs put ``everosSkillLight`` under ``agents.defaults``.
    The migration removes it from that location (the new home is under
    ``skillForge.everos``; see test_config_raven_loader for the
    receiving side)."""
    p = tmp_path / "config.json"
    _write(
        p,
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {"enabled": True},
                },
            },
        },
    )
    cfg = load_config(p)
    assert not hasattr(cfg.agents.defaults, "everosSkillLight")
    assert not hasattr(cfg.agents.defaults, "everos_skill_light")


def test_legacy_everos_skill_light_retired_keys_stripped() -> None:
    """everosSkillLight carrying the retired minMessages/minToolCalls must
    relocate to skillForge.everos with those keys dropped (EverOSConfig is
    extra='forbid'), while the surviving fields are kept."""
    from raven.config.loader import _migrate_config

    out = _migrate_config(
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {
                        "enabled": True,
                        "minMessages": 4,
                        "minToolCalls": 2,
                        "maxSkillsTopK": 5,
                    },
                },
            },
        },
        pop_extension_keys=False,
    )
    everos = out["skillForge"]["everos"]
    assert "minMessages" not in everos
    assert "minToolCalls" not in everos
    assert everos["maxSkillsTopK"] == 5
    assert everos["enabled"] is True


def test_legacy_everos_skill_light_retired_keys_stripped_snake_case() -> None:
    """snake_case variant (min_messages / min_tool_calls) is stripped too."""
    from raven.config.loader import _migrate_config

    out = _migrate_config(
        {
            "agents": {
                "defaults": {
                    "everos_skill_light": {
                        "min_messages": 4,
                        "min_tool_calls": 2,
                        "enabled": False,
                    },
                },
            },
        },
        pop_extension_keys=False,
    )
    everos = out["skillForge"]["everos"]
    assert "min_messages" not in everos
    assert "min_tool_calls" not in everos


def test_corrupted_json_falls_back_to_defaults(tmp_path: Path) -> None:
    """A mid-write race can leave the file half-flushed; tolerate it."""
    p = tmp_path / "config.json"
    p.write_text("{this is not json", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.agents.defaults.max_tool_iterations == 40


def test_schema_validation_error_raises(tmp_path: Path) -> None:
    """A user / programmer config error must NOT silently fall back to
    defaults — that masks misconfig as "feature X did nothing"."""
    p = tmp_path / "config.json"
    # ``max_tool_iterations`` is an int — pass a string to force a
    # pydantic ValidationError, which is a ValueError subclass we
    # explicitly re-raise rather than swallow.
    _write(
        p,
        {
            "agents": {"defaults": {"max_tool_iterations": "not-an-int"}},
        },
    )
    with pytest.raises(ValueError, match="schema validation"):
        load_config(p)


def test_read_raw_or_raise_absent_returns_empty(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    assert read_raw_or_raise(tmp_path / "nope.json") == {}


def test_read_raw_or_raise_valid(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "c.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert read_raw_or_raise(p) == {"a": 1}


def test_read_raw_or_raise_malformed_raises(tmp_path: Path) -> None:
    from raven.config.loader import ConfigReadError, read_raw_or_raise

    p = tmp_path / "bad.json"
    p.write_text("{  // comment\n}", encoding="utf-8")
    with pytest.raises(ConfigReadError):
        read_raw_or_raise(p)


def test_load_config_malformed_warns_loudly_and_uses_defaults(tmp_path: Path, capsys) -> None:
    from raven.config.loader import load_config
    from raven.config.schema import Config

    p = tmp_path / "bad.json"
    p.write_text("{  // comment\n}", encoding="utf-8")
    cfg = load_config(p)  # must NOT raise (boot resilience)
    assert isinstance(cfg, Config)
    assert "IGNORING" in capsys.readouterr().err  # loud stderr warning, not silent


def test_read_raw_or_raise_empty_file_is_empty_dict(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "empty.json"
    p.write_text("   \n", encoding="utf-8")
    assert read_raw_or_raise(p) == {}  # empty = no data to lose, not malformed


def test_read_raw_or_raise_json_null_is_empty_dict(tmp_path: Path) -> None:
    from raven.config.loader import read_raw_or_raise

    p = tmp_path / "null.json"
    p.write_text("null", encoding="utf-8")
    assert read_raw_or_raise(p) == {}  # valid JSON but not an object -> {} (no AttributeError)


def test_config_read_error_is_not_runtimeerror() -> None:
    # Intentional: the CLI write commands wrap ops in `except RuntimeError`
    # (OAuth-refusal etc.); ConfigReadError must NOT be a RuntimeError so a parse
    # error bypasses those and reaches the single run() handler. Do not "fix"
    # this to RuntimeError.
    from raven.config.loader import ConfigReadError

    assert not issubclass(ConfigReadError, RuntimeError)
    assert issubclass(ConfigReadError, Exception)
