"""Per-vendor request and response contracts of the host web tools.

Several backends share each tool. What differs is the request a vendor expects
and the payload it answers with; everything the model reads -- the rendered
result list, the fetch envelope -- is one code path, so each vendor is
normalised to the Serper (search) or Jina (fetch) shape before anything reads
it. These tests pin the request each vendor receives and the rendering its
payload produces, with the transport replaced by a recorder.

The vendor list itself has three homes -- the schema's ``Literal``, the spec
tables here, and the settings page's own arrays -- so the last section pins all
three together. The front end cannot import the Python one, and a list mirrored
by hand drifts silently: adding a vendor to the schema alone leaves the browser
offering one fewer.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from raven.agent.loop.failure_streak import failure_class, is_hard_tool_failure
from raven.agent.tools import web as web_mod
from raven.agent.tools.web import (
    DEFAULT_FETCH_PROVIDER,
    DEFAULT_SEARCH_PROVIDER,
    FETCH_PROVIDERS,
    SEARCH_PROVIDERS,
    WebFetchTool,
    WebSearchTool,
)
from raven.config.schema import (
    WEB_VENDOR_ENV_VARS,
    Config,
    WebFetchProvider,
    WebProvidersConfig,
    WebSearchProvider,
)

pytestmark = pytest.mark.asyncio


class _Recorder:
    """Stands in for ``httpx.AsyncClient``: records the call, answers a canned body."""

    def __init__(self, payload: Any, status: int = 200, text: str | None = None) -> None:
        self.payload = payload
        self.status = status
        self.text = text
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_Recorder":
        return self

    async def __aenter__(self) -> "_Recorder":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        request = httpx.Request(method, url)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, request=request)
        return httpx.Response(self.status, json=self.payload, request=request)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("POST", url, kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("GET", url, kwargs)


@contextmanager
def _patched(monkeypatch: pytest.MonkeyPatch, payload: Any, **kw: Any):
    recorder = _Recorder(payload, **kw)
    monkeypatch.setattr(web_mod.httpx, "AsyncClient", recorder)
    yield recorder


@pytest.fixture(autouse=True)
def _no_ambient_vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in WEB_VENDOR_ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# The tables agree with the schema


def test_the_spec_tables_match_the_schema_literals() -> None:
    from typing import get_args

    assert set(SEARCH_PROVIDERS) == set(get_args(WebSearchProvider))
    assert set(FETCH_PROVIDERS) == set(get_args(WebFetchProvider))
    for vendor, spec in {**SEARCH_PROVIDERS, **FETCH_PROVIDERS}.items():
        assert spec.env_var == WEB_VENDOR_ENV_VARS[vendor]
        assert spec.config_path == f"tools.web.providers.{vendor}.apiKey"


def test_an_unknown_vendor_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        WebSearchTool(provider="bing")
    with pytest.raises(ValueError):
        WebFetchTool(provider="bing")


# --------------------------------------------------------------------------- #
# Keys: the constructor value first, then the vendor's own env var


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
def test_each_search_vendor_falls_back_to_its_own_env_var(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not WebSearchTool(provider=vendor).api_key
    monkeypatch.setenv(WEB_VENDOR_ENV_VARS[vendor], "from-env")
    assert WebSearchTool(provider=vendor).api_key == "from-env"
    assert WebSearchTool(api_key="from-config", provider=vendor).api_key == "from-config"
    assert WebSearchTool.is_configured(None, provider=vendor)
    # Another vendor's variable does not satisfy this one.
    other = next(v for v in SEARCH_PROVIDERS if v != vendor)
    assert not WebSearchTool.is_configured(None, provider=other)


async def test_the_unconfigured_error_names_the_selected_vendors_slot(tmp_path) -> None:
    from raven.config import loader

    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    loader.set_config_path(cfg)
    try:
        answer = await WebSearchTool(provider="tavily").execute("q")
    finally:
        loader.set_config_path(None)
    assert "Tavily" in answer
    assert "tools.web.providers.tavily.apiKey" in answer and "TAVILY_API_KEY" in answer
    assert str(cfg) in answer


# --------------------------------------------------------------------------- #
# Search: what each vendor is sent


_SEARCH_REQUESTS: dict[str, tuple[str, str, dict[str, Any], dict[str, str]]] = {
    "serper": ("POST", "https://google.serper.dev/search", {"json": {"q": "q1", "num": 4}}, {"X-API-KEY": "k"}),
    "serpapi": (
        "GET",
        "https://serpapi.com/search",
        {"params": {"engine": "google", "q": "q1", "num": 4, "api_key": "k"}},
        {},
    ),
    "anysearch": (
        "POST",
        "https://api.anysearch.com/v1/search",
        {"json": {"query": "q1", "max_results": 4}},
        {"Authorization": "Bearer k"},
    ),
    "tavily": (
        "POST",
        "https://api.tavily.com/search",
        {"json": {"query": "q1", "max_results": 4}},
        {"Authorization": "Bearer k"},
    ),
    "exa": (
        "POST",
        "https://api.exa.ai/search",
        {"json": {"query": "q1", "numResults": 4, "contents": {"highlights": {"maxCharacters": 300}}}},
        {"x-api-key": "k"},
    ),
    "brave": (
        "GET",
        "https://api.search.brave.com/res/v1/web/search",
        {"params": {"q": "q1", "count": 4}},
        {"X-Subscription-Token": "k"},
    ),
    "firecrawl": (
        "POST",
        "https://api.firecrawl.dev/v1/search",
        {"json": {"query": "q1", "limit": 4}},
        {"Authorization": "Bearer k"},
    ),
    "serply": (
        "GET",
        "https://api.serply.io/v1/search",
        {"params": {"q": "q1", "num": 4}},
        {"X-Api-Key": "k"},
    ),
}


@pytest.mark.parametrize("vendor", sorted(_SEARCH_REQUESTS))
async def test_each_search_vendor_gets_the_request_it_documents(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    method, url, body, auth = _SEARCH_REQUESTS[vendor]
    with _patched(monkeypatch, {}) as client:
        await WebSearchTool(api_key="k", provider=vendor).execute("q1", count=4)

    got_method, got_url, kwargs = client.calls[0]
    assert (got_method, got_url) == (method, url)
    for field, expected in body.items():
        assert kwargs[field] == expected
    for header, value in auth.items():
        assert kwargs["headers"][header] == value


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
async def test_a_status_error_never_echoes_the_key(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx puts the whole request URL in a status error, and SerpApi carries
    its key as a query parameter, so the default text would hand the credential
    to the model and the log. Every vendor renders as vendor plus status."""
    with _patched(monkeypatch, {}, status=500):
        rendered = await WebSearchTool(api_key="SECRET-KEY-123", provider=vendor).execute("q1")

    assert "SECRET-KEY-123" not in rendered
    assert rendered == f"Error: {SEARCH_PROVIDERS[vendor].label} answered HTTP 500"


@pytest.mark.parametrize("vendor", sorted(_SEARCH_REQUESTS))
async def test_probe_spends_exactly_one_result_on_the_vendors_own_request(
    vendor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The onboard wizard's key check is a real search, so it must be the same
    request ``execute`` sends -- a probe built any other way could pass a key
    the tool then fails with -- and the smallest one the shape allows."""
    method, url, _body, auth = _SEARCH_REQUESTS[vendor]
    with _patched(monkeypatch, {"organic": [{"title": "t", "link": "u"}]}) as client:
        ok, detail = await WebSearchTool(api_key="k", provider=vendor).probe("q1")

    assert ok is True
    got_method, got_url, kwargs = client.calls[0]
    assert (got_method, got_url) == (method, url)
    size = kwargs.get("json") or kwargs.get("params")
    assert 1 in size.values(), "one result is the whole budget of a probe"
    for header, value in auth.items():
        assert kwargs["headers"][header] == value
    assert detail == ("1 result(s)" if vendor == "serper" else "0 result(s)")


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
async def test_probe_reports_a_refused_key_without_echoing_it(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, {}, status=401):
        ok, detail = await WebSearchTool(api_key="SECRET-KEY-123", provider=vendor).probe()

    assert ok is False
    assert "SECRET-KEY-123" not in detail
    assert detail == f"{SEARCH_PROVIDERS[vendor].label} answered HTTP 401"


async def test_probe_reports_an_unreachable_vendor_as_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down(_Recorder):
        def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
            raise httpx.ConnectError("refused", request=httpx.Request(method, url))

    monkeypatch.setattr(web_mod.httpx, "AsyncClient", _Down({}))

    ok, detail = await WebSearchTool(api_key="k", provider="brave").probe()

    assert ok is False
    assert detail == "Brave Search could not be reached (ConnectError)"


async def test_probe_reports_an_unusable_proxy_without_echoing_its_url() -> None:
    """A proxy URL can carry credentials, and httpx quotes the URL in its
    error, so the detail names the class of failure and nothing else."""
    ok, detail = await WebSearchTool(api_key="k", provider="serper", proxy="ftp://user:secret@proxy").probe()

    assert ok is False
    assert detail == "the configured web proxy is not usable (ValueError)"
    assert "secret" not in detail


async def test_probe_reads_a_firecrawl_refusal_inside_a_200_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, {"success": False, "error": "Unauthorized"}):
        ok, detail = await WebSearchTool(api_key="k", provider="firecrawl").probe()

    assert ok is False
    assert "Unauthorized" in detail


def test_every_search_vendor_is_covered_by_the_request_table() -> None:
    assert set(_SEARCH_REQUESTS) == set(SEARCH_PROVIDERS)


# --------------------------------------------------------------------------- #
# Search: what each vendor's payload renders as


_SEARCH_PAYLOADS: dict[str, Any] = {
    "serper": {"organic": [{"title": "T1", "link": "https://a.example", "snippet": "S1"}]},
    "serpapi": {"organic_results": [{"title": "T1", "link": "https://a.example", "snippet": "S1"}]},
    "anysearch": {"data": {"results": [{"title": "T1", "url": "https://a.example", "content": "S1"}]}},
    "tavily": {"results": [{"title": "T1", "url": "https://a.example", "content": "S1"}]},
    "exa": {"results": [{"title": "T1", "url": "https://a.example", "highlights": ["S1\n\nmore"], "text": "x" * 500}]},
    "brave": {"web": {"results": [{"title": "T1", "url": "https://a.example", "description": "S1"}]}},
    "firecrawl": {"success": True, "data": [{"title": "T1", "url": "https://a.example", "description": "S1"}]},
    "serply": {"results": [{"title": "T1", "link": "https://a.example", "description": "S1"}]},
}


@pytest.mark.parametrize("vendor", sorted(_SEARCH_PAYLOADS))
async def test_each_search_vendor_renders_through_the_shared_path(vendor: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, _SEARCH_PAYLOADS[vendor]):
        rendered = await WebSearchTool(api_key="k", provider=vendor).execute("q1")

    assert rendered.startswith("Results for: q1\n")
    assert "1. T1\n   https://a.example\n   S1" in rendered
    # Exa's whole-page ``text`` never reaches the rendering; only the highlight does.
    assert "xxxx" not in rendered


async def test_a_firecrawl_refusal_inside_a_200_reads_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, {"success": False, "error": "quota exhausted"}):
        rendered = await WebSearchTool(api_key="k", provider="firecrawl").execute("q1")

    assert rendered.startswith("Error:") and "quota exhausted" in rendered
    assert "No results" not in rendered


async def test_an_empty_payload_is_a_dry_search_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with _patched(monkeypatch, {"results": []}):
        rendered = await WebSearchTool(api_key="k", provider="tavily").execute("q1")

    assert rendered == "No results for: q1"


# --------------------------------------------------------------------------- #
# Fetch: registration falls back to Jina when the selected reader cannot run


def test_a_keyed_reader_without_a_key_is_replaced_by_jina(monkeypatch: pytest.MonkeyPatch) -> None:
    assert WebFetchTool.effective_provider("firecrawl", None) == "jina"
    assert WebFetchTool.effective_provider("firecrawl", "k") == "firecrawl"
    monkeypatch.setenv("FIRECRAWL_API_KEY", "from-env")
    assert WebFetchTool.effective_provider("firecrawl", None) == "firecrawl"
    # Jina needs nothing, so it is never replaced.
    assert WebFetchTool.effective_provider("jina", None) == "jina"


# --------------------------------------------------------------------------- #
# Fetch: what each reader is sent, and what its page becomes


@pytest.fixture
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF gate resolves the hostname, which a test host must not do; the
    gate has its own suite (``test_security_web_ssrf``)."""
    monkeypatch.setattr(web_mod, "validate_url_target", lambda url: (True, ""))

    async def unavailable(self, url):
        raise httpx.ConnectError("Direct origin unavailable")

    monkeypatch.setattr(WebFetchTool, "_direct_fetch", unavailable)


_FETCH_CASES: dict[str, tuple[str, str, dict[str, Any], Any]] = {
    "jina": ("GET", "https://r.jina.ai/https://a.example", {}, None),
    "tavily": (
        "POST",
        "https://api.tavily.com/extract",
        {"json": {"urls": ["https://a.example"]}},
        {"results": [{"url": "https://a.example", "raw_content": "PAGE"}]},
    ),
    "exa": (
        "POST",
        "https://api.exa.ai/contents",
        {"json": {"urls": ["https://a.example"], "text": True}},
        {"results": [{"url": "https://a.example", "title": "T", "text": "PAGE"}]},
    ),
    "firecrawl": (
        "POST",
        "https://api.firecrawl.dev/v1/scrape",
        {"json": {"url": "https://a.example", "formats": ["markdown"]}},
        {"success": True, "data": {"markdown": "PAGE", "metadata": {"title": "T"}}},
    ),
    "anysearch": (
        "POST",
        "https://api.anysearch.com/v1/extract",
        {"json": {"url": "https://a.example"}},
        {"code": 0, "data": {"content": "PAGE", "title": "T"}},
    ),
}


@pytest.mark.parametrize("vendor", sorted(_FETCH_CASES))
async def test_each_reader_serves_the_page_through_one_envelope(
    vendor: str, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    method, url, body, payload = _FETCH_CASES[vendor]
    kw = {"payload": payload} if payload is not None else {"payload": None, "text": "PAGE"}
    with _patched(monkeypatch, **kw) as client:
        raw = await WebFetchTool(api_key="k", provider=vendor).execute("https://a.example")

    got_method, got_url, kwargs = client.calls[0]
    assert (got_method, got_url) == (method, url)
    for field, expected in body.items():
        assert kwargs[field] == expected
    if vendor != "jina":
        assert "Bearer k" in kwargs["headers"].values() or kwargs["headers"].get("x-api-key") == "k"

    envelope = json.loads(raw)
    assert envelope["text"] == "PAGE"
    assert envelope["extractor"] == FETCH_PROVIDERS[vendor].extractor
    assert envelope["length"] == 4 and envelope["truncated"] is False


@pytest.mark.parametrize("vendor", sorted(FETCH_PROVIDERS))
async def test_a_reader_status_error_names_the_vendor_and_status_only(
    vendor: str, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    with _patched(monkeypatch, {}, status=500):
        raw = await WebFetchTool(api_key="SECRET-KEY-123", provider=vendor).execute("https://a.example")

    envelope = json.loads(raw)
    assert "SECRET-KEY-123" not in raw
    assert envelope["error"] == f"{FETCH_PROVIDERS[vendor].label} answered HTTP 500"


# --------------------------------------------------------------------------- #
# A refused key pauses the tool instead of failing every call the same way


@pytest.mark.parametrize("status", sorted(web_mod.FETCH_REFUSAL_STATUSES))
@pytest.mark.parametrize("vendor", sorted(FETCH_PROVIDERS))
async def test_a_reader_refusing_the_key_pauses_the_tool(
    vendor: str, status: int, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    """On 2026-09-20 Jina answered 402 on every fetch of a session and the tool
    kept asking: each call was one more identical envelope. A refusal is about
    the key, so the next call is answered without a request, in the same words,
    with what the user has to do."""
    with _patched(monkeypatch, {}, status=status) as recorder:
        tool = WebFetchTool(api_key="SECRET-KEY-123", provider=vendor)
        first = json.loads(await tool.execute("https://a.example"))
        second = json.loads(await tool.execute("https://b.example"))

    assert len(recorder.calls) == 1, "the second call never reached the vendor"
    label = FETCH_PROVIDERS[vendor].label
    assert first["error"] == second["error"] == f"{label} refused the key (HTTP {status})"
    assert first["paused"] is True and second["paused"] is True
    assert "SECRET-KEY-123" not in json.dumps([first, second])
    assert "not sent" in second["detail"] and "Tell the user" in second["detail"]
    assert FETCH_PROVIDERS[vendor].config_path in first["detail"]
    assert "tools.web.fetch.provider" in first["detail"]
    # Each remedy says when it takes effect: the two config routes are read live, the env var on restart.
    assert (
        "select another vendor under tools.web.fetch.provider (both are read from the config file without a restart)"
        in first["detail"]
    )
    assert f"or restart with {FETCH_PROVIDERS[vendor].env_var} set" in first["detail"]
    # The failure streak reads a refusal as a deterministic failure, so a model
    # that keeps calling meets the stop-repeating nudge rather than a retry.
    from raven.agent.loop.failure_streak import failure_class, is_hard_tool_failure

    assert is_hard_tool_failure(json.dumps(second))
    assert failure_class(json.dumps(first)) == failure_class(json.dumps(second))


class _RotatingInFlight:
    """Stands in for ``httpx.AsyncClient``: refuses ``KEY-OLD`` with a 402 and,
    while that request is in flight, rotates the key source to ``KEY-NEW``,
    which it serves."""

    def __init__(self, keys: dict[str, str], served: Any) -> None:
        self.keys, self.served = keys, served
        self.sent: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_RotatingInFlight":
        return self

    async def __aenter__(self) -> "_RotatingInFlight":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
        headers = kwargs.get("headers") or {}
        sent = headers.get("Authorization", "").removeprefix("Bearer ") or headers.get("X-API-KEY") or ""
        self.sent.append(sent)
        request = httpx.Request(method, url)
        if sent == "KEY-OLD":
            self.keys["k"] = "KEY-NEW"
            return httpx.Response(402, json={}, request=request)
        return httpx.Response(200, json=self.served, request=request)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("POST", url, kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("GET", url, kwargs)


_TOOLS_ON_A_LIVE_KEY: dict[str, tuple[Any, Any, Any, Any]] = {
    "web_search": (
        lambda src: WebSearchTool(api_key=src, provider="tavily"),
        lambda tool: tool.execute("q"),
        {"results": [{"title": "T", "url": "https://a.example", "content": "S"}]},
        lambda out: out.startswith("Results for: q"),
    ),
    "image_search": (
        lambda src: web_mod.ImageSearchTool(api_key=src, provider="serper"),
        lambda tool: tool.execute(query="sky"),
        {"images": [{"title": "T", "imageUrl": "https://i.example/a.png", "imageWidth": 1280, "imageHeight": 720}]},
        lambda out: "1. T" in out,
    ),
    "web_fetch": (
        lambda src: WebFetchTool(api_key=src, provider="tavily"),
        lambda tool: tool.execute("https://a.example"),
        {"results": [{"url": "https://a.example", "raw_content": "PAGE"}]},
        lambda out: json.loads(out).get("text") == "PAGE",
    ),
}


@pytest.mark.parametrize("name", sorted(_TOOLS_ON_A_LIVE_KEY))
async def test_a_key_rotated_while_a_request_is_in_flight_is_tried_before_it_is_paused(
    name: str, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    """The key is read once per call and that one value is what the request
    carries and what a refusal is recorded against. Read again at ``note``
    time, a key the user replaced during the request's flight was paused
    without ever having been sent, and the main loop's tools serve every
    session of the process, so all of them lost the tool for the pause."""
    build, call, served, is_served = _TOOLS_ON_A_LIVE_KEY[name]
    keys = {"k": "KEY-OLD"}
    reads: list[str] = []

    def source() -> str:
        reads.append(keys["k"])
        return keys["k"]

    transport = _RotatingInFlight(keys, served)
    monkeypatch.setattr(web_mod.httpx, "AsyncClient", transport)
    tool = build(source)

    refused = await call(tool)

    assert transport.sent == ["KEY-OLD"]
    assert reads == ["KEY-OLD"], "one read per call: the value sent is the value paused"
    assert "refused the key (HTTP 402)" in refused
    reads.clear()

    again = await call(tool)

    assert transport.sent == ["KEY-OLD", "KEY-NEW"], "the replacement is tried, not answered from the pause"
    assert is_served(again), again
    assert reads == ["KEY-NEW"]


class _TwoReaders:
    """Stands in for ``httpx.AsyncClient``: Tavily answers a page as JSON, Jina as text; both are recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_TwoReaders":
        return self

    async def __aenter__(self) -> "_TwoReaders":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs.get("headers") or {}))
        payload = {"results": [{"url": "https://a.example", "raw_content": "TAVILY PAGE"}]}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs.get("headers") or {}))
        return httpx.Response(200, text="JINA PAGE", request=httpx.Request("GET", url))


async def test_a_keyed_reader_whose_key_is_cleared_falls_back_to_jina_per_call(
    monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    """The key is live, so the Jina substitution has to be too: a keyed
    backend whose key is cleared in the file would otherwise stay registered
    and send an empty ``Authorization: Bearer`` on every call, and the 401 it
    got back could arm no pause, there being no key to record it against.
    The substitution is decided per call from the same read as the key."""
    keys = {"tavily": "sk-tavily"}
    reader = _TwoReaders()
    monkeypatch.setattr(web_mod.httpx, "AsyncClient", reader)
    tool = WebFetchTool(api_key=lambda: keys["tavily"], provider="tavily")

    first = json.loads(await tool.execute("https://a.example"))
    keys["tavily"] = ""
    second = json.loads(await tool.execute("https://b.example"))

    assert first["extractor"] == "tavily-extract" and first["text"] == "TAVILY PAGE"
    assert reader.calls[0][0] == "https://api.tavily.com/extract"
    assert reader.calls[0][1]["Authorization"] == "Bearer sk-tavily"
    assert second["extractor"] == "jina-reader" and second["text"] == "JINA PAGE"
    assert reader.calls[1][0] == "https://r.jina.ai/https://b.example"
    assert "Authorization" not in reader.calls[1][1], "no credential is sent for a keyless read"
    assert not [h for _, h in reader.calls if h.get("Authorization") == "Bearer "], "an empty key is never sent"


async def test_a_refusal_by_one_vendor_pauses_no_other(monkeypatch: pytest.MonkeyPatch, _open_gate: None) -> None:
    """The vendor is live too (``tools.web.<kind>.provider`` is read per call),
    so the pause is recorded against the (vendor, key) pair the request carried:
    a vendor switched under the tool is a new request, whatever its key."""
    vendor = {"now": "tavily"}
    with _patched(monkeypatch, {}, status=402) as recorder:
        tool = WebFetchTool(api_key="same-key", provider=lambda: vendor["now"])
        first = json.loads(await tool.execute("https://a.example"))
        vendor["now"] = "exa"
        second = json.loads(await tool.execute("https://a.example"))

    assert first["error"] == "Tavily refused the key (HTTP 402)"
    assert len(recorder.calls) == 2, "another vendor on the same key string is not paused"
    assert second["error"] == "Exa refused the key (HTTP 402)" and "not sent" not in second["detail"]


async def test_a_new_key_lifts_the_pause_at_once(monkeypatch: pytest.MonkeyPatch, _open_gate: None) -> None:
    """Through the key source the loops hand the tool, not a private field: the
    refusal tells the user to set a new key, so the value that lifts the pause
    has to be one the tool reads on the next call."""
    keys = {"tavily": "old"}
    with _patched(monkeypatch, {}, status=402) as recorder:
        tool = WebFetchTool(api_key=lambda: keys["tavily"], provider="tavily")
        await tool.execute("https://a.example")
        await tool.execute("https://a.example")
        assert len(recorder.calls) == 1, "same key: paused, not sent"
        keys["tavily"] = "new"
        await tool.execute("https://a.example")

    assert len(recorder.calls) == 2
    assert recorder.calls[-1][2]["headers"]["Authorization"] == "Bearer new"


async def test_the_pause_ends_after_the_cooldown_and_one_request_goes_through(
    monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(web_mod.time, "monotonic", lambda: clock[0])
    with _patched(monkeypatch, {}, status=402) as recorder:
        tool = WebFetchTool(api_key="k", provider="tavily")
        await tool.execute("https://a.example")
        clock[0] += web_mod.VENDOR_REFUSAL_PAUSE_S - 1
        paused = json.loads(await tool.execute("https://a.example"))
        clock[0] += 2
        again = json.loads(await tool.execute("https://a.example"))

    assert paused["paused"] is True and "not sent" in paused["detail"]
    assert len(recorder.calls) == 2, "the cooldown's end sends one real request"
    assert "not sent" not in again["detail"], "a refusal met again re-arms the pause"


async def test_a_status_that_is_not_about_the_key_does_not_pause(
    monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    with _patched(monkeypatch, {}, status=500) as recorder:
        tool = WebFetchTool(api_key="k", provider="tavily")
        await tool.execute("https://a.example")
        await tool.execute("https://a.example")

    assert len(recorder.calls) == 2


class _OneHostRefused:
    """Stands in for ``httpx.AsyncClient``: one host answers ``status``, every other serves a page."""

    def __init__(self, host: str, status: int) -> None:
        self.host, self.status = host, status
        self.calls: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_OneHostRefused":
        return self

    async def __aenter__(self) -> "_OneHostRefused":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(url)
        request = httpx.Request("GET", url)
        if self.host in url:
            return httpx.Response(self.status, json={"code": self.status}, request=request)
        return httpx.Response(200, text="PAGE", request=request)


@pytest.mark.parametrize("status", sorted(web_mod.SEARCH_REFUSAL_STATUSES))
async def test_a_keyless_reader_is_never_paused_by_a_status(
    status: int, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    """The default reader is Jina without a key, and Jina answers an anonymous
    request for a domain it has blocked with 403 for every URL under it. A
    request that carried no key cannot have had one refused, so no status
    pauses the tool: the blocked page is reported as before, the next URL is
    fetched, and nobody is told to replace a key that does not exist."""
    reader = _OneHostRefused("blocked.example", status)
    monkeypatch.setattr(web_mod.httpx, "AsyncClient", reader)
    tool = WebFetchTool(provider="jina")
    assert tool.api_key == ""

    blocked = json.loads(await tool.execute("https://blocked.example/page"))
    served = [json.loads(await tool.execute(url)) for url in ("https://a.example/", "https://b.example/")]

    assert blocked["error"] == f"Jina Reader answered HTTP {status}" and "paused" not in blocked
    assert [page["text"] for page in served] == ["PAGE", "PAGE"]
    assert len(reader.calls) == 3, "every URL reached the reader"


@pytest.mark.parametrize("vendor", sorted(FETCH_PROVIDERS))
async def test_a_readers_403_is_about_the_page_not_the_key(
    vendor: str, monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    """Through a reader a 403 speaks of the URL, not the key: Jina answers 403
    for a domain it blocks, Firecrawl for a site its policy does not scrape.
    It is reported per URL, and the next URL is fetched."""
    with _patched(monkeypatch, {}, status=403) as recorder:
        tool = WebFetchTool(api_key="k", provider=vendor)
        first = json.loads(await tool.execute("https://a.example"))
        second = json.loads(await tool.execute("https://b.example"))

    assert len(recorder.calls) == 2, "a 403 pauses nothing"
    assert first["error"] == second["error"] == f"{FETCH_PROVIDERS[vendor].label} answered HTTP 403"
    assert "paused" not in first and "paused" not in second


@pytest.mark.parametrize("status", sorted(web_mod.SEARCH_REFUSAL_STATUSES))
@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
async def test_a_search_vendor_refusing_the_key_pauses_the_tool(
    vendor: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search vendor's API is the endpoint, so its 403 speaks of the key the
    way 401 and 402 do: Serper answers a rejected key with 403."""
    with _patched(monkeypatch, {}, status=status) as recorder:
        tool = WebSearchTool(api_key="SECRET-KEY-123", provider=vendor)
        first = await tool.execute("q1")
        second = await tool.execute("q2")

    assert len(recorder.calls) == 1
    label = SEARCH_PROVIDERS[vendor].label
    assert first.startswith(f"Error: {label} refused the key (HTTP {status}). ")
    assert second.startswith(f"Error: {label} refused the key (HTTP {status}). ")
    assert "SECRET-KEY-123" not in first + second
    assert "tools.web.search.provider" in first and "not sent" in second
    assert (
        "select another vendor under tools.web.search.provider (both are read from the config file without a restart)"
        in first
    )


async def test_an_image_search_refusal_pauses_the_later_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.tools.web import ImageSearchTool

    with _patched(monkeypatch, {}, status=403) as recorder:
        tool = ImageSearchTool(api_key="k", provider="serper")
        first = await tool.execute(query="sky")
        second = await tool.execute(query="sea")

    assert len(recorder.calls) == 1
    assert "Serper refused the key (HTTP 403)" in first and "Serper refused the key (HTTP 403)" in second
    assert "not sent" in second


@contextmanager
def _transport_dies(monkeypatch: pytest.MonkeyPatch, exc: Exception):
    """A reader whose transport raises the same way whatever the host."""

    class _Dead(_Recorder):
        def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
            raise exc

    monkeypatch.setattr(web_mod.httpx, "AsyncClient", _Dead(None))
    yield


async def test_one_transport_fault_on_two_hosts_is_one_streak_class(
    monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    """One broken reader walked across hosts has to read as one repeated cause.

    ``failure_class`` keys an envelope on its ``error`` string alone, so an
    exception's own text there carries the host into the key: the same dead
    certificate on two hosts becomes two classes, the streak never reaches
    ``_LOOP_BREAK_THRESHOLD``, and the nudge that exists for exactly this -- a
    model repeating one dead call -- never fires. The host stays in the envelope
    for the model to read; it just does not decide the class.
    """
    envelopes = []
    for host in ("alpha.example.com", "beta.example.org"):
        exc = httpx.ConnectError(f"[SSL: CERTIFICATE_VERIFY_FAILED] hostname '{host}' does not match")
        with _transport_dies(monkeypatch, exc):
            envelopes.append(await WebFetchTool(api_key="k", provider="jina").execute(f"https://{host}/p"))

    assert all(is_hard_tool_failure(raw) for raw in envelopes)
    assert len({failure_class(raw) for raw in envelopes}) == 1
    assert "alpha.example.com" in envelopes[0] and "beta.example.org" in envelopes[1]


async def test_two_vendor_refusals_stay_two_streak_classes(monkeypatch: pytest.MonkeyPatch, _open_gate: None) -> None:
    """The other direction, which the fix above must not trade away.

    A vendor that answered without a page composes its own message here, and two
    different refusals are two causes: folding them onto the exception type would
    make a drained key and a blocked page one streak, which is the failure
    ``failure_class`` was written to avoid.
    """
    with _patched(monkeypatch, {"code": 401, "message": "bad key"}):
        spent = await WebFetchTool(api_key="k", provider="anysearch").execute("https://a.example")
    with _patched(monkeypatch, {"results": [], "failed_results": [{"url": "https://a.example", "error": "blocked"}]}):
        blocked = await WebFetchTool(api_key="k", provider="tavily").execute("https://a.example")

    assert failure_class(spent) != failure_class(blocked)


async def test_one_blocked_reason_on_two_hosts_is_one_streak_class() -> None:
    """The gate in front of the reader, which the two tests above never reach.

    Every reason this gate composes names the address it refused, so the reason
    in the classification key gave a model walking an internal range one class
    per address: the streak never reached the break threshold and the nudge that
    would have sent it somewhere else never fired. The gate runs before any
    transport, so nothing here needs a reader.
    """
    envelopes = [
        await WebFetchTool(api_key="k", provider="jina").execute(u)
        for u in ("http://10.0.0.1/p", "http://192.168.1.1/p")
    ]

    assert all(is_hard_tool_failure(raw) for raw in envelopes)
    assert len({failure_class(raw) for raw in envelopes}) == 1
    # The address the model has to stop reaching for is still in front of it.
    assert "10.0.0.1" in json.loads(envelopes[0])["detail"]


def test_every_reader_is_covered_by_the_fetch_table() -> None:
    assert set(_FETCH_CASES) == set(FETCH_PROVIDERS)


async def test_a_reader_answering_without_a_page_is_an_error_envelope(
    monkeypatch: pytest.MonkeyPatch, _open_gate: None
) -> None:
    with _patched(monkeypatch, {"results": [], "failed_results": [{"url": "https://a.example", "error": "blocked"}]}):
        raw = await WebFetchTool(api_key="k", provider="tavily").execute("https://a.example")

    envelope = json.loads(raw)
    assert "text" not in envelope
    assert "blocked" in envelope["error"]


async def test_an_anysearch_envelope_failure_is_read_as_one(monkeypatch: pytest.MonkeyPatch, _open_gate: None) -> None:
    with _patched(monkeypatch, {"code": 401, "message": "bad key"}):
        raw = await WebFetchTool(api_key="k", provider="anysearch").execute("https://a.example")

    assert "bad key" in json.loads(raw)["error"]


# --------------------------------------------------------------------------- #
# The settings page's own copy of the vendor list


# The table lives in source.ts rather than the Tools page itself, so the
# onboarding wizard's web-search step (features/settings/SetupBodies.tsx) can
# read the same copy through webStepDone rather than a second one.
_SETTINGS_PAGE = Path(__file__).resolve().parents[1] / "ui-web/src/features/settings/source.ts"


def _tsx_vendor_pick(tool: str) -> dict[str, Any]:
    """One ``WEB_VENDOR`` entry as the settings page declares it.

    Parsed from the source rather than exported through a generated artefact:
    the list is eight short strings, and a build step to carry them would be
    more machinery than the thing it carries. The asserts below fail loudly if
    the shape it reads stops being there, which is the state a silent regex
    would report as an empty list.
    """
    body = _SETTINGS_PAGE.read_text(encoding="utf-8")
    entry = re.search(rf"\b{tool}:\s*\{{(.*?)\}},", body, re.S)
    assert entry, f"{tool} has no WEB_VENDOR entry in {_SETTINGS_PAGE.name}"
    block = entry.group(1)
    vendors = re.search(r"vendors:\s*\[(.*?)\]", block, re.S)
    path = re.search(r"path:\s*'([^']+)'", block)
    fallback = re.search(r"fallback:\s*'([^']+)'", block)
    assert vendors and path and fallback, f"{tool}'s WEB_VENDOR entry lost a field"
    return {
        "vendors": re.findall(r"'([^']+)'", vendors.group(1)),
        "path": path.group(1),
        "fallback": fallback.group(1),
    }


def _tsx_labels() -> dict[str, str]:
    body = _SETTINGS_PAGE.read_text(encoding="utf-8")
    block = re.search(r"WEB_VENDOR_LABEL:\s*Record<string, string>\s*=\s*\{(.*?)\n\}", body, re.S)
    assert block, f"WEB_VENDOR_LABEL is gone from {_SETTINGS_PAGE.name}"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


def test_the_settings_page_offers_exactly_the_schemas_vendors() -> None:
    """Same vendors, same order, same default. Order because the pick is a
    ``select``: a list the browser shows in another order than the wizard is
    two answers to one question."""
    from typing import get_args

    search = _tsx_vendor_pick("web_search")
    fetch = _tsx_vendor_pick("web_fetch")

    assert search["vendors"] == list(get_args(WebSearchProvider))
    assert fetch["vendors"] == list(get_args(WebFetchProvider))
    # Not vacuous: the parse found real lists, so an empty one cannot pass.
    assert len(search["vendors"]) == 8 and len(fetch["vendors"]) == 5
    assert search["fallback"] == DEFAULT_SEARCH_PROVIDER
    assert fetch["fallback"] == DEFAULT_FETCH_PROVIDER
    assert search["path"] == "tools.web.search.provider"
    assert fetch["path"] == "tools.web.fetch.provider"


def test_the_settings_page_names_every_vendor_the_way_the_spec_table_does() -> None:
    """The label is what a person picks by. Two spellings of one vendor across
    two surfaces reads as two vendors."""
    labels = _tsx_labels()
    spec_labels = {v: s.label for v, s in SEARCH_PROVIDERS.items()}
    for vendor, spec in FETCH_PROVIDERS.items():
        assert spec_labels.setdefault(vendor, spec.label) == spec.label, (
            f"{vendor} is labelled differently in the two spec tables"
        )

    assert labels == spec_labels
    assert set(labels) == set(WEB_VENDOR_ENV_VARS), "every vendor a key can be held for needs a label"


# --------------------------------------------------------------------------- #
# The credential table as a hand-edited file reaches it
# --------------------------------------------------------------------------- #


def test_a_misspelled_vendor_fails_validation() -> None:
    """What the section's docstring promises. The tree's default policy is to
    drop an unknown member, which would leave the vendor unkeyed and its tool
    withheld -- the "feature X did nothing" case the loader refuses to mask."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WebProvidersConfig.model_validate({"tavili": {"apiKey": "sk-typo"}})
    # Through a whole-config load, which is how a hand-edited file arrives.
    with pytest.raises(ValidationError):
        Config.model_validate({"tools": {"web": {"providers": {"tavili": {"apiKey": "sk-typo"}}}}})


def test_forbidding_extras_keeps_both_key_spellings() -> None:
    """``extra="forbid"`` beside an alias generator is the combination that
    would reject the config file's own snake_case spelling."""
    assert WebProvidersConfig.model_validate({"tavily": {"apiKey": "sk-a"}}).key_for("tavily") == "sk-a"
    assert WebProvidersConfig.model_validate({"tavily": {"api_key": "sk-b"}}).key_for("tavily") == "sk-b"
