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
    allow_exec_pattern,
    initialize_a2a_server,
    reset_cron_config,
    set_default_model,
    set_memory_backend,
    set_playbook_disabled,
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
        "maxNudgesPerHour": 1,
        "maxNudgesPerDay": 3,
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
    cfg_path.write_text(
        json.dumps(
            {
                "sentinel": {"nudge_policy": {"max_nudges_per_hour": 9}},
            }
        ),
        encoding="utf-8",
    )
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
    prev = update_cron_config("default_timezone", "UTC", config_path=cfg_path)
    assert prev is None
    data = _read(cfg_path)
    assert data["cron"]["defaultTimezone"] == "UTC"


def test_update_cron_config_returns_previous_value(cfg_path: Path) -> None:
    update_cron_config("default_timezone", "UTC", config_path=cfg_path)
    prev = update_cron_config("default_timezone", "America/Vancouver", config_path=cfg_path)
    assert prev == "UTC"
    data = _read(cfg_path)
    assert data["cron"]["defaultTimezone"] == "America/Vancouver"


def test_update_cron_config_unknown_key_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown cron config key"):
        update_cron_config("nonexistent_key", "x", config_path=cfg_path)


def test_update_cron_config_retired_forward_channels_raises(cfg_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown cron config key"):
        update_cron_config("forward_channels", ["telegram"], config_path=cfg_path)


def test_reset_cron_config_removes_section(cfg_path: Path) -> None:
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
    update_cron_config("default_timezone", "UTC", config_path=cfg_path)
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o"
    assert data["providers"]["openai"]["apiKey"] == "sk-keep-me"
    assert data["cron"]["defaultTimezone"] == "UTC"


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
    cfg_path.write_text(json.dumps({"providers": {"openai": {"apiKey": "sk-keep"}}}))
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
    cfg_path.write_text(json.dumps({"agents": {"defaults": {"model": "openai/gpt-4o"}}}))
    set_memory_backend("everos", config_path=cfg_path)
    data = _read(cfg_path)
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o"
    assert data["memory"]["backend"] == "everos"


# ---------------------------------------------------------------------------
# sparse init: a fresh config carries intent, never defaults
# ---------------------------------------------------------------------------


def test_a_fresh_config_file_is_sparse(cfg_path: Path) -> None:
    """Onboard used to seed the extension blocks with their defaults; a value
    that lands on disk stops following its declaration. A fresh file now
    records nothing the user did not set, and every extension reader falls
    back to its declared defaults when the block is absent."""
    from raven.config.loader import Config, save_config
    from raven.config.raven import load_raven_config

    save_config(Config(), config_path=cfg_path)
    data = _read(cfg_path)
    for block in ("memory", "plugins", "sentinel", "skillForge"):
        assert block not in data, f"fresh config must not seed {block}"

    rc = load_raven_config(config_path=cfg_path)
    assert rc is not None


def test_malformed_config_refuses_write_and_preserves_file(cfg_path: Path) -> None:
    # REGRESSION: update.py is the write path for cron/sentinel/onboard; a
    # present-but-unparseable config must NOT be clobbered (the real-machine bug
    # reproduced via set_sandbox_backend wiping providers).
    from raven.config.loader import ConfigReadError
    from raven.config.update import set_default_model, set_language

    original = '{\n  "providers": {"openai": {"apiKey": "sk-o"}},\n  // comment => invalid JSON\n}\n'
    cfg_path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigReadError):
        set_language("zh", config_path=cfg_path)
    assert cfg_path.read_text(encoding="utf-8") == original  # untouched
    with pytest.raises(ConfigReadError):
        set_default_model("openrouter/x", config_path=cfg_path)
    assert cfg_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# set_playbook_disabled
# ---------------------------------------------------------------------------


def test_playbook_disable_adds_the_name(cfg_path: Path) -> None:
    assert set_playbook_disabled("weekly-feedback", True, config_path=cfg_path) is True
    assert _read(cfg_path)["playbooks"]["disabled"] == ["weekly-feedback"]


def test_playbook_disable_is_idempotent(cfg_path: Path) -> None:
    set_playbook_disabled("weekly-feedback", True, config_path=cfg_path)
    before = cfg_path.read_text(encoding="utf-8")
    assert set_playbook_disabled("weekly-feedback", True, config_path=cfg_path) is False
    assert cfg_path.read_text(encoding="utf-8") == before


def test_playbook_enable_removes_only_that_name(cfg_path: Path) -> None:
    set_playbook_disabled("a", True, config_path=cfg_path)
    set_playbook_disabled("b", True, config_path=cfg_path)
    assert set_playbook_disabled("a", False, config_path=cfg_path) is True
    assert _read(cfg_path)["playbooks"]["disabled"] == ["b"]
    # enabling a name that is not on the list is a no-op, not an error
    assert set_playbook_disabled("ghost", False, config_path=cfg_path) is False


def test_playbook_disabled_preserves_sibling_fields(cfg_path: Path) -> None:
    cfg_path.write_text('{"playbooks": {"enabled": true, "dir": "/x"}}', encoding="utf-8")
    set_playbook_disabled("a", True, config_path=cfg_path)
    data = _read(cfg_path)["playbooks"]
    assert data["enabled"] is True and data["dir"] == "/x"
    assert data["disabled"] == ["a"]


def test_allow_exec_pattern_writes_and_is_idempotent(cfg_path: Path) -> None:
    cfg_path.write_text("{}", encoding="utf-8")
    assert allow_exec_pattern("git push *", config_path=cfg_path) is True
    assert allow_exec_pattern("git push *", config_path=cfg_path) is False
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["permissions"]["tools"]["exec"] == {"git push *": "allow"}


def test_allow_exec_pattern_refuses_to_overwrite_a_plain_tier_the_user_set(cfg_path: Path) -> None:
    cfg_path.write_text(json.dumps({"permissions": {"tools": {"exec": "ask"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a table"):
        allow_exec_pattern("git push *", config_path=cfg_path)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["permissions"]["tools"]["exec"] == "ask"


def test_allow_exec_pattern_roundtrips_through_the_loader(cfg_path: Path) -> None:
    from raven.config.loader import load_config

    cfg_path.write_text("{}", encoding="utf-8")
    allow_exec_pattern("git push *", config_path=cfg_path)
    assert load_config(cfg_path).permissions.tools == {"exec": {"git push *": "allow"}}


# ---------------------------------------------------------------------------
# initialize_a2a_server
# ---------------------------------------------------------------------------


def test_a2a_init_mints_a_token_and_switches_the_face_on(cfg_path: Path) -> None:
    token = initialize_a2a_server(config_path=cfg_path)

    server = _read(cfg_path)["a2a"]["server"]
    assert server["enabled"] is True
    assert server["token"] == token
    # Long enough that guessing is not the attack: token_urlsafe(32) is 256
    # bits of entropy, rendered as 43 characters.
    assert len(token) >= 40


def test_a2a_init_enables_nothing_without_a_token(cfg_path: Path) -> None:
    """The pair is written together or not at all.

    ``a2a/auth.py`` refuses every caller while the token is empty, so an
    enabled face with no credential is not a lax door but a shut one -- it
    would advertise a capability that answers nobody.
    """
    initialize_a2a_server(config_path=cfg_path)
    server = _read(cfg_path)["a2a"]["server"]
    assert server["enabled"] is True and server["token"]


def test_a2a_init_never_rotates_a_token_it_finds(cfg_path: Path) -> None:
    """Re-onboarding must not invalidate credentials already given to callers."""
    first = initialize_a2a_server(config_path=cfg_path)

    assert initialize_a2a_server(config_path=cfg_path) is None
    assert _read(cfg_path)["a2a"]["server"]["token"] == first


def test_a2a_init_leaves_a_face_the_operator_switched_off(cfg_path: Path) -> None:
    """The token, not ``enabled``, is the already-initialized mark.

    Keying on ``enabled`` instead would re-assert it on every onboard, and an
    operator who deliberately closed the face would find it reopened by an
    unrelated ``raven onboard``.
    """
    initialize_a2a_server(config_path=cfg_path)
    data = _read(cfg_path)
    data["a2a"]["server"]["enabled"] = False
    cfg_path.write_text(json.dumps(data), encoding="utf-8")

    initialize_a2a_server(config_path=cfg_path)

    assert _read(cfg_path)["a2a"]["server"]["enabled"] is False


def test_a2a_init_creates_a_missing_config_owner_only(cfg_path: Path) -> None:
    """The mode has to hold for a file this write creates, not only for one it
    finds: ``atomic_update`` would otherwise land a brand-new config at the
    process umask -- 0644 under the common one -- with the token already in it.
    """
    assert not cfg_path.exists()

    initialize_a2a_server(config_path=cfg_path)

    assert cfg_path.stat().st_mode & 0o777 == 0o600


def test_a2a_init_narrows_a_world_readable_config(cfg_path: Path) -> None:
    """Nothing else narrows this file, and onboarding creates it under the
    umask, so the write that puts a credential in it is the one that owes the
    mode -- alongside the provider API keys already there.
    """
    cfg_path.write_text(json.dumps({"providers": {"openai": {"apiKey": "sk-x"}}}), encoding="utf-8")
    cfg_path.chmod(0o644)

    initialize_a2a_server(config_path=cfg_path)

    assert cfg_path.stat().st_mode & 0o777 == 0o600


def test_a2a_init_that_mints_nothing_leaves_the_mode_alone(cfg_path: Path) -> None:
    """Narrowing rides the minting write rather than the call, so a repeat
    onboard does not keep overriding a mode the operator chose afterwards.
    """
    initialize_a2a_server(config_path=cfg_path)
    cfg_path.chmod(0o640)

    initialize_a2a_server(config_path=cfg_path)

    assert cfg_path.stat().st_mode & 0o777 == 0o640


def test_a2a_init_touches_no_unrelated_field(cfg_path: Path) -> None:
    cfg_path.write_text(
        json.dumps(
            {
                "providers": {"openai": {"apiKey": "sk-keep"}},
                "agents": {"defaults": {"model": "openai/gpt-4o"}},
                "a2a": {"peers": [{"origin": "https://peer.example"}]},
            }
        ),
        encoding="utf-8",
    )

    initialize_a2a_server(config_path=cfg_path)

    data = _read(cfg_path)
    assert data["providers"]["openai"]["apiKey"] == "sk-keep"
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o"
    assert data["a2a"]["peers"] == [{"origin": "https://peer.example"}]
    assert data["a2a"]["server"]["enabled"] is True
