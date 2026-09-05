"""What the shared page is allowed to navigate to.

The page is driven by the agent's tools as well as by a reader, and the agent's
instructions can come from a page it already visited. So a navigation target is
untrusted input even when the request arrives through a handler a person is
looking at, and the check belongs in the driver where both callers pass.

Two refusals, for two different reasons.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import idna

ALLOWED_SCHEMES = frozenset({"http", "https"})
"""Everything else is refused, and ``file:`` is why this module exists.

``browser.open("file:///Users/<me>/.raven/serve.json")`` followed by
``browser.read`` returns the gateway's session token as page text -- the split
that keeps that token out of a client's reach does not survive the browser being
able to read the disk. `chrome:`, `devtools:` and `view-source:` reach the same
class of thing by other routes, and `data:` / `blob:` are how a refused target
gets smuggled back in.

``about:blank`` is allowed by name: it is the empty page, not a scheme with
anything behind it.
"""

BLANK = "about:blank"

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")


class NavigationRefusedError(ValueError):
    """The target is not something this browser will open."""


def _mapped_host(host: str) -> str:
    """The host as a browser reads it, before anything looks at its shape.

    The WHATWG host parser runs *after* UTS-46 mapping, and this check used to
    run on the raw string: ``169．254．169．254`` (fullwidth stops) and a label
    ending in superscript digits are 169.254.169.254 to Chromium and unparseable
    here, so they went through. Real UTS-46 rather than NFKC, because the point
    of this module is to reach the same verdict as the browser rather than a
    close one -- the previous version of this check was trusted to agree and did
    not.

    A host UTS-46 refuses outright is handed back unchanged: it is not an
    address, and Chromium will not load it either.
    """
    try:
        return idna.uts46_remap(host, std3_rules=False, transitional=False)
    except Exception:  # noqa: BLE001 - a host this cannot map is not one we can shape-check
        return host


def _whatwg_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the host the way a browser does, or None if it is not an address.

    ``ipaddress.ip_address`` takes dotted-quad and nothing else, while Chromium
    implements the WHATWG host parser: ``2852039166``, ``0xA9FEA9FE`` and
    ``0251.0376.0251.0376`` are all 169.254.169.254 to it. Checking Python's
    answer while Chromium acts on its own is how a refused address stays
    reachable under another spelling.

    The rules are the WHATWG ones: at most four parts, each decimal, octal
    (leading zero) or hex (``0x``); every part but the last is one byte, and the
    last fills whatever the earlier parts left.
    """
    parts = host.split(".")
    if parts and parts[-1] == "":  # a trailing dot is still the same host
        parts = parts[:-1]
    if not parts or len(parts) > 4:
        return None
    numbers: list[int] = []
    for part in parts:
        # Spelled out rather than deferred to int(part, 0): python dropped the
        # bare-leading-zero octal literal, so `int("0251", 0)` raises -- which
        # is the one form that made this worth writing.
        if part[:2].lower() == "0x":
            body, base = part[2:], 16
        elif len(part) > 1 and part[0] == "0":
            body, base = part[1:], 8
        else:
            body, base = part, 10
        # ASCII-only, because WHATWG's IPv4 parser is: python's `int` and
        # `isalnum` read every Unicode digit range, so an Arabic-Indic or
        # Devanagari host -- which UTS-46 correctly leaves alone, and Chromium
        # reads as an ordinary domain -- parsed here as an address and got
        # refused for living somewhere it does not.
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


def _host_ip(host: str) -> ipaddress._BaseAddress | None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return _whatwg_ipv4(host)
    # ::ffff:169.254.169.254 is the v4 address wearing a v6 spelling, and a
    # network membership test short-circuits on version rather than raising, so
    # without this it passes both branches.
    return getattr(ip, "ipv4_mapped", None) or ip


def check_navigation(url: str) -> str:
    """Return the URL to navigate to, or raise :class:`NavigationRefusedError`.

    A bare ``example.com`` is completed to https, which is the behaviour a
    reader expects from an address bar; the completion happens before the check
    so the check sees what Chromium will.
    """
    candidate = url.strip()
    if not candidate:
        raise NavigationRefusedError("no url")
    if candidate == BLANK:
        return candidate

    # Matched on the scheme prefix, not on "://": `data:text/html,...` and
    # `javascript:...` carry no slashes, so completing anything without them to
    # https would turn a refusable target into an https request to a host named
    # after its payload -- harmless, and a confusing way to be harmless.
    declared = _SCHEME_RE.match(candidate)
    if declared and declared.group(1).lower() not in ALLOWED_SCHEMES:
        raise NavigationRefusedError(
            f"{declared.group(1)}: is not a scheme this browser opens; "
            f"only http and https are (asked for {candidate!r})"
        )
    target = candidate if declared else f"https://{candidate}"

    parts = urlsplit(target)
    host = _mapped_host(parts.hostname or "")
    if not host:
        raise NavigationRefusedError(f"no host in {candidate!r}")

    ip = _host_ip(host)
    if ip is not None and (ip in _LINK_LOCAL_V4 or ip in _LINK_LOCAL_V6):
        # 169.254.169.254 is the cloud metadata endpoint on every major
        # provider, and it answers credentials to anything that can make a plain
        # GET from the instance. Loopback and private ranges are deliberately
        # NOT refused with it: a reader pointing the page at their own dev server
        # is the ordinary case, and breaking that to close a hole that link-local
        # already closes would trade a real feature for no gain.
        raise NavigationRefusedError(f"{host} is link-local, which is where instance credentials live")
    # Not covered, and it cannot be from here: a *name* that resolves to a
    # link-local address -- metadata.google.internal, or any A record an
    # attacker publishes -- is indistinguishable from any other name on the URL
    # string. Closing that means checking where the address is known, not where
    # the URL is parsed. Said out loud because the refusal above otherwise reads
    # as a boundary, and someone will build on it as though it were one.

    return target


def navigation_refusal(url: str) -> str | None:
    """Why this URL is refused, or None if it is fine.

    The same rule as :func:`check_navigation`, asked in the shape the driver's
    request interceptor needs: it is handed a URL Chromium is already about to
    load -- from a click, a redirect, a popup -- and has to decide, not correct.
    """
    try:
        check_navigation(url)
    except NavigationRefusedError as exc:
        return str(exc)
    return None


__all__ = [
    "ALLOWED_SCHEMES",
    "BLANK",
    "NavigationRefusedError",
    "check_navigation",
    "navigation_refusal",
]
