"""Unit tests for the URL trust rules every hub endpoint and download passes."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "https://hub.evermind.ai",
        "http://localhost:8000",
        "http://127.0.0.1:8000/",
        "http://[::1]:8000",
    ],
)
def test_a_hub_endpoint_may_be_https_or_local_plaintext(url: str) -> None:
    from raven.security.urls import require_https

    assert require_https(url, what="hub") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://hub.example.com",
        "http://10.0.0.5:8000",
        "file:///etc/passwd",
        "ftp://hub.example.com",
        "hub.example.com",
        "https://user:pass@hub.example.com",
        # Hostile DNS can point *.localhost at a public address, so the suffix is
        # not evidence of loopback and must not buy plaintext.
        "http://evil.localhost",
        # An address nobody agrees how to read: inet_aton says 8.0.0.1, some
        # resolvers say 10.0.0.1. Guessing is worse than refusing.
        "http://010.0.0.1",
        "http://2130706433",
    ],
)
def test_a_hub_endpoint_may_not_be_plaintext_remote_or_non_http(url: str) -> None:
    """Whoever answers the endpoint dictates the catalogue, and every stdio entry
    in it is a command line. Over plain http that is whoever is on the path."""
    from raven.security.urls import HubTrustError, require_https

    with pytest.raises(HubTrustError):
        require_https(url, what="hub")


def test_an_absent_override_leaves_the_default_endpoint(monkeypatch) -> None:
    from raven.security.urls import hub_endpoint

    assert hub_endpoint(None, "https://hub.evermind.ai/", what="X") == "https://hub.evermind.ai"
    assert hub_endpoint("   ", "https://hub.evermind.ai", what="X") == "https://hub.evermind.ai"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.zip",
        "https://192.168.1.5/a.zip",
        "https://169.254.169.254/",
        "http://cdn.example.com/a",
        # Legacy address spellings: ipaddress reads them as hostnames, resolvers
        # do not, and they do not even agree with each other.
        "https://127.1/z.zip",
        "https://2130706433/z.zip",
        "https://0x7f000001/z.zip",
        "https://0/z.zip",
        "https://010.0.0.1/z.zip",
    ],
)
def test_a_hub_supplied_download_url_may_not_aim_inside_the_network(url: str) -> None:
    from raven.security.urls import HubTrustError, require_public_https

    with pytest.raises(HubTrustError):
        require_public_https(url, what="zip_url")


def test_a_hub_supplied_download_url_may_be_an_ordinary_cdn() -> None:
    from raven.security.urls import require_public_https

    assert require_public_https("https://cdn.example.com/a.zip", what="zip_url")


@pytest.mark.parametrize(
    "url",
    [
        # IDNA maps the ideographic full stop onto ".", so urlsplit sees one
        # ordinary-looking label while the client connects to 127.0.0.1.
        "https://127。0。0。1/x.zip",
        "https://127。1/x.zip",
        "https://10。0。0。1/x.zip",
        "https://169。254。169。254/latest/meta-data",
        # localhost by name: loopback per RFC 6761, whatever the label in front.
        "https://evil.localhost/x.zip",
        "https://localhost./x.zip",
    ],
)
def test_a_download_url_is_judged_on_the_host_the_client_will_use(url: str) -> None:
    from raven.security.urls import HubTrustError, require_public_https

    with pytest.raises(HubTrustError):
        require_public_https(url, what="zip_url")


def test_the_plaintext_exemption_is_still_only_for_this_machine() -> None:
    """The two questions have different answers for the same name: *.localhost is
    loopback enough to refuse a hub-supplied target, and not trustworthy enough
    to buy the http exemption (hostile DNS can point it anywhere)."""
    from raven.security.urls import HubTrustError, require_https

    assert require_https("http://localhost:8000", what="hub")
    assert require_https("http://127.0.0.1:8000", what="hub")
    with pytest.raises(HubTrustError):
        require_https("http://evil.localhost", what="hub")
