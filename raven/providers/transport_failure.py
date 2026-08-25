"""Did the upstream actually answer, or only say that it had.

Beside :mod:`raven.providers.truncation` and for its reasons: everything read
here belongs to this layer, and the non-streaming caller has to reach it
*before* returning, while the ``llm.call`` span is still open.

The fault it exists for: an upstream can mark a call failed in the response
body -- ``finish_reason=error`` over HTTP 200, with nothing in it -- and the
client library rewrites that to ``stop`` from a lookup table, in place and
without a log line (litellm 1.97.0, ``core_helpers.py``: ``"error": "stop"``;
``gpt_transformation.py`` overwrites the field, and no public field keeps the
original). Raven's retry ladder gates on ``finish_reason != "error"``, so it
read the call as healthy and neither retried, classified nor fell back. In the
control experiment the same request succeeded on the next backend, so this was
a blip one retry would have healed.

**The original value cannot be recovered**, so the verdict is built from local
evidence instead. That bounds what this can see: a laundered failure that
carried a usable answer is indistinguishable from a healthy one here, and is
deliberately delivered as the answer it looks like -- the invariant is that a
failure must be *visible*, not that such a response be thrown away.

Both response paths ask this, for the reason ``truncation`` gives for its own
question: a verdict reached on one path and not the other makes the same fault
read differently inside the TUI than outside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.providers.base import ToolCallRequest

#: Sentinels an upstream can leak in place of content. They mean the same as
#: nothing was said, so they are folded into the emptiness test rather than
#: given a branch of their own.
_UPSTREAM_SENTINELS = ("<|endoftext|>", "<|im_end|>", "<|eot_id|>")

#: Below this, an accounted prompt is not evidence of anything -- a short
#: request really can cost a handful of tokens.
_MIN_PROMPT_CHARS = 400

#: `prompt_tokens` this far under what was sent means the request was never
#: processed. Deliberately far below any tokeniser's ratio (~4 chars/token, so
#: a truthful accounting lands near 25%) rather than tuned to the incident,
#: where about 3000 tokens were billed as 1.
_ABSURD_ACCOUNTING = 0.02


def prompt_chars(messages: list[dict[str, Any]]) -> int:
    """Roughly how much prompt went up, for the accounting to be weighed against.

    Shared rather than done at each call site, because the two disagreed and the
    verdict rests on the answer. The streaming path measured
    ``len(str(content))``, which reprs a block list -- the shape a message takes
    after a tool result returned a picture -- and counted a base64 data URI into
    the total. Four orders of magnitude out on a vision turn, which inflated the
    threshold until truthful usage read as absurd.

    Only text is counted. An image costs prompt tokens that no character count
    predicts, so a picture contributes nothing here and an image-only prompt
    falls under :data:`_MIN_PROMPT_CHARS` and is never judged. That is the safe
    direction: the evidence this looks for is a prompt billed at a fraction of
    what was sent, and a number that cannot be estimated cannot support it.

    Characters, not tokens: the comparison this feeds is two orders of magnitude
    wide, so paying a tokeniser for precision it does not use would only make
    the response exit slower.
    """

    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(str(part.get("text") or ""))
    return total


def _stripped(text: str | None) -> str:
    """``text`` without the sentinels that stand for nothing having been said."""
    out = text or ""
    for sentinel in _UPSTREAM_SENTINELS:
        out = out.replace(sentinel, "")
    return out.strip()


def _said_nothing(content: str | None, tool_calls: list["ToolCallRequest"]) -> bool:
    """Whether this response carries no reply for anyone.

    Deliberately does not consult ``reasoning_content``. It used to, on the
    grounds that a model which emitted only thought had done work an identical
    retry would repeat -- but that premise is the one the accounting denies. A
    prompt billed at a fraction of what was sent was never read, and nothing
    that comes back from an unread prompt is thought about it.

    The incident is that case: twelve recorded responses carrying
    ``reasoning_content`` of a single ``"#"`` and one completion token, beside
    an empty answer and a one-token prompt. Weighed as thought, it vetoed the
    stronger evidence and the detector never fired on the case it was built
    for.

    Content and tool calls still veto, and for a different reason: those are
    delivered. An answer is an answer whatever its accounting says, and a
    verdict that discarded one would trade this fault for a worse one.
    """
    if tool_calls:
        return False
    return not _stripped(content)


def _accounting_is_absurd(usage: dict[str, Any] | None, sent_chars: int | None) -> str | None:
    """Whether the prompt was billed at a size the request cannot have been.

    This is the whole verdict, not a decoration on it. Silence on its own does
    not distinguish a request that was never processed from a model that simply
    said nothing, and the second is common enough to have its own recovery
    (``raven.agent.loop.recovery``: prefill, a post-tool nudge, a plain retry).
    Claiming transport failure without transport evidence takes that recovery
    out of service and tells the reader something false about their own system.

    Cached tokens are added back before comparing: a cache hit legitimately
    bills a fraction of the prompt, and reading ``prompt_tokens`` alone would
    call every cached call a failure.
    """
    if not usage or not sent_chars or sent_chars < _MIN_PROMPT_CHARS:
        return None
    accounted = (
        int(usage.get("prompt_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
    # A block of nothing but zeros is a provider that does not report usage,
    # not a request that was never processed -- reading it as evidence would
    # condemn every empty response such a provider ever returns. A zero *prompt*
    # beside a non-zero anything else is the strongest form of the evidence
    # there is, and is read as such.
    if not any(int(v or 0) for v in usage.values() if isinstance(v, int | float)):
        return None
    estimate = sent_chars / 4
    if accounted < estimate * _ABSURD_ACCOUNTING:
        return f"the prompt was {sent_chars} characters and was billed as {accounted} tokens"
    return None


def flag_transport_failure(
    *,
    finish_reason: str | None,
    content: str | None,
    tool_calls: list["ToolCallRequest"],
    #: Accepted and not weighed. Kept in the signature so both call sites keep
    #: reading it off the response, and so the reason it does not decide has
    #: somewhere to be stated rather than being invisible at the boundary.
    reasoning: str | None = None,
    usage: dict[str, Any] | None = None,
    sent_chars: int | None = None,
) -> str | None:
    """The evidence that this call failed in transport, or ``None``.

    Two things must hold together. Nothing was delivered -- a normal end, no
    content, no tool call -- **and** the prompt was billed at a size the
    request cannot have been. Both, because either one alone is a different
    fault with a different owner:

    * Silence with honest usage is a model that said nothing. The agent loop
      recovers that by changing the request (a prefill, a post-tool nudge) or
      retrying it, and none of that is reachable once a response is reported as
      an error. Retrying the identical request, which is all this verdict can
      ask for, is also the one thing that does not help there.
    * An answer with strange accounting is an answer. Gateways under-report
      usage, and a verdict that overrode a real reply would trade this fault
      for a worse one.

    ``reasoning_content`` is not weighed either way -- see
    :func:`_said_nothing`. When the two signals disagree the accounting is the
    truthful one, because a prompt that was never read cannot have been
    reasoned about.

    So a laundered failure whose accounting looks ordinary is not caught here.
    That is deliberate: locally it is indistinguishable from a silent model,
    and the loop already has a recovery for exactly that shape.

    ``finish_reason`` must be the value the upstream actually sent, not one
    synthesised when it sent none. A stream that yields no terminal delta at
    all is a different situation with its own settled behaviour, and widening
    to cover it would change a case nobody reported.

    ``length`` is not read here. A cut-off run belongs to
    :func:`raven.providers.truncation.flag_truncation`, which already owns it.
    """
    if finish_reason != "stop":
        return None
    if not _said_nothing(content, tool_calls):
        return None
    accounting = _accounting_is_absurd(usage, sent_chars)
    if not accounting:
        return None
    return f"the call ended normally without any content or tool call, and {accounting}"


def transport_failure_message(evidence: str) -> str:
    """What the caller is told, worded so the blame lands where the fault is.

    The caller used to receive a well-formed response holding nothing, and
    could only read it as the model having chosen to stay silent -- so the
    agent went off repairing its own prompt, a dimension unrelated to the
    actual fault. Naming the upstream, and the evidence, is what stops that.
    """
    return (
        f"The upstream reported a failed call rather than an answer: {evidence}. "
        "This is a transport failure, not a refusal to answer."
    )


__all__ = ["flag_transport_failure", "prompt_chars", "transport_failure_message"]
