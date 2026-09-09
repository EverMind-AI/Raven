"""The question this turn is supposed to answer.

Two flow gates judge a draft against "the task": the draft reviewer
(``gates/verify.py``) asks whether the draft answers it, and the forced-finalize
salvage (``gates/finalize.py``) writes an answer to it from the researcher's
notes. Both used to derive it the same way - walk ``ctx.messages`` and take the
first ``role == "user"`` entry - and both were wrong in two ways that a
single-turn benchmark cannot show.

(1) In a conversation the first user message is the FIRST TURN's question, not
this one. Measured end to end on 2026-08-17: turn one asked what Serper.dev
costs, turn two asked "and what about Jina Reader?", the model researched Jina
correctly and drafted a correct Jina answer, and the reviewer - handed turn
one's question as the task - rejected it with ``0 unsupported claim(s)``,
because nothing in it was unsupported; it simply did not answer the task it was
shown. The rejection bounces the turn back with "fix exactly the listed issues
and produce the corrected final answer", so the model rewrote its answer to be
about Serper. The user asks a follow-up and gets the first question answered
again.

Every benchmark trajectory is a single turn, so first-user and this-turn's-user
are the same message there and the derivation was correct by accident. The
defect lived only on the shipped conversational surface, which has no measured
arm - and it needed one more coincidence to stay hidden: a reject naming no
unsupported claim is degraded to a pass when ``verify.strict_reject_only`` is
set, which all nine bench configs set and the shipped product config does not.
Two independent reasons the batches could not see it, either one sufficient.

(2) The task string carried the harness's own envelopes. The context builder
prepends a ``[Runtime Context …]`` block (current time, channel, chat id) to
every turn's user message, and the research memo and recovery notice wrap
themselves around that, so the reviewer was reading metadata as part of the
question it was checking the answer against.

The turn's question is captured ONCE, at loop entry, from the message the loop
itself already treats as this turn's inbound user message (the loop stamps it
as ``ctx.turn_question``). It is deliberately not re-derived later from
``ctx.messages``: by review time a rejected draft has appended its own
``{"role": "user", …}`` revision prompt, so "the last user message" would then
be harness text rather than the question. Capturing before the loop runs is what
makes the value independent of anything a hook does to the list afterwards.
"""

from __future__ import annotations

from typing import Any

from raven.agent.context import ContextBuilder
from research_flow.gates.conversation import MEMO_CLOSE, MEMO_OPEN

_RUNTIME_TAG = ContextBuilder._RUNTIME_CONTEXT_TAG


def _strip_memo(text: str) -> str:
    """Drop a leading research memo block.

    Not reusing ``conversation.strip_memo``: that one is anchored to the start
    of the string because it runs on the message as injected, while here the
    memo may already have had a recovery notice put in front of it.
    """
    start = text.find(MEMO_OPEN)
    if start < 0:
        return text
    end = text.find(MEMO_CLOSE, start)
    if end < 0:
        return text
    return (text[:start] + text[end + len(MEMO_CLOSE) :]).lstrip("\n")


def _strip_runtime_context(text: str) -> str:
    """Return what follows the ``[Runtime Context …]`` block.

    The context builder emits this block exactly once, in front of the inbound
    text, so "everything after this block" is the user's text and cutting there
    removes every envelope that sits outside it in one step rather than
    requiring one matcher per envelope. The memo is stripped before this runs
    (``user_question``), position-independently, so it is removed whether it
    was wrapped outside the block or folded into the inbound text inside it.
    Anchoring on ``startswith`` instead would have made this a no-op the moment
    a recovery notice landed in front, and a stripper that silently stops
    stripping is the failure mode this module exists to fix.

    A message that is only the block leaves nothing behind, and returning "" is
    right: the caller then falls back rather than passing metadata off as a
    question.
    """
    idx = text.find(_RUNTIME_TAG)
    if idx < 0:
        return text
    parts = text[idx:].split("\n\n", 1)
    return parts[1] if len(parts) > 1 else ""


def user_question(content: Any) -> str:
    """The text the user actually typed, with harness envelopes removed.

    Accepts either shape a user message can carry: a plain string, or the
    multimodal block list, where the envelopes are separate text blocks and the
    image blocks carry no question.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            c.get("text") or ""
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
        ]
        text = "\n\n".join(p for p in parts if p)
    else:
        return ""
    # Memo first: it is injected last and therefore sits outermost.
    return _strip_runtime_context(_strip_memo(text)).strip()


def turn_question(messages: list[dict[str, Any]] | None) -> str:
    """This turn's question, from the loop's inbound user message.

    Call at loop entry, where the last user message is this turn's - see the
    module docstring on why later is too late.
    """
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            text = user_question(m.get("content"))
            if text:
                return text
    return ""


def task_for(ctx: Any) -> str:
    """The task a flow gate judges a draft against.

    Prefers the question captured at loop entry, passed through
    ``user_question`` once more: the loop's stamp removes the runtime-context
    block but knows nothing of the research memo, which on this loop is folded
    into the inbound text before that block is added and so survives the stamp.
    The fallback covers contexts built outside the loop (a hook driven
    directly, as tests do) and keeps the pre-fix reading order - first user
    message, not last - because without the loop's stamp there is nothing to
    distinguish this turn's question from a revision prompt appended to the
    same list.
    """
    captured = user_question(getattr(ctx, "turn_question", None))
    if captured:
        return captured
    for m in getattr(ctx, "messages", None) or []:
        if isinstance(m, dict) and m.get("role") == "user":
            text = user_question(m.get("content"))
            if text:
                return text
    return ""


__all__ = ["task_for", "turn_question", "user_question"]
