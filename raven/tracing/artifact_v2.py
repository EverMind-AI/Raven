"""The ``audit.artifact.v2`` reference format: address, validate, resolve.

One module owns the whole wire shape because the bundled viewer mirrors it in
JavaScript (``raven/cli/tracing_viewer/server.js``) and the two must not
drift - a cross-language round-trip test compares this module's output against
the viewer's. The canonical serialization, the envelope key, the reference
validation and the per-field resolution rules all live here and nowhere else.

The wire shape itself is stdlib; the default reader decodes the store's zstd
frames through :mod:`raven.tracing.codec`, and falls back to a packed slice
through :mod:`raven.tracing.message_pack`, both imported lazily so this module
stays cheap to import.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from .codec import MAX_PLAINTEXT_BYTES, read_plaintext

ARTIFACT_FORMAT = "audit.artifact.v2"
MESSAGES_DIR_NAME = "_messages"
REF_KEY = "$msg"

# Fields whose reference resolves to the message's text rather than to the
# message object. ``app.js`` calls ``.match()`` on these, so handing it an
# object throws.
TEXT_FIELDS = ("systemPrompt", "prompt")

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def message_text(message: Any) -> str:
    """The canonical serialization a message's address is taken over.

    Key order is preserved, not sorted. Sorting would let two orderings of one
    message share a blob, but it also makes resolution hand back a reordered
    copy - and :data:`TEXT_FIELDS` carry that order baked inside a serialized
    string, so the round trip would stop being exact. Over 406,357 recorded
    message occurrences, sorting changed the distinct count by zero: messages
    are built by the same code path every turn, so the trade bought nothing.

    ``default=str`` differs from :func:`coerce_text`, which has no fallback
    encoder: a message carrying something unserializable is stored with that
    value coerced, where v1 would have stringified the whole content. Such a
    message cannot reach a provider, so the divergence is unreachable.

    This is part of the v2 contract: changing it re-addresses every message
    ever stored.
    """
    return json.dumps(message, ensure_ascii=False, default=str)


def message_sha1(message: Any) -> str:
    return hashlib.sha1(message_text(message).encode("utf-8")).hexdigest()


def make_ref(sha1: str) -> dict[str, str]:
    return {REF_KEY: sha1}


def ref_sha1(value: Any) -> str | None:
    """The address ``value`` references, or None when it is not a reference.

    Rejects anything but 40 lowercase hex characters before a caller can build
    a path from it: a shell is data, and ``../`` in this position would
    otherwise reach outside the message store.
    """
    if not isinstance(value, dict) or len(value) != 1:
        return None
    sha1 = value.get(REF_KEY)
    if not isinstance(sha1, str) or not _SHA1_RE.match(sha1):
        return None
    return sha1


def messages_dir(artifacts_dir: Path | str) -> Path:
    return Path(artifacts_dir) / MESSAGES_DIR_NAME


def message_path(artifacts_dir: Path | str, sha1: str) -> Path:
    return messages_dir(artifacts_dir) / sha1[:2] / f"{sha1}.json"


def is_v2(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("artifactFormat") == ARTIFACT_FORMAT


def coerce_text(value: Any) -> str:
    """The text a v1 ``systemPrompt`` / ``prompt`` field carried.

    ``separators`` is not cosmetic: JavaScript's ``JSON.stringify`` emits no
    space after ``:`` or ``,``, and the viewer resolves these same fields.
    Python's default separators would make a multimodal prompt resolve to
    different bytes on the two sides.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def placeholder(sha1: str) -> dict[str, str]:
    return {"role": "unknown", "content": f"[message blob missing: {sha1}]"}


def _read_stored_text(path: Path, limit: int = MAX_PLAINTEXT_BYTES) -> str:
    """A blob's plaintext, whichever encoding it was stored under."""
    plain = read_plaintext(path.read_bytes(), limit)
    if plain is None:
        raise ValueError("corrupt or oversized compressed message blob")
    return plain.decode("utf-8")


def _packed_text(artifacts_dir: Path | str, sha1: str, limit: int = MAX_PLAINTEXT_BYTES) -> str | None:
    from . import message_pack

    packed = message_pack.read_message(artifacts_dir, sha1, limit)
    return None if packed is None else packed.decode("utf-8")


def resolve_payload(
    payload: Any,
    artifacts_dir: Path | str,
    *,
    on_missing: Callable[[str], Any] | None = None,
    read: Callable[[Path], str | None] | None = None,
    max_total_bytes: int = MAX_PLAINTEXT_BYTES,
) -> Any:
    """A v2 shell as its v1 equivalent; any other payload returned unchanged.

    ``on_missing`` supplies the stand-in for an address whose blob is gone and
    is called once per distinct missing address, so a caller can record them.
    A message is never dropped: consumers compare message-list length and
    position, so omitting one shifts every later message and turns a locatable
    gap into a spurious full divergence. ``read`` replaces how a blob's text is
    read (None means it cannot be), for a caller that must bound what it reads.

    ``max_total_bytes`` bounds the plaintext one shell may pull in. A shell can
    name any number of addresses, and an imported one is untrusted input, so
    the addresses past the budget resolve to the missing-blob stand-in instead
    of being read.
    """
    if not is_v2(payload):
        return payload
    missing = placeholder if on_missing is None else on_missing
    cache: dict[str, Any] = {}
    used = 0

    def load(sha1: str) -> Any:
        nonlocal used
        if sha1 not in cache:
            text: str | None = None
            remaining = max_total_bytes - used
            if remaining > 0:
                try:
                    path = message_path(artifacts_dir, sha1)
                    text = _read_stored_text(path, remaining) if read is None else read(path)
                except (OSError, ValueError):
                    text = None
                if text is not None and len(text.encode("utf-8")) > remaining:
                    text = None
                if text is None and read is None:
                    # A packed address has no file; the other encoding of the
                    # same store is a pack slice. A caller-supplied read owns
                    # its own fallback, because it may bound or confine it.
                    text = _packed_text(artifacts_dir, sha1, remaining)
            try:
                if text is None:
                    raise OSError(f"blob {sha1} could not be read")
                cache[sha1] = json.loads(text)
                used += len(text.encode("utf-8"))
            except (OSError, ValueError):
                cache[sha1] = missing(sha1)
        return cache[sha1]

    def one(value: Any) -> Any:
        sha1 = ref_sha1(value)
        return load(sha1) if sha1 is not None else value

    out = {key: value for key, value in payload.items() if key != "artifactFormat"}
    if isinstance(out.get("messages"), list):
        out["messages"] = [one(item) for item in out["messages"]]
    for field in TEXT_FIELDS:
        if field in out:
            resolved = one(out[field])
            out[field] = coerce_text(resolved.get("content") if isinstance(resolved, dict) else resolved)
    return out
