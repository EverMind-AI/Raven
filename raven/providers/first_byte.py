"""The bound on time to first byte, and the error it raises when none arrives.

``llm_call_timeout`` is a whole-call budget and has to stay wide: a real
generation at ~50 tok/s writing 48k tokens takes 930 s, and the deck agents ship
``llmCallTimeout: 1800`` for exactly that. Under that one number a model
generating for fifteen minutes and a model that has not sent a byte for fifteen
minutes are the same event -- which is how a 916 s call that completed and a
turn that never issued a request both passed unremarked on 2026-09-10.

This carries the second, much shorter bound. It applies to getting *started*,
not to finishing, so it can be tens of seconds while the total stays in the
tens of minutes. Measured against the model this repo ships
(``z-ai/glm-5.3-flash`` on OpenRouter, provider fence Z.AI/DeepInfra/Novita,
110 streamed calls on 2026-09-10): time to first chunk was 1.15-4.37 s on a
trivial prompt, 4.13-14.32 s on a deck-shaped prompt of 61k tokens with 16
tools, and 5.54-17.45 s at 210k tokens, over low, medium and high reasoning
effort -- p50 4.35 s, p90 9.45 s, p99 16.49 s, worst 17.45 s, no failures.
``DEFAULT_FIRST_BYTE_TIMEOUT`` clears the worst of those by about 7x, because
the number has to survive a cold route and a queued gateway rather than only
the warm ones a measurement catches. It is also well under the 180 s idle cap,
which keeps the two bounds in the right order: starting is quicker than
continuing.

What it does *not* bound is a non-streaming call: ``acompletion`` without
``stream=True`` returns nothing until the answer is finished, so its first
response byte and its last are the same byte and there is no early signal to
wait for. What is separable there is the phase before the model starts
generating -- DNS, connect, TLS, sending the request, waiting for a pooled
connection -- and ``httpx_timeout`` bounds those at the first-byte budget while
leaving the read (the generation itself) on the total budget.

Every adapter that is handed this setting reads it, which had to be said in one
place because it was true of one of four: LiteLLM bounds its open and first
chunk, and the three direct SSE adapters (``anthropic_messages_provider``,
``openai_responses_provider``, and ``openai_codex_provider``, whose SSE reader
the second of those shares) bound their client's pre-generation phases and the
wait for their first event. For a *stream*, waiting on the response headers is
a read, so ``httpx_timeout`` alone does not cover the open -- the Anthropic
adapter bounds that await itself.
"""

from __future__ import annotations

from typing import Any

#: Seconds. See the module docstring for the measurement this comes from.
DEFAULT_FIRST_BYTE_TIMEOUT = 120.0

#: The config key this bound is spelled with, quoted verbatim in the record so
#: a reader of the log knows which knob to turn.
BOUND_NAME = "llmFirstByteTimeout"


class FirstByteTimeoutError(TimeoutError):
    """No first byte inside the first-byte budget.

    Subclasses ``TimeoutError`` so every ``except TimeoutError`` arm already on
    the streaming path keeps catching it, and so ``classify_error`` keeps
    reaching a retryable verdict even if this class is never special-cased.
    """

    def __init__(self, *, phase: str, budget: float, waited: float) -> None:
        super().__init__(f"no first byte after {waited:.1f}s while {phase} (bound {BOUND_NAME}={budget:g}s)")
        self.phase = phase
        self.budget = budget
        self.waited = waited


def first_byte_budget(generation: Any) -> float:
    """The configured first-byte budget, clamped to the total call, or 0.0 for none.

    Read defensively for the same reason the adapters read ``stream_idle_timeout``
    that way: a ``GenerationSettings`` from before this field exists, or a test
    double with none at all, must behave as it did before rather than crash.
    Clamping here rather than rejecting at config load is deliberate -- a
    hand-written ``llmFirstByteTimeout`` above the call budget is a mistake worth
    ignoring, not one worth refusing to boot over.

    0.0 means "no first-byte bound", not "bound it at the total": a caller that
    needs a number of its own to wait on picks its own fallback, so switching the
    bound off restores exactly what that caller did before rather than something
    wider.
    """
    total = float(getattr(generation, "timeout", 0.0) or 0.0)
    budget = float(getattr(generation, "first_byte_timeout", 0.0) or 0.0)
    if budget <= 0:
        return 0.0
    if total > 0:
        return min(budget, total)
    return budget


def stream_first_byte_budget(generation: Any) -> float:
    """What a streaming adapter waits for its first chunk.

    The first-byte bound when there is one; otherwise the idle cap, which is what
    bounded the open and the first pull before this bound existed, and the total
    only if there is no idle cap either.
    """
    return (
        first_byte_budget(generation)
        or float(getattr(generation, "stream_idle_timeout", 0.0) or 0.0)
        or float(getattr(generation, "timeout", 0.0) or 0.0)
    )


def httpx_timeout(generation: Any) -> Any:
    """Per-phase transport caps: the pre-generation phases at the first-byte
    budget, the read left on the total.

    A float ``timeout`` reaches httpx as the same number on all four phases, so
    today a dead route costs the whole call budget before anyone notices. Only
    ``read`` has to stay wide -- for a non-streaming completion the body does
    not begin until the answer is finished, so a tight read cap would kill every
    long generation. Returns ``None`` when there is nothing to narrow, so the
    caller keeps passing the plain float it always did.
    """
    total = float(getattr(generation, "timeout", 0.0) or 0.0)
    budget = first_byte_budget(generation)
    if total <= 0 or budget <= 0 or budget >= total:
        return None
    import httpx

    return httpx.Timeout(connect=budget, read=total, write=budget, pool=budget)
