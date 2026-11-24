"""Network security utilities — SSRF protection for outbound URL fetches.

Two layers: :func:`validate_url_target` / :func:`validate_resolved_url` answer
"may this address be fetched", and :func:`guarded_fetch` is the fetch that
keeps asking — a redirect chain followed one checked hop at a time. The second
layer is not optional: validating once and then handing the URL to a client
that follows redirects on its own checks the first address and none of the
ones it is sent to.

The fetch also connects to the address the judge saw. One resolution per hop
serves both the verdict and the connection, so a DNS answer that changes
between them — rebinding — has nothing left to bite; the URL keeps the
hostname, so Host, SNI and the certificate check still name the site.

Ported from nanobot/security/network.py (MIT) with the module-level CIDR
allowlist removed; revisit if a Tailscale-style whitelist becomes needed.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urljoin, urlparse

from loguru import logger

from raven.security.hosts import as_ip, is_legacy_ip_form, names_this_machine, not_public

if TYPE_CHECKING:
    import httpx

# Redirect chains have to end. Each hop costs a DNS resolution and a request,
# and no legitimate media URL needs many.
DEFAULT_MAX_REDIRECTS = 5


class _Fetcher(Protocol):
    """The one method :func:`guarded_fetch` needs from an HTTP client."""

    async def get(self, url: str, **kwargs: object) -> "httpx.Response": ...


# Names a resolver other than the public DNS answers: a single label is completed
# with the resolving machine's search domain, and these suffixes are reserved for
# site-local or link-local naming. Refused only when the proxy resolves the name
# (``resolve=False``): there the proxy's own network is what such a name reaches.
_SITE_LOCAL_SUFFIXES = (".local", ".internal", ".localdomain", ".home.arpa", ".intranet", ".lan", ".corp")


def _wire_host(url: str, hostname: str) -> str:
    """The host an HTTP client will connect to, after IDNA/UTS-46 mapping.

    ``urlparse`` reports ``169\uff0e254\uff0e169\uff0e254`` as one opaque label;
    httpx maps the fullwidth stops onto ``.`` and connects to the metadata
    address. Judging the string the client will use closes that class.
    """
    try:
        import httpx

        return (httpx.URL(url).host or hostname).lower().rstrip(".")
    except Exception:  # noqa: BLE001 -- a URL httpx cannot parse is judged on urlparse's reading
        return hostname.lower().rstrip(".")


def _judge_name_unresolved(host: str) -> str:
    """Why ``host`` may not be handed to a proxy for remote resolution, or "".

    Without a local resolution the address behind a name is unknown here, so
    every spelling that denotes an address, or a machine on the resolver's own
    network, is refused by its shape: this machine's names (``*.localhost``
    included), an IPv4 address written any way but as a dotted quad
    (``2130706433``, ``0x7f.1``, ``127.1``), a single label and the site-local
    suffixes.
    """
    if names_this_machine(host):
        return f"Blocked: {host} names this machine"
    if is_legacy_ip_form(host):
        return f"Blocked: {host} is an address written in a non-canonical form"
    if "." not in host:
        return f"Blocked: {host} is a single-label name, resolved on the proxy's own network"
    if host.endswith(_SITE_LOCAL_SUFFIXES):
        return f"Blocked: {host} is a site-local name"
    return ""


def _judge_infos(hostname: str, infos: list) -> tuple[list[str], list[str]]:
    """Split a resolver answer into the public addresses and the refused ones.

    A mixed answer is filtered, not refused whole: the fetch is pinned to a
    judged address, so an address left out is one the connection never dials.
    Refusing the whole name because one record was internal made the verdict
    depend on which records a resolver happened to return -- a poisoned or
    synthesising resolver (one that answers ``2001::1`` next to a real A record)
    blocked public sites some of the time.
    """
    public: list[str] = []
    refused: list[str] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        (refused if not_public(addr) else public).append(str(addr))
    if refused and public:
        logger.debug("{}: ignoring non-public records {} next to {}", hostname, refused, public)
    return public, refused


def judge_url_target(url: str, *, resolve: bool = True) -> tuple[bool, str, tuple[str, ...]]:
    """Judge a URL and return the addresses the verdict was made on.

    Returns ``(ok, error_message, addresses)``. The addresses are what the
    hostname resolved to when it was judged -- the caller that connects to
    one of them is connecting to what was actually checked. Empty when the
    target needs no pin (a literal-IP URL, or ``resolve=False``) or the
    verdict is a refusal.

    ``resolve=False`` is for a fetch that goes through an HTTP proxy: the proxy
    resolves the name, so a local answer would judge an address the connection
    never uses -- and pinning it would override the proxy's correct resolution
    with a local one that may be poisoned or missing. The name is then judged by
    its shape alone (see :func:`_judge_name_unresolved`); literal addresses are
    judged as always.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e), ()

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'", ()
    if not p.netloc:
        return False, "Missing domain", ()

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname", ()

    host = _wire_host(url, hostname) if not resolve else hostname
    literal = as_ip(host)
    if literal is not None:
        if not_public(literal):
            return False, f"Blocked: {host} is a private/internal address", ()
        return True, "", ()

    if not resolve:
        reason = _judge_name_unresolved(host)
        return (False, reason, ()) if reason else (True, "", ())

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}", ()

    public, refused = _judge_infos(hostname, infos)
    if not public:
        if refused:
            return False, f"Blocked: {hostname} resolves to private/internal address {refused[0]}", ()
        return False, f"Cannot resolve hostname: {hostname}", ()
    return True, "", tuple(public)


def validate_url_target(url: str, *, resolve: bool = True) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    Returns (ok, error_message). When ok is True, error_message is empty.
    ``resolve=False``: the fetch goes through a proxy that resolves the name;
    see :func:`judge_url_target`.
    """
    ok, err, _ = judge_url_target(url, resolve=resolve)
    return ok, err


def judge_resolved_url(url: str, *, resolve: bool = True) -> tuple[bool, str, tuple[str, ...]]:
    """Judge a redirect target and return the addresses the verdict was made on.

    Same tolerances as :func:`validate_resolved_url`; the third element is
    what :func:`judge_url_target` returns it as, and ``resolve`` means what it
    means there.
    """
    try:
        p = urlparse(url)
    except Exception:
        return True, "", ()

    # A redirect naming a scheme we do not fetch is refused here rather than
    # left to the HTTP client: that a client happens not to support file:// is
    # not a security property, and this validator is what the caller asks
    # before every hop. A target with no scheme at all stays tolerated -- a
    # relative Location is legal HTTP, and the hostname check below still runs
    # on the protocol-relative form (``//host/path``).
    if p.scheme and p.scheme not in ("http", "https"):
        return False, f"Redirect target is not http/https: '{p.scheme}'", ()

    hostname = p.hostname
    if not hostname:
        return True, "", ()

    host = _wire_host(url, hostname) if not resolve else hostname
    literal = as_ip(host)
    if literal is not None:
        if not_public(literal):
            return False, f"Redirect target is a private address: {literal}", ()
        return True, "", ()

    if not resolve:
        reason = _judge_name_unresolved(host)
        return (False, f"Redirect target refused: {reason}", ()) if reason else (True, "", ())

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Redirect target {hostname!r} did not resolve", ()
    public, refused = _judge_infos(hostname, infos)
    if not public:
        if refused:
            return False, f"Redirect target {hostname} resolves to private address {refused[0]}", ()
        return False, f"Redirect target {hostname!r} did not resolve", ()
    return True, "", tuple(public)


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate a URL after redirect resolution: the scheme, then the address the host resolves to.

    The resolved-side sibling of :func:`validate_url_target`, kept for API
    symmetry: production redirects go through :func:`judge_resolved_url`
    inside ``guarded_fetch`` (one resolution serves verdict and connection),
    so today this wrapper's callers are the security tests that pin the
    verdict logic in isolation.
    """
    ok, err, _ = judge_resolved_url(url)
    return ok, err


def _pin_request(current: str, addrs: tuple[str, ...]) -> tuple[str, dict]:
    """The request that connects to a judged address while answering as the hostname.

    Returns ``(url, extra_kwargs)``. With nothing to pin (a literal-IP URL,
    or a tolerated target the judge did not resolve) the URL passes through
    untouched. Otherwise the URL's host is replaced by the first judged
    address (IPv4 preferred -- a v6 pin on a v4-only network fails a fetch
    the hostname would have served), the Host header keeps the site's name,
    and for https the SNI extension carries it too, so certificate
    verification still checks the name the user saw.
    """
    if not addrs:
        return current, {}
    p = urlparse(current)
    hostname = p.hostname or ""
    pin = next((a for a in addrs if ":" not in a), addrs[0])
    host_for_url = f"[{pin}]" if ":" in pin else pin
    auth = ""
    if p.username is not None:
        auth = p.username
        if p.password is not None:
            auth += f":{p.password}"
        auth += "@"
    netloc = f"{auth}{host_for_url}" + (f":{p.port}" if p.port is not None else "")
    pinned = p._replace(netloc=netloc).geturl()
    host_header = hostname if p.port is None else f"{hostname}:{p.port}"
    extra: dict = {"headers": {"Host": host_header}}
    if p.scheme == "https":
        extra["extensions"] = {"sni_hostname": hostname}
    return pinned, extra


async def guarded_fetch(
    client: _Fetcher,
    url: str,
    *,
    what: str,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    resolve: bool = True,
) -> "httpx.Response | None":
    """GET ``url``, checking the target before every hop of the redirect chain.

    Returns the first non-redirect response, or ``None`` when a hop was refused
    or the chain ran too long -- both already logged, naming ``what`` so an
    operator can tell which caller declined.

    Each hop connects to the address the judge saw: one resolution serves
    both the verdict and the connection, so a DNS answer that changes between
    them (rebinding) has nothing left to bite. The URL keeps the hostname for
    ``Host``, SNI and certificate verification; only the TCP connection is
    pinned.

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

    ``resolve=False`` is for a client that sends every request through an HTTP
    proxy: no hop is resolved here or pinned, the proxy resolves each name, and
    every hop is still judged by its shape (:func:`judge_url_target`).
    """
    current = url
    # Passed on only when set, so a resolving fetch calls the judges exactly as before.
    unresolved: dict[str, bool] = {} if resolve else {"resolve": False}
    for hop in range(max_redirects + 1):
        ok, err, addrs = (judge_url_target if hop == 0 else judge_resolved_url)(current, **unresolved)
        if not ok:
            logger.warning("{}: refusing {} ({})", what, current, err)
            return None
        target, extra = _pin_request(current, addrs)
        response = await client.get(target, follow_redirects=False, **extra)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location") or ""
        if not location:
            logger.warning("{}: redirect from {} carried no location header", what, current)
            return None
        current = urljoin(current, location)
    logger.warning("{}: {} redirected more than {} times", what, url, max_redirects)
    return None
