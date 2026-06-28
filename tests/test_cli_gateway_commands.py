"""CLI tests for ``raven gateway``.

The ``gateway`` command spawns the full agent loop + channel manager + cron +
heartbeat stack and runs forever. Smoke-level coverage only: ``--help`` works,
options are surfaced, the no-API-key path exits cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


def test_gateway_help_works() -> None:
    """``raven gateway --help`` lists the documented options."""
    r = runner.invoke(app, ["gateway", "--help"])
    assert r.exit_code == 0
    assert "Start the Raven gateway" in r.stdout
    assert "--port" in r.stdout
    assert "--workspace" in r.stdout
    assert "--verbose" in r.stdout
    assert "--config" in r.stdout


def test_gateway_config_short_alias_removed() -> None:
    """``-c`` no longer binds ``--config`` (UN-41); only the long form remains."""
    bad = runner.invoke(app, ["gateway", "-c", "/tmp/whatever.json"])
    assert bad.exit_code != 0

    r = runner.invoke(app, ["gateway", "--help"])
    assert r.exit_code == 0
    assert "--config" in r.stdout


def test_gateway_without_api_key_exits_with_error(tmp_config: Path) -> None:
    """With no provider configured, gateway must exit non-zero — and crucially
    must not raise a crash-class exception (NameError / AttributeError /
    ImportError). Those would indicate a regression like a missing import.
    """
    from raven.config.loader import save_config
    from raven.config.schema import Config

    save_config(Config())  # default config, no keys

    r = runner.invoke(app, ["gateway"])
    if r.exception is not None:
        assert not isinstance(r.exception, (NameError, AttributeError, ImportError)), (
            f"Crash-class exception leaked through: {r.exception!r}"
        )
    assert r.exit_code != 0


# Deeper coverage (mocked provider + early-exit) was attempted but hangs:
# gateway() builds AgentLoop + ChannelManager + Cron + Heartbeat stacks and
# their shutdown paths assume a running event loop. Unit-level mocking can't
# unwind that cleanly. Mark this as out-of-scope for unit tests — a real
# E2E harness (or a focused refactor that splits gateway init from run)
# is the right place to cover the deeper paths.


def test_gateway_refuses_second_instance(tmp_config: Path, monkeypatch) -> None:
    """When the instance lock is already held, gateway exits 1 with a clear
    message and never builds the agent/channel stack."""
    from raven.config.loader import save_config
    from raven.config.schema import Config

    save_config(Config())

    from raven.cli import _gateway_lock

    def _raise(now: float):
        raise _gateway_lock.GatewayAlreadyRunningError(
            _gateway_lock.LockInfo(pid=4242, started_at=0.0, config_path=str(tmp_config))
        )

    monkeypatch.setattr(_gateway_lock, "acquire", _raise)

    r = runner.invoke(app, ["gateway"])
    assert r.exit_code == 1
    assert "already running for this instance" in r.stdout
    assert "4242" in r.stdout


def test_gateway_log_config_defaults() -> None:
    from raven.config.schema import GatewayConfig

    log = GatewayConfig().log
    assert log.rotation == "10 MB"
    assert log.retention == 7
    assert log.level == "INFO"
    assert log.console_level == "INFO"


def test_gateway_log_config_omitted_section_is_backward_compatible() -> None:
    from raven.config.schema import GatewayConfig

    cfg = GatewayConfig.model_validate({"port": 18790, "heartbeat": {"enabled": True}})
    assert cfg.log.rotation == "10 MB"
    assert cfg.log.retention == 7
    assert cfg.log.console_level == "INFO"


def test_gateway_log_config_overrides_parse() -> None:
    from raven.config.schema import GatewayConfig

    cfg = GatewayConfig.model_validate(
        {
            "log": {
                "rotation": "00:00",
                "retention": "14 days",
                "level": "DEBUG",
                "console_level": "WARNING",
            }
        }
    )
    assert cfg.log.rotation == "00:00"
    assert cfg.log.retention == "14 days"
    assert cfg.log.level == "DEBUG"
    assert cfg.log.console_level == "WARNING"


def test_gateway_channels_excludes_tui_when_no_im_enabled() -> None:
    # The gateway does not claim ephemeral "tui" cron jobs — those fire in the
    # TUI process, so a TUI-set reminder is never forwarded to an IM channel.
    from unittest.mock import MagicMock

    from raven.cli.gateway_commands import _build_gateway_channels

    cfg = MagicMock()
    for name in (
        "whatsapp",
        "telegram",
        "discord",
        "feishu",
        "mochat",
        "dingtalk",
        "email",
        "slack",
        "qq",
        "matrix",
        "wecom",
        "weixin",
    ):
        ch = MagicMock()
        ch.enabled = False
        setattr(cfg.channels, name, ch)
    assert _build_gateway_channels(cfg) == set()  # no IM enabled, and no "tui"


def test_gateway_channels_excludes_tui_alongside_enabled_im() -> None:
    from unittest.mock import MagicMock

    from raven.cli.gateway_commands import _build_gateway_channels

    cfg = MagicMock()
    for name in (
        "whatsapp",
        "telegram",
        "discord",
        "feishu",
        "mochat",
        "dingtalk",
        "email",
        "slack",
        "qq",
        "matrix",
        "wecom",
        "weixin",
    ):
        ch = MagicMock()
        ch.enabled = name == "telegram"
        setattr(cfg.channels, name, ch)
    result = _build_gateway_channels(cfg)
    assert "tui" not in result
    assert "telegram" in result
    assert "discord" not in result
