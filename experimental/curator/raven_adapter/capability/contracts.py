"""Capability resource delivery and translations for Raven's tool and skill consumers."""

from dataclasses import dataclass, field

from pydantic import Field, model_validator

from raven.contracts.tool import Tool

from ..strategy import TaskBinding
from ..targets import EntryPoint

TARGET = "capability.strategy"


@dataclass(frozen=True)
class CapabilityResources:
    """Inert native tools and skill-package files returned by provide().

    Tool objects use the native Tool contract; registration never grants execution
    permission. skills maps relative package paths (including SKILL.md) to text.
    Files live under this generation's private skill root, not in agent home.
    Do not start resources here; background services use their native target.
    """

    tools: tuple[Tool, ...] = ()
    skills: dict[str, str] = field(default_factory=dict)


class CapabilityBinding(TaskBinding):
    """Translate current need and selection while preserving native authority."""

    need: EntryPoint | None = Field(
        default=None,
        description="translate(offered: list[dict], step: StepView) -> NeedT. Called before a model request with the current offered definitions. "
        "Offered definitions include installed tools subject to native selection; requesting a definition "
        "does not establish execution permission.",
    )
    expose: EntryPoint | None = Field(
        default=None,
        description="translate(selection: SelectionT) -> list[str] | None, requires need. Names narrow the offered "
        "definitions; [] exposes no tools, None preserves them. No invented names or replacement schemas. "
        "All translations are synchronous and cannot mutate strategy state.",
    )

    context: EntryPoint | None = Field(
        default=None,
        description="render(selection: SelectionT) -> str | None, requires need. Optional usage knowledge or "
        "guidance from the same selection enters the model addendum. It does not imply that a "
        "registered skill body was read. The turn wrapper shares one selection with tool exposure.",
    )

    @model_validator(mode="after")
    def selection_consumers(self):
        if (self.need is None) != (self.expose is None and self.context is None):
            raise ValueError("capability need requires expose or context, and consumers require need")
        return self
