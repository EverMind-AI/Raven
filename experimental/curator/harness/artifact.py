"""Plans and native payloads passed between generation, checks and assembly."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator

from .state import StateUse


def relative_path(value: str) -> str:
    """Accept a canonical path within an artifact, without touching the filesystem."""
    if (
        not value
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(char in value for char in ("\\", ":", "\x00"))
    ):
        raise ValueError("expected a relative POSIX artifact path without empty, dot or parent segments")
    return value


ArtifactPath = Annotated[str, AfterValidator(relative_path)]


class Change(BaseModel):
    """One selected host target and the behavior its change should improve."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    verification: str = Field(min_length=1)


class Selection(BaseModel):
    """An initial diagnosis and chosen entries, before their mechanism is designed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    understanding: str = Field(min_length=1)
    targets: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_targets(self) -> "Selection":
        if len(self.targets) != len(set(self.targets)):
            raise ValueError("each selected target must occur once")
        return self


class Plan(BaseModel):
    """Selection lives in changes; state descriptions belong to the proposed mechanism."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    understanding: str = Field(min_length=1)
    design: str = Field(
        default="", description="Concrete mechanism, collaboration, state ownership and failure behavior."
    )
    changes: tuple[Change, ...] = ()
    state: tuple[StateUse, ...] = ()
    node_reasons: dict[ArtifactPath, Annotated[str, Field(min_length=1)]] = Field(
        default_factory=dict,
        description="For a composed root, explain each node's keep/change/withdraw decision. Requirements determine delegation; reasons are analysis, not another management switch.",
    )

    @model_validator(mode="after")
    def unique_targets(self) -> "Plan":
        if self.changes and not self.design.strip():
            raise ValueError("a changed Harness requires a concrete design")
        names = [change.target for change in self.changes]
        if len(names) != len(set(names)):
            raise ValueError("each target must occur once; combine its changes in one payload")
        resources = [state.resource for state in self.state]
        if len(resources) != len(set(resources)):
            raise ValueError("each state resource must have one description")
        return self


class Artifact(BaseModel):
    """Native values and supporting files; this model does not load or execute them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: dict[str, JsonValue] = Field(
        description="All selected target payloads belong inside this object, keyed by their exact target names. "
        "Target names must not be siblings of values.",
    )
    files: dict[ArtifactPath, str] = Field(
        default_factory=dict,
        description="Supporting files of the authored package: relative path to complete content, for example "
        "'planning_impl.py' to its full Python source. Every module a value references as module:attribute must be "
        "here or already authored; files omitted from an update stay as they are.",
    )
    remove: tuple[str, ...] = Field(
        default=(),
        description="Explicitly retire selected, currently authored target bindings. Omission preserves an existing binding.",
    )

    remove_paths: dict[str, tuple[ArtifactPath, ...]] = Field(
        default_factory=dict,
        description="Explicitly retire currently authored paths in selected content targets. Supply that target's value, which may be empty. Other omitted paths remain authored.",
    )

    @model_validator(mode="after")
    def distinct_file_paths(self) -> "Artifact":
        supplied = self.values.model_fields_set if isinstance(self.values, BaseModel) else self.values.keys()
        if len(self.remove) != len(set(self.remove)) or set(self.remove) & supplied:
            raise ValueError("retire each target once and do not also supply its value")
        for target, paths in self.remove_paths.items():
            if not paths or len(paths) != len(set(paths)):
                raise ValueError("retired content paths must be nonempty and unique")
            if target in self.remove or target not in supplied:
                raise ValueError("path retirement requires a supplied target value")
            value = self.values[target] if isinstance(self.values, dict) else getattr(self.values, target)
            if not isinstance(value, dict) or set(paths) & value.keys():
                raise ValueError("cannot write and retire the same content path")
        for path in self.files:
            parents = PurePosixPath(path).parents
            if any(str(parent) in self.files for parent in parents):
                raise ValueError(f"artifact file conflicts with a parent file: {path}")
        return self


@dataclass(frozen=True)
class Candidate:
    """Host-attached baseline identity alongside a plan and its schema-checked artifact."""

    baseline: str
    contract_id: str
    plan: Plan
    artifact: Artifact


@dataclass(frozen=True)
class Validation:
    """Host check failures and observed evidence, shared with generation."""

    errors: list[str]
    observations: list[dict]

    @property
    def passed(self) -> bool:
        return not self.errors
