"""The web tools, refused before the socket is opened.

The transport test beside this one proves the order for the transport. This
one proves it for the tools that use it, because a tool that built its own
request would keep the transport's promise and break the tool's: the docs say
both web tools are guarded, and that is only true while their requests go
through the guarded transport.

Each tool is driven through its real path - the real httpx client, the real
request, the real rendering - with the transport standing where the socket
would be opened. A refusal that came after the request would show up here as
a dial rather than as a refusal.

Nothing here reaches the internet, and nothing here needs a key: the test is
about what does *not* happen, and about what still happens for a source whose
terms permit the call.
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from raven.agent.tools import web as web_mod
from raven.agent.tools.web import WebFetchTool, WebSearchTool
from raven.config import loader

REPO = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.tools import web as flow_web  # noqa: E402
from research_flow.tools.web import WebFetchTool as FlowWebFetchTool  # noqa: E402
from research_flow.tools.web import WebSearchTool as FlowWebSearchTool  # noqa: E402
from research_flow.tools.web import set_current_session  # noqa: E402

pytestmark = pytest.mark.integration

#: A vendor the tools offer and the registry does not know. Not a real vendor,
#: so no row for it is ever added by accident: what is refused here is the
#: shape of the refusal, not one company's terms.
_UNREGISTERED = "a_vendor_nobody_looked_at"


class _DialCounter:
    """The real transport, standing where the socket is opened.

    Every request a tool makes arrives here, so a request made before the
    registry agreed to it shows up as a dial rather than as a refusal.
    """

    def __init__(self) -> None:
        self.dials = 0
        self.urls: list[str] = []
        self.answer: Callable[[httpx.Request], httpx.Response] | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.dials += 1
        self.urls.append(str(request.url))
        if self.answer is None:
            raise AssertionError("the request reached the transport before the registry agreed to it")
        return self.answer(request)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _DialCounter:
    """The transport the tools build for themselves, with the dial counted."""
    counter = _DialCounter()

    async def _handle(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return await counter.handle(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _handle)
    return counter


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public answer for every name, so the URL checks pass on their own terms."""

    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture(autouse=True)
def _no_opt_ins(tmp_path: Path) -> Iterator[None]:
    """The opt-in record is the operator's, and this test reads none of it."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    loader.set_config_path(cfg)
    try:
        yield
    finally:
        loader.set_config_path(None)


@pytest.fixture
def unregistered_search_vendor(monkeypatch: pytest.MonkeyPatch) -> str:
    """A search vendor the tool offers and the registry does not know."""
    monkeypatch.setitem(
        web_mod.SEARCH_PROVIDERS,
        _UNREGISTERED,
        web_mod.SearchProviderSpec(_UNREGISTERED, "A Vendor Nobody Looked At", "A_VENDOR_API_KEY", "https://example.test"),
    )
    return _UNREGISTERED


@pytest.fixture
def unregistered_fetch_vendor(monkeypatch: pytest.MonkeyPatch) -> str:
    """A fetch vendor the tool offers and the registry does not know."""
    monkeypatch.setitem(
        web_mod.FETCH_PROVIDERS,
        _UNREGISTERED,
        web_mod.FetchProviderSpec(
            _UNREGISTERED, "A Vendor Nobody Looked At", "A_VENDOR_API_KEY", "https://example.test", "a-vendor", False
        ),
    )
    return _UNREGISTERED


@pytest.fixture
def unregistered_flow_search_vendor(monkeypatch: pytest.MonkeyPatch) -> str:
    """A search vendor the research flow offers and the registry does not know."""
    monkeypatch.setitem(
        flow_web.SEARCH_PROVIDERS,
        _UNREGISTERED,
        flow_web.SearchProviderSpec(
            vendor=_UNREGISTERED, label="A Vendor Nobody Looked At", env_var="A_VENDOR_API_KEY", paginates=True
        ),
    )
    return _UNREGISTERED


@pytest.fixture
def unregistered_flow_fetch_vendor(monkeypatch: pytest.MonkeyPatch) -> str:
    """A fetch vendor the research flow offers and the registry does not know."""
    monkeypatch.setitem(
        flow_web.FETCH_PROVIDERS,
        _UNREGISTERED,
        flow_web.FetchProviderSpec(
            vendor=_UNREGISTERED,
            label="A Vendor Nobody Looked At",
            env_var="A_VENDOR_API_KEY",
            extractor="a-vendor",
            needs_key=False,
        ),
    )
    return _UNREGISTERED


def _search_answer(request: httpx.Request) -> httpx.Response:
    """A Serper-shaped answer, which is the shape both search tools render from."""
    return httpx.Response(
        200,
        json={"organic": [{"title": "A title", "link": "https://example.test/a", "snippet": "A snippet"}]},
        request=request,
    )


def _page_answer(request: httpx.Request) -> httpx.Response:
    """The page, read directly, and the same page read through the reader."""
    if request.url.host == "r.jina.ai":
        return httpx.Response(200, text="the page body", request=request)
    return httpx.Response(
        200,
        text="<html><body>the page body</body></html>",
        headers={"content-type": "text/html"},
        request=request,
    )


# --------------------------------------------------------------------------- #
# The runtime's own web tools


@pytest.mark.asyncio
async def test_the_search_tool_refuses_an_unregistered_vendor_without_opening_a_socket(
    transport: _DialCounter, unregistered_search_vendor: str
) -> None:
    answer = await WebSearchTool(api_key="k", provider=unregistered_search_vendor).execute("q")

    assert transport.dials == 0
    assert "refused by source policy" in answer
    assert "not in the registry" in answer


@pytest.mark.asyncio
async def test_the_search_tool_still_reaches_a_permitted_vendor(transport: _DialCounter) -> None:
    transport.answer = _search_answer

    answer = await WebSearchTool(api_key="k", provider="serper").execute("q")

    assert transport.dials == 1
    assert "Results for: q" in answer
    assert "A title" in answer


@pytest.mark.asyncio
async def test_the_fetch_tool_refuses_an_unregistered_vendor_without_opening_a_socket(
    transport: _DialCounter, unregistered_fetch_vendor: str
) -> None:
    out = await WebFetchTool(api_key="k", provider=unregistered_fetch_vendor).execute("https://example.test/page")

    assert transport.dials == 0
    payload = json.loads(out)
    assert payload["error"] == f"{unregistered_fetch_vendor} refused by source policy"
    assert "not in the registry" in payload["detail"]


@pytest.mark.asyncio
async def test_the_fetch_tool_still_reads_a_page_for_a_permitted_vendor(transport: _DialCounter) -> None:
    transport.answer = _page_answer

    out = await WebFetchTool(api_key="k", provider="jina").execute("https://example.test/page")

    payload = json.loads(out)
    assert payload["extractor"] == "jina-reader"
    assert "the page body" in payload["text"]
    assert any("r.jina.ai" in url for url in transport.urls)


# --------------------------------------------------------------------------- #
# The research flow's web tools


@pytest.mark.asyncio
async def test_the_research_flow_search_tool_refuses_an_unregistered_vendor_without_opening_a_socket(
    transport: _DialCounter, unregistered_flow_search_vendor: str
) -> None:
    set_current_session("guard-search")

    answer = await FlowWebSearchTool(api_key="k", provider=unregistered_flow_search_vendor).execute("q")

    assert transport.dials == 0
    assert "refused by source policy" in answer
    assert "not in the registry" in answer


@pytest.mark.asyncio
async def test_the_research_flow_search_tool_still_reaches_a_permitted_vendor(transport: _DialCounter) -> None:
    transport.answer = _search_answer
    set_current_session("guard-search-permitted")

    answer = await FlowWebSearchTool(api_key="k", provider="serper").execute("q")

    assert transport.dials == 1
    assert "Results for: q" in answer
    assert "A title" in answer


@pytest.mark.asyncio
async def test_the_research_flow_fetch_tool_refuses_an_unregistered_vendor_without_opening_a_socket(
    transport: _DialCounter, unregistered_flow_fetch_vendor: str
) -> None:
    set_current_session("guard-fetch")

    out = await FlowWebFetchTool(api_key="k", provider=unregistered_flow_fetch_vendor).execute("https://example.test/page")

    assert transport.dials == 0
    payload = json.loads(out)
    assert payload["error"] == f"{unregistered_flow_fetch_vendor} refused by source policy"
    assert "not in the registry" in payload["detail"]


@pytest.mark.asyncio
async def test_the_research_flow_fetch_tool_still_reads_a_page_for_a_permitted_vendor(transport: _DialCounter) -> None:
    transport.answer = _page_answer
    set_current_session("guard-fetch-permitted")

    out = await FlowWebFetchTool(api_key="k", provider="jina").execute("https://example.test/page")

    payload = json.loads(out)
    assert payload["extractor"] == "jina-reader"
    assert "the page body" in payload["text"]
    assert any("r.jina.ai" in url for url in transport.urls)
