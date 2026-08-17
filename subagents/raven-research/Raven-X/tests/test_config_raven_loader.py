"""Tests for ``load_raven_config`` reading Raven extension blocks
(``sentinel`` / ``skill_forge`` / ``context`` / ``token_wise``) from the
same JSON file as the base Config.

Pre-fix the loader silently ignored the extension keys — every install
got default ``SkillForgeConfig`` regardless of what the user wrote.
These tests pin the post-fix behavior so it doesn't regress.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.config import raven as ec_module


def _write_config(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture
def stub_config_path(monkeypatch, tmp_path: Path):
    """Redirect ``get_config_path()`` (used by both base loader and the
    new extension-block reader) to a tmp file we control per test."""
    p = tmp_path / "config.json"

    def _stub() -> Path:
        return p

    # Both call sites read the symbol directly from their own module
    # namespace, so we have to patch in both places.
    monkeypatch.setattr("raven.config.loader.get_config_path", _stub)
    monkeypatch.setattr("raven.config.raven.get_config_path", _stub)
    return p


def test_missing_config_falls_through_to_defaults(stub_config_path) -> None:
    # No file on disk — both base + extensions should be defaults.
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.enabled is True
    assert cfg.skill_forge.top_k == 5
    assert cfg.sentinel is not None  # exists with defaults


def test_skill_forge_block_loaded_from_snake_case(stub_config_path: Path) -> None:
    _write_config(
        stub_config_path,
        {
            "skill_forge": {
                "enabled": True,
                "top_k": 3,
                "reranker_enabled": False,
            },
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.enabled is True
    assert cfg.skill_forge.top_k == 3
    assert cfg.skill_forge.reranker_enabled is False


def test_skill_forge_block_loaded_from_camel_case(stub_config_path: Path) -> None:
    """Match the format ``raven onboard`` writes (camelCase via
    ``model_dump(by_alias=True)``)."""
    _write_config(
        stub_config_path,
        {
            "skillForge": {
                "enabled": True,
                "topK": 7,
                "rerankerEnabled": False,
            },
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.enabled is True
    assert cfg.skill_forge.top_k == 7
    assert cfg.skill_forge.reranker_enabled is False


def test_explicit_null_falls_back_to_defaults(stub_config_path: Path) -> None:
    """A user editing config and leaving ``"skill_forge": null`` must not
    crash the loader — treat as 'use defaults'."""
    _write_config(stub_config_path, {"skill_forge": None})
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.enabled is True  # default


def test_only_specified_block_overrides(stub_config_path: Path) -> None:
    """Setting just ``skill_forge`` shouldn't disturb sentinel."""
    _write_config(
        stub_config_path,
        {
            "skill_forge": {"enabled": True},
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.enabled is True
    # Sentinel untouched → default.
    sentinel_default = type(cfg.sentinel)()
    assert cfg.sentinel == sentinel_default


def test_mass_library_db_path_round_trips(stub_config_path: Path) -> None:
    """The string lands in skill_forge.mass_library_db verbatim — used
    by ``SkillService.__init__`` to attach the mass-pool SQLite file."""
    _write_config(
        stub_config_path,
        {
            "skill_forge": {
                "enabled": True,
                "massLibraryDb": "/tmp/some/path/skills.db",
            },
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.mass_library_db == "/tmp/some/path/skills.db"


def test_invalid_json_falls_through(stub_config_path: Path) -> None:
    stub_config_path.write_text("{ this is not valid json", encoding="utf-8")
    cfg = ec_module.load_raven_config()
    # Doesn't raise; uses defaults.
    assert cfg.skill_forge.enabled is True


def test_everos_under_skill_forge(stub_config_path: Path) -> None:
    """The everos block now lives under skill_forge."""
    _write_config(
        stub_config_path,
        {
            "skill_forge": {
                "enabled": True,
                "everos": {
                    "enabled": True,
                    "max_skills_top_k": 6,
                },
            },
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.everos.enabled is True
    assert cfg.skill_forge.everos.max_skills_top_k == 6


def test_everos_camel_case_under_skill_forge(
    stub_config_path: Path,
) -> None:
    _write_config(
        stub_config_path,
        {
            "skillForge": {
                "enabled": True,
                "everos": {
                    "enabled": True,
                    "maxSkillsTopK": 3,
                },
            },
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.everos.enabled is True
    assert cfg.skill_forge.everos.max_skills_top_k == 3


def test_legacy_agents_defaults_everos_skill_light_migrated(
    stub_config_path: Path,
) -> None:
    """Old configs put ``everosSkillLight`` under ``agents.defaults``;
    the loader migration relocates it under ``skillForge.everos`` so
    users don't lose their settings."""
    _write_config(
        stub_config_path,
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {"enabled": True, "maxSkillsTopK": 7},
                },
            },
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.everos.enabled is True
    assert cfg.skill_forge.everos.max_skills_top_k == 7


def test_legacy_everos_skill_light_with_retired_keys_loads_without_crash(
    stub_config_path: Path,
) -> None:
    """Regression: an old config whose agents.defaults.everosSkillLight still
    carries the retired minMessages/minToolCalls (and a retired everos block)
    must load without a ValidationError. EverOSConfig is extra='forbid', so the
    migration has to strip those keys before relocating the block."""
    _write_config(
        stub_config_path,
        {
            "agents": {
                "defaults": {
                    "everos": {"enabled": False, "baseUrl": "http://localhost:1995"},
                    "everosSkillLight": {
                        "enabled": False,
                        "minMessages": 4,
                        "minToolCalls": 2,
                        "maxSkillsTopK": 5,
                        "retireConfidence": 0.1,
                        "minQualityForSkillExtract": 0.2,
                    },
                },
            },
        },
    )
    cfg = ec_module.load_raven_config()  # must not raise
    assert cfg.skill_forge.everos.max_skills_top_k == 5
    assert cfg.skill_forge.everos.retire_confidence == 0.1


def test_new_location_wins_when_both_present(stub_config_path: Path) -> None:
    """If a user has both old and new locations set, the new one takes
    precedence — migration must not overwrite an explicit new value."""
    _write_config(
        stub_config_path,
        {
            "agents": {
                "defaults": {
                    "everosSkillLight": {"enabled": False, "maxSkillsTopK": 2},
                },
            },
            "skillForge": {
                "everos": {"enabled": True, "maxSkillsTopK": 9},
            },
        },
    )
    cfg = ec_module.load_raven_config()
    assert cfg.skill_forge.everos.enabled is True
    assert cfg.skill_forge.everos.max_skills_top_k == 9


def test_extension_keys_with_unknown_field_rejected(stub_config_path: Path) -> None:
    """Pydantic should reject unknown fields under skill_forge to catch
    typos in user config — better a loud error than silent default.
    ``_Base`` is configured ``extra='forbid'`` so the loader raises a
    ``ValidationError`` instead of silently dropping the typo."""
    _write_config(
        stub_config_path,
        {
            "skill_forge": {
                "enabled": True,
                "totally_made_up_field": "oops",
            },
        },
    )
    with pytest.raises(ValidationError, match="totally_made_up_field"):
        ec_module.load_raven_config()


def test_a_profile_suffix_cannot_smuggle_a_superseded_flow_label():
    """The supersession check matches the BASE label, not the whole string.

    Exact membership let "dr@1.9-futurex" through, so a batch ran a dr@2.0 build
    under a dr@1.9 label - the single outcome AGENTS.md 0.2 exists to prevent.
    Profile suffixes are legitimate (they name a profile, not flow semantics), so
    they must survive while the base label is still checked.
    """
    import pytest

    from raven.config.raven import DRFlowConfig

    # dr@2.3 joined the superseded set when dr@2.4 shipped (per-arm SERP-snippet
    # restoration on the candidate-selection arm); dr@2.2 joined it when dr@2.3 shipped.
    # The just-superseded label carries the highest smuggle risk (it is closest to the
    # current one), so it leads the list.
    for smuggled in (
        "dr@2.3-futurex",
        "dr@2.2-futurex",
        "dr@2.1-futurex",
        "dr@2.0-futurex",
        "dr@1.9-futurex",
        "dr@1.4-web",
        "dr@1",
    ):
        with pytest.raises(Exception):
            DRFlowConfig(enabled=True, version=smuggled)

    # The current label, its suffixed form, and a future one must survive. All derived
    # from the field default: a test that hardcodes the version is one more place a bump
    # has to reach, which is exactly the drift this check exists to catch. The literal
    # list here was the fourth such place found on 2026-08-04.
    current = DRFlowConfig.model_fields["version"].default
    major, minor = current.rsplit(".", 1)
    future = f"{major}.{int(minor) + 1}"
    for allowed in (current, f"{current}-futurex", future):
        assert DRFlowConfig(enabled=True, version=allowed).version == allowed

    # A superseded label with the flow OFF is still allowed: the anchor arm carries
    # historical labels and must stay loadable.
    assert DRFlowConfig(enabled=False, version="dr@2.0").version == "dr@2.0"


def test_the_shipped_example_configs_load_on_this_build():
    """The examples are the only surface a new user touches, and nothing else
    checked them.

    dr@2.6 superseded dr@2.5 while two of the three examples still pinned that
    label, so the first command in QUICKSTART died on our own validator - a
    correct rule, reading to a newcomer as "this repo ships broken configs". The
    full suite was green throughout: 3,916 tests covered the validator's
    behaviour and none covered the files we hand people.

    Pinning a version in an example is what created the coupling, so the assert
    below also forbids it. An arm publishing a number pins its label; an example
    inherits it and stays correct across every future bump.
    """
    import json
    from pathlib import Path

    from raven.config.raven import DRFlowConfig

    examples = sorted((Path(__file__).parent.parent / "examples").glob("*.json"))
    assert examples, "examples/ has no configs to check"

    for path in examples:
        flow = json.loads(path.read_text()).get("drFlow")
        assert flow is not None, f"{path.name}: no drFlow block"
        assert "version" not in flow, (
            f"{path.name} pins drFlow.version; examples must inherit it or they break on the next bump"
        )
        cfg = DRFlowConfig(**flow)
        assert cfg.version == DRFlowConfig.model_fields["version"].default
        # Every shaping value written out: an inherited default does not appear in
        # the file that describes the run. The expected set is derived from the model
        # rather than spelled out - dr@2.8 added a third knob, and a hardcoded pair
        # here would have kept passing while the examples silently inherited a default
        # that changes what the model reads.
        from raven.config.raven import DRFlowFinalShapeConfig

        expected = {f.alias or n for n, f in DRFlowFinalShapeConfig.model_fields.items()}
        assert set(flow.get("finalShape", {})) == expected, (
            f"{path.name}: finalShape must state all of {sorted(expected)} explicitly"
        )
