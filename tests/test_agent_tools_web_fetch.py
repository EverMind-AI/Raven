"""Direct data reads and HTML fallback contracts for the host web_fetch tool."""

import asyncio
import json

import httpx
import pytest

from raven.agent.tools import web
from raven.security import network


@pytest.fixture
def transport(monkeypatch):
    calls = []
    responses = {}
    client_type = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        response = responses[request.url.host]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(web, "validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(network, "judge_url_target", lambda url: (True, "", ()))
    monkeypatch.setattr(network, "judge_resolved_url", lambda url: (True, "", ()))
    return responses, calls


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json; charset=utf-8", '{"title": "A", "authors": ["B"]}\n'),
        ("application/ld+json", '{"@type": "Article"}'),
        ("application/xml", '<?xml version="1.0"?><feed><title>A</title></feed>'),
        ("text/xml", '<feed xmlns="http://www.w3.org/2005/Atom"><entry>A</entry></feed>'),
        ("application/atom+xml", '<feed xmlns="http://www.w3.org/2005/Atom"><entry>A</entry></feed>'),
        ("text/plain; charset=utf-8", "  title\n\n    indented text\n"),
    ],
)
async def test_data_without_a_file_extension_preserves_original_text(transport, content_type, body):
    responses, calls = transport
    responses["origin.example"] = httpx.Response(200, text=body, headers={"Content-Type": content_type})

    result = json.loads(await web.WebFetchTool(api_key="secret").execute("https://origin.example/api?query=a"))

    assert result["text"] == body
    assert result["extractor"] == "direct-http"
    assert result["contentType"] == content_type
    assert result["length"] == len(body)
    assert result["truncated"] is False
    assert len(calls) == 1
    assert "authorization" not in calls[0].headers


@pytest.mark.parametrize("content_type", ["text/html", "application/xhtml+xml"])
async def test_html_still_prefers_jina(transport, content_type):
    responses, calls = transport
    responses["origin.example"] = httpx.Response(200, text="<p>Local</p>", headers={"Content-Type": content_type})
    responses["r.jina.ai"] = httpx.Response(200, text="# Jina article")

    result = json.loads(await web.WebFetchTool(api_key="secret").execute("https://origin.example/article"))

    assert result["text"] == "# Jina article"
    assert result["extractor"] == "jina-reader"
    assert [call.url.host for call in calls] == ["origin.example", "r.jina.ai"]
    assert "authorization" not in calls[0].headers
    assert calls[1].headers["authorization"] == "Bearer secret"


@pytest.mark.parametrize("vendor", sorted(web.FETCH_PROVIDERS))
@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 429, 451, 500, 503])
async def test_vendor_http_errors_use_direct_html(transport, vendor, status):
    responses, calls = transport
    responses["origin.example"] = httpx.Response(
        200,
        text="<html><body><h1>Title</h1><script>secret()</script><style>.x{}</style><p>Body</p></body></html>",
        headers={"Content-Type": "text/html"},
    )
    for host in ["r.jina.ai", "api.tavily.com", "api.exa.ai", "api.firecrawl.dev", "api.anysearch.com"]:
        responses[host] = httpx.Response(status)
    tool = web.WebFetchTool(api_key="secret", provider=vendor)

    result = json.loads(await tool.execute("https://origin.example/article"))

    assert result["text"] == "Title\nBody"
    assert result["status"] == 200
    assert result["extractor"] == "direct-http"
    assert len(calls) == 2
    assert tool._refusal.active(vendor, "secret") == (status if status in {401, 402} else None)


async def test_paused_vendor_still_allows_direct_read(transport):
    responses, calls = transport
    responses["origin.example"] = httpx.Response(200, text="<p>Body</p>", headers={"Content-Type": "text/html"})
    responses["r.jina.ai"] = httpx.Response(402)
    tool = web.WebFetchTool(api_key="secret")

    first = json.loads(await tool.execute("https://origin.example/a"))
    second = json.loads(await tool.execute("https://origin.example/b"))

    assert first["text"] == second["text"] == "Body"
    assert [call.url.host for call in calls] == ["origin.example", "r.jina.ai", "origin.example"]


@pytest.mark.parametrize("failure", [httpx.ConnectError("unavailable"), httpx.ReadTimeout("timed out")])
async def test_direct_connection_failure_does_not_prevent_jina(transport, failure):
    responses, calls = transport
    responses["origin.example"] = failure
    responses["r.jina.ai"] = httpx.Response(200, text="Jina article")

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/article"))

    assert result["text"] == "Jina article"
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timed out"), httpx.ProxyError("proxy failed")])
async def test_vendor_transport_failure_uses_direct_html(transport, failure):
    responses, _ = transport
    responses["origin.example"] = httpx.Response(200, text="<p>Body</p>", headers={"Content-Type": "text/html"})
    responses["r.jina.ai"] = failure

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/article"))

    assert result["text"] == "Body"
    assert result["extractor"] == "direct-http"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<?xml version="1.0" encoding="utf-8"?><html><body><p>Body</p></body></html>', "Body"),
        ("<script>hidden()</script><p>Body</p>", "Body"),
        ("<p>caf&#233;</p>", "caf\u00e9"),
        ("", ""),
    ],
)
async def test_direct_html_handles_declarations_scripts_and_entities(transport, body, expected):
    responses, _ = transport
    responses["origin.example"] = httpx.Response(200, text=body, headers={"Content-Type": "text/html"})
    responses["r.jina.ai"] = httpx.Response(503)

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/article"))

    assert result["text"] == expected


async def test_binary_data_is_not_presented_as_a_successful_text_fallback(transport):
    responses, _ = transport
    responses["origin.example"] = httpx.Response(200, content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"})
    responses["r.jina.ai"] = httpx.Response(503)

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/document"))

    assert result["error"] == "Jina Reader answered HTTP 503"
    assert result["detail"] == "Unsupported direct content type"
    assert "text" not in result


async def test_both_routes_fail_reports_both_statuses(transport):
    responses, calls = transport
    responses["origin.example"] = httpx.Response(404)
    responses["r.jina.ai"] = httpx.Response(451)

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/missing"))

    assert result["error"] == "Jina Reader answered HTTP 451"
    assert result["detail"] == "Direct fetch answered HTTP 404"
    assert "text" not in result
    assert len(calls) == 2


async def test_raw_data_truncation_is_reported(transport):
    responses, _ = transport
    responses["origin.example"] = httpx.Response(200, text="x" * 150)

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/data", maxChars=100))

    assert result["text"] == "x" * 100
    assert result["length"] == 100
    assert result["truncated"] is True


def test_tool_discloses_third_party_url_sharing_and_removes_unused_mode():
    assert "third-party" in web.WebFetchTool.description
    assert "full URL" in web.WebFetchTool.description
    assert "extractMode" not in web.WebFetchTool.parameters["properties"]


async def test_legacy_mode_is_not_echoed_as_an_output_promise(transport):
    responses, _ = transport
    responses["origin.example"] = httpx.Response(200, json={"a": 1})

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/data", extractMode="markdown"))

    assert "extractMode" not in result
    assert json.loads(result["text"]) == {"a": 1}


class _TrackedStream(httpx.AsyncByteStream):
    """Detect over-reading and confirm closure on every streaming exit path."""

    def __init__(self, chunks, *, max_reads=None, delay=0):
        self.chunks = chunks
        self.max_reads = max_reads
        self.delay = delay
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if self.max_reads is not None:
                assert self.reads <= self.max_reads, "response read past its budget"
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("content_length", [None, "1", "999999999999"])
async def test_streaming_size_limit_does_not_trust_content_length(transport, content_length):
    responses, calls = transport
    chunk = b"x" * (64 * 1024)
    limit = web.DIRECT_FETCH_MAX_BYTES
    stream = _TrackedStream([chunk] * 100, max_reads=limit // len(chunk) + 1)
    headers = {"Content-Type": "text/plain"}
    if content_length is not None:
        headers["Content-Length"] = content_length
    responses["origin.example"] = httpx.Response(200, headers=headers, stream=stream)

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/data", maxChars=limit + 100))

    assert result["text"] == "x" * limit
    assert result["length"] == limit
    assert result["truncated"] is True
    assert stream.closed
    assert len(calls) == 1


@pytest.mark.parametrize("size_delta", [-1, 0, 1])
async def test_streaming_limit_boundary(transport, size_delta):
    responses, _ = transport
    limit = web.DIRECT_FETCH_MAX_BYTES
    body = b"x" * (limit + size_delta)
    stream = _TrackedStream([body])
    responses["origin.example"] = httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream)

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/data", maxChars=limit + 100))

    assert result["length"] == min(len(body), limit)
    assert result["truncated"] is (size_delta > 0)
    assert stream.closed


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/pdf"},
        {"Content-Type": "application/zip"},
        {"Content-Type": "text/plain", "Content-Encoding": "gzip"},
    ],
)
async def test_unsupported_bodies_are_closed_without_reading(transport, headers):
    responses, calls = transport
    stream = _TrackedStream([b"must not be read"], max_reads=0)
    responses["origin.example"] = httpx.Response(200, headers=headers, stream=stream)
    responses["r.jina.ai"] = httpx.Response(200, text="Provider extraction")

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/data"))

    assert result["text"] == "Provider extraction"
    assert stream.reads == 0
    assert stream.closed
    assert calls[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("status", [302, 404, 503])
async def test_redirect_and_error_bodies_are_never_buffered(transport, status):
    responses, _ = transport
    stream = _TrackedStream([b"must not be read"], max_reads=0)
    responses["origin.example"] = httpx.Response(
        status, headers={"Location": "https://next.example/data"}, stream=stream
    )
    responses["next.example"] = httpx.Response(200, text="Direct data")
    responses["r.jina.ai"] = httpx.Response(200, text="Provider extraction")

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/data"))

    assert result["text"] == ("Direct data" if status == 302 else "Provider extraction")
    assert stream.reads == 0
    assert stream.closed


async def test_continuously_producing_stream_hits_total_deadline(transport, monkeypatch):
    responses, _ = transport
    monkeypatch.setattr(web, "DIRECT_FETCH_TIMEOUT_S", 0.03)
    stream = _TrackedStream([b"x"] * 1000, delay=0.005)
    responses["origin.example"] = httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream)
    responses["r.jina.ai"] = httpx.Response(200, text="Provider extraction")

    result = json.loads(await asyncio.wait_for(web.WebFetchTool().execute("https://origin.example/data"), 1))

    assert result["text"] == "Provider extraction"
    assert 0 < stream.reads < 1000
    assert stream.closed


async def test_size_limited_html_is_available_when_jina_fails(transport):
    responses, _ = transport
    stream = _TrackedStream([b"<p>" + b"x" * web.DIRECT_FETCH_MAX_BYTES + b"</p>"])
    responses["origin.example"] = httpx.Response(200, headers={"Content-Type": "text/html"}, stream=stream)
    responses["r.jina.ai"] = httpx.Response(451)

    result = json.loads(await web.WebFetchTool().execute("https://origin.example/article"))

    assert result["extractor"] == "direct-http"
    assert result["truncated"] is True
    assert result["text"] == "x" * 50000
    assert stream.closed
