"""Network security utilities — SSRF protection for outbound URL fetches.

Two layers: :func:`validate_url_target` / :func:`validate_resolved_url` answer
"may this address be fetched", and :func:`guarded_fetch` is the fetch that
keeps asking — a redirect chain followed one checked hop at a time. The second
layer is not optional: validating once and then handing the URL to a client
that follows redirects on its own checks the first address and none of the
ones it is sent to.

Ported from nanobot/security/network.py (MIT) with the module-level CIDR
allowlist removed; revisit if a Tailscale-style whitelist becomes needed.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urljoin, urlparse

from loguru import logger

from raven.security.hosts import not_public

if TYPE_CHECKING:
    import httpx

# Redirect chains have to end. Each hop costs a DNS resolution and a request,
# and no legitimate media URL needs many.
DEFAULT_MAX_REDIRECTS = 5


class _Fetcher(Protocol):
    """The one method :func:`guarded_fetch` needs from an HTTP client."""

    async def get(self, url: str, **kwargs: object) -> "httpx.Response": ...


def validate_url_target(url: str) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    Returns (ok, error_message). When ok is True, error_message is empty.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not_public(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate a URL after redirect resolution: the scheme, then the address the host resolves to."""
    try:
        p = urlparse(url)
    except Exception:
        return True, ""

    # A redirect naming a scheme we do not fetch is refused here rather than
    # left to the HTTP client: that a client happens not to support file:// is
    # not a security property, and this validator is what the caller asks
    # before every hop. A target with no scheme at all stays tolerated -- a
    # relative Location is legal HTTP, and the hostname check below still runs
    # on the protocol-relative form (``//host/path``).
    if p.scheme and p.scheme not in ("http", "https"):
        return False, f"Redirect target is not http/https: '{p.scheme}'"

    hostname = p.hostname
    if not hostname:
        return True, ""

    try:
        addr = ipaddress.ip_address(hostname)
        if not_public(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return False, f"Redirect target {hostname!r} did not resolve"
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if not_public(addr):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


async def guarded_fetch(
    client: _Fetcher,
    url: str,
    *,
    what: str,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> "httpx.Response | None":
    """GET ``url``, checking the target before every hop of the redirect chain.

    Returns the first non-redirect response, or ``None`` when a hop was refused
    or the chain ran too long -- both already logged, naming ``what`` so an
    operator can tell which caller declined.

    The chain is followed by hand, with ``follow_redirects=False`` on each
    request, because that is the only way each hop gets checked: a client that
    follows redirects itself turns one validated address into a chain of
    unvalidated ones, and the interesting hop is the one that points inward. A
    relative ``Location`` is resolved against the URL it came from, so every
    hop is an absolute URL that a check can be run on -- and so a legitimate
    relative redirect keeps working.

    Raises whatever the client raises: a transport failure is the caller's to
    classify (it decides what to tell the user), while a refusal is this
    function's verdict and comes back as ``None``.
    """
    current = url
    for hop in range(max_redirects + 1):
        ok, err = (validate_url_target if hop == 0 else validate_resolved_url)(current)
        if not ok:
            logger.warning("{}: refusing {} ({})", what, current, err)
            return None
        response = await client.get(current, follow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location") or ""
        if not location:
            logger.warning("{}: redirect from {} carried no location header", what, current)
            return None
        current = urljoin(current, location)
    logger.warning("{}: {} redirected more than {} times", what, url, max_redirects)
    return None
