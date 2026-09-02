"""A route, named: which stages run, on what backend, with what permissions.

This is the extension point the predecessor lacked. There, the route was a
boolean threaded through an 8129-line module, and `build_ppt_tools()` had ten
`if not free_composition` branches; a third route would have multiplied them.
Here a route is a value. Adding one means registering a profile and writing the
stages it does not already share -- no existing branch grows a third case.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raven_ppt.contracts.capability import Capabilities


@dataclass(frozen=True)
class StageSpec:
    """One step of a route, and the tool the model reaches it through.

    `tool` is None for a stage the pipeline runs itself -- publication, which
    happens inside the build rather than through a call the model makes.
    """

    name: str
    tool: str | None = None
    required: bool = True


@dataclass(frozen=True)
class Profile:
    """A complete route."""

    name: str
    backend: str
    stages: tuple[StageSpec, ...]
    capabilities: Capabilities = field(default_factory=Capabilities)
    # Finding kinds that refuse publication. Everything else warns and comes back
    # to the author with the renders.
    blocking_kinds: frozenset[str] = field(default_factory=frozenset)
    skill: str | None = None

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError(f"profile {self.name} has no stages")
        names = [s.name for s in self.stages]
        if len(names) != len(set(names)):
            raise ValueError(f"profile {self.name} repeats a stage: {names}")
        # The two prohibitions, checked where a profile is built rather than
        # only in the suite, so a profile constructed at runtime cannot open
        # them either.
        if self.capabilities.physical_geometry or self.capabilities.font_size:
            raise ValueError(
                f"profile {self.name} would let the model write physical geometry or a font size; "
                "those stay with the engine in every route"
            )

    def stage(self, name: str) -> StageSpec | None:
        return next((s for s in self.stages if s.name == name), None)

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(s.tool for s in self.stages if s.tool)
