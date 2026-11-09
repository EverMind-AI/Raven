"""How the window is divided: the parts of one request's prompt.

A window is one number -- how full it is -- and a reader who asks what is
filling it needs the parts: the system prompt, the tool schemas, the
conversation, and what this turn recalled on top of it. This measures the
parts apart on the request that was sent, so a page can say which of them
grew.

Each part is measured on its own content and never as "what is left of the
whole": the estimator caches a prompt it has already counted and counts a
fragment of it differently, so a part measured by subtraction comes out empty
on a short turn and wrong on a long one.
"""

from __future__ import annotations

from typing import Any

from raven.utils.tokens import estimate_prompt_tokens

SYSTEM = "system"
TOOLS = "tools"
CONVERSATION = "conversation"
MEMORY = "memory"
SKILLS = "skills"

# The parts, in the order a window fills: what is fixed first, then the
# conversation, and last what this turn added on top of it.
PARTS = (SYSTEM, TOOLS, CONVERSATION, MEMORY, SKILLS)


def measure_parts(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    turn_scoped_parts: dict[str, str] | None = None,
) -> dict[str, int]:
    """The prompt's tokens, by part.

    The system prompt is the request's system message; the tools are the
    schemas sent beside the messages; the memory and the skills are the two
    segments the turn recalled and routed -- each one's own text, as the
    assembler split it -- counted apart because they are what a turn adds on
    top of the talk. The conversation is what the rest of the prompt is.

    The recalled text is taken out of wherever it rides: the turn's scratch at
    the end of the user message by default, the system prompt when
    ``context.cache.turn_scratch`` is off.

    A part that is not there is zero rather than missing: an empty prompt has
    no tools, and a reader asking what is filling the window is answered the
    same way either way.
    """
    scoped = turn_scoped_parts or {}
    memory_text = scoped.get(MEMORY) or ""
    skills_text = scoped.get(SKILLS) or ""
    cut = (memory_text, skills_text)
    system_messages = [_cut(m, cut) for m in messages if m.get("role") == "system"]
    rest = [_cut(m, cut) for m in messages if m.get("role") != "system"]
    return {
        SYSTEM: estimate_prompt_tokens(system_messages) if system_messages else 0,
        TOOLS: estimate_prompt_tokens([], tools) if tools else 0,
        CONVERSATION: estimate_prompt_tokens(rest) if rest else 0,
        MEMORY: _text_tokens(memory_text),
        SKILLS: _text_tokens(skills_text),
    }


def _cut(message: dict[str, Any], cut: tuple[str, ...]) -> dict[str, Any]:
    """The message without the recalled text, wherever the text sits in it.

    What is left of the scratch around the recalled segments -- its tag, its
    separators -- stays: it is the host's overhead, and the host's own words
    are the conversation's.
    """
    texts = [t for t in cut if t]
    if not texts:
        return message
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": _cut_text(content, texts)}
    if isinstance(content, list):
        return {**message, "content": [_cut_block(b, texts) for b in content]}
    return message


def _cut_text(text: str, texts: list[str]) -> str:
    for one in texts:
        text = text.replace(one, "")
    return text


def _cut_block(block: Any, texts: list[str]) -> Any:
    if isinstance(block, dict) and isinstance(block.get("text"), str):
        return {**block, "text": _cut_text(block["text"], texts)}
    return block


def _text_tokens(text: str) -> int:
    """The tokens of one segment's text, or none when it has none."""
    if not text:
        return 0
    return estimate_prompt_tokens([{"role": "user", "content": text}])
