"""Visible-answer extraction across think formats.

Serving stacks that prefill the opening ``<think>`` tag deliver
assistant content as ``reasoning</think>answer`` — a closing tag with no
opener. A plain ``<think>…</think>`` regex never matches that shape, so
a think-blind consumer mistakes pure reasoning for an answer. Draft
gates and finalize triggers must instead reason about the *visible*
answer: what remains once every think shape is folded away.
"""

from __future__ import annotations

import re

_THINK_PAIR_RE = re.compile(r"<think>.*?</think>", re.S)
_CLOSE_TAG = "</think>"
_OPEN_TAG = "<think>"


def visible_answer(text: str | None, *, closing_tag_required: bool = False) -> str:
    """Answer text a reader sees once reasoning is folded away.

    Handles the three think shapes a generation can carry: well-formed
    ``<think>…</think>`` pairs, a dangling closing tag from
    template-prefilled reasoning (everything before it is reasoning),
    and an unclosed opener from a generation cut mid-think (everything
    after it is reasoning).

    ``closing_tag_required`` covers a fourth shape the other three cannot
    see: a template that prefills the opener leaves no opener *in the
    content*, so a generation cut before it emits the closing tag carries
    no tag at all — and reasoning is then indistinguishable from an
    answer by inspection alone. Under such a serving stack every complete
    turn ends up with a closing tag, so its absence means the turn never
    reached its answer. Off by default: a stack that emits no think tags
    at all would otherwise have every answer erased.
    """
    if not text:
        return ""
    had_close = _CLOSE_TAG in text
    text = _THINK_PAIR_RE.sub("", text)
    idx = text.rfind(_CLOSE_TAG)
    if idx >= 0:
        text = text[idx + len(_CLOSE_TAG) :]
    cut = text.find(_OPEN_TAG)
    if cut >= 0:
        text = text[:cut]
    if closing_tag_required and not had_close:
        return ""
    return text.strip()


def closing_tag_bar(configured: bool, reasoning_content: object | None) -> bool:
    """Effective ``closing_tag_required`` for one response.

    The closing-tag bar presumes reasoning shares the content channel
    (``reasoning</think>answer``), so a missing tag means the turn was cut
    before it reached its answer. A response that delivered its reasoning
    out-of-band -- a non-empty ``reasoning_content``, the channel LiteLLM
    keeps separate from ``content`` -- has by construction only answer text
    in ``content``, and holding it to the bar erases every complete answer.
    Measured on a live-web config over OpenRouter (channel-separated
    reasoning): 4 of 4 turns force-finalized as ``empty_visible_answer``
    while carrying a full report, and the dr@3.4 verify gate never saw a
    draft.

    Truthiness, not ``is None``: the provider normalizes an empty stream to
    ``None``, but a persisted message dict may carry either.
    """
    return configured and not reasoning_content


__all__ = ["closing_tag_bar", "visible_answer"]
