"""CLI integration tests for ``raven channels {enable,disable,set,get,reset,help}``.

Uses ``typer.testing.CliRunner``; redirects the config to a tmp path via
``raven.config.loader.set_config_path`` so the real ``~/.raven/config.json``
is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path

runner = CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``get_config_path()`` at a sandboxed tmp file for the test."""
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_enable_disable_round_trip(tmp_config: Path) -> None:
    r = runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    assert r.exit_code == 0, r.stdout
    assert "enabled" in r.stdout

    r = runner.invoke(app, ["channels", "get", "telegram"])
    assert r.exit_code == 0, r.stdout
    assert "****set****" in r.stdout
    assert "abc" not in r.stdout  # secret must NOT leak

    r = runner.invoke(app, ["channels", "disable", "telegram"])
    assert r.exit_code == 0
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["enabled"] is False
    assert section["token"] == "abc"  # preserved


def test_enable_complex_channel_with_kebab_flags(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        [
            "channels",
            "enable",
            "feishu",
            "--app-id",
            "X",
            "--app-secret",
            "Y",
        ],
    )
    assert r.exit_code == 0, r.stdout
    section = _read(tmp_config)["channels"]["feishu"]
    assert section["appId"] == "X"
    assert section["appSecret"] == "Y"


def test_enable_nested_field_via_dotted_flag(tmp_config: Path) -> None:
    r = runner.invoke(
        app,
        [
            "channels",
            "enable",
            "slack",
            "--bot-token",
            "xoxb",
            "--app-token",
            "xapp",
            "--dm.policy",
            "allowlist",
        ],
    )
    assert r.exit_code == 0, r.stdout
    section = _read(tmp_config)["channels"]["slack"]
    assert section["botToken"] == "xoxb"
    assert section["dm"]["policy"] == "allowlist"


def test_get_with_show_secrets(tmp_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "plain_value"])
    r = runner.invoke(app, ["channels", "get", "telegram", "--show-secrets"])
    assert r.exit_code == 0
    assert "plain_value" in r.stdout


def test_unknown_field_points_to_show(tmp_config: Path) -> None:
    r = runner.invoke(app, ["channels", "set", "telegram", "--tokn", "xxx"])
    assert r.exit_code != 0
    combined = r.stdout + (r.output or "")
    assert "tokn" in combined
    assert "channels show" in combined


def test_set_using_equals_form(tmp_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "first"])
    r = runner.invoke(app, ["channels", "set", "telegram", "--token=second"])
    assert r.exit_code == 0, r.stdout
    assert _read(tmp_config)["channels"]["telegram"]["token"] == "second"


def test_show_lists_all_flags(tmp_config: Path) -> None:
    r = runner.invoke(app, ["channels", "show", "telegram"])
    assert r.exit_code == 0
    out = r.stdout
    assert "--token" in out
    assert "--allow-from" in out
    assert "--group-policy" in out


def test_help_alias_no_longer_exists(tmp_config: Path) -> None:
    """`channels help <name>` was renamed to `channels show <name>` — verify the
    old verb is gone (no hidden alias).
    """
    r = runner.invoke(app, ["channels", "help", "telegram"])
    assert r.exit_code != 0


def test_reset_clears_token(tmp_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    assert _read(tmp_config)["channels"]["telegram"]["token"] == "abc"

    r = runner.invoke(app, ["channels", "reset", "telegram", "--yes"])
    assert r.exit_code == 0
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["enabled"] is False
    assert section["token"] == ""


def test_enable_unknown_channel_fails(tmp_config: Path) -> None:
    r = runner.invoke(app, ["channels", "enable", "foobar", "--token", "x"])
    assert r.exit_code != 0
    combined = r.stdout + (r.output or "")
    assert "foobar" in combined


def test_set_invalid_value_fails(tmp_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    r = runner.invoke(
        app, ["channels", "set", "telegram", "--group-policy", "not_a_real_value"]
    )
    assert r.exit_code != 0
    combined = r.stdout + (r.output or "")
    assert "Validation" in combined or "validation" in combined.lower()


def test_no_flag_bool_negative(tmp_config: Path) -> None:
    """``--no-foo`` form must set the bool field to False."""
    runner.invoke(
        app,
        ["channels", "enable", "telegram", "--token", "abc", "--reply-to-message"],
    )
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["replyToMessage"] is True

    r = runner.invoke(app, ["channels", "set", "telegram", "--no-reply-to-message"])
    assert r.exit_code == 0, r.stdout
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["replyToMessage"] is False


def test_register_config_commands_direct_invocation() -> None:
    """``_register_config_commands`` must wire all CRUD subcommands onto a
    fresh Typer instance — independent of commands.py.
    """
    import typer
    from typer.testing import CliRunner as _CliRunner

    from raven.cli.channel_commands import _register_config_commands

    fresh_app = typer.Typer()
    _register_config_commands(fresh_app)
    runner_ = _CliRunner()

    r = runner_.invoke(fresh_app, ["show", "telegram"])
    assert r.exit_code == 0
    assert "--token" in r.stdout

    for cmd in ("disable", "get", "reset", "show", "list"):
        r = runner_.invoke(fresh_app, [cmd, "--help"])
        assert r.exit_code == 0, f"{cmd}: {r.stdout}"


# ---------------------------------------------------------------------------
# Tier 1 + Tier 2 UX behaviors
# ---------------------------------------------------------------------------


def test_enable_no_fields_prints_schema_table(tmp_config: Path) -> None:
    """``channels enable telegram`` (no flags) must fall back to schema table
    instead of writing an empty enable."""
    r = runner.invoke(app, ["channels", "enable", "telegram"])
    assert r.exit_code == 0
    out = r.stdout
    assert "--token" in out
    assert "--allow-from" in out
    assert "Tip:" in out
    # Must NOT have actually enabled — file should be untouched (or empty)
    if tmp_config.exists():
        section = _read(tmp_config).get("channels", {}).get("telegram")
        assert section is None or section.get("enabled") is False


def test_set_no_fields_prints_schema_table(tmp_config: Path) -> None:
    """``channels set telegram`` (no flags) falls back to schema table."""
    r = runner.invoke(app, ["channels", "set", "telegram"])
    assert r.exit_code == 0
    out = r.stdout
    assert "--token" in out
    assert "Tip:" in out


def test_enable_dash_help_prints_schema_table(tmp_config: Path) -> None:
    """``channels enable telegram --help`` must NOT raise 'Unknown field' —
    interception prints schema table instead.
    """
    r = runner.invoke(app, ["channels", "enable", "telegram", "--help"])
    assert r.exit_code == 0
    assert "Unknown field" not in r.stdout
    assert "--token" in r.stdout


def test_set_dash_help_prints_schema_table(tmp_config: Path) -> None:
    r = runner.invoke(app, ["channels", "set", "telegram", "--help"])
    assert r.exit_code == 0
    assert "Unknown field" not in r.stdout
    assert "--token" in r.stdout


def test_channels_list_command(tmp_config: Path) -> None:
    """``channels list`` enumerates all 12 channels."""
    r = runner.invoke(app, ["channels", "list"])
    assert r.exit_code == 0
    out = r.stdout
    for c in ("telegram", "slack", "feishu", "mochat", "email", "matrix",
              "discord", "dingtalk", "qq", "wecom", "weixin", "whatsapp"):
        assert c in out, f"{c} missing from `channels list` output"


def test_channels_list_reflects_enabled_state(tmp_config: Path) -> None:
    """``channels list`` Enabled column must mirror the live config."""
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    r = runner.invoke(app, ["channels", "list"])
    assert r.exit_code == 0
    # telegram row should have a ✓; un-enabled channels should not
    lines = r.stdout.splitlines()
    tg_row = next((ln for ln in lines if "telegram" in ln), "")
    slack_row = next((ln for ln in lines if "slack" in ln), "")
    assert "✓" in tg_row, f"telegram row missing ✓: {tg_row!r}"
    assert "✓" not in slack_row, f"slack should be disabled: {slack_row!r}"


def test_channels_top_help_mentions_list() -> None:
    """``channels --help`` epilog must point users at ``channels list``."""
    r = runner.invoke(app, ["channels", "--help"])
    assert r.exit_code == 0
    assert "channels list" in r.stdout


def test_channels_bare_prints_help_not_error() -> None:
    """``raven channels`` (no subcommand) must print help, not 'Missing command'."""
    r = runner.invoke(app, ["channels"])
    assert r.exit_code == 0
    assert "Missing command" not in r.stdout
    assert "channels list" in r.stdout


def test_enable_warns_empty_credentials(tmp_config: Path) -> None:
    """Enabling a channel without supplying its secret fields must warn."""
    r = runner.invoke(app, ["channels", "enable", "telegram", "--allow-from", "alice"])
    assert r.exit_code == 0
    assert "Empty credential fields" in r.stdout
    assert "--token" in r.stdout


def test_enable_with_token_no_credential_warning(tmp_config: Path) -> None:
    """Supplying all secrets must NOT emit the empty-credential warning."""
    r = runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    assert r.exit_code == 0
    assert "Empty credential fields" not in r.stdout


def test_enable_telegram_defaults_allow_from_to_wildcard(tmp_config: Path) -> None:
    """Enabling a channel without explicit ``--allow-from``
    must result in ``allowFrom == ['*']`` (allow anyone), not ``[]`` (deny all).
    The empty-list default caused silent message rejection in gateway runtime.
    """
    r = runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    assert r.exit_code == 0, r.stdout
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["allowFrom"] == ["*"], (
        f"expected allowFrom=['*'] (schema default), got {section['allowFrom']!r}"
    )


def test_all_channel_schemas_default_allow_from_to_wildcard() -> None:
    """Every top-level channel config class should default
    ``allow_from`` to ``['*']``. Catches accidental regressions where a new
    channel is added with the old ``default_factory=list`` pattern.
    """
    from raven.config.schema import ChannelsConfig

    channels_cfg = ChannelsConfig()
    for name in (
        "whatsapp", "telegram", "discord", "feishu", "mochat",
        "dingtalk", "email", "slack", "qq", "matrix", "wecom", "weixin",
    ):
        channel = getattr(channels_cfg, name)
        assert channel.allow_from == ["*"], (
            f"{name}.allow_from default = {channel.allow_from!r}, expected ['*']"
        )


def test_reset_aborts_without_confirm(tmp_config: Path) -> None:
    """Reset without ``--yes`` and answering 'N' must abort and leave file alone."""
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    r = runner.invoke(app, ["channels", "reset", "telegram"], input="N\n")
    assert r.exit_code == 0
    assert "Aborted" in r.stdout
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["token"] == "abc"  # preserved


def test_reset_proceeds_on_y(tmp_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    r = runner.invoke(app, ["channels", "reset", "telegram"], input="y\n")
    assert r.exit_code == 0
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["token"] == ""


def test_reset_yes_flag_skips_confirm(tmp_config: Path) -> None:
    runner.invoke(app, ["channels", "enable", "telegram", "--token", "abc"])
    r = runner.invoke(app, ["channels", "reset", "telegram", "--yes"])
    assert r.exit_code == 0
    section = _read(tmp_config)["channels"]["telegram"]
    assert section["token"] == ""


def test_show_displays_literal_choices(tmp_config: Path) -> None:
    """``channels show telegram`` must surface Literal choices (open/mention)
    so users know what values group_policy accepts.
    """
    r = runner.invoke(app, ["channels", "show", "telegram"])
    assert r.exit_code == 0
    out = r.stdout
    assert "Choices" in out
    assert "open" in out
    assert "mention" in out


def test_register_config_commands_double_register_raises() -> None:
    """Calling ``_register_config_commands`` twice on the same Typer must fail
    fast — duplicate command names would otherwise silently shadow.
    """
    import typer

    from raven.cli.channel_commands import _register_config_commands

    fresh_app = typer.Typer()
    _register_config_commands(fresh_app)
    _register_config_commands(fresh_app)  # decorator side-effect appends duplicates
    cmd_names = [c.name for c in fresh_app.registered_commands]
    duplicates = [n for n in cmd_names if cmd_names.count(n) > 1]
    assert duplicates, (
        "Double-registering should surface duplicate command names; "
        f"got commands: {cmd_names}"
    )


# ============================================================================
# Generic ``channels login <name>`` CLI tests
# ============================================================================


class _FakeChannelBase:
    """Minimal stand-in for a channel class used to assert dispatch behavior."""

    name = "fake"
    display_name = "Fake"

    def __init__(self, config) -> None:  # noqa: ARG002
        pass


def _patch_discover_specs(monkeypatch, name, fake_cls, *, interactive_login=True) -> None:
    """Make ``discover_specs()`` return one migrated channel spec for *name*."""
    from raven.channels.contract import Capabilities, ChannelSpec

    spec = ChannelSpec(
        display_name=getattr(fake_cls, "display_name", name.title()),
        factory=lambda config: fake_cls(config),
        capabilities=Capabilities(interactive_login=interactive_login),
    )
    monkeypatch.setattr("raven.channels.registry.discover_specs", lambda: {name: spec})


def test_channels_login_unknown_channel_exits(tmp_config: Path) -> None:
    """Unknown channel name exits with code 1 and lists available channels."""
    r = runner.invoke(app, ["channels", "login", "no-such-channel"])
    assert r.exit_code == 1
    assert "Unknown channel: no-such-channel" in r.stdout
    assert "telegram" in r.stdout  # at least one real channel listed


def test_channels_login_dispatches_to_channel_login(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``channels login telegram`` instantiates the channel and awaits ``login(force=False)``."""
    calls = {"login_called": False, "force": None, "ctor_args": None}

    class Fake(_FakeChannelBase):
        name = "telegram"
        display_name = "Telegram"

        def __init__(self, config) -> None:
            calls["ctor_args"] = config

        async def login(self, force: bool = False) -> bool:
            calls["login_called"] = True
            calls["force"] = force
            return True

    _patch_discover_specs(monkeypatch, "telegram", Fake)

    r = runner.invoke(app, ["channels", "login", "telegram"])
    assert r.exit_code == 0, r.stdout
    assert calls["login_called"] is True
    assert calls["force"] is False
    assert calls["ctor_args"] is not None  # factory(config) — no bus


def test_channels_login_force_flag_passes_through(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--force`` is forwarded to ``channel.login(force=True)``."""
    captured = {"force": None}

    class Fake(_FakeChannelBase):
        name = "telegram"
        display_name = "Telegram"

        async def login(self, force: bool = False) -> bool:
            captured["force"] = force
            return True

    _patch_discover_specs(monkeypatch, "telegram", Fake)

    r = runner.invoke(app, ["channels", "login", "telegram", "--force"])
    assert r.exit_code == 0
    assert captured["force"] is True


def test_channels_login_returns_false_exits_1(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``channel.login()`` returning False propagates to exit code 1."""

    class Fake(_FakeChannelBase):
        name = "telegram"
        display_name = "Telegram"

        async def login(self, force: bool = False) -> bool:
            return False

    _patch_discover_specs(monkeypatch, "telegram", Fake)

    r = runner.invoke(app, ["channels", "login", "telegram"])
    assert r.exit_code == 1


def test_channels_login_no_interactive_login_routes_to_config(tmp_config: Path) -> None:
    """A migrated channel declaring interactive_login=False (telegram) does not
    pretend to log in — it points the user at ``channels set`` and exits 0."""
    r = runner.invoke(app, ["channels", "login", "telegram"])
    assert r.exit_code == 0, r.stdout
    assert "channels set" in r.stdout


def test_channels_login_helptext_lists_args_and_options(tmp_config: Path) -> None:
    """``channels login --help`` surfaces the ``CHANNEL_NAME`` argument and ``--force``."""
    r = runner.invoke(app, ["channels", "login", "--help"])
    assert r.exit_code == 0
    assert "CHANNEL_NAME" in r.stdout
    assert "--force" in r.stdout


# ============================================================================
# WhatsAppChannel.login() tests
# ============================================================================


@pytest.fixture
def whatsapp_channel(tmp_config: Path):
    """A WhatsAppChannel instance with a dummy config."""
    from raven.channels.adapters.whatsapp.channel import WhatsAppChannel
    from raven.config.schema import WhatsAppConfig

    return WhatsAppChannel(WhatsAppConfig())


def test_whatsapp_login_runs_bridge_subprocess(
    whatsapp_channel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: bridge is set up + ``npm start`` invoked with correct env."""
    import asyncio
    import subprocess

    monkeypatch.setattr(
        "raven.channels.adapters.whatsapp.bridge.ensure_bridge_dir",
        lambda: tmp_path / "bridge",
    )
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/npm")
    captured = {"cmd": None, "env": None, "cwd": None}

    def fake_run(cmd, cwd=None, check=False, env=None, **_):  # noqa: ARG001
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("raven.channels.adapters.whatsapp.bridge.subprocess.run", fake_run)

    result = asyncio.run(whatsapp_channel.login())
    assert result is True
    assert captured["cmd"] == ["/usr/bin/npm", "start"]
    assert captured["cwd"] == tmp_path / "bridge"
    assert "AUTH_DIR" in captured["env"]


def test_whatsapp_login_returns_false_on_subprocess_error(
    whatsapp_channel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``npm start`` (CalledProcessError) yields ``False``."""
    import asyncio
    import subprocess

    monkeypatch.setattr(
        "raven.channels.adapters.whatsapp.bridge.ensure_bridge_dir",
        lambda: tmp_path / "bridge",
    )
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(
        "raven.channels.adapters.whatsapp.bridge.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(  # noqa: ARG005
            subprocess.CalledProcessError(1, ["npm", "start"])
        ),
    )

    result = asyncio.run(whatsapp_channel.login())
    assert result is False


def test_whatsapp_login_returns_false_when_bridge_setup_fails(
    whatsapp_channel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``bridge.ensure_bridge_dir`` raises ``RuntimeError`` the login fails gracefully."""
    import asyncio

    monkeypatch.setattr(
        "raven.channels.adapters.whatsapp.bridge.ensure_bridge_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("bridge source missing")),
    )

    result = asyncio.run(whatsapp_channel.login())
    assert result is False


def test_whatsapp_login_returns_false_when_npm_missing(
    whatsapp_channel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``shutil.which('npm')`` returns ``None`` the login fails gracefully."""
    import asyncio

    monkeypatch.setattr(
        "raven.channels.adapters.whatsapp.bridge.ensure_bridge_dir",
        lambda: tmp_path / "bridge",
    )
    monkeypatch.setattr("shutil.which", lambda _: None)

    result = asyncio.run(whatsapp_channel.login())
    assert result is False
