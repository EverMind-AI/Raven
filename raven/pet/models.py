"""Pydantic models for the derived pet identity profile and hatch run state.

Serialization uses camelCase aliases so the on-disk profile matches the contract in
docs/memory-driven-pet-hatching-design.md. Every model forbids extra keys: an unknown
field in a profile file is a corruption or an injection attempt, not a forward-compatible
extension.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = 1

ATLAS_COLUMNS = 8
ATLAS_ROWS = 11
ATLAS_CELL_WIDTH = 192
ATLAS_CELL_HEIGHT = 208
ATLAS_WIDTH = ATLAS_COLUMNS * ATLAS_CELL_WIDTH
ATLAS_HEIGHT = ATLAS_ROWS * ATLAS_CELL_HEIGHT
SPRITE_VERSION_NUMBER = 2

MAX_TRAITS = 5
MAX_MOTIFS = 3
MAX_AESTHETIC_PREFERENCES = 3
MAX_WORK_PATTERNS = 4
MAX_AVOID = 8
PALETTE_SIZE = 3

MANDATORY_AVOID = ("text", "logos", "real-person likeness")

MemoryScope = Literal["profile", "profile-and-episodes"]
StylePreset = Literal[
    "auto",
    "pixel",
    "plush",
    "clay",
    "sticker",
    "flat-vector",
    "3d-toy",
    "painterly",
]
HatchStage = Literal[
    "COLLECTING_MEMORY",
    "BUILDING_PROFILE",
    "AWAITING_CONFIRMATION",
    "GENERATING_BASE",
    "READY",
    "FAILED",
    "CANCELLED",
]
FailureClass = Literal[
    "memory-collection",
    "profile-derivation",
    "insufficient-evidence",
    "image-generation",
    "visual-semantics",
    "deterministic-validation",
]

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase kebab-case id. Raises ValueError when nothing usable remains."""
    slug = _SLUG_STRIP.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive an id from {value!r}")
    return slug


def _dedupe(values: list[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        item = " ".join(raw.split())
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) == limit:
            break
    return out


class _PetModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class TraitCandidate(_PetModel):
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)


class VisualTranslation(_PetModel):
    form: str
    silhouette: str
    palette: list[str]
    material: str
    markings: str
    eyes: str
    props: list[str] = Field(default_factory=list)
    style_preset: StylePreset = "auto"

    @field_validator("palette")
    @classmethod
    def _check_palette(cls, value: list[str]) -> list[str]:
        if len(value) != PALETTE_SIZE:
            raise ValueError(f"palette must hold exactly {PALETTE_SIZE} colors, got {len(value)}")
        for color in value:
            if not _HEX_COLOR.match(color):
                raise ValueError(f"palette entry {color!r} is not a #RRGGBB hex color")
        return [color.upper() for color in value]

    @field_validator("props")
    @classmethod
    def _cap_props(cls, value: list[str]) -> list[str]:
        return _dedupe(value, limit=2)


class ChromaKey(_PetModel):
    hex: str
    rgb: list[int]
    name: str
    selection: Literal["auto", "manual", "fallback"] = "auto"

    @field_validator("hex")
    @classmethod
    def _check_hex(cls, value: str) -> str:
        if not _HEX_COLOR.match(value):
            raise ValueError(f"{value!r} is not a #RRGGBB hex color")
        return value.upper()


class SafetyReport(_PetModel):
    excluded_categories: list[str] = Field(default_factory=list)
    raw_memory_forwarded: bool = False
    redactions: dict[str, int] = Field(default_factory=dict)


class MemorySnapshot(_PetModel):
    profile_hash: str
    collected_at: str
    recall_ids: list[str] = Field(default_factory=list)
    evidence_count: int = 0
    backend_available: bool = True


class ClarificationItem(_PetModel):
    question: str
    reason: Literal["conflict", "insufficient-evidence"]
    options: list[str] = Field(default_factory=list)


class ProfileDecision(_PetModel):
    mode: Literal["draft", "edited", "confirmed"] = "draft"
    approved_at: str | None = None


class PetIdentityProfile(_PetModel):
    schema_version: int = SCHEMA_VERSION
    pet_id: str
    display_name: str
    description: str
    traits: list[TraitCandidate] = Field(default_factory=list)
    work_patterns: list[str] = Field(default_factory=list)
    aesthetic_preferences: list[str] = Field(default_factory=list)
    motifs: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    visual_translation: VisualTranslation
    clarifications: list[ClarificationItem] = Field(default_factory=list)
    safety: SafetyReport = Field(default_factory=SafetyReport)
    memory_snapshot: MemorySnapshot
    decision: ProfileDecision = Field(default_factory=ProfileDecision)

    @field_validator("traits")
    @classmethod
    def _cap_traits(cls, value: list[TraitCandidate]) -> list[TraitCandidate]:
        ranked = sorted(value, key=lambda trait: trait.confidence, reverse=True)
        seen: set[str] = set()
        out: list[TraitCandidate] = []
        for trait in ranked:
            key = trait.value.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(trait)
            if len(out) == MAX_TRAITS:
                break
        return out

    @field_validator("motifs")
    @classmethod
    def _cap_motifs(cls, value: list[str]) -> list[str]:
        return _dedupe(value, limit=MAX_MOTIFS)

    @field_validator("aesthetic_preferences")
    @classmethod
    def _cap_aesthetics(cls, value: list[str]) -> list[str]:
        return _dedupe(value, limit=MAX_AESTHETIC_PREFERENCES)

    @field_validator("work_patterns")
    @classmethod
    def _cap_work_patterns(cls, value: list[str]) -> list[str]:
        return _dedupe(value, limit=MAX_WORK_PATTERNS)

    @field_validator("avoid")
    @classmethod
    def _merge_avoid(cls, value: list[str]) -> list[str]:
        return _dedupe([*MANDATORY_AVOID, *value], limit=MAX_AVOID)


class FailureRecord(_PetModel):
    failure_class: FailureClass
    message: str
    at: str


class HatchRunState(_PetModel):
    run_id: str
    pet_id: str
    stage: HatchStage
    preview_only: bool = True
    memory_scope: MemoryScope = "profile"
    style_preset: StylePreset = "auto"
    created_at: str
    updated_at: str
    attempts: dict[str, int] = Field(default_factory=dict)
    cancel_requested: bool = False
    input_snapshot_hash: str = ""
    base_preview_path: str | None = None
    failure: FailureRecord | None = None
