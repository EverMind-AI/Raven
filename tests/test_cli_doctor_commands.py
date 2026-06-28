"""CLI tests for ``raven doctor``.

Static checks are validated against on-disk config produced by the
``tmp_config`` / ``healthy_config`` fixtures. The probe boundary
(:func:`raven.cli.doctor_commands.send_probe`) is monkeypatched
so tests never touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli import doctor_commands
from raven.cli.commands import app
from raven.config.loader import save_config, set_config_path
from raven.config.schema import Config

runner = CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Point the loader at a tmp config file; tests opt-in via save_config."""
    cfg = tmp_path / "config.json"
    set_config_path(cfg)
    yield cfg
    set_config_path(None)  # type: ignore[arg-type]


@pytest.fixture
def healthy_config(tmp_config: Path, tmp_path: Path) -> Path:
    """Persist a config that routes cleanly to a real provider name."""
    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    cfg.agents.defaults.workspace = str(tmp_path / "workspace")
    cfg.providers.anthropic.api_key = "sk-fake"
    save_config(cfg)
    return tmp_config


# --------------------------------------------------------------------------- help


def test_doctor_help_lists_all_flags() -> None:
    """``--help`` exposes the full flag surface."""
    r = runner.invoke(app, ["doctor", "--help"])
    assert r.exit_code == 0, r.stdout
    for flag in ("--probe", "--json", "--timeout"):
        assert flag in r.stdout, f"missing flag in help: {flag}"


# --------------------------------------------------------------------------- default mode


def test_doctor_default_on_missing_config_exit1(tmp_config: Path) -> None:
    """No config file → exit 1 with a hint to run ``onboard``."""
    assert not tmp_config.exists()
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1, r.stdout
    assert "not configured" in r.stdout
    assert "raven onboard" in r.stdout


def test_doctor_default_healthy_exit0(healthy_config: Path) -> None:
    """Resolved routing + no probe → exit 0, no network call made."""
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.stdout
    # Routing section should mention the resolved provider name
    assert "anthropic" in r.stdout.lower()
    assert (
        "Configuration looks healthy" in r.stdout
        or "All checks passed" in r.stdout
    )


def test_doctor_unresolved_routing_exit1(tmp_config: Path) -> None:
    """Model that no configured provider can serve → exit 1."""
    cfg = Config()
    cfg.agents.defaults.model = "anthropic/claude-sonnet-4-5"
    # Leave every api_key empty so ``_match_provider`` returns ``(None, None)``.
    save_config(cfg)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1, r.stdout
    assert (
        "unresolved" in r.stdout.lower()
        or "could not be routed" in r.stdout
    )


# --------------------------------------------------------------------------- gateway status


def test_doctor_shows_gateway_running(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held instance lock surfaces as ``running (pid …)`` in the Gateway section."""
    from raven.cli import _gateway_lock

    monkeypatch.setattr(
        _gateway_lock,
        "read_status",
        lambda now: _gateway_lock.LockInfo(pid=999, started_at=1_700_000_000.0, config_path=""),
    )
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.stdout
    assert "Gateway" in r.stdout
    assert "running" in r.stdout
    assert "999" in r.stdout


def test_doctor_shows_gateway_not_running(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.cli import _gateway_lock

    monkeypatch.setattr(_gateway_lock, "read_status", lambda now: None)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.stdout
    assert "not running" in r.stdout


# --------------------------------------------------------------------------- --probe


def test_doctor_probe_success(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--probe`` invokes send_probe → exit 0, response shown in output."""
    monkeypatch.setattr(
        doctor_commands,
        "send_probe",
        lambda **_: ("Hello!", 42, 1.5),
    )
    r = runner.invoke(app, ["doctor", "--probe"])
    assert r.exit_code == 0, r.stdout
    assert "Hello!" in r.stdout
    assert "42 tokens" in r.stdout


def test_doctor_probe_failure_exit2(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Static checks pass but probe raises → exit 2."""

    def _boom(**_):
        raise RuntimeError("auth failed")

    monkeypatch.setattr(doctor_commands, "send_probe", _boom)
    r = runner.invoke(app, ["doctor", "--probe"])
    assert r.exit_code == 2, r.stdout
    assert "auth failed" in r.stdout


def test_doctor_timeout_flag_passed_through(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--timeout 3`` reaches ``send_probe`` as ``timeout_s=3``."""
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return ("ok", 1, 0.1)

    monkeypatch.setattr(doctor_commands, "send_probe", _capture)
    r = runner.invoke(app, ["doctor", "--probe", "--timeout", "3"])
    assert r.exit_code == 0, r.stdout
    assert captured.get("timeout_s") == 3


# --------------------------------------------------------------------------- --json


def test_doctor_json_default_structure(healthy_config: Path) -> None:
    """``--json`` emits a parseable doc with the documented top-level keys."""
    r = runner.invoke(app, ["doctor", "--json"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    assert data["version"] == 1
    for key in ("paths", "routing", "features", "gateway"):
        assert key in data, f"missing top-level key: {key}"
    assert "running" in data["gateway"]
    # No probe was requested → key present but null
    assert data["probe"] is None


def test_doctor_json_with_probe_structure(
    healthy_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json --probe`` populates the probe key with the result fields."""
    monkeypatch.setattr(
        doctor_commands,
        "send_probe",
        lambda **_: ("hi", 10, 0.2),
    )
    r = runner.invoke(app, ["doctor", "--json", "--probe"])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    assert isinstance(data["probe"], dict)
    assert data["probe"]["ok"] is True
    assert data["probe"]["text"] == "hi"
    assert data["probe"]["tokens"] == 10
