"""Session modes: the catalogue, the per-session choice, and the wire state."""

from __future__ import annotations

import pytest

from raven.acp.modes import AcpModeProfile, SessionModes, build_session_modes
from raven.config.schema import AcpConfig, AcpModeConfig, Config


def _modes() -> SessionModes:
    return SessionModes(
        {
            "fast": AcpModeProfile(id="fast", name="Fast", description="bounded", max_iterations=None),
            "deep": AcpModeProfile(id="deep", name="Deep", description="longer", max_iterations=60, overlay={"k": 10}),
        },
        default="fast",
    )


def test_no_declared_modes_means_no_surface():
    modes = build_session_modes(Config())
    assert not modes.enabled
    assert modes.state("s1") is None
    assert modes.profile("s1") is None


def test_the_default_is_the_declared_one_or_the_first():
    assert _modes().default == "fast"
    assert SessionModes({"deep": _modes().profile("deep")}, default="nope").default == "deep"


def test_a_session_starts_on_the_default_and_switches():
    modes = _modes()
    assert modes.current("s1") == "fast"
    modes.set("s1", "deep")
    assert modes.current("s1") == "deep"
    assert modes.profile("s1").max_iterations == 60
    assert modes.current("s2") == "fast", "another session is untouched"
    with pytest.raises(KeyError):
        modes.set("s1", "ultra")


def test_the_wire_state_names_current_and_available():
    modes = _modes()
    modes.set("s1", "deep")
    assert modes.state("s1") == {
        "currentModeId": "deep",
        "availableModes": [
            {"id": "fast", "name": "Fast", "description": "bounded"},
            {"id": "deep", "name": "Deep", "description": "longer"},
        ],
    }


def test_config_resolves_into_profiles_with_overlay():
    config = Config()
    config.acp = AcpConfig(
        modes={"deep": AcpModeConfig(name="Deep", max_tool_iterations=60, overlay={"sufficiency": {"minSearches": 5}})},
        default_mode="deep",
    )
    modes = build_session_modes(config)
    profile = modes.profile("any")
    assert profile.id == "deep" and profile.max_iterations == 60
    assert profile.overlay == {"sufficiency": {"minSearches": 5}}
