"""Whether a request may carry Anthropic-shaped ``cache_control`` breakpoints.

One question, asked from three places -- the provider that builds the request,
and the two token strategies that place breakpoints before it. It used to be
answered by a copy of the same function in each, which is how the copies came to
disagree: the provider's only ever marked the system message and the tool list,
so fixing it there could not have changed what the strategies stamp onto the last
conversation message, and that is where the doubling came from.

The answer is **(wire x model family)**, not the wire alone:

* the wire has to have somewhere to put the field. An OpenAI-shaped API does
  not, and a gateway speaks its own shape regardless of who it fronts, so
  ``ProviderSpec.supports_prompt_caching`` is asked of whatever the request is
  actually addressed to.
* the model has to be one whose vendor reads it. A gateway accepts the field for
  every model it fronts and forwards it to vendors that do not: it is then billed
  as an unrecognized block rather than refused, which doubles a prompt silently.

Suppression is the fourth answer, and it is learned rather than declared,
because no table here can predict it. See ``suppress``.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

#: Lifetimes the field accepts. ``None`` asks for nothing and takes the vendor's
#: own default, five minutes; ``"1h"`` is the extended one.
#:
#: The trade is a straight one and it is priced, not guessed. Measured against
#: `anthropic/claude-haiku-4.5` through OpenRouter, per prompt token and
#: relative to the uncached rate: a five-minute write costs 1.25x, an hour-long
#: write 2.0x, and a read 0.10x under either. So the hour buys its extra 0.75x
#: back the first time it saves a re-write -- that is, the first time a gap
#: between two turns lands past five minutes and inside the hour. Whether that
#: happens is a fact about a deployment's rhythm, which is why this is a setting
#: and not a constant.
#: Breakpoints one request may carry. The vendor's hard limit, and refused
#: rather than trimmed: a fifth comes back as
#: "A maximum of 4 blocks with cache_control may be provided. Found 5."
MAX_BREAKPOINTS = 4

TTLS = (None, "1h")

_ttl: str | None = None


def set_ttl(ttl: str | None) -> None:
    """Set the lifetime every breakpoint asks for, process-wide.

    Process-wide rather than per provider, for the reason the field's value
    itself is: three modules place these marks, and a lifetime that differed
    between them would put two keys on one prefix and cache it twice. The same
    argument that keeps ``CACHE_CONTROL`` in one place keeps its lifetime here.

    A value outside :data:`TTLS` is refused rather than forwarded, because an
    unrecognised ``ttl`` is not an error the upstream reports as one: the field
    is accepted, the block is billed, and the cache behaves as though nothing
    was asked for.
    """
    global _ttl
    if ttl not in TTLS:
        raise ValueError(f"cache ttl must be one of {TTLS!r}, not {ttl!r}")
    _ttl = ttl


#: Key a request carries to say its breakpoints are already placed.
#:
#: Ownership is a fact about one request, not about the process. The first
#: version of this was a process-wide switch, set once when the token strategies
#: were installed, and it was wrong in the direction that costs money quietly:
#: it turned the provider's own placement off for *every* caller in the process,
#: while the strategy meant to place the marks instead runs at exactly one of
#: them -- ``AgentLoop``'s own call. The Curator, subagents, Sentinel, the
#: session titler and the memory consolidator all reach a provider directly, so
#: they lost every breakpoint they used to have and went back to paying full
#: price for prefixes that had been cached before.
#:
#: Asked of the request instead, the question answers itself: one that went
#: through a strategy says so, and one that did not says nothing -- which is
#: already what every direct caller means. It also survives what the switch was
#: process-wide in order to survive, ``providers.pool`` rebuilding a provider on
#: a model switch, because nothing about it lives on the provider.
#:
#: Sniffing for an existing ``cache_control`` would answer without a key, and
#: answer it wrong: a retried request can arrive carrying marks left over from
#: the attempt that failed -- the case :func:`strip` exists for -- and the
#: provider would read those as somebody's ownership claim.
MARKS_PLACED_KEY = "cache_marks_placed"


def claim_marks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of ``messages`` stamped as already carrying its own breakpoints.

    On the first message because every request has one; the stamp says
    something about the request, not about the message it rides on.
    """
    if not messages:
        return messages
    return [{**messages[0], MARKS_PLACED_KEY: True}, *messages[1:]]


def marks_already_placed(messages: list[dict[str, Any]]) -> bool:
    """Whether something upstream of the provider placed this request's marks."""
    return bool(messages) and bool(messages[0].get(MARKS_PLACED_KEY))


def write_rate_multiplier() -> float:
    """What a cache write costs, per token, relative to the uncached prompt rate.

    Measured against OpenRouter's own billing for `anthropic/claude-haiku-4.5`
    and normalised for prompt size: 1.25x at the default lifetime, 1.99x at
    ``1h``. Read rather than hardcoded because the lifetime is a setting, and a
    cost estimate that assumes the cheap one under-reports an hour-long write by
    more than a third -- on exactly the deployments that turned the hour on to
    spend more per write on purpose.
    """
    return 2.0 if _ttl == "1h" else 1.25


def cache_control() -> dict[str, str]:
    """The field's value, carrying whatever lifetime is configured.

    A fresh dict each call: these are written into request payloads that callers
    copy and mutate, and one shared object reaching two of them has the second
    one's edit show up in the first.
    """
    return dict(CACHE_CONTROL) if _ttl is None else {**CACHE_CONTROL, "ttl": _ttl}


#: Key a message may carry to say how much of its content never changes between
#: turns, in characters from the start.
#:
#: The system message is assembled from segments, and only the first few of them
#: are stable: identity and the bootstrap files are the same every turn, while
#: the memory recall, the skill router's hits and the Curator's working state are
#: all derived from what the user just said. A breakpoint keys its cache on
#: everything up to itself, so one placed at the end of that message -- the only
#: one there used to be -- carries the volatile tail into the key and misses on
#: every new turn, re-billing the stable head along with it.
#:
#: Carried beside the content rather than expressed as a split content list,
#: because the shape of that message is read by more than the request builder:
#: ``openai_codex_provider`` takes the system prompt only when it is a ``str``
#: and silently substitutes an empty one otherwise. An extra key is invisible to
#: every reader that does not look for it, and the sanitizer's allow-list drops
#: it before the request goes out.
STABLE_PREFIX_KEY = "cache_stable_prefix_chars"


def split_stable_prefix(content: Any, chars: int) -> list[dict[str, Any]] | None:
    """Split ``content`` into [stable, rest] text blocks, or None if it cannot be.

    None rather than a one-block list when the split would be degenerate -- a
    boundary at 0 or past the end, or content that is not a plain string -- so
    the caller keeps whatever it already had instead of reshaping a message for
    a breakpoint that would sit at one end and cache nothing extra.
    """
    if not isinstance(content, str) or not 0 < chars < len(content):
        return None
    return [
        {"type": "text", "text": content[:chars]},
        {"type": "text", "text": content[chars:]},
    ]


#: The vendor whose API defines this field. A model reaches it directly or
#: through a gateway; either way it is the one that reads the breakpoints.
_DIALECT_OWNER = "anthropic"

#: (provider, model) pairs an upstream rejected the field for, learned at
#: runtime. Process-local on purpose -- see ``suppress``.
_SUPPRESSED: set[str] = set()


def accepts_cache_control(model: str, *, addressed_to: str = "") -> bool:
    """May this request carry ``cache_control`` blocks?

    False for an empty id, for a wire with nowhere to put the field, for a model
    whose vendor does not read it, and for anything an upstream has already
    rejected it for.

    ``addressed_to`` is the provider actually serving the request, for the one
    caller that knows it independently of the id. A stored id names its provider,
    so the two normally agree -- but a bare ``anthropic/claude-...`` handed to a
    SiliconFlow client reads as Anthropic's wire from the id alone, and that wire
    has nowhere to put the field. Passing it keeps the answer about the request
    rather than about the string.

    A ``addressed_to`` naming a provider Raven carries no spec for (Bedrock,
    Vertex, a bare LiteLLM passthrough) resolves to nothing, and so does
    ``find_by_model`` on an id whose prefix nobody claims -- in both cases there
    is no spec to ask, not a spec that said no. Falling through to
    ``find_by_keywords`` there guesses the wire's dialect from the model's own
    name instead of giving up: Bedrock speaks Anthropic's ``cache_control``
    natively (translated to ``cachePoint`` on the way out) for exactly the ids
    that mention "claude", so the guess is right far more often than a blanket
    False would be. A guess is what it is, though -- wrong for a model renamed
    away from its vendor's naming, or a passthrough that fronts a wire this
    guess did not anticipate -- which is why ``suppress`` exists: an upstream
    rejection is learned at runtime and this guess never gets a second try for
    that model. Resolving *to* a spec, by contrast, is left exactly as strict as
    before -- that path is what stopped a gateway forwarding the field to a
    vendor that bills it as an unrecognized block instead of refusing it.
    """
    if not model or model in _SUPPRESSED:
        return False

    from raven.providers.registry import find_by_keywords, find_by_model, find_by_name

    addressed = find_by_name(addressed_to) if addressed_to else find_by_model(model)
    if addressed is None:
        addressed = find_by_keywords(model)
    if addressed is None or not addressed.supports_prompt_caching:
        return False

    # The family cannot be read off the id's prefixes: the leading one names the
    # gateway, and the upstream segment is spelled the gateway's way ("google",
    # not "gemini"). So it is read from the id's keywords. A direct route
    # answers the same way: `anthropic/claude-...` matches on both.
    family = find_by_keywords(model)
    return family is not None and family.name == _DIALECT_OWNER


def suppress(model: str) -> None:
    """Stop sending ``cache_control`` for this model for the rest of the process.

    Called when an upstream has answered a marked request with a rejection. The
    case this exists for cannot be predicted from any table: OpenRouter routes
    ``anthropic/claude-3-haiku`` to Amazon Bedrock, whose dialect is
    ``cachePoint``, and neither OpenRouter's catalogue nor LiteLLM's says so --
    both correctly report that the model caches.

    Deliberately not persisted. Upstream routing is a runtime decision that
    changes, so a file written tonight would still be answering next month; the
    cost of forgetting is one extra request per model per process, and the cost
    of a stale file is caching silently switched off for a model that regained
    it.
    """
    if model and model not in _SUPPRESSED:
        _SUPPRESSED.add(model)
        logger.info("prompt cache: {} rejected cache_control upstream; not sending it again", model)


def is_suppressed(model: str) -> bool:
    return model in _SUPPRESSED


def reset_suppressions() -> None:
    """Only useful for tests -- production learns and keeps."""
    _SUPPRESSED.clear()


#: How a client spells "the request was refused". Not redundant with the status
#: below: a gateway paraphrasing its upstream can drop the numeric code entirely
#: ("Bad Request: ... did not allow prompt caching"), and the spelling that
#: reaches us carries a space, which the run-together forms do not match.
_BAD_REQUEST_MARKERS = ("bad request", "badrequest", "bad_request", "invalid_request")

#: The status as its own token. As a bare substring it also matched the "400" in
#: "retry after 1400ms", so a rate limit or a timeout whose text happened to name
#: the field read as a refusal -- and the cost of that is caching switched off
#: for the model, quietly, for the rest of the process.
_STATUS_400 = re.compile(r"\b400\b")

#: How a refusal names itself. More than the field name, because a gateway
#: paraphrases its upstream: a Bedrock refusal reaches us saying only "did not
#: allow prompt caching", with the field name nowhere in the text.
_REFUSAL_MARKERS = ("cache_control", "prompt caching", "prompt_caching")


#: Where a client keeps the response body when its ``str()`` is a summary.
#: LiteLLM's streaming path raises ``MaskedHTTPStatusError``, whose text is
#: "Client error '400 Bad Request' for url ..." and names nothing at all; the
#: body it was built from sits on these attributes. Reading only ``str(exc)``
#: made the same refusal learnable on one path and invisible on the other.
_BODY_ATTRS = ("text", "message", "body")


def _searchable(error: object) -> str:
    """Everything this error says about itself, lowercased.

    Both layers paraphrase: the gateway paraphrases the upstream, and the client
    paraphrases the gateway, so the body has to be gathered off the exception
    rather than read out of ``str()``.
    """
    if isinstance(error, str):
        return error.lower()
    parts = [str(error)]
    parts.extend(str(getattr(error, attr, "") or "") for attr in _BODY_ATTRS)
    return " ".join(parts).lower()


def is_rejection(error: object) -> bool:
    """Is this error the upstream refusing prompt-cache breakpoints?

    Takes the exception rather than a rendered string, because which of the two
    carries the body depends on the path: the non-streaming call raises one whose
    ``str()`` includes it, and the streaming call raises one whose ``str()`` is a
    URL and a status.

    Narrow on purpose: both a name for what was refused and a refused-request
    marker are required. Naming alone would read a timeout whose payload was
    logged as a dialect problem and switch caching off for the rest of the
    process; the status alone would swallow every other way a request can be
    malformed into a silent retry.

    Nothing is swallowed either way -- the retry sends the same request without
    the field, and if that fails too the second error surfaces unchanged.
    """
    text = _searchable(error)
    if not any(name in text for name in _REFUSAL_MARKERS):
        return False
    return bool(_STATUS_400.search(text)) or any(m in text for m in _BAD_REQUEST_MARKERS)


#: Keys that ride beside a message for a placer's benefit and must not outlive
#: it. ``cache_control`` is the wire field; the other two are ours, and a
#: request that has just been stripped of its breakpoints is no longer one whose
#: marks are placed.
_OUT_OF_BAND_KEYS = frozenset({"cache_control", MARKS_PLACED_KEY, STABLE_PREFIX_KEY})


def strip(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Copies of ``messages`` and ``tools`` with every breakpoint removed.

    The last word belongs to whoever sends the request. A token strategy sees
    only the model id, and an id can name a vendor the request is not going to:
    `anthropic/claude-3` served through an OpenAI-shaped gateway is marked by the
    strategy and then refused -- or, worse, quietly billed twice -- by the wire it
    actually travels on. The client knows its own destination, so it is the one
    that takes off what should not go.

    Needed as well as ``suppress``, not instead of it: the strategies place their
    breakpoints upstream of the provider, so by the time a request has failed the
    marks are already in the payload the retry would resend. Suppression stops
    the provider from adding its own on the way back out; this takes off the ones
    that are already there.
    """
    return [_strip_message(m) for m in messages], _strip_blocks(tools)


def _strip_message(message: dict[str, Any]) -> dict[str, Any]:
    # Read before the key is taken off: it is what says which split was ours.
    boundary = message.get(STABLE_PREFIX_KEY)
    cleaned = {k: v for k, v in message.items() if k not in _OUT_OF_BAND_KEYS}
    content = cleaned.get("content")
    if isinstance(content, list):
        cleaned["content"] = _unsplit(_strip_blocks(content) or [], boundary)
    return cleaned


def _unsplit(blocks: list[dict[str, Any]], boundary: object) -> Any:
    """The string these blocks were made from, or the blocks themselves.

    Undoing the key is not undoing the marking. To have somewhere to put a
    breakpoint, a placer rewrites string content into text blocks -- so removing
    the field alone still sends an Anthropic-shaped payload to a wire that was
    just judged unable to carry one, and "content must be a string" is among the
    commonest ways an OpenAI-compatible endpoint refuses. That refusal names
    neither the field nor prompt caching, so nothing learns from it either.

    Two shapes get undone and no others. One block is the plain rewrite of a
    string. Two blocks are the stable-prefix split -- but only when the message
    still declares the boundary and the first block ends exactly on it, because
    a caller is entitled to hand us multi-block content of its own and get it
    back unchanged. Joining is exact either way: the split was ``content[:n]``
    and ``content[n:]``.
    """
    texts = [b["text"] for b in blocks if isinstance(b, dict) and set(b) == {"type", "text"} and b["type"] == "text"]
    if not blocks or len(texts) != len(blocks):
        return blocks
    if len(blocks) == 1:
        return texts[0]
    if len(blocks) == 2 and isinstance(boundary, int) and not isinstance(boundary, bool):
        if boundary == len(texts[0]):
            return "".join(texts)
    return blocks


def _strip_blocks(blocks: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if blocks is None:
        return None
    return [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b for b in blocks]
