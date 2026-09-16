"""How the window gets smaller mid-turn: the five operations and their bounds.

A tool-heavy turn appends results for tens of iterations while the context is
assembled once, so the working transcript grows until it hits the model's
window. These are the ways it is made to fit again -- eliding older tool
bodies, moving or withdrawing pictures, summarising the head -- and the bounds
on how often a turn may pay for each. Pure over the message list (the head
summary takes the provider it is to call as an argument), so the loop that
retries and the Memory role that decides can both read them, and neither has
to import the other.

Every operation is prefix-stable where it can be (see ``window_images``): a
withdrawn message is byte for byte what it was, because the request prefix is
what an upstream cache matches.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.agent.window import compaction
from raven.agent.window.images import (
    ATTACHED_IMAGE_KEY,
    IMAGE_SOURCES_KEY,
    wire_image_bytes,
    withdrawn_image_note,
)
from raven.providers.capabilities import image_placeholder_text
from raven.utils.images import is_image_part
from raven.utils.tokens import estimate_prompt_tokens

# Max emergency context shrinks per turn before a context overflow is fatal.
MAX_COMPRESS_RETRIES = 2

# Max image demotions per turn. One is enough: a refusal is deterministic for
# the model, and the first retry also caches the verdict, so a second attempt
# would mean the failure was never about images.
MAX_IMAGE_DEMOTE_RETRIES = 1

# Max times a request refused for the size of its pictures is asked again with
# the image window closed a notch. Two: from a window of two, the second notch
# reaches zero, past which no picture is sent and a refusal cannot be about one.
MAX_IMAGE_STRIP_RETRIES = 2

# The image window. Pictures a tool showed stay in the request while all of
# them together fit the configured budget (``agents.defaults.imageWindowBudgetBytes``,
# base64 bytes as they travel, carried on ``RecoveryLimits``); when they do not,
# every image-bearing message but the newest ``IMAGE_WINDOW_RECENT_MESSAGES``
# loses its pictures at once, each replaced by a note saying what it showed and
# how to see it again. Pictures stay for as long as a turn runs otherwise, and
# one deck build reached 75 of them in a single request (26.6 MB decoded, 35.5 MB
# encoded) before OpenRouter refused it with 413, four times across two runs; the
# refusals were measured to start at about 26.3 MB decoded, i.e. 35.1 MB on the
# wire. Counting decoded was the bug: 11.24 MB of pictures passed this 12 MB
# budget on a request that put 16.8 MB on the wire, and the gateway answered it
# with an empty 200 and zero usage. So the same 12 MB now bounds the encoded side,
# which is 9 MB decoded -- tighter by a third, and affordable because a deck's page
# renders are JPEG rather than PNG by the time they are encoded. 0 turns the
# standing pass off and leaves the refusal ladder.
# A collapse rather than a per-batch slide because every withdrawal
# breaks the prefix an upstream cache can match: sliding cost 16 breaks in 60
# calls and 20 points of cache hit rate on one measured deck, collapsing costs
# one to four per deck on the same sequences. Two kept so the render an edit
# was made against stays beside the render that came back from it.
IMAGE_WINDOW_RECENT_MESSAGES = 2

# Most recent tool results kept intact when emergency-shrinking; older ones
# are elided (their bodies are the bulk of mid-turn context growth).
SHRINK_KEEP_RECENT_TOOL_RESULTS = 3

# Image-bearing messages kept intact when emergency-shrinking. Tighter than
# the tool-result count because one image can cost 1568 tokens: the picture
# the model is currently reasoning about is worth keeping, older ones are the
# cheapest thing to give up.
SHRINK_KEEP_RECENT_IMAGES = 1


def emergency_shrink(messages: list[dict]) -> tuple[list[dict], int]:
    """Elide the bodies of older tool-result messages to fit a tighter window.

    Mid-turn context overflow is almost always accumulated tool output, so
    replacing the content of all but the most recent few ``role="tool"``
    messages with a short placeholder frees the most tokens while keeping
    system / user / assistant reasoning intact. Deterministic, no extra LLM
    call. Returns ``(new_messages, num_elided)``; ``num_elided == 0`` means
    there was nothing worth eliding (caller should not bother retrying).

    Three passes, cheapest loss first: the pictures tools showed, then older
    tool bodies, then -- only when those two freed nothing -- the pictures
    the user sent, all but the newest.
    """
    messages, elided = elide_older_images(messages)

    placeholder = "[earlier tool output elided to fit the context window]"
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    shrunk = messages
    if len(tool_idxs) > SHRINK_KEEP_RECENT_TOOL_RESULTS:
        elide = set(tool_idxs[:-SHRINK_KEEP_RECENT_TOOL_RESULTS])
        shrunk = []
        for i, m in enumerate(messages):
            if i in elide and m.get("content") and m.get("content") != placeholder:
                clean = dict(m)
                clean["content"] = placeholder
                shrunk.append(clean)
                elided += 1
            else:
                shrunk.append(m)
    if elided:
        return shrunk, elided
    # Nothing a result picture or a tool body could give back. The pictures the
    # user sent go last, newest kept: a turn that is nothing but pasted
    # screenshots overflows on them alone, and this path could reach them
    # before the standing window narrowed the first pass to results.
    out = list(shrunk)
    changed, _ = window_images(out, SHRINK_KEEP_RECENT_IMAGES, reason="context", any_role=True)
    return (out, changed) if changed else (shrunk, 0)


def demote_tool_images(messages: list[dict]) -> tuple[list[dict], int]:
    """Move images out of tool results into a following user message.

    The recovery for an endpoint that refuses a picture in ``role="tool"``.
    Produces exactly the message list a ``False`` capability verdict would
    have built in the first place, so the retry lands on the already-tested
    placeholder path rather than inventing a third shape.

    A run of consecutive tool messages is one batch answering one assistant
    message, so the pictures pulled out of it are attached once after the last
    of them -- putting one between two tool results leaves a tool_call
    unanswered where the API checks the sequence (measured, see the batching
    comment in the tool loop).

    Returns ``(new_messages, num_demoted)``; ``0`` means no tool result
    carried an image, so the refusal was about something else and the caller
    should not retry.
    """
    out: list[dict] = []
    pending: list[dict] = []
    pending_sources: list[dict] = []
    demoted = 0

    def flush() -> None:
        if pending:
            out.append(
                {
                    "role": "user",
                    "content": list(pending),
                    ATTACHED_IMAGE_KEY: True,
                    IMAGE_SOURCES_KEY: list(pending_sources),
                }
            )
            pending.clear()
            pending_sources.clear()

    for m in messages:
        content = m.get("content")
        if m.get("role") != "tool":
            flush()
            out.append(m)
            continue
        if not isinstance(content, list):
            out.append(m)
            continue
        images = [p for p in content if is_image_part(p)]
        if not images:
            out.append(m)
            continue
        clean = dict(m)
        clean["content"] = image_placeholder_text(content)
        sources = clean.pop(IMAGE_SOURCES_KEY, None) or [{"tool": m.get("name")} for _ in images]
        out.append(clean)
        pending.extend(images)
        pending_sources.extend(sources)
        demoted += len(images)
    flush()
    return out, demoted


def window_images(
    messages: list[dict],
    keep: int,
    *,
    budget: int | None = None,
    reason: str = "superseded",
    any_role: bool = False,
) -> tuple[int, int]:
    """Withdraw the pictures the request should no longer carry, in place.

    Two modes. Without ``budget``, every image-bearing message but the newest
    ``keep`` loses its pictures: the shape the overflow path and the refusal
    ladder want. With ``budget`` (base64 bytes, the size the pictures have on the
    wire and what the request body is charged), nothing happens while the pictures
    still live in the transcript fit under it, and the moment they do not, every
    message but the newest ``keep`` loses its pictures at once.
    A collapse rather than a slide because each withdrawal is a break in the
    prefix an upstream cache can match: sliding one message out per new batch
    broke the prefix on 16 of 60 calls in one measured run (19 of 41 in the
    other) and the hit rate went from 84.5% to 64.7%; collapsing when the
    budget is hit breaks it once or a handful of times per deck, and the
    pictures stay in view for longer in between.

    In place: each withdrawn message is replaced in ``messages`` by a copy whose
    image parts have become notes (:func:`withdrawn_image_note`), so what the
    model has been told about a picture it can no longer see is part of the
    transcript from then on, not something recomputed per request.

    Prefix-stable by construction. A message is touched only while it still
    carries a picture, so one withdrawn on an earlier iteration is byte for
    byte what it was; between two iterations either nothing changes or one
    collapse does. That is the property a cached prefix needs, and the reason
    this is not a pure function returning a fresh list.

    The pictures tools showed, on either transport: a ``tool`` message where
    the endpoint carries them there, the following ``user`` message the loop
    attached where it does not. A picture the user sent is the subject of the
    turn rather than a result, and stays -- unless ``any_role``, which is for
    the request the endpoint has already refused, when there is nothing else
    left to take out. Remote references count toward ``keep`` but weigh
    nothing in the budget: their size is unknowable without fetching them.

    Returns ``(messages_changed, pictures_withdrawn)``; ``(0, 0)`` means
    nothing had to go.
    """
    bearing = [
        i
        for i, m in enumerate(messages)
        if (any_role or m.get("role") == "tool" or m.get(ATTACHED_IMAGE_KEY))
        and isinstance(m.get("content"), list)
        and any(is_image_part(p) for p in m["content"])
    ]
    if budget is not None:
        live = sum(wire_image_bytes(p) for i in bearing for p in messages[i]["content"])
        if live <= budget:
            return 0, 0
    stale = bearing[:-keep] if keep else bearing
    pictures = 0
    for i in stale:
        m = messages[i]
        sources = m.get(IMAGE_SOURCES_KEY) or []
        total = sum(1 for p in m["content"] if is_image_part(p))
        parts: list[Any] = []
        seen = 0
        for p in m["content"]:
            if not is_image_part(p):
                parts.append(p)
                continue
            source = sources[seen] if seen < len(sources) else {}
            seen += 1
            note = withdrawn_image_note(source, index=seen, total=total, reason=reason, keep=keep)
            parts.append({"type": "text", "text": note})
        clean = dict(m)
        clean["content"] = parts
        messages[i] = clean
        pictures += total
    return len(stale), pictures


def elide_older_images(messages: list[dict]) -> tuple[list[dict], int]:
    """Drop pictures from all but the most recent image-bearing message, for the
    overflow path.

    Run before the tool-text pass because an image is by far the densest
    thing in the window -- one costs up to 1568 tokens, which is more than
    most tool outputs -- so dropping a stale picture buys more room than
    eliding several text results, and costs less of what the model still
    needs. The standing window (``IMAGE_WINDOW_RECENT_MESSAGES``) has
    usually already done this; the tighter count here is for the turn whose
    window was not enough.

    A new list, like the rest of the overflow path: the caller rebinds.
    """
    out = list(messages)
    changed, _ = window_images(out, SHRINK_KEEP_RECENT_IMAGES, reason="context")
    return (out, changed) if changed else (messages, 0)


async def summarize_head(
    messages: list[dict],
    *,
    provider: Any,
    model: str,
    window: int,
    ceiling: int,
    cfg: Any,
) -> tuple[list[dict], str]:
    """Replace the transcript head with one LLM-written handoff brief.

    The system prefix and the first user message never enter the summary,
    and a recent tail (``preserve_recent_tokens`` budget) stays verbatim so
    the model keeps its most recent working state. The summary runs on the
    turn's own provider and model: a pinned summary model would outlive a
    model switch and then route every summary to a retired endpoint.

    It does not run at the turn's own reasoning effort. That was the shape
    first written -- a model call of the turn like any other -- and the
    reason it has to go is that the two are not alike: a turn thinks in
    order to decide, while a handoff brief is a transcript read back, and
    thinking is spent from the same budget as the brief. Measured, two
    summary calls at the turn's effort returned an empty body having spent
    the whole budget before the brief began, and compaction then degraded
    to blind elision for the rest of the run.

    The ceiling is ``SUMMARY_MAX_TOKENS`` bounded by the model's own and by
    what is left of the window once the request is built, so raising the
    budget can neither turn a summary into a request a small model refuses
    nor into one that asks for more than the window has left.

    Returns ``(messages, verdict)`` with verdict one of ``"changed"``
    (head replaced), ``"failed"`` (a summary call was paid for and freed
    nothing -- the caller must count it against the shared retry budget or
    a failing endpoint would be paid once per iteration) or ``"skipped"``
    (no call was made: the head is too small to be worth one, or the window
    has no room left to answer in). Anything but ``"changed"`` returns the
    input untouched, so the caller degrades to pruning plus the existing
    overflow path and is never worse off than today.
    """
    limit = window
    reserved = compaction.reserved_tokens(cfg.reserved_tokens, ceiling)
    budget = compaction.tail_budget(cfg.preserve_recent_tokens, limit, reserved)
    split = compaction.select_split(messages, budget, estimate_prompt_tokens)
    protect_end = compaction.protected_prefix_end(messages)
    if split is None or protect_end is None:
        return messages, "skipped"
    transcript = compaction.render_transcript(messages[protect_end:split])
    request = [
        {"role": "system", "content": compaction.SUMMARY_INSTRUCTIONS},
        {"role": "user", "content": transcript},
    ]
    # Bounded by the request that is about to go out, not by the trigger's
    # arithmetic: the rendering caps each message, so the head that
    # overflowed the window is not the size it reaches the summary at.
    budget = compaction.summary_output_budget(limit, estimate_prompt_tokens(request), ceiling)
    if budget <= 0:
        # A warning rather than an error, and repeated per iteration by
        # design: nothing was paid for, so this does not count against the
        # retry budget the paid failures share, and the state it reports
        # usually resolves as pruning shrinks the head. It sits beside the
        # per-iteration elision warning that says the same thing.
        logger.warning(
            "Transcript head summary has no room to answer in ({} head message(s), {} chars, "
            "{} of window): compaction falls back to eliding older items, which drops them "
            "rather than summarizing them",
            split - protect_end,
            len(transcript),
            limit,
        )
        return messages, "skipped"
    try:
        response = await provider.chat(
            messages=request,
            tools=None,
            model=model,
            max_tokens=budget,
            reasoning_effort=compaction.SUMMARY_REASONING_EFFORT,
        )
    except Exception as exc:
        logger.warning("Transcript head summary call raised: {}", exc)
        return messages, "failed"
    summary = (response.content or "").strip()
    if response.finish_reason == "length":
        # A cut brief is worse than no brief: it is non-empty, so it would
        # be accepted below and replace the head it stops halfway through.
        logger.error(
            "Transcript head summary was cut at its {}-token budget ({} head message(s), {} chars). "
            "Compaction falls back to eliding older items rather than replacing the head with a "
            "brief that stops mid-sentence",
            budget,
            split - protect_end,
            len(transcript),
        )
        return messages, "failed"
    if response.finish_reason == "error" or not summary:
        # The reason decides the fix (transcript too long vs endpoint
        # refusal vs empty completion), so record it -- and say what the
        # turn does next, because the consequence is what a reader needs
        # and it lands later, in a line about eliding that on its own looks
        # like ordinary housekeeping.
        logger.error(
            "Transcript head summary failed ({} head message(s), {} chars): {}. "
            "Compaction falls back to eliding older items, which drops them rather than summarizing them",
            split - protect_end,
            len(transcript),
            str(response.content or "empty summary")[:300],
        )
        return messages, "failed"
    return compaction.build_compacted(messages, split, summary), "changed"
