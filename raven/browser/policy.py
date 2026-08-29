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

from raven.security.hosts import as_ip, embedded_v4, legacy_ipv4, mapped_host

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
    host = mapped_host(parts.hostname or "")
    if not host:
        raise NavigationRefusedError(f"no host in {candidate!r}")

    ip = as_ip(host) or legacy_ipv4(host)
    reachable = [ip, *embedded_v4(ip)] if ip is not None else []
    if any(a in _LINK_LOCAL_V4 or a in _LINK_LOCAL_V6 for a in reachable):
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
