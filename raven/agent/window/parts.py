"""How the window is divided: the parts of one request's prompt.

A window is one number -- how full it is -- and a reader who asks what is
filling it needs the parts: the system prompt, the tool schemas, the
conversation, and the skill material the prompt carries. This measures the
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
# conversation, and last the memory and the skill material it carries.
PARTS = (SYSTEM, TOOLS, CONVERSATION, MEMORY, SKILLS)

# The segment names the assembler records, by the part they answer for. A
# skill is skill bytes wherever it rides: the always-on digest sits in the
# system prompt, a routed segment in the turn scratch, and the pull-mode menu
# in the user envelope. Each is cut out of its default bucket and counted here.
SEGMENTS = {
    MEMORY: ("memory",),
    SKILLS: ("active_skills", "scent", "skills"),
}

# Tool results made of skill material, by the name of the tool that produced
# them. A body a turn pulled in reaches the request as an ordinary tool
# message, and one the model reached through ``tool_call`` is named for the
# target that forwarding resolved to; counted as conversation, the cost of a
# skill would be put on the talk that used it. ``find_skill`` belongs here
# because its hits are the same catalogue lines the scent menu spends -- the
# rule is what the skill subsystem put in the prompt, not whether a body was
# read yet.
SKILL_TOOLS = frozenset({"find_skill", "read_skill", "use_skill"})


def measure_parts(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    window_parts: dict[str, str] | None = None,
) -> dict[str, int]:
    """The prompt's tokens, by part.

    The system prompt is the request's system message; the tools are the
    schemas sent beside the messages; the memory and the skills are the
    segments ``window_parts`` names, each measured on its own text; and the
    conversation is what the rest of the prompt is. Skill material arrives by
    two routes: those named segments, and the results of the skill tools.

    ``window_parts`` carries the text of every segment that answers for a part
    of its own, keyed by segment name (see :data:`SEGMENTS`). The text is taken
    out of wherever it rides: the turn's scratch at the end of the user message
    by default, the system prompt for the always-on digest and when
    ``context.cache.turn_scratch`` is off.

    A part that is not there is zero rather than missing: an empty prompt has
    no tools, and a reader asking what is filling the window is answered the
    same way either way.
    """
    scoped = window_parts or {}
    memory_texts = _texts(scoped, MEMORY)
    skill_texts = _texts(scoped, SKILLS)
    cut = (*memory_texts, *skill_texts)
    kept = [_cut(m, cut) for m in messages]
    system_messages = [m for m in kept if m.get("role") == "system"]
    rest = [m for m in kept if m.get("role") != "system"]
    skill_results = [m for m in rest if _is_skill_result(m)]
    conversation = [m for m in rest if not _is_skill_result(m)]
    return {
        SYSTEM: estimate_prompt_tokens(system_messages) if system_messages else 0,
        TOOLS: estimate_prompt_tokens([], tools) if tools else 0,
        CONVERSATION: estimate_prompt_tokens(conversation) if conversation else 0,
        MEMORY: _texts_tokens(memory_texts),
        SKILLS: _texts_tokens(skill_texts) + (estimate_prompt_tokens(skill_results) if skill_results else 0),
    }


def _texts(scoped: dict[str, str], part: str) -> tuple[str, ...]:
    """The recorded texts that answer for one part, empty ones dropped."""
    return tuple(text for name in SEGMENTS[part] if (text := scoped.get(name)))


def _texts_tokens(texts: tuple[str, ...]) -> int:
    return sum(_text_tokens(text) for text in texts)


def _is_skill_result(message: dict[str, Any]) -> bool:
    name = message.get("name")
    return message.get("role") == "tool" and isinstance(name, str) and name in SKILL_TOOLS


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
