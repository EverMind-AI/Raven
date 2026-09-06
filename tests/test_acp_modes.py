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


def test_an_explicitly_empty_catalogue_means_no_surface():
    config = Config()
    config.acp = AcpConfig(modes={})
    modes = build_session_modes(config)
    assert not modes.enabled
    assert modes.state("s1") is None
    assert modes.profile("s1") is None


def test_the_default_tier_is_a_rung_of_the_ladder():
    """Two constants that must agree: a `DEFAULT_TIER` outside `TIER_LADDER` would
    make every default install start on a mode the clamp cannot rank."""
    from raven.config.schema import DEFAULT_TIER, TIER_LADDER

    assert DEFAULT_TIER in TIER_LADDER


def test_an_untouched_config_serves_the_three_built_in_tiers():
    modes = build_session_modes(Config())
    assert modes.enabled
    assert modes.ids() == ("medium", "high", "max")
    assert modes.default == "high"


def test_the_built_in_tiers_are_inert_for_raven_itself():
    profile = build_session_modes(Config()).profile("s1")
    assert profile.max_iterations is None
    assert profile.overlay == {}


def test_a_declared_catalogue_replaces_the_built_in_one():
    config = Config()
    config.acp = AcpConfig(modes={"turbo": AcpModeConfig(name="Turbo")})
    modes = build_session_modes(config)
    assert modes.ids() == ("turbo",)
    assert modes.default == "turbo", "an unknown default degrades to the first declared"


def test_a_custom_catalogue_containing_high_still_degrades_to_its_first_entry():
    """The built-in default must not leak into a catalogue that never asked for it.

    `test_a_declared_catalogue_replaces_the_built_in_one` passes on a catalogue of
    `turbo` alone only because the built-in default names a rung it does not have.
    A deployment whose own vocabulary happens to include `high` is the case that
    tells the two apart: it named no default, so it must start on its first entry.
    """
    config = Config()
    config.acp = AcpConfig(modes={"fast": AcpModeConfig(name="Fast"), "high": AcpModeConfig(name="High")})
    modes = build_session_modes(config)
    assert modes.ids() == ("fast", "high")
    assert modes.default == "fast", "no default was named, so the first declared entry stands"


def test_naming_a_default_on_a_custom_catalogue_is_still_honoured():
    config = Config()
    config.acp = AcpConfig(
        modes={"fast": AcpModeConfig(name="Fast"), "high": AcpModeConfig(name="High")}, default_mode="high"
    )
    assert build_session_modes(config).default == "high"


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


def test_each_built_in_rung_says_what_distinguishes_it():
    """One row, one fact. The descriptions used to be "Sub-agents run at their
    <tier> tier. Raven's own effort is the same in every mode." -- a first half
    restating the name the row already shows, and a second half identical on all
    three, so a picker printed one fact three times and the scope once more.

    Position is the only thing that actually differs between the built-in rungs:
    they have no per-tier behaviour beyond their order and the per-agent clamp.
    So that is what each says, and the scope sentence is stated once by whichever
    surface draws the control.
    """
    modes = build_session_modes(Config())
    described = {p.id: p.description for p in modes.profiles()}

    assert len(set(described.values())) == 3, f"each rung needs its own sentence, got {described}"
    for tier, text in described.items():
        assert "same in every mode" not in text, f"{tier} still carries the shared sentence"
        assert tier not in text.lower(), f"{tier} restates the name its row already shows"
        assert text, f"{tier} has no description"


def test_the_built_in_descriptions_are_translated_but_a_deployments_are_not():
    """Raven owns its own three rows and translates them. It does not presume to
    translate a catalogue somebody else authored -- that text is in whatever
    language its author wrote, and passing it through `t()` would look up an id
    nobody registered while implying we own the words.
    """
    from raven import i18n

    before = i18n.current_language()
    try:
        i18n.set_language("zh")
        builtin = {p.id: p.description for p in build_session_modes(Config()).profiles()}
        assert all(any("\u4e00" <= ch <= "\u9fff" for ch in text) for text in builtin.values()), (
            f"raven's own rows should be Chinese under zh, got {builtin}"
        )

        own = Config()
        own.acp = AcpConfig(modes={"turbo": AcpModeConfig(name="Turbo", description="as fast as it goes")})
        assert [p.description for p in build_session_modes(own).profiles()] == ["as fast as it goes"]
    finally:
        i18n.set_language(before)


def test_no_rung_claims_a_default_the_config_can_move():
    """The rows describe rungs; the catalogue's default is a deployment's to set.

    A config may keep the built-in rungs and still name its own default -- omit
    `modes`, set `defaultMode: medium` -- and then a row saying "where every
    session starts" contradicts the `*` marker beside another row in the same
    menu. Reported by chandler.zhang against `611bb5f5`.
    """
    moved = Config(acp=AcpConfig(default_mode="medium"))
    catalogue = build_session_modes(moved)
    assert catalogue.default == "medium", "the premise: a built-in catalogue with a moved default"

    for profile in catalogue.profiles():
        assert "session starts" not in profile.description, f"{profile.id} claims a default it does not hold"
        assert "default" not in profile.description.lower(), f"{profile.id} describes the default rather than the rung"
