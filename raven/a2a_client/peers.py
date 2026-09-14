"""Which remote A2A agents this host may call, and the credential for each.

The only place an outbound credential is attached. The model supplies a card
URL and never holds a secret, so an origin absent from the configured list is
called with no credential rather than with someone else's.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from raven.config.schema import A2aConfig, A2aPeerConfig

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def resolve_peer(config: A2aConfig, card_url: str) -> A2aPeerConfig | None:
    """The configured peer whose origin matches `card_url`, or None."""
    origin = _origin_of(card_url)
    if origin is None:
        return None
    for peer in config.peers:
        if _origin_of(peer.origin) == origin:
            return peer
    return None


def auth_headers(peer: A2aPeerConfig | None) -> dict[str, str]:
    """Request headers carrying `peer`'s credential; empty for an unlisted peer."""
    if peer is None or not peer.credential:
        return {}
    if peer.auth_scheme == "bearer":
        return {"Authorization": f"Bearer {peer.credential}"}
    return {peer.auth_scheme: peer.credential}
