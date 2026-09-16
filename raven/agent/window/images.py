"""The pictures a turn carries: where each came from, and the note left when one goes.

Two conventions the loop and the window share. ``ATTACHED_IMAGE_KEY`` and
``IMAGE_SOURCES_KEY`` mark, on a message, that the loop attached these pictures
and which tool showed them in which round; ``withdrawn_image_note`` is the text
the window writes in a picture's place when it is withdrawn, and
``filed_image_note`` is that note reduced to the facts a later session may keep.
One module for both because the note is composed from the provenance, and its
filed form is parsed back out of the note -- three readers, one grammar.
"""

from __future__ import annotations

import re
from typing import Any

from raven.utils.images import is_image_part, is_inline_image

# Marks the synthetic user message that carries images a transport cannot put in
# a tool result. Not persisted: the tool result above it already names the file
# path, so the only thing this message would add to the transcript is a user turn
# saying "[image]" that the user never sent -- misleading on resume and in
# session export. Deliberately a different key from ``_recovery_synthetic``:
# that one marks empty-response recovery scaffolding, and collapsing the two
# would make either meaning impossible to reason about separately.
ATTACHED_IMAGE_KEY = "_attached_image"
#: Where each picture in a message came from, one entry per image part in content
#: order: ``{"tool", "iteration", "caption"}``. Stamped when the picture enters the
#: transcript and read when it leaves it, so the note that stands in for a withdrawn
#: picture can say which tool showed which page in which round. Not sent (the
#: provider allow-list drops it) and not filed in the session (``_save_turn`` pops
#: it): the picture lives for the turn only, and so does its provenance. The
#: tracing ``llm.input`` artifact records the request as handed to the provider,
#: before the allow-list, so on a stock install the entries do appear there,
#: beside the pictures they describe.
IMAGE_SOURCES_KEY = "_image_sources"

_CAPTION_MAX_CHARS = 120


def image_sources(tool_name: str, blocks: list[Any], iteration: int) -> list[dict[str, Any]]:
    """Provenance for every image part in ``blocks``, in content order.

    A tool that returns pictures writes a text part in front of each saying what
    it shows (``Page 7 of 20: ...``, ``[image: shot.png] | 300x200px``); its first
    line is the caption, and it stands for every picture up to the next text part.
    Read off the blocks as the tool returned them: before the trust fence wraps
    the text, and before a transport that cannot carry a picture in a tool result
    separates it from the line that named it.
    """
    out: list[dict[str, Any]] = []
    caption: str | None = None
    for part in blocks:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text") or ""
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            caption = first[:_CAPTION_MAX_CHARS] or None
        elif is_image_part(part):
            out.append({"tool": tool_name, "iteration": iteration, "caption": caption})
    return out


def wire_image_bytes(part: Any) -> int:
    """What an inline picture costs the request, 0 for anything else (a remote
    reference has no known size).

    The base64 payload, not the bytes it decodes to. What the budget above it is
    protecting is the request body, and a data URI travels encoded: counting the
    decoded side let 11.24MB of pictures pass a 12MB budget while 16.8MB left the
    process, which the gateway answered with an empty 200 and zero usage five times
    over on a byte-identical payload.
    """
    if not is_inline_image(part):
        return 0
    return len(part["image_url"]["url"].partition(",")[2])


# The opening words of every reason a withdrawal note can give. The composer starts
# each reason with one of these and the filed-form parser finds the end of the facts
# by them, so a caption may contain ". " without being cut short. A reason worded
# past them would file the whole note into the session unnoticed, which is why the
# composer takes its openings from here rather than spelling them out again.
_REFUSED = "The endpoint refused"
_ELIDED = "It was elided"
_OUTGREW = "This turn's pictures"
_SEEN = "You looked at it"
_WITHDRAWAL_OPENINGS = (_REFUSED, _ELIDED, _OUTGREW, _SEEN)
_WITHDRAWN_NOTE_FACTS = re.compile(
    r"^(\[image no longer in context: .*?)\. (?:" + "|".join(map(re.escape, _WITHDRAWAL_OPENINGS)) + r").*\]$",
    re.DOTALL,
)


def filed_image_note(text: str) -> str:
    """The note as it is filed in the session: the facts, not the turn's reasons.

    The live note says why the picture left and how to ask for it again, which is
    this turn's business ("this turn's pictures outgrew their byte budget"); a
    resumed session would replay that as current. The part that stays true across
    turns is which picture, which tool and which round, so that is what is kept.
    A text that is not a withdrawal note is returned unchanged.
    """
    match = _WITHDRAWN_NOTE_FACTS.match(text)
    return f"{match.group(1)}.]" if match else text


def withdrawn_image_note(source: dict[str, Any], *, index: int, total: int, reason: str, keep: int) -> str:
    """The text that stands where a picture was, once the picture is withdrawn.

    Says what the picture showed, who showed it and when, why it is gone, and how
    to see it again -- so the model reads "I looked at page 7 already and can ask
    for it again" rather than a bare "elided" it has to guess about. ``reason`` is
    ``budget`` (the turn's pictures outgrew their byte budget and collapsed to the
    newest few), ``superseded`` (a fixed count moved past it), ``refused`` (the
    endpoint would not take the request's pictures) or ``context`` (an overflow).
    Written once, when the picture leaves; the same inputs always give the same
    text.
    """
    caption = source.get("caption")
    tool = source.get("tool")
    iteration = source.get("iteration")
    what = f'"{caption}"' if caption else f"picture {index} of {total}"
    origin = f" from {tool}" if tool else ""
    when = f" at iteration {iteration}" if iteration is not None else ""
    again = f"ask {tool} for it again" if tool else "ask for it again"
    if reason == "refused":
        why = f"{_REFUSED} this request's pictures as too large."
    elif reason == "context":
        why = f"{_ELIDED} to fit the context window."
    elif reason == "budget":
        why = f"{_OUTGREW} outgrew their byte budget, so only the {keep} newest image-bearing result(s) keep theirs."
    elif keep == 0:
        why = f"{_REFUSED} this turn's pictures as too large, so none are kept in context now."
    else:
        why = f"{_SEEN} when it arrived; only the {keep} newest image-bearing result(s) keep their pictures."
    return f"[image no longer in context: {what}{origin}{when}. {why} To see it again, {again}]"
