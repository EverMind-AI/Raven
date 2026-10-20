"""The correlation token: a short, quotable handle on one outbound message.

A token says which message a reply belongs to. It is put in the outbound
message, and a reply that quotes it resolves back to the interaction that
sent it -- and from there to the record the message was about.

The token is random rather than derived, for one reason: it has to be
different for every attempt. A derived token would give two attempts at the
same subject the same handle, and the second attempt would look like the
first -- which is the confusion this package exists to prevent.

It is a handle, not a secret. It is quoted in the body of a message that
anybody may read, so it carries no authority: knowing one tells you which
message a reply belongs to and nothing about the record behind it.
"""

from __future__ import annotations

import re
import secrets

__all__ = ["TOKEN_PREFIX", "is_token", "new_token", "token_from_text"]

#: Every token starts with this, so a reply quoting one is recognisable
#: without the store being consulted.
TOKEN_PREFIX = "rv-"  # noqa: S105 -- the shape of a handle, not a credential

#: A token is the prefix and ten hex characters. Fixed width, so a quoted
#: token can be pulled out of a line of prose without ambiguity.
_TOKEN_RE = re.compile(rf"{TOKEN_PREFIX}[0-9A-F]{{10}}\b")


def new_token() -> str:
    """A fresh correlation token."""
    return TOKEN_PREFIX + secrets.token_hex(5).upper()


def is_token(value: object) -> bool:
    """Whether this string is a correlation token."""
    return isinstance(value, str) and bool(_TOKEN_RE.fullmatch(value))


def token_from_text(text: str) -> str | None:
    """The token quoted in this text, or None when there is not one.

    A reply quotes the token it is answering. Pulling it out of the prose is
    what lets a reply find the message it belongs to without the reply
    having been written for that purpose.
    """
    if not text:
        return None
    found = _TOKEN_RE.search(text)
    return found.group(0) if found else None
