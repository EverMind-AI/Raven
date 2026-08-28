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

if TYPE_CHECKING:
    import httpx

# Redirect chains have to end. Each hop costs a DNS resolution and a request,
# and no legitimate media URL needs many.
DEFAULT_MAX_REDIRECTS = 5


class _Fetcher(Protocol):
    """The one method :func:`guarded_fetch` needs from an HTTP client."""

    async def get(self, url: str, **kwargs: object) -> "httpx.Response": ...


_Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# Ranges refused outright, kept even though ``is_global`` already rejects every
# one of them: this is the floor that a regression in the standard library's
# classification cannot lower. Link-local is first because the cloud metadata
# service is what the exploit chain this check exists for went after.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    # Deprecated site-local (RFC 3879). No standard-library property reports
    # it: is_private, is_reserved and is_global all call fec0::1 a perfectly
    # good public destination, so this line is the only thing refusing it.
    ipaddress.ip_network("fec0::/10"),
]

# Forms that carry an IPv4 address in their low 32 bits. Spelled this way an
# internal IPv4 destination is the same destination, so the inner address is
# judged too -- ``::ffff:169.254.169.254`` reached the cloud metadata service
# through a check that only knew the ranges above.
_V4_IN_V6_PREFIXES = [
    ipaddress.ip_network("::/96"),  # IPv4-compatible (RFC 4291, deprecated)
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped
    ipaddress.ip_network("::ffff:0:0:0/96"),  # IPv4-translated (RFC 2765)
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 well-known (RFC 6052)
]


def _embedded_v4(addr: _Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address ``addr`` can reach through an IPv6 translation.

    The low-32-bit reading covers the prefixes with no standard-library
    accessor of their own. Two embeddings are deliberately not read: Teredo
    (``2001::/32``) and NAT64's local-use prefix (``64:ff9b:1::/48``, RFC
    8215), because ``is_global`` refuses both prefixes whole. Reading them
    could not change a verdict, and unreachable machinery in a security check
    reads as protection where the protection is actually elsewhere. The
    refusal of each is pinned by its own row in the threat table, so if the
    standard library ever stops refusing the prefix, a test says so.
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return []
    found = [inner for inner in (addr.ipv4_mapped, addr.sixtofour) if inner is not None]
    if any(addr in net for net in _V4_IN_V6_PREFIXES):
        found.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return found


def _is_fetchable(addr: _Address) -> bool:
    """Is this one address a globally routable unicast destination?

    Default-deny: everything that is not globally routable is refused, rather
    than enumerating what to refuse. A denylist answers "is this one of the
    ranges someone thought to list", which is the question that let every IPv6
    spelling of an internal address through; this asks "is this a place on the
    public internet", and a spelling nobody anticipated fails it by default.
    ``is_multicast`` is asked separately because ``is_global`` calls an
    assigned multicast group global -- true, and not a fetch target.
    """
    if not addr.is_global or addr.is_multicast:
        return False
    return not any(addr in net for net in _BLOCKED_NETWORKS)


def _is_private(addr: _Address) -> bool:
    """True when ``addr`` must not be fetched.

    Judged on the address itself and on every IPv4 address it can reach
    through an IPv6 translation, so the verdict cannot depend on which
    spelling arrived. A public address stays fetchable in every spelling,
    its translated forms included: refusing ``64:ff9b::8.8.8.8`` would cut off
    IPv4-only destinations in a NAT64 network.

    The name is historical -- what it reports is "not a public destination",
    which is wider than private.
    """
    if not _is_fetchable(addr):
        return True
    return any(not _is_fetchable(inner) for inner in _embedded_v4(addr))


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
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate a URL after redirect resolution. Only checks the IP path, skips strict DNS errors."""
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
        if _is_private(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr):
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
