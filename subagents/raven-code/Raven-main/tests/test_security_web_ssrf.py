"""WebFetchTool routes URL validation through the strong SSRF check.

Mocks DNS so a public-looking hostname resolves to a private/internal IP
(the DNS-rebinding class the old scheme-only validator missed); the fetch
must refuse before any HTTP request.
"""

from __future__ import annotations

import pytest

from raven.agent.tools.web import WebFetchTool


def _resolve_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    def fake_getaddrinfo(host, *_a, **_k):
        return [(0, 0, 0, "", (ip, 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)


async def test_rejects_url_resolving_to_private_ip(monkeypatch):
    _resolve_to(monkeypatch, "169.254.169.254")  # cloud metadata endpoint

    # If validation is bypassed this would attempt a real fetch; fail loudly.
    def _boom(*_a, **_k):
        raise AssertionError("HTTP client must not be constructed for a blocked URL")

    monkeypatch.setattr("httpx.AsyncClient", _boom)

    out = await WebFetchTool().execute(url="http://totally-public.example.com/x")
    assert out.startswith("Error")
    assert "validation failed" in out
    assert "private/internal" in out


async def test_rejects_loopback(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("HTTP client must not be constructed for a blocked URL")

    monkeypatch.setattr("httpx.AsyncClient", _boom)

    out = await WebFetchTool().execute(url="http://127.0.0.1/admin")
    assert out.startswith("Error")
    assert "validation failed" in out


async def test_rejects_non_http_scheme(monkeypatch):
    out = await WebFetchTool().execute(url="file:///etc/passwd")
    assert out.startswith("Error")
    assert "validation failed" in out
