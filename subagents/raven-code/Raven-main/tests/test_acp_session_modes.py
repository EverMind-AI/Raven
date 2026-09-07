"""Session modes (raven/acp/modes.py): the catalogue a client picks from, and
which entry each session is on.

Pure state, no protocol: what a session starts on, what a switch does, what the
``SessionModeState`` on the wire looks like, and which declarations refuse to
build -- at startup, before any client has been shown the menu.
"""

from __future__ import annotations

import pytest

from raven.acp.modes import build_session_modes
from raven.config.schema import Config

CATALOGUE = {
    "low": {"name": "Low", "description": "quick", "reasoningEffort": "low"},
    "high": {"name": "High", "description": "today's behaviour"},
    "max": {"name": "Max", "reasoningEffort": "max"},
}


def _build(modes: dict | None, default: str | None = None):
    block: dict = {}
    if modes is not None:
        block["modes"] = modes
    if default is not None:
        block["defaultMode"] = default
    return build_session_modes(Config.model_validate({"acp": block}))


def test_nothing_declared_is_no_surface() -> None:
    modes = _build(None)
    assert not modes.enabled
    assert modes.ids() == ()
    assert modes.state("s") is None
    assert modes.profile("s") is None


def test_the_default_is_the_declared_one_or_the_first() -> None:
    assert _build(CATALOGUE, "high").default == "high"
    assert _build(CATALOGUE).default == "low"


def test_ids_keep_their_declaration_order() -> None:
    assert _build(CATALOGUE, "high").ids() == ("low", "high", "max")


def test_an_undeclared_default_refuses_to_build() -> None:
    with pytest.raises(ValueError, match="defaultMode"):
        _build(CATALOGUE, "turbo")


def test_a_blank_mode_id_refuses_to_build() -> None:
    with pytest.raises(ValueError, match="mode id"):
        _build({"": {"name": "Nameless"}})


def test_a_blank_effort_refuses_to_build() -> None:
    """A mode declared to move the effort to nothing is a typo, not a profile."""
    with pytest.raises(ValueError, match="reasoningEffort"):
        _build({"odd": {"name": "Odd", "reasoningEffort": "  "}})


def test_a_session_starts_on_the_default_and_switches() -> None:
    modes = _build(CATALOGUE, "high")
    assert modes.current("s") == "high"
    assert modes.profile("s").reasoning_effort is None

    modes.set("s", "low")

    assert modes.current("s") == "low"
    assert modes.profile("s").reasoning_effort == "low"
    assert modes.current("other") == "high"


def test_an_unknown_mode_is_a_key_error_the_caller_translates() -> None:
    modes = _build(CATALOGUE, "high")
    with pytest.raises(KeyError):
        modes.set("s", "turbo")
    assert modes.current("s") == "high"


def test_forgetting_a_session_returns_it_to_the_default() -> None:
    modes = _build(CATALOGUE, "high")
    modes.set("s", "max")
    modes.forget("s")
    assert modes.current("s") == "high"
    modes.forget("never-seen")


def test_the_wire_state_names_current_and_available() -> None:
    modes = _build(CATALOGUE, "high")
    modes.set("s", "max")
    assert modes.state("s") == {
        "currentModeId": "max",
        "availableModes": [
            {"id": "low", "name": "Low", "description": "quick"},
            {"id": "high", "name": "High", "description": "today's behaviour"},
            {"id": "max", "name": "Max", "description": ""},
        ],
    }
