"""Unit tests for ``raven.config.update`` — the misc-ops write path.

Companion to ``test_config_update_providers.py`` /
``test_config_update_channels.py``. Covers the small focused helpers that
patch one or two fields without re-serializing the entire Pydantic model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config.update import (
    reset_cron_config,
    set_default_model,
    set_memory_backend,
    set_sandbox_backend,
    set_sentinel_nudge_quota,
    update_cron_config,
)


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# set_sentinel_nudge_quota
# ---------------------------------------------------------------------------


def test_nudge_quota_writes_camel_into_empty_config(cfg_path: Path) -> None:
    changed = set_sentinel_nudge_quota(per_hour=1, per_day=3, config_path=cfg_path)
    data = _read(cfg_path)
    assert data["sentinel"]["nudgePolicy"] == {
        "maxNudgesPerHour": 1, "maxNudgesPerDay": 3,
    }
    assert changed == {
        "max_nudges_per_hour": (None, 1),
        "max_nudges_per_day": (None, 3),
    }


def test_nudge_quota_partial_update_returns_prev(cfg_path: Path) -> None:
    set_sentinel_nudge_quota(per_hour=5, per_day=20, config_path=cfg_path)
    changed = set_sentinel_nudge_quota(per_hour=1, config_path=cfg_path)
    assert changed == {"max_nudges_per_hour": (5, 1)}
    data = _read(cfg_path)
    assert data["sentinel"]["nudgePolicy"]["maxNudgesPerHour"] == 1
    assert data["sentinel"]["nudgePolicy"]["maxNudgesPerDay"] == 20  # untouched


def test_nudge_quota_respects_existing_snake_casing(cfg_path: Path) -> None:
    cfg_path.write_text(json.dumps({
        "sentinel": {"nudge_policy": {"max_nudges_per_hour": 9}},
    }), encoding="utf-8")
    set_sentinel_nudge_quota(per_hour=1, per_day=3, config_path=cfg_path)
    np = _read(cfg_path)["sentinel"]["nudge_policy"]
    # no duplicate camel keys introduced alongside the snake ones
    assert np == {"max_nudges_per_hour": 1, "max_nudges_per_day": 3}
    assert "maxNudgesPerHour" not in np


def test_nudge_quota_roundtrips_through_loader(cfg_path: Path) -> None:
    from raven.config.raven import load_raven_config

    set_sentinel_nudge_quota(per_hour=1, per_day=3, config_path=cfg_path)
    cfg = load_raven_config(cfg_path)
    assert cfg.sentinel.nudge_policy.max_nudges_per_hour == 1
    assert cfg.sentinel.nudge_policy.max_nudges_per_day == 3


def test_nudge_quota_rejects_below_one(cfg_path: Path) -> None:
    with pytest.raises(ValueError):
        set_sentinel_nudge_quota(per_hour=0, config_path=cfg_path)
    assert not cfg_path.exists()  # nothing written on validation failure


def test_nudge_quota_requires_at_least_one_arg(cfg_path: Path) -> None:
    with pytest.raises(ValueError):
        set_sentinel_nudge_quota(config_path=cfg_path)


# ---------------------------------------------------------------------------
# set_default_model
# ---------------------------------------------------------------------------


def test_set_default_model_writes_into_empty_config(cfg_path: Path) -> None:
    prev = set_default_model("openrouter/anthropic/claude-sonnet-4-5", config_path=cfg_path)
    assert prev is None
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "openrouter/anthropic/claude-sonnet-4-5"


def test_set_default_model_returns_previous_value(cfg_path: Path) -> None:
    cfg_path.write_text(json.dumps({"agents": {"defaults": {"model": "openai/gpt-4o"}}}))
    prev = set_default_model("anthropic/claude-sonnet-4-5", config_path=cfg_path)
    assert prev == "openai/gpt-4o"
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "anthropic/claude-sonnet-4-5"


def test_set_default_model_preserves_sibling_fields(cfg_path: Path) -> None:
    cfg_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "old-model",
                        "maxTokens": 4096,
                        "temperature": 0.5,
                    }
                },
                "providers": {"openai": {"apiKey": "sk-keep-me"}},
            }
        )
    )
    set_default_model("new-model", config_path=cfg_path)
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "new-model"
    assert data["agents"]["defaults"]["maxTokens"] == 4096
    assert data["agents"]["defaults"]["temperature"] == 0.5
    assert data["providers"]["openai"]["apiKey"] == "sk-keep-me"


def test_set_default_model_creates_nested_structure_when_missing(cfg_path: Path) -> None:
    cfg_path.write_text(json.dumps({"providers": {}}))
    set_default_model("some-model", config_path=cfg_path)
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "some-model"
    assert data["providers"] == {}


# ---------------------------------------------------------------------------
# update_cron_config / reset_cron_config
# ---------------------------------------------------------------------------


def test_update_cron_config_writes_into_empty_config(cfg_path: Path) -> None:
    prev = update_cron_config("forward_channels", ["telegram"], config_path=cfg_path)
    assert prev is None
    data = _read(cfg_path)
    assert data["cron"]["forwardChannels"] == ["telegram"]


def test_update_cron_config_returns_previous_value(cfg_path: Path) -> None:
    update_cron_config("forward_channels", ["telegram"], config_path=cfg_path)
    prev = update_cron_config("forward_channels", ["feishu"], config_path=cfg_path)
    assert prev == ["telegram"]
    data = _read(cfg_path)
    assert data["cron"]["forwardChannels"] == ["feishu"]


def test_update_cron_config_default_timezone(cfg_path: Path) -> None:
    update_cron_config("default_timezone", "America/Vancouver", config_path=cfg_path)
    data = _read(cfg_path)
    assert data["cron"]["defaultTimezone"] == "America/Vancouver"


def test_update_cron_config_unknown_key_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown cron config key"):
        update_cron_config("nonexistent_key", "x", config_path=cfg_path)


def test_reset_cron_config_removes_section(cfg_path: Path) -> None:
    update_cron_config("forward_channels", ["telegram"], config_path=cfg_path)
    update_cron_config("default_timezone", "UTC", config_path=cfg_path)
    reset_cron_config(config_path=cfg_path)
    data = _read(cfg_path)
    assert "cron" not in data


def test_update_cron_preserves_sibling_sections(cfg_path: Path) -> None:
    cfg_path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": "openai/gpt-4o"}},
                "providers": {"openai": {"apiKey": "sk-keep-me"}},
            }
        )
    )
    update_cron_config("forward_channels", ["telegram"], config_path=cfg_path)
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o"
    assert data["providers"]["openai"]["apiKey"] == "sk-keep-me"
    assert data["cron"]["forwardChannels"] == ["telegram"]


# ---------------------------------------------------------------------------
# set_sandbox_backend
# ---------------------------------------------------------------------------


def test_set_sandbox_backend_writes_and_returns_prev(cfg_path: Path) -> None:
    # sandbox is nested under tools, not at the root.
    assert set_sandbox_backend("boxlite", config_path=cfg_path) is None
    assert _read(cfg_path)["tools"]["sandbox"]["backend"] == "boxlite"
    prev = set_sandbox_backend("none", config_path=cfg_path)
    assert prev == "boxlite"
    assert _read(cfg_path)["tools"]["sandbox"]["backend"] == "none"


def test_set_sandbox_backend_preserves_siblings(cfg_path: Path) -> None:
    cfg_path.write_text(
        json.dumps({"providers": {"openai": {"apiKey": "sk-keep"}}})
    )
    set_sandbox_backend("boxlite", config_path=cfg_path)
    data = _read(cfg_path)
    assert data["providers"]["openai"]["apiKey"] == "sk-keep"
    assert data["tools"]["sandbox"]["backend"] == "boxlite"


def test_set_sandbox_backend_survives_reload(cfg_path: Path) -> None:
    # Regression: a top-level "sandbox" key fails Config's extra=forbid on the
    # next load. The write must land under tools.sandbox so load_config round-trips.
    from raven.config.loader import load_config

    set_sandbox_backend("boxlite", config_path=cfg_path)
    cfg = load_config(cfg_path)
    assert cfg.tools.sandbox.backend == "boxlite"


# ---------------------------------------------------------------------------
# set_memory_backend
# ---------------------------------------------------------------------------


def test_set_memory_backend_everos_then_none(cfg_path: Path) -> None:
    assert set_memory_backend("everos", config_path=cfg_path) is None
    assert _read(cfg_path)["memory"]["backend"] == "everos"
    prev = set_memory_backend(None, config_path=cfg_path)
    assert prev == "everos"
    assert _read(cfg_path)["memory"]["backend"] is None


def test_set_memory_backend_preserves_siblings(cfg_path: Path) -> None:
    cfg_path.write_text(
        json.dumps({"agents": {"defaults": {"model": "openai/gpt-4o"}}})
    )
    set_memory_backend("everos", config_path=cfg_path)
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o"
    assert data["memory"]["backend"] == "everos"
