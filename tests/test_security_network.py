"""Tests for ``raven.security.network`` — SSRF URL validation."""

from __future__ import annotations

import httpx
import pytest

from raven.security import network as net


def _mock_resolve(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """Force socket.getaddrinfo to resolve hostnames to a fixed IP."""

    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [(0, 0, 0, "", (ip, 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# validate_url_target
# ---------------------------------------------------------------------------


def test_blocks_loopback() -> None:
    ok, err = net.validate_url_target("http://127.0.0.1/x")
    assert not ok
    assert "private/internal" in err


def test_blocks_link_local() -> None:
    """169.254.169.254 = AWS / GCP / Azure metadata endpoint."""
    ok, err = net.validate_url_target("http://169.254.169.254/latest/meta-data/")
    assert not ok
    assert "private/internal" in err


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://100.64.0.1/x",
    ],
)
def test_blocks_private_ipv4(url: str) -> None:
    ok, err = net.validate_url_target(url)
    assert not ok, f"should block {url}"
    assert "private/internal" in err


def test_blocks_unique_local_v6() -> None:
    ok, err = net.validate_url_target("http://[fc00::1]/x")
    assert not ok
    assert "private/internal" in err


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/x",
        "javascript:alert(1)",
    ],
)
def test_blocks_non_http_scheme(url: str) -> None:
    ok, err = net.validate_url_target(url)
    assert not ok
    assert "http/https" in err


def test_blocks_missing_hostname() -> None:
    ok, err = net.validate_url_target("http:///path-only")
    assert not ok


def test_blocks_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def fake_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    ok, err = net.validate_url_target("https://nx.example.invalid/")
    assert not ok
    assert "Cannot resolve" in err


def test_allows_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DNS resolves to a public IP, allow."""
    _mock_resolve(monkeypatch, "93.184.216.34")

    ok, err = net.validate_url_target("https://example.com/img.png")
    assert ok, f"unexpectedly blocked: {err}"
    assert err == ""


def test_allows_public_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_resolve(monkeypatch, "2606:2800:220:1:248:1893:25c8:1946")

    ok, err = net.validate_url_target("https://example.com/img.png")
    assert ok, f"unexpectedly blocked: {err}"


# ---------------------------------------------------------------------------
# validate_resolved_url
# ---------------------------------------------------------------------------


def test_resolved_blocks_private_ip_literal() -> None:
    ok, err = net.validate_resolved_url("http://10.0.0.1/x")
    assert not ok
    assert "private" in err


def test_resolved_blocks_private_via_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect target is a hostname that resolves to a private IP → block."""
    _mock_resolve(monkeypatch, "10.0.0.1")

    ok, err = net.validate_resolved_url("https://attacker.example/x")
    assert not ok
    assert "private" in err


def test_resolved_allows_public_ip_literal() -> None:
    ok, err = net.validate_resolved_url("http://93.184.216.34/x")
    assert ok, f"unexpectedly blocked: {err}"


def test_resolved_refuses_an_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hop whose host does not resolve cannot be vetted, so it is refused: the
    client would use the same resolver, and the one case where the two differ is
    the attack the check exists for."""
    import socket

    def fake_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    ok, err = net.validate_resolved_url("https://nx.example.invalid/")
    assert not ok
    assert "did not resolve" in err


# ---------------------------------------------------------------------------
# The threat table
#
# The tests above were written against the hand-written CIDR list: one
# representative per list entry. That is why they all passed while
# ``::ffff:169.254.169.254`` reached the cloud metadata service -- an internal
# IPv4 address wearing an IPv6 costume is in none of those CIDRs. Twenty of the
# 31 rows below were allowed by the list-based check this table replaced.
#
# Each row is a way to NAME a destination, and the attacker picks the spelling
# (they control the DNS answer). A dangerous row must be refused however it is
# spelled; a public row must stay allowed -- including public addresses spelled
# as IPv6 translations, since refusing those breaks IPv4-only destinations in
# NAT64 networks.
# ---------------------------------------------------------------------------

DANGEROUS = [
    ("169.254.169.254", "cloud metadata service -- the payload of the real exploit chain"),
    ("127.0.0.1", "loopback"),
    ("10.1.2.3", "RFC1918"),
    ("172.16.5.5", "RFC1918"),
    ("192.168.1.1", "RFC1918"),
    ("100.64.0.1", "carrier-grade NAT"),
    ("0.0.0.0", "unspecified -- reaches every local interface"),
    ("192.0.0.1", "IETF protocol assignments"),
    ("198.18.0.1", "benchmarking range"),
    ("224.0.0.1", "IPv4 multicast"),
    ("240.0.0.1", "reserved"),
    ("255.255.255.255", "broadcast"),
    ("::1", "IPv6 loopback"),
    ("::", "IPv6 unspecified"),
    ("fe80::1", "IPv6 link-local"),
    ("fec0::1", "deprecated site-local -- no stdlib property flags it"),
    ("fc00::1", "IPv6 unique-local"),
    ("ff02::1", "IPv6 multicast -- stdlib is_global says True"),
    ("100::1", "discard-only"),
    ("2001:db8::1", "documentation range"),
    ("::ffff:169.254.169.254", "IPv4-mapped metadata -- the reported bypass"),
    ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
    ("::ffff:a9fe:a9fe", "IPv4-mapped metadata written in hex"),
    ("::169.254.169.254", "IPv4-compatible metadata -- stdlib is_global says True"),
    ("::7f00:1", "IPv4-compatible loopback in hex"),
    ("::ffff:0:169.254.169.254", "IPv4-translated metadata -- stdlib is_global says True"),
    ("64:ff9b::169.254.169.254", "NAT64 well-known prefix carrying metadata"),
    ("64:ff9b::7f00:1", "NAT64 carrying loopback"),
    ("64:ff9b:1::7f00:1", "NAT64 local-use prefix"),
    ("2002:a9fe:a9fe::", "6to4 carrying metadata"),
    ("2002:7f00:1::", "6to4 carrying loopback"),
    ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "Teredo tunnel -- refused as a whole prefix, not by unwrapping"),
    ("64:ff9b:1::a9fe:a9fe", "NAT64 local-use -- likewise refused whole"),
]

PUBLIC = [
    ("8.8.8.8", "ordinary public IPv4"),
    ("93.184.216.34", "ordinary public IPv4"),
    ("2606:4700::1111", "ordinary public IPv6"),
    ("2001:4860:4860::8888", "ordinary public IPv6"),
    ("::ffff:8.8.8.8", "a public IPv4 spelled as mapped -- judge the inner address"),
    ("64:ff9b::8.8.8.8", "a public IPv4 reached through NAT64 -- legitimate in such a network"),
]


@pytest.mark.parametrize("addr,reason", DANGEROUS, ids=[c[0] for c in DANGEROUS])
def test_a_hostname_resolving_to_a_dangerous_address_is_refused(
    addr: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attacker owns the DNS answer, so the spelling is theirs to choose."""
    _mock_resolve(monkeypatch, addr)

    ok, err = net.validate_url_target("https://attacker.example/latest/meta-data/")

    assert not ok, f"allowed {addr}: {reason}"
    assert err


@pytest.mark.parametrize("addr,reason", PUBLIC, ids=[c[0] for c in PUBLIC])
def test_a_hostname_resolving_to_a_public_address_is_allowed(
    addr: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_resolve(monkeypatch, addr)

    ok, err = net.validate_url_target("https://example.com/img.png")

    assert ok, f"blocked {addr} ({reason}): {err}"


@pytest.mark.parametrize("addr,reason", DANGEROUS, ids=[c[0] for c in DANGEROUS])
def test_a_redirect_to_a_dangerous_address_is_refused(addr: str, reason: str) -> None:
    """A validated URL that redirects inward is the same attack one hop later,
    so the redirect check answers the same table."""
    host = f"[{addr}]" if ":" in addr else addr

    ok, err = net.validate_resolved_url(f"http://{host}/")

    assert not ok, f"allowed redirect to {addr}: {reason}"
    assert err


def test_credentials_in_the_url_do_not_disguise_the_host() -> None:
    """``http://example.com@169.254.169.254/`` names the metadata service; the
    userinfo before the @ is decoration."""
    ok, _ = net.validate_url_target("http://example.com@169.254.169.254/")
    assert not ok


# 0177.0.0.1 is deliberately absent: this platform resolves it to 177.0.0.1,
# a public address, and the fetch that follows uses the same resolver -- so
# there is no disagreement to exploit, and refusing it would be over-blocking.
@pytest.mark.parametrize("host", ["2130706433", "127.1", "0x7f.0.0.1"])
def test_an_alternate_ipv4_notation_is_refused(host: str) -> None:
    """Decimal, octal and short forms are expanded by the resolver, which is
    why the check judges what a name resolved to and not how it was written."""
    ok, _ = net.validate_url_target(f"http://{host}/")
    assert not ok, host


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/x", "data:text/plain,x"],
)
def test_a_redirect_to_a_non_http_scheme_is_refused(url: str) -> None:
    """Before this, only the address was checked on a redirect hop, so a
    ``file://`` Location passed validation and it was the HTTP client's lack of
    support -- not a decision -- that stopped it."""
    ok, err = net.validate_resolved_url(url)
    assert not ok, url
    assert "http/https" in err


def test_a_protocol_relative_redirect_is_still_address_checked() -> None:
    """No scheme, but a host: the tolerated-scheme path must not skip the
    address check, or ``//169.254.169.254/`` would walk through."""
    ok, _ = net.validate_resolved_url("//169.254.169.254/latest/meta-data/")
    assert not ok


# ---------------------------------------------------------------------------
# guarded_fetch: the fetch that keeps checking
#
# Both adapters that fetch a URL somebody else chose share this helper. Tested
# here rather than through either adapter: the logic is the helper's, and one
# of those adapters needs an optional SDK a default install does not have -- a
# check whose tests only run with an extra installed is a check nobody watches.
# ---------------------------------------------------------------------------


@pytest.fixture
def public_dns(monkeypatch):
    """Every hostname resolves to one ordinary public address."""
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))])


class _Recorder:
    """An HTTP client stand-in that records what was actually requested."""

    def __init__(self, script: list[tuple[int, str]]) -> None:
        self.script = script
        self.requested: list[str] = []
        self.follow_flags: list[object] = []
        self.kwargs: list[dict] = []

    async def get(self, url: str, follow_redirects: object = "unset", **_kw):
        self.requested.append(url)
        self.follow_flags.append(follow_redirects)
        self.kwargs.append(dict(_kw))
        status, location = self.script.pop(0)
        return httpx.Response(
            status_code=status,
            headers={"location": location} if location else {},
            request=httpx.Request("GET", url),
        )


async def test_a_url_naming_an_internal_address_is_not_requested_at_all() -> None:
    """For the QQ path this is the whole exposure: allow_from defaults to
    everyone and the URL comes out of the inbound event, so without the check
    the gateway would GET whatever address a message named."""
    from raven.security.network import guarded_fetch

    rec = _Recorder([(200, "")])

    out = await guarded_fetch(rec, "http://169.254.169.254/latest/meta-data/", what="test")

    assert out is None
    assert rec.requested == [], "refused targets must not be contacted"


async def test_a_redirect_pointing_inward_is_refused_at_that_hop(public_dns) -> None:
    """The first address being public is not enough: the check runs again on
    where the redirect points, which is why the chain is followed by hand."""
    from raven.security.network import guarded_fetch

    rec = _Recorder([(302, "http://127.0.0.1/secret")])

    out = await guarded_fetch(rec, "https://cdn.example.test/a.png", what="test")

    assert out is None
    assert rec.requested == ["https://93.184.216.34/a.png"], "the hop connects to the judged address"
    assert rec.follow_flags == [False], "the client must not follow the chain itself"


async def test_a_relative_redirect_is_resolved_against_the_url_it_came_from(public_dns) -> None:
    """A relative Location is legal HTTP. Joining it keeps every hop absolute,
    which is what makes the hop checkable -- and keeps the legitimate case
    working."""
    from raven.security.network import guarded_fetch

    rec = _Recorder([(302, "/moved/a.png"), (200, "")])

    out = await guarded_fetch(rec, "https://cdn.example.test/dir/a.png", what="test")

    assert out is not None and out.status_code == 200
    assert rec.requested == [
        "https://93.184.216.34/dir/a.png",
        "https://93.184.216.34/moved/a.png",
    ], "every hop connects pinned; the Location was still resolved against the hostname URL"


async def test_a_redirect_to_a_non_http_scheme_is_refused(public_dns) -> None:
    from raven.security.network import guarded_fetch

    rec = _Recorder([(302, "file:///etc/passwd")])

    assert await guarded_fetch(rec, "https://cdn.example.test/a.png", what="test") is None


async def test_an_endless_redirect_chain_stops(public_dns) -> None:
    from raven.security.network import guarded_fetch

    rec = _Recorder([(302, "https://cdn.example.test/next")] * 9)

    out = await guarded_fetch(rec, "https://cdn.example.test/a.png", what="test", max_redirects=3)

    assert out is None
    assert len(rec.requested) == 4, "one initial request plus max_redirects hops"


async def test_a_redirect_without_a_location_is_refused(public_dns) -> None:
    from raven.security.network import guarded_fetch

    rec = _Recorder([(302, "")])

    assert await guarded_fetch(rec, "https://cdn.example.test/a.png", what="test") is None


async def test_an_ordinary_public_fetch_still_goes_through(public_dns) -> None:
    from raven.security.network import guarded_fetch

    rec = _Recorder([(200, "")])

    out = await guarded_fetch(rec, "https://cdn.example.test/a.png", what="test")

    assert out is not None and out.status_code == 200


async def test_the_connection_goes_where_the_judge_looked(public_dns) -> None:
    """The pin: the TCP target is the judged address, while Host and SNI keep
    the site's name so the certificate check still names the site."""
    from raven.security.network import guarded_fetch

    rec = _Recorder([(200, "")])

    out = await guarded_fetch(rec, "https://cdn.example.test/a.png", what="test")

    assert out is not None
    assert rec.requested == ["https://93.184.216.34/a.png"]
    assert rec.kwargs[0]["headers"]["Host"] == "cdn.example.test"
    assert rec.kwargs[0]["extensions"]["sni_hostname"] == "cdn.example.test"


async def test_one_resolution_serves_verdict_and_connection(monkeypatch) -> None:
    """The rebinding regression: a resolver that answers public first and
    private afterwards never gets its second answer used, because the hop
    resolves exactly once."""
    from raven.security.network import guarded_fetch

    answers = [("93.184.216.34", 0), ("127.0.0.1", 0)]
    calls = {"n": 0}

    def rebinding_resolver(*_a, **_k):
        answer = answers[min(calls["n"], len(answers) - 1)]
        calls["n"] += 1
        return [(0, 0, 0, "", answer)]

    monkeypatch.setattr("socket.getaddrinfo", rebinding_resolver)
    rec = _Recorder([(200, "")])

    out = await guarded_fetch(rec, "https://cdn.example.test/a.png", what="test")

    assert out is not None and out.status_code == 200
    assert calls["n"] == 1, "one resolution per hop; the rebound answer was never consulted"
    assert rec.requested == ["https://93.184.216.34/a.png"]


async def test_a_port_survives_the_pin(public_dns) -> None:
    from raven.security.network import guarded_fetch

    rec = _Recorder([(200, "")])

    await guarded_fetch(rec, "https://cdn.example.test:8443/a.png", what="test")

    assert rec.requested == ["https://93.184.216.34:8443/a.png"]
    assert rec.kwargs[0]["headers"]["Host"] == "cdn.example.test:8443"


async def test_a_literal_ip_url_is_not_rewritten(public_dns) -> None:
    """A literal public IP has nothing to pin: no resolution happened, so
    there is no answer to hold the connection to."""
    from raven.security.network import guarded_fetch

    rec = _Recorder([(200, "")])

    await guarded_fetch(rec, "http://93.184.216.34/a.png", what="test")

    assert rec.requested == ["http://93.184.216.34/a.png"]
    assert rec.kwargs[0] == {}


async def test_a_plain_http_pin_carries_host_but_no_sni(public_dns) -> None:
    from raven.security.network import guarded_fetch

    rec = _Recorder([(200, "")])

    await guarded_fetch(rec, "http://cdn.example.test/a.png", what="test")

    assert rec.requested == ["http://93.184.216.34/a.png"]
    assert rec.kwargs[0]["headers"]["Host"] == "cdn.example.test"
    assert "extensions" not in rec.kwargs[0]
