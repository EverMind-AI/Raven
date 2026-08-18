"""Strip credentials out of log records before they reach a sink.

Some upstreams put the credential in the URL itself -- the Telegram Bot API
keys every route on ``/bot<token>/``, and several providers take an API key as
a query parameter -- so any library that logs its request line (httpx does, at
INFO) writes a working credential into a persisted, retained file. The gateway
log is the one users attach to a bug report, which is exactly the wrong place
for it.

This is the message-body counterpart to ``diagnose=False`` in
:mod:`raven.cli._log_file`, which already keeps tracebacks from serializing
locals holding secrets.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram: /bot<id>:<secret>/. The numeric id stays -- it identifies the
    # bot for debugging and is not the secret half.
    (re.compile(r"/bot(\d{5,}):[A-Za-z0-9_-]{15,}"), rf"/bot\1:{REDACTED}"),
    # Credential passed as a query parameter (Gemini's ?key=, and friends).
    (
        re.compile(r"([?&](?:api[-_]?key|access[-_]?token|auth[-_]?token|token|key|secret)=)[^&\s\"']+", re.I),
        rf"\1{REDACTED}",
    ),
    # Basic-auth credentials embedded in a URL.
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), rf"\1{REDACTED}@"),
    # Authorization: Bearer <token>.
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.I), rf"\1{REDACTED}"),
    # Bare vendor-prefixed keys that appear outside any URL.
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|xox[abprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,})"), REDACTED),
)


def redact(text: str) -> str:
    """Return ``text`` with every known credential shape masked."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redacting_filter(record: Any) -> bool:
    """Loguru sink filter that rewrites the record in place, always keeping it.

    Loguru formats a record *after* its filters run, so mutating
    ``record["message"]`` here is what reaches every sink.
    """
    message = record.get("message")
    if isinstance(message, str):
        record["message"] = redact(message)
    return True


def combine_filters(*filters: Any) -> Any:
    """Chain sink filters, dropping the record as soon as one rejects it.

    The redacting filter must run even when a caller supplied its own
    noise-dropping filter, so the two are composed rather than one replacing
    the other.
    """
    active = [f for f in filters if f is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def _chained(record: Any) -> bool:
        return all(f(record) for f in active)

    return _chained


__all__ = ["REDACTED", "combine_filters", "redact", "redacting_filter"]
