"""Recovering reasoning text a backend meant to hide but couldn't.

Prompt caching answers "does this request understand the field" as
(wire x model family) -- two axes, both readable from the request. Reasoning
tagging has a third axis neither of those covers: whether the *inference
server* was started with a parser for the model's think tags. sglang and
vLLM both ship one, but it is an opt-in flag; run without it and the tag the
model was trained to emit at the start of its turn is swallowed into the
prompt template before generation even begins, so the completion that comes
back is not a well-formed ``<think>...</think>`` block but its second half:
bare reasoning prose followed by an orphaned ``</think>``, with no opening
tag anywhere in the text to pair it with.

LiteLLM has one fallback for this, ``_parse_content_for_reasoning`` in
``litellm_core_utils/prompt_templates/common_utils.py``, but it anchors on
the tag at the *start* of the string with ``re.match`` -- built for a model
that emits ``<think>reasoning</think>answer`` and drops only the wrapper, not
for a string that already begins mid-reasoning. It also runs on the
non-streaming path only. So this is where the missing half of that fallback
lives: given the shape sglang/vLLM without ``--reasoning-parser`` actually
produce, split the leading reasoning off from the answer that follows the
orphaned closing tag.

This module only ever sees text that is already whole. A streaming delta
cannot be judged as it arrives -- until the closing tag shows up there is no
way to tell "the model is still reasoning" apart from "the model already
started its answer and just writes like that", and buffering deltas on
spec to find out would turn a streaming response into a delayed one for
every model that behaves normally. So there is no delta-level counterpart
here; callers normalize once the full text is in hand, whether that text
came back in one response or was assembled from a stream.
"""

from __future__ import annotations

import re

_CLOSE_TAG_RE = re.compile(r"</think>|</thinking>", re.IGNORECASE)


def split_orphan_think(text: str) -> tuple[str | None, str]:
    """Split ``text`` into ``(reasoning, content)`` if it holds an orphan closing tag.

    An orphan is a ``</think>`` or ``</thinking>`` with no matching opening tag
    before it -- the shape produced when the server swallowed the opener into
    its prompt template. A paired block (an opener present earlier in the
    text) is left alone and returned as ``(None, text)`` unchanged, for the
    existing complete-block handlers (``_strip_think`` et al.) to take care
    of; so is text with no closing tag at all.

    The pairing check looks for the ``<think`` prefix shared by both opening
    variants, not the one matching the closing tag's own spelling -- a model
    that opens ``<think>`` and closes ``</thinking>`` (or vice versa) is still
    a paired block, and treating the mismatched opener as absent would leak it
    into the reasoning text this function returns.

    Everything before the tag becomes ``reasoning`` once stripped, unless that
    strips to nothing, in which case ``reasoning`` is ``None`` and only the
    stray tag is removed. Everything after the tag becomes ``content``, minus
    a single leading newline (the one the model wrote to separate reasoning
    from answer, not meaningful content).
    """
    match = _CLOSE_TAG_RE.search(text)
    if match is None:
        return None, text

    prefix = text[: match.start()]
    if "<think" in prefix.lower():
        return None, text

    rest = text[match.end() :]
    if rest.startswith("\n"):
        rest = rest[1:]

    reasoning = prefix.strip()
    return (reasoning or None), rest
