"""Where a knowledge base gets a picture described.

Raven could draw an image long before it could look at one: ``tools.media``
holds the painting tool, and nothing named the other direction. This is the
other direction -- one model call that turns a figure into the text an index can
hold, because a vector store has nothing to store about an image otherwise.

The endpoint is the ``vision`` pin: a model and the provider serving it, bound
through the same :class:`~raven.providers.pool.ProviderPool` every other
subsystem pin resolves through. Unset means off, deliberately rather than
"follow the conversation's model" -- there is no conversation behind an
indexing run, and posting an image to a model that cannot see one fails at the
endpoint with a message about content parts.

The prompt is ported from RAGFlow's ``vision_llm_figure_describe_prompt``
(Apache-2.0; see NOTICES.md). Its two modes are the part worth having: a chart
becomes a list of its data points, and everything else becomes dense prose,
which are the two things a retrieval index can actually match a query against.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger

#: What a description is asked to fit. Not a layout rule -- it is what stops a
#: model that ignored the instruction from writing an essay into one chunk,
#: which would then be the one piece of the document too long to embed whole.
DESCRIPTION_BUDGET = 4000

_LANGUAGE_NAMES = {"en": "English", "zh": "Chinese"}


class VisionError(RuntimeError):
    """No model could describe the image, or the one configured refused to."""


_PROMPT = """## ROLE

You are an expert visual data analyst.

## GOAL

Analyze the image and produce a textual representation strictly based on what is
visible in the image.{context_goal}

## OUTPUT LANGUAGE

- Write all descriptions and field values in {language}.
- Preserve all visible text verbatim in its original language; do not translate it.
- Keep the required output field names exactly as specified below.
{context_sections}
## DECISION RULE (CRITICAL)

First, determine whether the image contains an explicit visual data
representation with enumerable data units forming a coherent dataset.

Enumerable data units are clearly separable, repeatable elements intended for
comparison, measurement, or aggregation, such as:

- rows or columns in a table
- individual bars in a bar chart
- identifiable data points or series in a line graph
- labeled segments in a pie chart

The mere presence of numbers, icons, UI elements, or labels does NOT qualify
unless they together form such a dataset.

## TASKS

1. Inspect the image and determine which output mode applies based on the decision rule.
{context_task}2. Follow the output rules strictly.
3. Include only content that is explicitly visible in the image.
4. Do not infer intent, functionality, process logic, or meaning beyond what is
   visually or textually shown.

## OUTPUT RULES (STRICT)

- Produce output in **exactly one** of the two modes defined below.
- Do NOT mention, label, or reference the modes in the output.
- Do NOT combine content from both modes.
- Do NOT explain or justify the choice of mode.
- Do NOT add any headings, titles, or commentary beyond what the mode requires.

## MODE 1: STRUCTURED VISUAL DATA OUTPUT

(Use only if the image contains enumerable data units forming a coherent dataset.)

Output **only** the following fields, in list form. Do NOT add free-form
paragraphs or additional sections.

- Visual Type:
- Title:
- Axes / Legends / Labels:
- Data Points:
- Captions / Annotations:

## MODE 2: GENERAL FIGURE CONTENT

(Use only if the image does NOT contain enumerable data units.)

Write the content directly, starting from the first sentence. Do NOT add any
introductory labels, titles, headings, or prefixes.

Requirements:

- Describe visible regions and components in a stable order (e.g. top-to-bottom,
  left-to-right).
- Explicitly name interface elements or visual objects exactly as they appear
  (e.g. tabs, panels, buttons, icons, input fields).
- Transcribe all visible text verbatim; do not paraphrase, summarize, or
  reinterpret labels.
- Describe spatial grouping, containment, and alignment of elements.
- Do NOT interpret intent, behavior, workflows, gameplay rules, or processes.
- Do NOT describe the figure as a chart, diagram, process, phase, or sequence
  unless such words explicitly appear in the image text.
- Avoid narrative or stylistic language unless it is a dominant and functional
  visual element.

Use concise, information-dense sentences. Do not use bullet lists or structured
fields in this mode.
"""

_CONTEXT_GOAL = """
Surrounding context may be used only for minimal clarification or
disambiguation of terms that appear in the image, not as a source of new
information."""

_CONTEXT_SECTIONS = """
## CONTEXT (ABOVE)

{above}

## CONTEXT (BELOW)

{below}
"""

_CONTEXT_TASK = "2. Use surrounding context only to disambiguate terms that appear in the image.\n"


def build_prompt(*, language: str = "English", context_above: str = "", context_below: str = "") -> str:
    """The instruction sent with the image.

    Context is a section of the prompt rather than a second message, and it is
    left out entirely when there is none: an empty "CONTEXT (ABOVE)" heading
    reads as a region the model failed to see, and the models that follow this
    prompt closely enough to be worth using are exactly the ones that would try
    to account for it.
    """
    has_context = bool(context_above.strip() or context_below.strip())
    return _PROMPT.format(
        language=language,
        context_goal=_CONTEXT_GOAL if has_context else "",
        context_sections=(
            _CONTEXT_SECTIONS.format(above=context_above.strip() or "(none)", below=context_below.strip() or "(none)")
            if has_context
            else ""
        ),
        context_task=_CONTEXT_TASK if has_context else "",
    )


def configured_language() -> str:
    """The language a description is written in: the install's own.

    Read from the config rather than from :func:`raven.i18n.current_language`,
    which is process state a caller has to have set. An indexing run reaches
    this from the gateway, a CLI reindex from somewhere else, and both should
    describe a picture in the language the person reading it chose.
    """
    try:
        from raven.config.loader import load_config

        return _LANGUAGE_NAMES.get(str(load_config().language), "English")
    except Exception:  # noqa: BLE001 - an unreadable config is not this module's to report
        return "English"


@dataclass(frozen=True)
class VisionModel:
    """The configured describer: a bound model, and what it is allowed to cost."""

    provider: Any
    model: str
    timeout_s: float = 90.0
    max_figures: int = 64
    language: str = "English"

    async def describe(self, image: bytes, *, mime: str = "", context_above: str = "", context_below: str = "") -> str:
        """One image as text, or a raise saying why there is none.

        The image is prepared before it is sent -- downscaled and re-encoded to
        the ceilings every vision endpoint enforces -- because a figure lifted
        out of a document is whatever resolution the author pasted in, and the
        endpoints that do not refuse an oversized one downscale it themselves
        and bill for the original.
        """
        from raven.utils.images import detect_image_mime, image_block, prepare_image, text_block, to_data_uri

        kind = mime or detect_image_mime(image) or "image/png"
        try:
            payload, wire_mime, _ = await asyncio.to_thread(prepare_image, image, kind)
        except Exception as exc:
            raise VisionError(f"the image could not be prepared for the model: {exc}") from exc

        prompt = build_prompt(language=self.language, context_above=context_above, context_below=context_below)
        messages = [
            {
                "role": "user",
                "content": [image_block(to_data_uri(payload, wire_mime)), text_block(prompt)],
            }
        ]
        try:
            response = await asyncio.wait_for(
                self.provider.chat_with_retry(messages=messages, model=self.model),
                timeout=self.timeout_s,
            )
        except TimeoutError as exc:
            raise VisionError(f"{self.model!r} did not answer within {self.timeout_s:g}s") from exc
        except Exception as exc:
            raise VisionError(f"{self.model!r} could not describe the image: {exc}") from exc
        return clean_description(_content_of(response))


def _content_of(response: Any) -> str:
    """The text of a chat answer, whatever shape the provider returned it in."""
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # A provider that answered in content parts. Only the text ones say
        # anything here; an image part in a reply to "describe this" is noise.
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def clean_description(raw: str, *, budget: int = DESCRIPTION_BUDGET) -> str:
    """A model's answer as an indexable description.

    Clamped rather than discarded past the budget, which is the opposite of
    what ``session/title.py`` does with an over-long title: half a description
    still describes the top half of the figure, while half a title is not a
    name. Fenced output is unwrapped -- a model asked for prose sometimes
    answers in a code block, and the fences would be indexed as content.
    """
    text = raw.strip()
    if text.startswith("```"):
        _, _, rest = text.partition("\n")
        text = rest.rpartition("```")[0].strip() or rest.strip()
    return text[:budget].strip()


def load_vision_model() -> VisionModel | None:
    """The configured describer, or ``None`` when nothing is configured.

    ``None`` rather than a raise, because an install with no vision model is an
    ordinary state: a document parses without its figures being described, and
    only an upload that is *nothing but* a picture has to fail. The two callers
    want different things from the same absence, so this reports it rather than
    deciding for them.
    """
    try:
        from raven.config.loader import load_config
        from raven.config.raven import load_raven_config
        from raven.providers.pool import ProviderPool
    except Exception:  # noqa: BLE001 - an import failure here is not this module's to report
        return None

    try:
        pin = load_raven_config().vision
    except Exception as exc:  # noqa: BLE001 - an unreadable config configures nothing
        logger.debug("knowledge: cannot read the vision pin ({}); pictures are not described", exc)
        return None
    if not pin.model:
        return None

    binding = ProviderPool(load_config).bind_pin(pin.model, pin.provider)
    if binding is None:
        # `bind_pin` has already said which half is unusable. Said again here
        # in the caller's own terms, because the consequence is this feature
        # rather than the pin: a reader whose figures stopped being described
        # is looking for this line.
        logger.warning(
            "knowledge: the vision model {!r} cannot be bound, so pictures are not described",
            pin.model,
        )
        return None
    return VisionModel(
        provider=binding.provider,
        model=binding.model,
        timeout_s=float(pin.timeout_seconds),
        max_figures=int(pin.max_figures),
        language=configured_language(),
    )


__all__ = [
    "DESCRIPTION_BUDGET",
    "VisionError",
    "VisionModel",
    "build_prompt",
    "clean_description",
    "configured_language",
    "load_vision_model",
]
