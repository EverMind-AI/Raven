"""Schema-level guarantees for the derived pet identity profile."""

import json

import pytest
from pydantic import ValidationError

from raven.pet.models import (
    ATLAS_CELL_HEIGHT,
    ATLAS_CELL_WIDTH,
    ATLAS_HEIGHT,
    ATLAS_WIDTH,
    SCHEMA_VERSION,
    SPRITE_VERSION_NUMBER,
    ClarificationItem,
    MemorySnapshot,
    PetIdentityProfile,
    ProfileDecision,
    SafetyReport,
    TraitCandidate,
    VisualTranslation,
    slugify,
)


def _visual() -> VisualTranslation:
    return VisualTranslation(
        form="compact baby raven",
        silhouette="small rounded body with readable wings and feet",
        palette=["#252832", "#6E63A8", "#D6C56E"],
        material="soft matte plush",
        markings="one subtle violet feather edge",
        eyes="large focused eyes with restrained expression",
    )


def _profile(**overrides) -> PetIdentityProfile:
    base = dict(
        pet_id="careful-raven",
        display_name="Careful Raven",
        description="A focused little raven shaped by stable working preferences.",
        traits=[TraitCandidate(value="deliberate", confidence=0.93, evidence_refs=["profile:sha256:abc123def456"])],
        work_patterns=["tool-oriented"],
        aesthetic_preferences=["compact", "dark-neutral"],
        motifs=["raven", "small-tool"],
        avoid=[],
        visual_translation=_visual(),
        safety=SafetyReport(),
        memory_snapshot=MemorySnapshot(profile_hash="0" * 64, collected_at="2026-07-28T00:00:00Z"),
        decision=ProfileDecision(),
    )
    base.update(overrides)
    return PetIdentityProfile(**base)


def test_atlas_constants_match_codex_v2_contract():
    assert (ATLAS_CELL_WIDTH, ATLAS_CELL_HEIGHT) == (192, 208)
    assert (ATLAS_WIDTH, ATLAS_HEIGHT) == (1536, 2288)
    assert SPRITE_VERSION_NUMBER == 2


def test_profile_serializes_with_camel_case_keys():
    payload = json.loads(_profile().model_dump_json(by_alias=True))
    assert payload["schemaVersion"] == SCHEMA_VERSION
    assert payload["visualTranslation"]["stylePreset"] == "auto"
    assert payload["safety"]["rawMemoryForwarded"] is False
    assert payload["traits"][0]["evidenceRefs"] == ["profile:sha256:abc123def456"]


def test_profile_round_trips_from_camel_case_json():
    payload = _profile().model_dump(by_alias=True)
    assert PetIdentityProfile.model_validate(payload).pet_id == "careful-raven"


def test_mandatory_avoid_entries_are_always_present():
    assert set(_profile(avoid=[]).avoid) >= {"text", "logos", "real-person likeness"}


def test_avoid_list_deduplicates_and_keeps_user_entries():
    profile = _profile(avoid=["text", "text", "scenery"])
    assert profile.avoid.count("text") == 1
    assert "scenery" in profile.avoid


def test_traits_are_capped_at_five():
    traits = [TraitCandidate(value=f"trait-{i}", confidence=0.5) for i in range(9)]
    assert len(_profile(traits=traits).traits) == 5


def test_motifs_capped_at_three_and_aesthetics_at_three():
    profile = _profile(motifs=["a", "b", "c", "d"], aesthetic_preferences=["p", "q", "r", "s"])
    assert len(profile.motifs) == 3
    assert len(profile.aesthetic_preferences) == 3


def test_palette_must_hold_exactly_three_hex_colors():
    with pytest.raises(ValidationError):
        VisualTranslation(**{**_visual().model_dump(), "palette": ["#252832", "#6E63A8"]})


def test_palette_rejects_non_hex_entries():
    with pytest.raises(ValidationError):
        VisualTranslation(**{**_visual().model_dump(), "palette": ["#252832", "violet", "#D6C56E"]})


def test_palette_is_normalized_to_uppercase():
    visual = VisualTranslation(**{**_visual().model_dump(), "palette": ["#252832", "#6e63a8", "#d6c56e"]})
    assert visual.palette == ["#252832", "#6E63A8", "#D6C56E"]


def test_confidence_outside_unit_interval_is_rejected():
    with pytest.raises(ValidationError):
        TraitCandidate(value="deliberate", confidence=1.4)


def test_unknown_style_preset_is_rejected():
    with pytest.raises(ValidationError):
        VisualTranslation(**{**_visual().model_dump(), "style_preset": "photoreal"})


def test_profile_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        PetIdentityProfile.model_validate({**_profile().model_dump(by_alias=True), "rawMemory": "secret"})


def test_clarification_requires_a_known_reason():
    assert ClarificationItem(question="Which mood?", reason="conflict").options == []
    with pytest.raises(ValidationError):
        ClarificationItem(question="Which mood?", reason="vibes")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Careful Raven", "careful-raven"),
        ("  Mixed   Case  ", "mixed-case"),
        ("weird!!name??", "weird-name"),
        ("---leading-and-trailing---", "leading-and-trailing"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slugify_rejects_input_without_alphanumerics():
    with pytest.raises(ValueError):
        slugify("###")
