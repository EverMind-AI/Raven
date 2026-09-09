"""The address vocabulary every URL policy in raven reads from.

Three policies decide differently -- the egress fetch refuses every destination
that is not public, the market allows plaintext to this machine only, the
browser refuses link-local and nothing else -- but they have to agree on what a
host string denotes: which spellings are addresses, which addresses a spelling
can reach, and which names mean this machine. That vocabulary lives here once.
A policy that parsed hosts on its own is how the same address stayed reachable
under a spelling that policy had not thought of.
"""

from __future__ import annotations

import ipaddress
import socket

import idna

Address = ipaddress.IPv4Address | ipaddress.IPv6Address

LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain"})

# Ranges refused as destinations, kept even though ``is_global`` already rejects
# every one of them: this is the floor that a regression in the standard
# library's classification cannot lower. Link-local is first because the cloud
# metadata service is what the exploit chain this check exists for went after.
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


def is_local_name(host: str) -> bool:
    """Whether ``host`` is one of the names that mean this machine outright.

    ``*.localhost`` is deliberately not one of them: hostile DNS can point
    ``evil.localhost`` at a public address, and a policy that treats it as
    loopback would let it buy the plaintext exemption.
    """
    return host.lower().rstrip(".") in LOCAL_NAMES


def names_this_machine(host: str) -> bool:
    """Whether ``host`` names this machine by name, ``*.localhost`` included.

    Wider than :func:`is_local_name` on purpose: ``*.localhost`` is loopback by
    RFC 6761, so a remote party handing back ``https://evil.localhost/x`` is
    pointing raven at itself. One name, two answers, depending on whether the
    question is "may it be plaintext" or "may it be a destination at all".
    """
    h = host.lower().rstrip(".")
    return h in LOCAL_NAMES or h.endswith(".localhost")


def as_ip(host: str) -> Address | None:
    """The address ``host`` denotes when written in canonical form, else None."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_legacy_ip_form(host: str) -> bool:
    """Whether ``host`` is an address written in a form resolvers disagree on.

    ``ipaddress`` parses only dotted quads, but a resolver also accepts
    ``127.1``, ``2130706433``, ``0x7f000001``, ``0`` and ``010.0.0.1`` -- and
    those disagree about what they mean: ``inet_aton`` reads ``010`` as octal 8
    while ``getaddrinfo`` on some platforms reads it as decimal 10. A policy
    that wants certainty refuses such a host outright; one that has to agree
    with a browser reads it with :func:`legacy_ipv4`.
    """
    bare = host.strip("[]")
    if as_ip(bare) is not None:
        return False
    try:
        socket.inet_aton(bare)
        return True
    except OSError:
        pass
    # inet_aton is stricter than some resolvers about leading zeros, so also
    # count anything whose labels are purely numeric -- no real hostname is.
    labels = bare.split(".")
    return bool(bare) and all(label.isdigit() for label in labels if label != "")


def legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse ``host`` the way a browser does, or None if it is not an address.

    Chromium implements the WHATWG host parser: ``2852039166``, ``0xA9FEA9FE``
    and ``0251.0376.0251.0376`` are all 169.254.169.254 to it. The rules are
    the WHATWG ones: at most four parts, each decimal, octal (leading zero) or
    hex (``0x``); every part but the last is one byte, and the last fills
    whatever the earlier parts left. ASCII-only, because WHATWG's IPv4 parser
    is: an Arabic-Indic or Devanagari host is an ordinary domain to a browser.
    """
    parts = host.split(".")
    if parts and parts[-1] == "":  # a trailing dot is still the same host
        parts = parts[:-1]
    if not parts or len(parts) > 4:
        return None
    numbers: list[int] = []
    for part in parts:
        if part[:2].lower() == "0x":
            body, base = part[2:], 16
        elif len(part) > 1 and part[0] == "0":
            body, base = part[1:], 8
        else:
            body, base = part, 10
        if not body or not body.isascii() or not all(c.isalnum() for c in body):
            return None
        try:
            value = int(body, base)
        except ValueError:
            return None
        if value < 0:
            return None
        numbers.append(value)
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (4 - (len(numbers) - 1)):
        return None
    total = numbers[-1]
    for i, n in enumerate(numbers[:-1]):
        total += n << (8 * (3 - i))
    try:
        return ipaddress.IPv4Address(total)
    except ValueError:
        return None


def mapped_host(host: str) -> str:
    """``host`` as a browser or HTTP client reads it, after UTS-46 mapping.

    Fullwidth stops and superscript digits map onto ASCII before a host is
    parsed, so ``169.254.169.254`` spelled with U+FF0E is that address to
    Chromium and to httpx while being one opaque label to ``urlsplit``. A host
    UTS-46 refuses outright is handed back unchanged: it is not an address, and
    no client will load it either.
    """
    try:
        return idna.uts46_remap(host, std3_rules=False, transitional=False)
    except Exception:  # noqa: BLE001 - a host this cannot map is not one we can shape-check
        return host


def embedded_v4(addr: Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address ``addr`` can reach through an IPv6 translation.

    The low-32-bit reading covers the prefixes with no standard-library
    accessor of their own. Two embeddings are deliberately not read: Teredo
    (``2001::/32``) and NAT64's local-use prefix (``64:ff9b:1::/48``, RFC
    8215), because ``is_global`` refuses both prefixes whole; reading them
    could not change a verdict, and each refusal is pinned by its own row in
    the threat table.
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return []
    found = [inner for inner in (addr.ipv4_mapped, addr.sixtofour) if inner is not None]
    if any(addr in net for net in _V4_IN_V6_PREFIXES):
        found.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return found


def is_public(addr: Address) -> bool:
    """Is this one address a globally routable unicast destination?

    Default-deny: everything that is not globally routable is refused, rather
    than enumerating what to refuse. A denylist answers "is this one of the
    ranges someone thought to list", which is the question that let every IPv6
    spelling of an internal address through; this asks "is this a place on the
    public internet", and a spelling nobody anticipated fails it by default.
    ``is_multicast`` is asked separately because ``is_global`` calls an
    assigned multicast group global -- true, and not a destination.
    """
    if not addr.is_global or addr.is_multicast:
        return False
    return not any(addr in net for net in _BLOCKED_NETWORKS)


def not_public(addr: Address) -> bool:
    """True when ``addr`` is not a public destination, in any of its spellings.

    Judged on the address itself and on every IPv4 address it can reach
    through an IPv6 translation, so the verdict cannot depend on which
    spelling arrived. A public address stays public in every spelling, its
    translated forms included: refusing ``64:ff9b::8.8.8.8`` would cut off
    IPv4-only destinations in a NAT64 network.
    """
    if not is_public(addr):
        return True
    return any(not is_public(inner) for inner in embedded_v4(addr))


__all__ = [
    "Address",
    "LOCAL_NAMES",
    "as_ip",
    "embedded_v4",
    "is_legacy_ip_form",
    "is_local_name",
    "is_public",
    "legacy_ipv4",
    "mapped_host",
    "names_this_machine",
    "not_public",
]
