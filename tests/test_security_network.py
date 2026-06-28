"""Tests for ``raven.security.network`` — SSRF URL validation."""

from __future__ import annotations

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


@pytest.mark.parametrize("url", [
    "http://10.0.0.1/x",
    "http://192.168.1.1/x",
    "http://172.16.0.1/x",
    "http://100.64.0.1/x",
])
def test_blocks_private_ipv4(url: str) -> None:
    ok, err = net.validate_url_target(url)
    assert not ok, f"should block {url}"
    assert "private/internal" in err


def test_blocks_unique_local_v6() -> None:
    ok, err = net.validate_url_target("http://[fc00::1]/x")
    assert not ok
    assert "private/internal" in err


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/x",
    "javascript:alert(1)",
])
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


def test_resolved_tolerates_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_resolved_url is more lenient on DNS failure than validate_url_target."""
    import socket
    def fake_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("no such host")
    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    ok, _ = net.validate_resolved_url("https://nx.example.invalid/")
    assert ok
