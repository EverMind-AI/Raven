"""Which remote A2A agents this host may call, and the credential for each.

The only place an outbound credential is attached. The model supplies a card
URL and never holds a secret, so an origin absent from the configured list is
called with no credential rather than with someone else's.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from raven.config.schema import A2aConfig, A2aPeerConfig

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _origin_of(url: str) -> tuple[str, str, str, int | None] | None:
    """Extract and canonicalize origin as (scheme, userinfo, host, effective_port).

    Canonicalization includes:
    - Lowercase scheme and host (RFC 3986 6.2.2.1)
    - Strip trailing dot from FQDN
    - Drop default ports (80 for http, 443 for https)
    - Preserve userinfo (required to prevent authority confusion attacks)
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme not in _ALLOWED_SCHEMES or not parts.netloc:
        return None

    scheme = parts.scheme.lower()

    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
    else:
        userinfo = ""
        hostport = netloc

    if hostport.startswith("["):
        if "]" in hostport:
            host, portpart = hostport.rsplit("]", 1)
            host = host[1:]
            port_str = portpart.lstrip(":")
        else:
            return None
    else:
        if ":" in hostport:
            host, port_str = hostport.rsplit(":", 1)
        else:
            host = hostport
            port_str = ""

    canonical_host = host.lower().rstrip(".")

    if port_str:
        try:
            port = int(port_str)
        except ValueError:
            return None
    else:
        port = None

    if port is None:
        effective_port = None
    else:
        default_port = 443 if scheme == "https" else 80
        effective_port = None if port == default_port else port

    return (scheme, userinfo, canonical_host, effective_port)


def resolve_peer(config: A2aConfig, card_url: str) -> A2aPeerConfig | None:
    """The configured peer whose origin matches `card_url`, or None."""
    card_origin = _origin_of(card_url)
    if card_origin is None:
        return None
    for peer in config.peers:
        peer_origin = _origin_of(peer.origin)
        if peer_origin == card_origin:
            return peer
    return None


def same_origin(url_a: str, url_b: str) -> bool:
    """Whether `url_a` and `url_b` canonicalize to the same origin.

    Built on `_origin_of` so every origin comparison in this package -- peer
    lookup and this one -- shares one canonicalization and can't drift apart.
    Either URL failing to parse as http(s) counts as "not the same origin".
    """
    origin_a = _origin_of(url_a)
    return origin_a is not None and origin_a == _origin_of(url_b)


def auth_headers(peer: A2aPeerConfig | None) -> dict[str, str]:
    """Request headers carrying `peer`'s credential; empty for an unlisted peer."""
    if peer is None or not peer.credential:
        return {}
    if peer.auth_scheme.lower() == "bearer":
        return {"Authorization": f"Bearer {peer.credential}"}
    return {peer.auth_scheme: peer.credential}
