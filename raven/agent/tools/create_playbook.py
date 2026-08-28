"""``create_playbook`` — capture a procedure from the conversation as a playbook.

The conversational half of the creation story (the other is ``raven playbook
create``): when the user asks to save a workflow they just described or ran,
the agent hands the description to the same generator the CLI uses, and the
product lands in the same place under the same rules — the user layer of the
library, enabled and handed to the live runtime as soon as it passes validation.

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
        *,
        adopt: Callable[[str], bool] | None = None,
    ) -> None:
        self._generator = generator
        self._store = store
        #: Hands the written playbook to the live library. Optional because the
        #: CLI's creation entry has no runtime to hand it to -- the next process
        #: reads the file anyway.
        self._adopt = adopt

    @property
    def name(self) -> str:
        return "create_playbook"

    @property
    def description(self) -> str:
        return (
            "Save a recurring multi-step procedure as a playbook, so the same work can be "
            "run the same way later. Use it when the user wants to keep a workflow -- 'save "
            "this as a playbook', 'make this repeatable'. Describe the whole procedure from "
            "the conversation: the steps and their order, which results feed which steps, the "
            "parameters that change per run, and the words someone would use when they want "
            "this done (those make it easier to find later, they do not run it). It is usable "
            "as soon as it is written -- tell them where it landed and what it will do, and do "
            "not run it unless they ask."
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

        from raven.playbook import PlaybookExistsError, PlaybookGenerationError
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
            return (
                f"Error: playbook generation failed: {exc}. Do not write directly into the Playbook "
                "library or report success; a Playbook is usable only after its official validation passes."
            )
        spec = generated.spec.model_copy(update={"name": name})
        try:
            path = self._store.save(spec, notes=generated.notes)
        except PlaybookExistsError:
            return (
                f"Error: playbook {name!r} was created by another request while this one was generating. "
                "The existing file was kept; review it or choose another name."
            )
        # Enabled on arrival. It used to be written onto the deny list for the
        # user to review, which read as caution and behaved as a dead end: the
        # only way back off that list was a CLI command, and the runtime had read
        # the list once at start, so even running it did nothing until the next
        # process. Disabling is still a real need and `raven playbook disable`
        # still serves it -- it is just not the state a fresh playbook starts in.
        adopted = self._adopt(name) if self._adopt is not None else True
        notes = "".join(f"\n- {n}" for n in generated.notes)
        review = f"\nOpen questions for the user's review:{notes}" if notes else ""
        if not adopted:
            # Written but unreadable: say which half happened, because "created"
            # alone would send the caller to load a name that cannot resolve.
            return (
                f"Created playbook {name!r} at {path}, but it could not be loaded back, so it is "
                f"not available in this conversation. Ask the user to check the file.{review}"
            )
        return (
            f"Created playbook {name!r} at {path}, and it is available now -- `load_playbook` "
            f"can run it in this conversation, or `raven playbook run {name}` from a shell. "
            f"Tell the user where it landed and what it does; do not run it unless they "
            f"ask.{review}"
        )
