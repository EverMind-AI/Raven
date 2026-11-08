"""Reply headroom -- how much of the window a turn holds back for the answer.

The compacting history strategy bounds its prompts by
``window - reply_headroom(...)`` -- its trigger and the trimmer it hands the
budget to -- and the loop sizes a near-window request's ``max_tokens`` with
:func:`request_max_tokens`; refusals are learned here whichever strategy runs.
The per-turn :class:`~raven.contracts.assembled.TokenBudget` still reserves
the model's whole output ceiling (``reserved_output``): that is the Curator's
arithmetic, and the compacting strategy computes its own room rather than
reading it. The table is one number per model for the whole process, not a
strategy's private tuning, which is why this module sits beside
``history_trimmer`` rather than inside a strategy package.

The number it answers with is a **floor plus what refusals taught it**, not
the model's output ceiling. Reserving the ceiling is arithmetically safe and
practically ruinous on a small window -- a 16384-token ceiling on a 32768
window is half the window held back from a reply that almost never arrives,
and the share it costs grows as the window shrinks, which is the wrong
direction. So the floor is a fixed 4096 and providers that really do validate
``prompt + ceiling <= window`` teach the rest, once, from a real refusal.

That trade only holds because the refusal is recoverable: the loop's
context-overflow path retries the iteration, and calls :func:`learn_from_overflow`
so the next turn's budget already carries what this one had to discover.
Learning is per process and per wire model id -- it is a cache of an
observation, not state anyone persists.

Shared across sessions, and never decayed within the process, on purpose. What
a refusal teaches is mostly a property of the model's wire, not of the
conversation that hit it: either the provider validates ``prompt + max_tokens``
against the window, or the local estimate under-counts that model's tokenizer.
A per-session value would make every new session pay the same refusal again,
and decaying it would schedule the next one. The cost is bounded: a refusal of a request already narrowed to the room
beside its prompt moves the value by one step; any other moves it to the larger
of one step more and the model's output ceiling (or the room the request left
plus a step); never past half the window; a restart clears it.

Precedent for learning from one refusal rather than guessing statically:
``providers.prompt_cache.suppress`` and the loop's vision / image-in-tool-result
verdicts do the same thing.
"""

from __future__ import annotations

import threading

DEFAULT_MIN_REPLY_HEADROOM_TOKENS = 4_096
"""The floor, before anything has been learned."""

DEFAULT_OVERFLOW_BACKOFF_TOKENS = 8_192
"""Extra room added per refusal that the model's own ceiling did not explain."""

# The share of a local prompt estimate given up to the provider's tokenizer.
_ESTIMATE_SLACK = 0.05

_lock = threading.Lock()
_required: dict[str, int] = {}


def required_headroom(model: str) -> int:
    """What refusals have taught us this model needs. ``0`` until one lands."""
    return _required.get(model or "", 0)


def reply_headroom(
    model: str,
    *,
    window: int,
    minimum: int = DEFAULT_MIN_REPLY_HEADROOM_TOKENS,
) -> int:
    """Tokens of ``window`` that this turn must not fill with prompt.

    Clamped to the window itself: a hand-configured window smaller than the
    floor would otherwise hand out a negative prompt bound.
    """
    return max(0, min(max(minimum, required_headroom(model)), max(0, window)))


def max_prompt_tokens(
    model: str,
    *,
    window: int,
    minimum: int = DEFAULT_MIN_REPLY_HEADROOM_TOKENS,
) -> int:
    """The prompt bound this policy implies. Never below 1."""
    return max(1, window - reply_headroom(model, window=window, minimum=minimum))


def learn_from_overflow(
    model: str,
    *,
    window: int,
    prompt_tokens: int,
    step: int = DEFAULT_OVERFLOW_BACKOFF_TOKENS,
    narrowed: bool = False,
) -> int:
    """Record that ``model`` refused a prompt of ``prompt_tokens`` on ``window``.

    The refusal says the room that request left -- ``window - prompt_tokens``
    -- was not enough. Which of two explanations to believe turns on whether
    that room already covered the model's output ceiling:

    - it did not, so a provider validating ``prompt + ceiling <= window``
      explains the refusal completely and the ceiling is the answer. One
      refusal, converged, which is the case worth optimising for;
    - it did, and the request was refused anyway, so the real bound is
      something else and the only honest move is to go past what was just
      tried: the room left, plus ``step``.

    Either way at least ``step`` more than last time, so a provider that
    refuses twice cannot settle on the same number twice.

    Both explanations assume the refused request asked for the model's whole
    ceiling. One whose ``max_tokens`` was ``narrowed`` to the room beside its
    prompt asked for exactly that room, so a refusal says the prompt estimate
    ran short, not that the ceiling did; it moves the table by ``step`` and no
    more. A prompt that alone fills the window teaches nothing about the reply
    and moves nothing. Capped at half the
    window: past that the cure costs more history than the overflow did, and a
    model needing more than half its own window for a reply is one this loop
    cannot serve anyway.
    """
    from raven.providers.rates import resolve_max_output_tokens

    key = model or ""
    # allow_fetch=False: this runs on the loop's thread, in the recovery path
    # of a request that just failed, and the catalogue's importing tier costs
    # seconds. An under-estimate here is corrected by the next refusal.
    ceiling = resolve_max_output_tokens(key, allow_fetch=False)
    room_left = window - prompt_tokens
    explained_by_ceiling = room_left < ceiling
    with _lock:
        if room_left <= 0:
            return _required.get(key, 0)
        if narrowed:
            proposed = _required.get(key, 0) + step
        else:
            proposed = max(
                _required.get(key, 0) + step,
                ceiling if explained_by_ceiling else room_left + step,
            )
        learned = max(0, min(proposed, max(0, window) // 2))
        _required[key] = learned
        return learned


def request_max_tokens(*, window: int, prompt_tokens: int, ceiling: int, floor: int) -> int | None:
    """The ``max_tokens`` a request should carry so it fits beside its prompt.

    ``None`` while the room left beside the prompt still covers ``ceiling`` --
    the request goes out as it always did. Narrowed, it is that room, never
    below ``floor``: the trigger keeps a prompt at least ``floor`` below the
    window, so a request that needs less than the floor is one a provider
    refuses either way and the overflow path is what answers it. The prompt
    side is a local estimate against the provider's own count, so 5% of it is
    given up to the disagreement.
    """
    if window <= 0 or ceiling <= 0:
        return None
    room = window - int(prompt_tokens * (1 + _ESTIMATE_SLACK))
    if room >= ceiling:
        return None
    # Never above the ceiling: a configured ``maxTokens`` below the floor is an
    # operator's choice this does not overrule.
    return min(ceiling, max(floor, room))


def reset() -> None:
    """Forget every learned value. For tests -- the table is process state."""
    with _lock:
        _required.clear()


__all__ = [
    "DEFAULT_MIN_REPLY_HEADROOM_TOKENS",
    "DEFAULT_OVERFLOW_BACKOFF_TOKENS",
    "learn_from_overflow",
    "max_prompt_tokens",
    "reply_headroom",
    "request_max_tokens",
    "required_headroom",
    "reset",
]
