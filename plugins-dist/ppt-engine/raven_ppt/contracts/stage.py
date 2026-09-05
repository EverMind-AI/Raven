"""What a stage hands back, and what a backend must be able to do.

Note what is *not* here: a uniform stage input. Stages differ too much for one
signature to be honest about them -- ingest takes a materials directory, the
build takes a program, publication takes a destination -- and a `**kwargs`
protocol would only pretend otherwise. What the pipeline
actually needs in common is the *output*: every stage reports findings the same
way, so the gate flow and the feedback loop are written once. Backends do have
one signature, and it is declared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from raven_ppt.contracts.build import BuildOutcome
from raven_ppt.contracts.deck import DeckPlan
from raven_ppt.contracts.findings import Finding
from raven_ppt.contracts.project import Project


@dataclass(frozen=True)
class StageResult:
    """One stage's outcome, in the shape every stage shares."""

    ok: bool
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    # Stage-specific payload. Read by the tool adapter that owns this stage and
    # by nothing else, which is why it stays a mapping rather than growing a
    # union of every stage's return type.
    data: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        object.__setattr__(self, "findings", tuple(self.findings))


@runtime_checkable
class Backend(Protocol):
    """How a plan becomes a `.pptx`.

    Three implementations with nothing in common inside them -- one runs a
    program the model wrote, one compiles slot geometry through the vendored
    SVG converter, one composites type over a generated image -- and one shape
    coming out. That is what lets measurement, the gates and publication be
    written without knowing which route produced the file.
    """

    name: str

    async def compose(self, project: Project, plan: DeckPlan | None) -> BuildOutcome: ...
