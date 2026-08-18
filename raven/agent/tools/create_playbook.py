"""``create_playbook`` — capture a procedure from the conversation as a playbook.

The conversational half of the creation story (the other is ``raven playbook
create``): when the user asks to save a workflow they just described or ran,
the agent hands the description to the same generator the CLI uses, and the
product lands in the same place under the same rules — the user layer of the
library, on the disabled list until the user reviews and enables it. Disabled
means out of the passive matcher only; it can still be run explicitly, so the
user can try it before switching it on.

The tool takes a description, never a spec: generation, validation and repair
stay inside :class:`PlaybookGenerator`, so a model cannot write an arbitrary
graph into the library through this tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from raven.agent.tools.base import Tool

if TYPE_CHECKING:
    from raven.playbook import PlaybookGenerator, PlaybookStore


class CreatePlaybookTool(Tool):
    """Generate a playbook from a workflow description and store it for review."""

    # Generation is a few LLM calls (draft plus repair rounds).
    timeout_seconds = 180.0

    def __init__(
        self,
        generator: "PlaybookGenerator",
        store: "PlaybookStore",
        set_disabled: Callable[[str, bool], bool],
    ) -> None:
        self._generator = generator
        self._store = store
        self._set_disabled = set_disabled

    @property
    def name(self) -> str:
        return "create_playbook"

    @property
    def description(self) -> str:
        return (
            "Save a recurring multi-step procedure as a playbook the user can trigger later "
            "by just asking. Use it when the user wants to keep a workflow -- 'save this as a "
            "playbook', 'make this repeatable'. Describe the whole procedure from the "
            "conversation: the steps and their order, which results feed which steps, the "
            "parameters that change per run, and the phrases that should trigger it. The "
            "playbook is stored disabled for the user's review; tell them where it landed and "
            "that it activates on 'enable <name>'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z0-9][a-z0-9-]*$",
                    "description": "Short kebab-case name; becomes the library directory name.",
                },
                "workflow": {
                    "type": "string",
                    "description": (
                        "The full procedure in plain language: steps in order, what each step "
                        "produces and consumes, per-run parameters, trigger phrases. Everything "
                        "the run needs must be in here -- the generator sees only this text."
                    ),
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill names to pin to specific steps, when the user named some.",
                },
            },
            "required": ["name", "workflow"],
        }

    async def execute(self, name: str, workflow: str, skills: list[str] | None = None, **kwargs: Any) -> str:
        import re

        from raven.playbook import PlaybookGenerationError
        from raven.playbook.types import NAME_RE

        # Refused before anything else: the schema's ``pattern`` is not
        # enforced by the tool argument validator, the name becomes a
        # directory, and ``workflow`` carries conversation content -- the
        # standing untrusted-input boundary. The store re-checks at the
        # write; this early check just refuses before spending a generation.
        if not re.fullmatch(NAME_RE, name or ""):
            return f"Error: playbook name {name!r} must be kebab-case ({NAME_RE})."
        origin = self._store.origin_of(name)
        if origin is not None:
            return (
                f"Error: a {origin} playbook named {name!r} already exists. "
                "Pick another name, or ask to revise the existing one."
            )
        try:
            generated = await self._generator.generate(workflow, skills)
        except PlaybookGenerationError as exc:
            return f"Error: playbook generation failed: {exc}"
        spec = generated.spec.model_copy(update={"name": name})
        path = self._store.save(spec, notes=generated.notes)
        self._set_disabled(name, True)
        notes = "".join(f"\n- {n}" for n in generated.notes)
        review = f"\nOpen questions for the user's review:{notes}" if notes else ""
        return (
            f"Created playbook {name!r} at {path}. It starts disabled: it will not trigger "
            f"automatically until the user reviews the file and enables it (say the word, or "
            f"`raven playbook enable {name}`). It can already be run explicitly by name.{review}"
        )
