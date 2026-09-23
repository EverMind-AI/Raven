"""WebFetchTool routes URL validation through the strong SSRF check.

Mocks DNS so a public-looking hostname resolves to a private/internal IP
(the DNS-rebinding class the old scheme-only validator missed); the fetch
must refuse before any HTTP request.
"""

from __future__ import annotations

import json

import httpx
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
    parsed = json.loads(out)
    assert "validation failed" in parsed["error"]
    # The reason sits in ``detail``: ``error`` is the tool-failure streak's
    # classification key, so it carries the class and never the address.
    assert "private/internal" in parsed["detail"]


async def test_rejects_loopback(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("HTTP client must not be constructed for a blocked URL")

    monkeypatch.setattr("httpx.AsyncClient", _boom)

    out = await WebFetchTool().execute(url="http://127.0.0.1/admin")
    parsed = json.loads(out)
    assert "validation failed" in parsed["error"]


async def test_rejects_non_http_scheme(monkeypatch):
    out = await WebFetchTool().execute(url="file:///etc/passwd")
    parsed = json.loads(out)
    assert "validation failed" in parsed["error"]


@pytest.mark.parametrize("location", ["http://127.0.0.1/admin", "http://169.254.169.254/latest", "file:///etc/passwd"])
async def test_direct_fetch_checks_redirect_targets(monkeypatch, location):
    _resolve_to(monkeypatch, "93.184.216.34")
    calls = []
    client_type = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handle)),
    )

    with pytest.raises(RuntimeError, match="Direct fetch blocked"):
        await WebFetchTool()._direct_fetch("https://public.example/data")

    assert len(calls) == 1
    assert calls[0].url.host == "93.184.216.34"
    assert calls[0].headers["host"] == "public.example"
    assert calls[0].extensions["sni_hostname"] == "public.example"


async def test_direct_fetch_follows_public_redirect_and_preserves_json(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    calls = []
    client_type = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/data"})
        return httpx.Response(200, text='{"items": [1, 2]}\n', headers={"Content-Type": "application/json"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handle)),
    )

    result = json.loads(await WebFetchTool().execute("https://public.example/start"))

    assert result["text"] == '{"items": [1, 2]}\n'
    assert result["extractor"] == "direct-http"
    assert [call.url.path for call in calls] == ["/start", "/data"]
    assert all(call.url.host == "93.184.216.34" for call in calls)
