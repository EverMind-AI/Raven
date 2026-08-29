"""Model-generated session titles: the prompt, the call, and the cleanup.

Naming a session is a side errand, not part of the turn: the caller fires this
concurrently with the turn it names and takes the mechanical title already in
place (``manager._derive_title``) whenever anything here declines to answer.
Every failure mode therefore returns ``None`` rather than raising -- a session
that could not be named is not a session that failed.

The numeric bounds here are runaway guards, not layout rules. Measured titles
run 2-21 codepoints, so ``TITLE_BUDGET`` is not what shapes a normal title; it
is what stops a model that ignored the instruction from writing a paragraph
into session metadata. How a title *fits* is the front end's business -- both
surfaces already truncate with an ellipsis and neither writes that truncation
back.
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from raven.i18n import zh_lexicon

TITLE_BUDGET = 24
"""Codepoints a generated title is asked (and clamped) to fit."""

TITLE_DISCARD_FACTOR = 2
"""Past ``TITLE_BUDGET * this`` the model plainly ignored the instruction, so
the output is dropped instead of clamped -- a clamp there would publish the
first fifth of a sentence as if it were a name."""

TITLE_STORAGE_MAX = 200
"""Ceiling on any title reaching session metadata, model-made or human-typed.
Not an aesthetic bound: it is what keeps a pasted document out of the metadata
record. Human titles are refused at this line rather than silently cut."""

_TOOL_NAME = "emit_session_title"

# A model told to answer with a title still sometimes answers with a labelled,
# quoted title. Both are recoverable formatting noise, so they are stripped
# rather than counted as a failure to follow the instruction.
_LABEL = re.compile(
    r"^\s*(?:title|session title|" + "|".join(zh_lexicon.TITLE_LABELS) + r")\s*" + zh_lexicon.COLON_CLASS + r"\s*",
    re.IGNORECASE,
)
_WRAPPING_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("\u300c", "\u300d"),
    ("\u300e", "\u300f"),
    ("\u300a", "\u300b"),
    ("(", ")"),
    ("[", "]"),
    ("**", "**"),
    ("*", "*"),
)


def collapse_to_line(text: str) -> str:
    """One line, single-spaced, no leading or trailing space.

    Applied to every title on the way in, whoever wrote it: a metadata record
    is one JSON line and a rail row is one line, so an embedded newline has
    nowhere to render and a run of spaces only ever reads as a mistake.
    """
    return " ".join(text.split())


def _unwrap(text: str) -> str:
    """Peel wrapping decoration, repeatedly: a model that emitted ``**"x"**``
    wrapped twice, and peeling one layer would leave the other on the title."""
    for _ in range(len(_WRAPPING_PAIRS)):
        for opening, closing in _WRAPPING_PAIRS:
            if len(text) > len(opening) + len(closing) and text.startswith(opening) and text.endswith(closing):
                text = text[len(opening) : -len(closing)].strip()
                break
        else:
            return text
    return text


def clean_model_title(raw: str, *, budget: int = TITLE_BUDGET) -> str | None:
    """A model's answer as a storable title, or ``None`` to keep the fallback.

    Strips the label and the wrapping quotes a model adds around an otherwise
    good title, then clamps to ``budget``. Returns ``None`` for an empty answer
    and for one past ``budget * TITLE_DISCARD_FACTOR`` -- see
    ``TITLE_DISCARD_FACTOR`` for why that case is dropped rather than cut.
    """
    text = collapse_to_line(raw)
    text = _unwrap(_LABEL.sub("", text).strip())
    text = text.rstrip("\u3002.!\uff01?\uff1f ,\uff0c;\uff1b:\uff1a")
    # A title with no letter or digit anywhere in it is decoration, not a name:
    # an empty quoted string unwraps to the quotes themselves, and a model that
    # answered "**" answered nothing. Checked after unwrapping so the pair that
    # was too short to peel is caught here instead.
    if not any(ch.isalnum() for ch in text):
        return None
    if len(text) > budget * TITLE_DISCARD_FACTOR:
        logger.debug("session title: model answered {} chars, past the discard line; keeping fallback", len(text))
        return None
    return text[:budget]


def title_tool_schema(budget: int = TITLE_BUDGET) -> list[dict[str, Any]]:
    """The single-function schema the call is constrained to.

    A tool call rather than free text for the reason the sentinel predictor
    uses one (``predictor/prompts.py``): the answer has exactly one field this
    consumes, and a schema is what keeps a cheap model from prefacing it.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": "Name this conversation with one short title.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": (
                                f"The conversation's title, at most {budget} characters "
                                f"(about {budget // 2} Chinese characters). Name what the user "
                                "wants done, in the user's own language: 'Fix the login redirect' / "
                                f"'{zh_lexicon.TITLE_EXAMPLE}'. No quotes, no trailing punctuation, "
                                "no 'Title:' prefix."
                            ),
                        },
                    },
                    "required": ["title"],
                },
            },
        }
    ]


def build_title_prompt(first_message: str, *, budget: int = TITLE_BUDGET) -> list[dict[str, str]]:
    """The two messages the call is made with.

    Only the user's first message is shown. The assistant's reply would name
    the answer rather than the request, and waiting for it would leave the
    front end's placeholder spinning for the length of a whole turn.
    """
    return [
        {
            "role": "system",
            "content": (
                "You name conversations. Read the user's opening message and call "
                f"{_TOOL_NAME} with a title of at most {budget} characters that says what "
                "they want done. Write it in the language they used. Nothing else."
            ),
        },
        {"role": "user", "content": first_message},
    ]


def extract_title(response: Any) -> str | None:
    """The ``title`` argument of the tool call, or ``None``.

    Tolerates the shapes a provider puts the arguments in -- a JSON string or a
    dict -- because neither is worth failing a turn over. An answer with no tool
    call in it yields ``None``: see below for why its prose is not salvaged.
    """
    for call in getattr(response, "tool_calls", None) or []:
        args = getattr(call, "arguments", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                logger.debug("session title: tool args not JSON: {!r}", args)
                continue
        if isinstance(args, dict) and isinstance(args.get("title"), str):
            return args["title"]
    # A model that answered in prose instead of calling the tool is NOT salvaged.
    # Its prose carries preamble the cleanup cannot see: the label pattern is
    # anchored at the start, so "Sure! Here's a title: Fix the login" survives
    # every guard and gets clamped to "Sure! Here's a title: Fi". The cheap tier
    # this config steers people toward is exactly the one that skips the tool
    # call and adds preamble, so the mechanical title is the better answer here.
    return None


async def generate_title(
    provider: Any,
    first_message: str,
    *,
    model: str | None = None,
    budget: int = TITLE_BUDGET,
) -> str | None:
    """One model call naming a session, or ``None`` on any refusal.

    ``chat_with_retry`` rather than ``chat`` for the reason the evolver judge
    uses it (``evolver/judge/llm_client.py``): it carries the empty-response
    retry the agent loop relies on, and an empty answer here is otherwise
    indistinguishable from a model that had nothing to say.
    """
    try:
        response = await provider.chat_with_retry(
            messages=build_title_prompt(first_message, budget=budget),
            tools=title_tool_schema(budget),
            model=model,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.debug("session title: generation call failed ({}); keeping fallback", exc)
        return None
    raw = extract_title(response)
    if not raw:
        # Said out loud for the same reason every other refusal here is: a
        # provider that never emits tool calls leaves the feature inert, and
        # without this line there is nothing anywhere saying why.
        logger.debug("session title: the model answered without calling the tool; keeping fallback")
        return None
    return clean_model_title(raw, budget=budget)
