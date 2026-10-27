"""The Upload Challenge: how a Bug Report Receiver tells Raven from any other HTTP client.

Before an upload the client asks the receiver for a challenge. The receiver
seals it with a key that lives only in its process memory -- a version, the
second it was issued, a random part, and an HMAC over the three -- so it keeps
no table of what it handed out, and a restart retires every challenge at once.
The client answers with an HMAC, under :data:`CLIENT_KEY`, of the challenge
together with the package's report id, sha256 and size, and sends both with
the upload. The receiver checks them from the request headers alone, before
it reads a byte of the body.

:data:`CLIENT_KEY` is published with the source and is not a credential: it
identifies nobody and is not configurable. What the gate stops is an HTTP
client that does not know this exchange, and an answer captured from one
upload being reused for a different package. What it does not stop is anyone
who has read the source, or Raven itself sending junk -- the receiver's
package checks, rate limits and budgets are what bound that. A challenge is
not consumed: replaying a whole captured request delivers the same package
again, which the receiver already answers as a duplicate.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

# Derived from a label rather than spelled as hex so it reads as the protocol
# constant it is, not as a leaked secret.
CLIENT_KEY = hashlib.sha256(b"raven bug report client gate v1").digest()
UNAUTHORIZED = "unauthorized"
CHALLENGE_EXPIRED = "challenge_expired"
MAX_CHALLENGE_CHARS = 256

_FUTURE_SLACK = 5.0
_CHALLENGE = re.compile(r"c1\.([0-9]{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})", re.ASCII)
_RESPONSE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_RESPONSE_LABEL = "raven-bugreport-auth-v1"


def _seal(server_key: bytes, body: str) -> str:
    return hmac.new(server_key, body.encode(), hashlib.sha256).hexdigest()


def issue_challenge(server_key: bytes, now: float) -> str:
    """A fresh challenge sealed with ``server_key``, issued at ``now`` (seconds since the epoch)."""
    body = f"c1.{int(now)}.{secrets.token_hex(16)}"
    return f"{body}.{_seal(server_key, body)}"


def challenge_problem(challenge: str, server_key: bytes, now: float, ttl: float) -> str | None:
    """Why ``challenge`` is refused (:data:`UNAUTHORIZED` or :data:`CHALLENGE_EXPIRED`), or None.

    The seal is checked before the time it carries, since a time in a
    challenge this receiver did not seal means nothing.
    """
    match = _CHALLENGE.fullmatch(challenge) if len(challenge) <= MAX_CHALLENGE_CHARS else None
    if match is None:
        return UNAUTHORIZED
    issued, nonce, seal = match.groups()
    if not hmac.compare_digest(seal, _seal(server_key, f"c1.{issued}.{nonce}")):
        return UNAUTHORIZED
    if int(issued) > now + _FUTURE_SLACK:
        return UNAUTHORIZED
    if now - int(issued) > ttl:
        return CHALLENGE_EXPIRED
    return None


def client_response(challenge: str, *, report_id: str, sha256: str, size_bytes: int) -> str:
    """The answer a Raven client gives to ``challenge`` for one package."""
    message = f"{_RESPONSE_LABEL}\n{challenge}\n{report_id}\n{sha256}\n{size_bytes}"
    # A header the server decoded with surrogate escapes must not make checking it raise.
    return hmac.new(CLIENT_KEY, message.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def response_matches(challenge: str, response: str, *, report_id: str, sha256: str, size_bytes: int) -> bool:
    if _RESPONSE.fullmatch(response) is None:
        return False
    expected = client_response(challenge, report_id=report_id, sha256=sha256, size_bytes=size_bytes)
    return hmac.compare_digest(response, expected)


__all__ = [
    "CHALLENGE_EXPIRED",
    "CLIENT_KEY",
    "MAX_CHALLENGE_CHARS",
    "UNAUTHORIZED",
    "challenge_problem",
    "client_response",
    "issue_challenge",
    "response_matches",
]
