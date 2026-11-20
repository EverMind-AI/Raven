"""The ACP adapter pin check, offline: the pins, the reading, and the three exit codes."""

from __future__ import annotations

import pytest

from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS
from scripts import check_acp_shim_pins as watch


def _latest_from_pins() -> dict[str, str]:
    return {pin.package: pin.version for pin in watch.shipped_pins()}


def test_the_checked_pins_are_the_ones_the_presets_ship() -> None:
    """The check reads the preset tables, so a pin that moves in the table
    moves the check with it. The package map is the one fact beside the
    command that the table does not carry, and it must name presets that
    exist."""
    assert set(watch.PACKAGES) <= set(THIRD_PARTY_SUBAGENT_PRESETS)

    pins = watch.shipped_pins()

    assert [pin.preset for pin in pins] == ["codex", "claude_code", "pi"]
    for pin in pins:
        command = THIRD_PARTY_SUBAGENT_PRESETS[pin.preset]["command"]
        assert pin.version == watch.pinned_version(pin.package, command)


def test_a_version_is_read_from_the_npx_argument() -> None:
    assert (
        watch.pinned_version("@agentclientprotocol/codex-acp", "npx -y @agentclientprotocol/codex-acp@2.7.0") == "2.7.0"
    )
    assert watch.pinned_version("@agentclientprotocol/codex-acp", "npx -y pi-acp@0.0.34") is None
    assert watch.pinned_version("@agentclientprotocol/codex-acp", "npx -y @agentclientprotocol/codex-acp") is None


def test_versions_rank_by_their_parts() -> None:
    assert watch.version_key("0.0.9") < watch.version_key("0.0.10")
    assert watch.version_key("2.7.0") < watch.version_key("2.10.0")
    assert watch.version_key("2.7.0") == watch.version_key("2.7.0-beta.1")


def test_current_pins_exit_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    latest = _latest_from_pins()
    monkeypatch.setattr(watch, "latest_version", lambda package: latest[package])

    assert watch.main([]) == 0

    out = capsys.readouterr().out
    assert "current" in out
    assert "BEHIND" not in out


def test_a_behind_pin_exits_one_and_names_both_versions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    latest = _latest_from_pins()
    latest["@agentclientprotocol/codex-acp"] = "99.0.0"
    monkeypatch.setattr(watch, "latest_version", lambda package: latest[package])

    assert watch.main(["--markdown"]) == 1

    out = capsys.readouterr().out
    assert "| codex | `@agentclientprotocol/codex-acp` |" in out
    assert "99.0.0" in out
    assert "session/load" in out


def test_an_unreadable_registry_exits_two_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(package: str) -> str:
        raise OSError("registry is down")

    monkeypatch.setattr(watch, "latest_version", fail)

    assert watch.main([]) == 2
    assert "registry is down" in capsys.readouterr().err
