"""Reading a call's transport facts off the client library, bounded and scrubbed.

The machinery behind :class:`raven.contracts.llm_provider.CallRecord`. Everything
it reads was already in hand at the provider boundary and was dropped before the
tracing extractor ran: the status code survived only as an
``ErrorClassification.category``, the response headers were never read at all,
and the serving backend -- which OpenRouter names in the body -- went out with
the parsed response object. So a failure that says nothing on its own
(HTTP 200, empty content, ``finish_reason: "stop"``, usage all zeros) left a
record that could not distinguish a gateway refusing a payload from one backend
of a fallback order misbehaving.

Three constraints shape what is kept:

* **Bounded.** A record sits beside a prompt that can already be tens of
  megabytes; it must not add a second copy of anything. The body is capped, the
  headers are capped in count and in value length, and a record never carries
  the request.
* **No credentials.** Response headers are kept by exclusion rather than by an
  allow list -- the header that explains an incident is exactly the one an allow
  list would not have thought of -- so the name is tested against
  :data:`_SECRET_HEADER` and a hit keeps the name with its value replaced. The
  body goes through :func:`_scrub`, deliberately a handful of patterns rather
  than the full table in ``raven.acp.redact``: the import contract forbids
  ``raven.providers`` reaching a surface package, and a response body is not a
  config file. The one realistic echo is a 401 quoting the request's own
  ``Authorization`` header, which is what the patterns are chosen for.
* **No image payloads.** A picture reaches here only inside the request, which
  this module never touches. How many rode along and what they weighed is
  counted on the ``llm.input`` side, from the messages, by
  ``raven.observability.semconv``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from raven.contracts.llm_provider import CallRecord

#: A response body is stored to be read by a person diagnosing one call. Past
#: this the head is kept and ``body_truncated`` says so -- an empty or refused
#: response is far smaller than this, and a body that is not is one whose first
#: few thousand characters already say which kind of failure it is.
MAX_BODY_CHARS = 8_000

#: A response header set runs to a couple of dozen entries; past this the rest
#: are dropped and ``x-dropped-header-count`` says how many.
MAX_HEADERS = 40

#: One header value. Long enough for a full ``x-ratelimit`` block or a gateway's
#: routing trace, short enough that forty of them stay a footnote.
MAX_HEADER_VALUE_CHARS = 300

#: The client library prefixes every non-OpenAI response header with this before
#: handing the set over (``litellm_core_utils.core_helpers.process_response_headers``).
#: Stripped for the secret test only, so ``llm_provider-authorization`` cannot
#: walk past a pattern anchored on the bare name; the stored key keeps the
#: prefix, because which side of the gateway a header came from is a fact.
_LIBRARY_HEADER_PREFIX = "llm_provider-"

#: Header names whose value is a credential or a session. Matched anywhere in the
#: name so ``x-api-key`` and ``proxy-authorization`` both land. ``token`` has to
#: end a segment: unanchored it also matched the plural inside
#: ``x-ratelimit-remaining-tokens`` and ``anthropic-ratelimit-input-tokens-remaining``,
#: which are counters, and replacing them cost the rate-limit block a reader of a
#: 429 record is looking for. A singular ``token`` in any segment still lands
#: (``x-auth-token``, ``access_token``, ``x-session-token-hash``).
_SECRET_HEADER = re.compile(
    r"authorization|api[-_]?key|(?:^|[-_])token(?:$|[-_])|secret|password|cookie|credential|x-amz-security",
    re.IGNORECASE,
)

#: Credentials as they appear inside a *response* body -- a gateway quoting the
#: request's own auth header back in a 401, and vendor-shaped keys that are
#: recognisable with no label beside them. Each pattern captures only the
#: secret, so the line stays readable and says what was there.
_BODY_SECRETS: tuple[re.Pattern[str], ...] = (
    re.compile(r"((?:Proxy-)?Authorization\"?\s*[:=]\s*\"?(?:Bearer|Basic|Token)?\s*)([\w\-.~+/=]{8,})", re.IGNORECASE),
    re.compile(r"(api[_-]?key\"?\s*[:=]\s*\"?)([^\s\"',;&|)]{6,})", re.IGNORECASE),
    re.compile(r"()(sk-(?:proj-|ant-|or-)?[A-Za-z0-9_-]{16,})"),
    re.compile(r"()(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
)

REPLACEMENT = "[redacted]"


def _scrub(text: str) -> str:
    """``text`` with the credential shapes a response body can carry replaced."""
    for pattern in _BODY_SECRETS:
        text = pattern.sub(lambda m: m.group(1) + REPLACEMENT, text)
    return text


def _bounded_body(value: Any) -> tuple[str | None, bool]:
    """``value`` as a scrubbed, capped string, and whether it was capped.

    Serialized with ``default=str`` because what arrives is the library's own
    parsed object graph, not JSON -- and a record that raised on an unexpected
    member would take the diagnosis down with it.
    """
    if value is None:
        return None, False
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - the record must never be the thing that fails
            text = str(value)
    text = _scrub(text)
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS], True
    return text, False


def keep_headers(headers: Any) -> dict[str, str]:
    """The response headers worth storing: everything, minus the credentials.

    Bounded twice over (:data:`MAX_HEADERS`, :data:`MAX_HEADER_VALUE_CHARS`) and
    reported when the count bound bites, so a reader can tell a gateway that
    sent little from a record that kept little.

    Any mapping, not a ``dict``: the two paths that reach here hand over
    different types. The client library's own ``additional_headers`` is a plain
    dict, and an exception's ``response.headers`` is an ``httpx.Headers`` --
    a ``Mapping`` but not a ``dict``, so a ``dict`` test dropped every header
    off the production failure path, which is the one where the request id and
    the rate-limit block are the whole diagnosis.
    """
    if not isinstance(headers, Mapping):
        return {}
    kept: dict[str, str] = {}
    dropped = 0
    for name, value in headers.items():
        if not isinstance(name, str):
            continue
        if len(kept) >= MAX_HEADERS:
            dropped += 1
            continue
        bare = name.lower()
        if bare.startswith(_LIBRARY_HEADER_PREFIX):
            bare = bare[len(_LIBRARY_HEADER_PREFIX) :]
        if _SECRET_HEADER.search(bare):
            kept[name] = REPLACEMENT
            continue
        text = value if isinstance(value, str) else str(value)
        kept[name] = text[:MAX_HEADER_VALUE_CHARS]
    if dropped:
        kept["x-dropped-header-count"] = str(dropped)
    return kept


def _hidden(obj: Any, key: str) -> Any:
    """One entry of the library's ``_hidden_params``, whichever shape it is in.

    It is a plain dict on a response the library assembled itself and a
    ``HiddenParams`` model when a router built it, and the two are read
    differently.
    """
    params = getattr(obj, "_hidden_params", None)
    if isinstance(params, dict):
        return params.get(key)
    return getattr(params, key, None) if params is not None else None


def from_response(response: Any, *, usable: bool) -> CallRecord:
    """The record for a completed non-streaming call.

    ``usable`` is the caller's verdict on whether the response delivered
    anything (content or a tool call). Only an unusable one stores its body: a
    delivered answer is already recorded as ``content``, and storing it twice
    would double the size of every record for nothing.

    A parsed response means the client library accepted the HTTP exchange, which
    it only does on a 2xx -- so 200 is a fact about this call, and recording it
    is what lets "an empty 200" be read back as one rather than inferred from
    the absence of an error.
    """
    return CallRecord(
        http_status=200,
        # The response's own word for who served it. ``ModelResponse`` allows
        # extra fields, so a gateway that names the backend in the body (
        # OpenRouter's ``provider``) keeps it as an attribute; one that does not
        # leaves this None rather than guessing from the model id.
        served_by=_text(getattr(response, "provider", None)),
        served_model=_text(getattr(response, "model", None)),
        response_id=_text(getattr(response, "id", None)),
        headers=keep_headers(_hidden(response, "additional_headers")),
        **_body_fields(None if usable else _dump(response)),
    )


def from_stream(stream: Any, chunk: Any = None) -> CallRecord:
    """The record for a stream, read off the wrapper and its first chunk.

    Taken at open rather than at the end because that is when the headers exist
    and while the wrapper still holds them; the accumulator that builds the
    response from deltas never sees this object. The body is not stored here for
    the same reason it is not stored for a usable response -- and what stands in
    for it on a stream is the retained chunks, which :func:`stream_body` reads
    when the accumulated answer turns out to be empty.

    Only what upstream actually said. The wrapper's ``custom_llm_provider`` and
    ``model`` are its own constructor arguments -- the request's adapter and the
    request's model id -- so reading them as serving attribution made
    ``served_by`` a second copy of the logical ``llm.provider`` while reading as
    the backend a gateway named, and ``served_model`` an echo of what was asked
    for. There is no response-side model on this path at all:
    ``CustomStreamWrapper.model_response_creator`` pops the upstream chunk's own
    ``model`` before substituting the request's, so every normalized chunk
    carries the request's id too. What did come from upstream is the response id
    (the wrapper sets it from the first chunk's ``id``), the response headers,
    and a ``provider`` member a gateway put in the chunk body -- OpenRouter
    names its backend there, and an unknown top-level member survives on the
    chunk model's extras. Absent any of those, the field stays absent.
    """
    return CallRecord(
        http_status=200,
        served_by=_text(getattr(chunk, "provider", None)),
        response_id=_text(getattr(stream, "response_id", None)),
        headers=keep_headers(_hidden(stream, "additional_headers")),
    )


def from_exception(exc: BaseException, *, status: int | None = None) -> CallRecord:
    """The record for a call the client library raised on.

    ``status`` comes from the caller, which has already walked the exception
    chain for one (``LLMProvider._extract_status_code``) -- asking twice would
    put a second answer to the same question in the tree.
    """
    response = getattr(exc, "response", None)
    body: Any = None
    if response is not None:
        try:
            body = response.text
        except Exception:  # noqa: BLE001 - a streamed body cannot be re-read; str(exc) still can
            body = None
    return CallRecord(
        http_status=status,
        headers=keep_headers(getattr(response, "headers", None) if response is not None else None),
        **_body_fields(body if body else str(exc)),
    )


def with_body(record: CallRecord | None, body: Any) -> CallRecord:
    """``record`` with ``body`` filled in, for a verdict reached after the fact.

    A stream's record is built when it opens, before anyone can know whether the
    deltas will add up to an answer; this is how the accumulator attaches the
    evidence once they do not.
    """
    base = record or CallRecord()
    text, truncated = _bounded_body(body)
    return CallRecord(
        http_status=base.http_status,
        served_by=base.served_by,
        served_model=base.served_model,
        response_id=base.response_id,
        headers=base.headers,
        body=text,
        body_truncated=truncated,
    )


#: How many of a stream's trailing chunks stand in for its body. Three rather
#: than one because the reason can arrive a chunk before the finish reason does.
_BODY_CHUNKS = 3


def stream_body(stream: Any) -> Any:
    """The raw chunks a stream wrapper retained, as the stand-in for its body.

    A stream has no single body to keep. What answers the same question is the
    chunks themselves -- above all the terminal one, which is where a gateway
    puts the reason it is ending a call it never ran. The tail is taken rather
    than the head: the opening chunks of an empty response say nothing, and the
    cap in :func:`_bounded_body` would otherwise spend itself on them.
    """
    chunks = getattr(stream, "chunks", None)
    if not isinstance(chunks, list) or not chunks:
        return None
    return [_dump(chunk) for chunk in chunks[-_BODY_CHUNKS:]]


def _dump(obj: Any) -> Any:
    """``obj`` as plain data, through the library's own serializer when it has one."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001 - fall back rather than lose the record
            pass
    return obj


def _body_fields(body: Any) -> dict[str, Any]:
    text, truncated = _bounded_body(body)
    return {"body": text, "body_truncated": truncated}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_payload(record: CallRecord | None) -> dict[str, Any] | None:
    """The record as it appears in the ``llm.output`` artifact, or None.

    Absent fields are dropped rather than written as nulls: a record whose only
    fact is a status code should read as one fact, not as six blanks and a
    number.
    """
    if record is None:
        return None
    out: dict[str, Any] = {}
    if record.http_status is not None:
        out["http_status"] = record.http_status
    for key in ("served_by", "served_model", "response_id"):
        value = getattr(record, key)
        if value:
            out[key] = value
    if record.headers:
        out["headers"] = dict(record.headers)
    if record.body is not None:
        out["body"] = record.body
        if record.body_truncated:
            out["body_truncated"] = True
    return out or None


__all__ = [
    "MAX_BODY_CHARS",
    "MAX_HEADERS",
    "MAX_HEADER_VALUE_CHARS",
    "REPLACEMENT",
    "as_payload",
    "from_exception",
    "from_response",
    "from_stream",
    "keep_headers",
    "stream_body",
    "with_body",
]
