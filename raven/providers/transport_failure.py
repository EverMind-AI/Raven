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

Two verdicts, asked in that order. The client library keeps a copy of the value
it renamed (``provider_specific_fields["native_finish_reason"]``, written in
``Choices.__init__``), so wherever a response was assembled through that
constructor the upstream's own word is still readable and nothing has to be
inferred. Where it was not -- several vendors map the reason and assign it to an
already-built choice, destroying the original -- the accounting evidence is all
there is.

Either way a response that delivered something is left alone: the invariant is
that a failure must be *visible*, not that a usable answer be thrown away.

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

#: Upstream spellings of a normal end that the client library renames to
#: ``stop``. Any *other* value it renamed to ``stop`` did not say this call
#: ended normally. An allow list rather than a list of failures, because the
#: spellings of "finished" are finite and stable across vendors while the
#: dialects for "failed" are invented per gateway -- so an unrecognised value
#: has to land on the side that keeps the failure visible.
_NORMAL_STOP_ALIASES = frozenset(
    {
        "stop_sequence",
        "end_turn",
        "COMPLETE",
        "STOP",
        "eos",
        "eos_token",
        "FINISH_REASON_UNSPECIFIED",
    }
)

#: Renames to ``stop`` observed to report a failure. The verdict does not read
#: this -- an unrecognised rename is a failure either way, which is what the
#: allow list is for -- so it is here to be *said*, not to decide. A rename this
#: does not name is either a new failure dialect or the one thing the allow list
#: gets wrong: a normal end spelled in a way nothing has mapped, reported as a
#: failure when the loop's own silence recovery should have had it. Naming the
#: two apart in the log is the only signal that case has.
_OBSERVED_FAILURE_REASONS = frozenset(
    {
        "error",
        "ERROR",
        "network_error",
        "MALFORMED_RESPONSE",
        "MALFORMED_FUNCTION_CALL",
        "TOO_MANY_TOOL_CALLS",
    }
)

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


def native_finish_reason(choice: Any) -> str | None:
    """The finish reason the upstream sent, where the library kept a copy of it.

    Read defensively on purpose: the field is written only when the mapping
    actually renamed something, so on an ordinary ``stop`` the attribute is
    absent from the choice altogether and reaching for it raises.
    """
    fields = getattr(choice, "provider_specific_fields", None) or {}
    value = fields.get("native_finish_reason") if isinstance(fields, dict) else None
    return str(value) if value else None


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
    native_finish_reason: str | None = None,
    #: Accepted and not weighed. Kept in the signature so both call sites keep
    #: reading it off the response, and so the reason it does not decide has
    #: somewhere to be stated rather than being invisible at the boundary.
    reasoning: str | None = None,
    usage: dict[str, Any] | None = None,
    sent_chars: int | None = None,
) -> str | None:
    """The evidence that this call failed in transport, or ``None``.

    Nothing may have been delivered -- a normal end, no content, no tool call --
    for either verdict to be reached. Given that, two things end the call:

    ``native_finish_reason`` outside :data:`_NORMAL_STOP_ALIASES` settles it on
    its own. It is the value the upstream actually sent, kept by the library
    beside the one it rewrote, so there is nothing to corroborate.

    Failing that -- the vendors that overwrite the reason on an already-built
    choice keep no copy -- the prompt must have been billed at a size the
    request cannot have been. That second signal needs the emptiness beside it,
    because either one alone is a different fault with a different owner:

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
    if native_finish_reason and native_finish_reason not in _NORMAL_STOP_ALIASES:
        # No corroboration asked for, and none needed: this is not an inference
        # about what the upstream meant, it is what the upstream said. The
        # accounting below exists to tell a failed request apart from a silent
        # model, and a model that stayed silent does not send one of these.
        seen = "" if native_finish_reason in _OBSERVED_FAILURE_REASONS else ", a spelling not seen before"
        return f"the upstream reported finish_reason={native_finish_reason!r}{seen}, renamed to 'stop' in transit"
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
