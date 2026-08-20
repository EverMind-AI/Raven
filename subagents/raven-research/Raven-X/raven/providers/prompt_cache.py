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
    cleaned = {k: v for k, v in message.items() if k != "cache_control"}
    content = cleaned.get("content")
    if isinstance(content, list):
        blocks = _strip_blocks(content) or []
        # Undoing the key is not undoing the marking. To have somewhere to put a
        # breakpoint, the strategy rewrites string content into a one-element
        # text block -- so removing the field alone still sends an
        # Anthropic-shaped payload to a wire that was just judged unable to carry
        # one, and "content must be a string" is among the commonest ways an
        # OpenAI-compatible endpoint refuses. That refusal names neither the
        # field nor prompt caching, so nothing learns from it either.
        only = blocks[0] if len(blocks) == 1 and isinstance(blocks[0], dict) else None
        if only is not None and set(only) == {"type", "text"} and only["type"] == "text":
            cleaned["content"] = only["text"]
        else:
            cleaned["content"] = blocks
    return cleaned


def _strip_blocks(blocks: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if blocks is None:
        return None
    return [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b for b in blocks]
