"""Prompt resource admission shared by authoring, validation and assembly."""

from pathlib import Path

from ...harness.declaration import Target
from ...harness.prompts import Prompt
from . import EntryPoint

TARGETS = (
    Target(
        roles=(),
        name="prompt.resources",
        contract=Prompt,
        binding="prompt.resources",
        payload=list[EntryPoint],
        channels=("model_input", "model_decision", "tool_interaction"),
        effect="Declare module:symbol references to Prompt objects whose UTF-8 templates are in Artifact.files. "
        "Input schemas and variables come from each Prompt. Rendering does not call a model or choose a role. "
        "Consumers remain in strategy code or host translations; verify actual use through execution.",
        knowledge=(Prompt, Path(__file__).resolve().parents[2] / "harness/reference/prompt-resources.md"),
    ),
)
