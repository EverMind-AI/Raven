"""Pictures from a report's cited page remain reachable beside image search."""

from __future__ import annotations

import httpx
import pytest

from raven.agent.tools.web import WebFetchTool


@pytest.mark.asyncio
async def test_web_fetch_images_prefers_captioned_figures_and_skips_page_chrome(monkeypatch) -> None:
    html = """
    <html><head><meta property="og:image" content="/preview.png"></head><body>
      <header><img src="/header-photo.png" alt="header"></header>
      <article>
        <figure><img src="/architecture.png" alt="Memory lifecycle"><figcaption>Official architecture</figcaption></figure>
        <img src="/product-screen.png" alt="Product screen">
      </article>
      <img src="/favicon.png" alt="noise">
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=request)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr("raven.agent.tools.web.validate_url_target", lambda _url: (True, ""))

    result = await WebFetchTool().execute("https://example.com/report", extractMode="images")

    assert "architecture.png" in result and "Official architecture" in result
    assert result.index("architecture.png") < result.index("preview.png")
    assert "product-screen.png" in result
    assert "header-photo.png" not in result and "favicon.png" not in result


def test_web_fetch_schema_exposes_the_images_mode() -> None:
    modes = WebFetchTool.parameters["properties"]["extractMode"]["enum"]
    assert "images" in modes


@pytest.mark.asyncio
async def test_a_redirect_to_a_private_address_is_not_followed(monkeypatch) -> None:
    """Only the submitted URL was validated, then redirects were followed automatically,
    so a public URL could hand back an internal page's images."""
    private = "http://127.0.0.1:8080/admin"
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": private}, request=request)
        return httpx.Response(
            200,
            text='<html><body><figure><img src="/secret.png" alt="internal only">'
            "<figcaption>Production credentials dashboard</figcaption></figure></body></html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    result = await WebFetchTool().execute("https://example.com/report", extractMode="images")

    assert "https://example.com/report" in asked, "the public page was never fetched, so this proves nothing"
    assert private not in asked, "the private address was requested: %r" % (asked,)
    assert "secret.png" not in result and "credentials" not in result
