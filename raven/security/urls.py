"""What a URL must satisfy before raven talks to the address behind it.

The address vocabulary in :mod:`raven.security.hosts` says what a host string
denotes; this module says which of those a URL is allowed to name, and answers
by raising rather than by returning a verdict. Three boundaries read it: the
PlugHub catalogue, the Skill Hub client, and anything else handed an endpoint
it did not choose.

Distinct from :mod:`raven.security.network`, which resolves a hostname, pins
the address the verdict was made on, and connects to it. The checks here stay
at the string level on purpose -- see :func:`require_public_https`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from raven.security.hosts import as_ip, is_legacy_ip_form, is_local_name, names_this_machine, not_public


class HubTrustError(ValueError):
    """An endpoint or catalogue entry failed a trust check."""


def _host_is_local(host: str) -> bool:
    """Whether a URL host may buy the plaintext exemption: one of the names that
    mean this machine outright, or a loopback address in canonical form."""
    if is_local_name(host):
        return True
    if is_legacy_ip_form(host):
        return False
    addr = as_ip(host)
    return addr is not None and addr.is_loopback


def _split(url: str, what: str) -> Any:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise HubTrustError(f"{what} is not an absolute http(s) URL: {url!r}")
    if parts.username or parts.password:
        raise HubTrustError(f"{what} must not carry credentials in the URL: {url!r}")
    return parts


def _wire_host(url: str, parts: Any) -> str:
    """The host the HTTP client will really use, not the one urlsplit reports.

    They differ, and the difference was a bypass: IDNA maps the ideographic full
    stop U+3002 (and its fullwidth siblings) onto ``.``, so ``127.0.0.1`` spelled with them
    reaches urlsplit as one non-numeric label -- an ordinary hostname as far as
    every check here is concerned -- while httpx connects to 127.0.0.1. Judging
    the string the connect will use closes that whole class rather than one
    spelling of it.
    """
    try:
        import httpx

        return httpx.URL(url).host or (parts.hostname or "")
    except Exception:  # noqa: BLE001 -- a URL httpx cannot even parse is refused below
        return parts.hostname or ""


def require_https(url: str, *, what: str) -> str:
    """Accept ``https``, or ``http`` only when the host is this machine.

    Loopback plaintext stays allowed because that is how the hub itself is
    developed; anything else on the network must be encrypted or the catalogue
    is whatever the path decides it is.
    """
    parts = _split(url, what)
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and _host_is_local(_wire_host(url, parts)):
        return url
    raise HubTrustError(f"{what} must use https (plain http is allowed only on localhost): {url!r}")


def require_public_https(url: str, *, what: str) -> str:
    """As :func:`require_https`, and the host may not be a private address.

    Used for URLs the hub *hands back* (a presigned download): those are
    followed without any further check, so a hub that answers with
    ``https://127.0.0.1:9000/...`` would be aiming raven's own credentials-free
    fetch at services behind the user's firewall. Only IP literals and local
    names are rejected -- resolving a hostname here would be a check the
    subsequent connect could invalidate anyway.
    """
    parts = _split(url, what)
    if parts.scheme != "https":
        raise HubTrustError(f"{what} must use https: {url!r}")
    host = _wire_host(url, parts)
    if _host_is_local(host) or names_this_machine(host):
        raise HubTrustError(f"{what} must not point at this machine: {url!r}")
    if is_legacy_ip_form(host):
        raise HubTrustError(f"{what} must write an address as a dotted quad, not {host!r}: {url!r}")
    addr = as_ip(host)
    if addr is None:
        return url
    if not_public(addr):
        raise HubTrustError(f"{what} must not point inside a private network: {url!r}")
    return url


def hub_endpoint(env_value: str | None, default: str, *, what: str) -> str:
    """Resolve a hub base URL from an override, falling back to ``default``."""
    raw = (env_value or "").strip()
    if not raw:
        return default.rstrip("/")
    return require_https(raw, what=what).rstrip("/")


# A hub-supplied download is often presigned behind a redirect, so redirects
# cannot simply be refused there -- but they must be followed by hand, checking
# each hop. `follow_redirects=True` erases whatever a URL check established: a
# URL that passes require_public_https can 302 to http://127.0.0.1/admin, which
# is the exact request the check exists to prevent.
MAX_REDIRECTS = 4


def redirect_target(base: str, location: str, *, check, what: str) -> str:
    """The next hop of a redirect chain, validated like the first one."""
    from urllib.parse import urljoin

    return check(urljoin(base, location), what=f"{what} (redirect target)")


__all__ = [
    "MAX_REDIRECTS",
    "HubTrustError",
    "hub_endpoint",
    "redirect_target",
    "require_https",
    "require_public_https",
]
