"""Web tools: search result shaping flags + fetch digest path.

Search: answerBox / knowledgeGraph / snippets are included by default
(product behavior unchanged) and individually removable — deep-research
profiles strip them so the model must browse pages instead of answering
from the SERP.

Fetch: long pages with an ``info_to_extract`` go through the injected
digest function; every digest failure mode (no fn, short page, timeout,
exception, empty output) degrades to the plain truncation path.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from raven.agent.tools.web import _SNIPPET_REPEAT_NOTE, WebFetchTool, WebSearchTool

SERPER_DATA = {
    "organic": [
        {"title": "Result One", "link": "https://one.example", "snippet": "first snippet"},
        {"title": "Result Two", "link": "https://two.example", "snippet": "second snippet"},
    ],
    "answerBox": {"answer": "42 is the answer"},
    "knowledgeGraph": {"title": "Entity", "description": "entity description"},
}


class _FakeResponse:
    def __init__(self, json_data=None, text=""):
        self._json = json_data
        self.text = text
        self.status_code = 200

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return self._response

    async def get(self, *args, **kwargs):
        return self._response


class _Patched:
    """Stub the HTTP client and the SSRF validator (fake hosts don't resolve)."""

    def __init__(self, response):
        self._client = patch("raven.agent.tools.web.httpx.AsyncClient", lambda **kw: _FakeClient(response))
        async def _ok(u, **_):
            return (True, "")

        self._validator = patch(
            "raven.agent.tools.web.validate_url_target_async", _ok
        )

    def __enter__(self):
        self._client.__enter__()
        self._validator.__enter__()
        return self

    def __exit__(self, *args):
        self._validator.__exit__(*args)
        return self._client.__exit__(*args)


def _patch_client(response):
    return _Patched(response)


@pytest.mark.asyncio
async def test_search_includes_direct_answers_by_default():
    tool = WebSearchTool(api_key="k")
    with _patch_client(_FakeResponse(json_data=SERPER_DATA)):
        out = await tool.execute(query="q")
    assert "Answer: 42 is the answer" in out
    assert "Knowledge: Entity" in out
    assert "first snippet" in out
    assert "https://one.example" in out


@pytest.mark.asyncio
async def test_search_strips_direct_answers_and_snippets_when_disabled():
    tool = WebSearchTool(
        api_key="k",
        include_answer_box=False,
        include_knowledge_graph=False,
        include_snippets=False,
    )
    with _patch_client(_FakeResponse(json_data=SERPER_DATA)):
        out = await tool.execute(query="q")
    assert "Answer:" not in out
    assert "Knowledge:" not in out
    assert "snippet" not in out
    assert "Result One" in out
    assert "https://one.example" in out


LONG_PAGE = "para about topic. " * 1000


@pytest.mark.asyncio
async def test_fetch_digests_long_page_with_info_to_extract():
    async def digest(text, info):
        return f"extracted[{info}] from {len(text)} chars"

    tool = WebFetchTool(api_key="k", digest_fn=digest, digest_threshold_chars=100)
    with _patch_client(_FakeResponse(text=LONG_PAGE)):
        out = json.loads(await tool.execute(url="https://ok.example/page", info_to_extract="founding year"))

    assert out["digested"] is True
    assert out["extractor"] == "jina-reader+digest"
    assert out["text"].startswith("extracted[founding year]")
    assert out["source_chars"] == len(LONG_PAGE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "digest_fn,info",
    [
        (None, "anything"),
        ("short_page", "anything"),
        ("no_info", None),
        ("raises", "anything"),
        ("empty", "anything"),
        ("times_out", "anything"),
    ],
)
async def test_fetch_degrades_to_truncation(digest_fn, info):
    async def raises(text, i):
        raise RuntimeError("digest model down")

    async def empty(text, i):
        return "   "

    async def times_out(text, i):
        await asyncio.sleep(5)
        return "late"

    fns = {"raises": raises, "empty": empty, "times_out": times_out}
    threshold = 100
    page = LONG_PAGE
    if digest_fn == "short_page":
        page = "tiny page"

        async def fine(text, i):
            return "should not be called"

        fn = fine
    elif digest_fn == "no_info":

        async def fine(text, i):
            return "should not be called"

        fn = fine
    elif digest_fn is None:
        fn = None
    else:
        fn = fns[digest_fn]

    tool = WebFetchTool(
        api_key="k",
        max_chars=500,
        digest_fn=fn,
        digest_threshold_chars=threshold,
        digest_timeout_s=0.05,
    )
    with _patch_client(_FakeResponse(text=page)):
        out = json.loads(await tool.execute(url="https://ok.example/page", info_to_extract=info))

    assert "digested" not in out
    assert out["extractor"] == "jina-reader"
    expected = page[:500] if len(page) > 500 else page
    assert out["text"] == expected


# --------------------------------------------------------------------------- #
# fixed-corpus backend (BrowseComp-Plus protocol: no live web)                  #
# --------------------------------------------------------------------------- #


class _CorpusTransport(httpx.AsyncBaseTransport):
    """Stands in for the local retrieval service."""

    def __init__(self, hit: bool = True):
        self.hit = hit
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "query": "q",
                    "results": [
                        {"docid": "5412", "title": "Arwa University", "url": "https://x/1", "snippet": "cultural"}
                    ],
                },
            )
        if not self.hit:
            return httpx.Response(404, json={"error": "not in corpus"})
        return httpx.Response(200, json={"docid": "5412", "url": "https://x/1", "title": "T", "text": "body text"})


def _patch_corpus_client(monkeypatch, transport):
    """Route the tool's httpx client at a stub transport.

    Bind the real class first: patching the name and then calling it inside the
    replacement would re-enter the patch.
    """
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_corpus_search_matches_the_live_web_shape(monkeypatch):
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765")

    out = await tool.execute(query="arwa university", count=3)

    assert out.startswith("Results for: arwa university")
    assert "https://x/1" in out and "Arwa University" in out
    # No Serper key involved, and the request went to the corpus service.
    assert transport.requests[0].url.path == "/search"


@pytest.mark.asyncio
async def test_corpus_fetch_reports_out_of_corpus_as_a_rule_not_an_outage(monkeypatch):
    transport = _CorpusTransport(hit=False)
    _patch_corpus_client(monkeypatch, transport)
    tool = WebFetchTool(corpus_endpoint="http://local:8765")

    payload = json.loads(await tool.execute(url="https://elsewhere/page"))

    assert payload["error"] == "not in corpus"
    assert "fixed corpus" in payload["hint"]


@pytest.mark.asyncio
async def test_corpus_fetch_returns_the_document(monkeypatch):
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebFetchTool(corpus_endpoint="http://local:8765", max_chars=4)

    payload = json.loads(await tool.execute(url="https://x/1"))

    assert payload["extractor"] == "corpus" and payload["docid"] == "5412"
    assert payload["text"] == "body" and payload["truncated"] is True


@pytest.mark.asyncio
async def test_repeat_notice_off_by_default_reruns_the_query(monkeypatch):
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765")

    first = await tool.execute(query="arwa university")
    second = await tool.execute(query="arwa university")

    assert first == second and "[repeat]" not in second
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_repeat_notice_annotates_and_serves_from_cache(monkeypatch):
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765", repeat_notice=True)

    first = await tool.execute(query="arwa university")
    # Case and whitespace do not make a different search.
    second = await tool.execute(query="  Arwa   University ")

    assert "[repeat]" not in first
    # No ordinal: the counter it named only advanced on cache misses, so the
    # index was wrong on 14 of 47 notices in one batch, once by 16.
    assert second.startswith("[repeat] This exact query already ran earlier in this turn")
    assert "cannot surface anything new" not in second
    assert first in second
    # The repeat never reached the retrieval service.
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_repeat_notice_keys_on_count_and_resets_per_turn(monkeypatch):
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765", repeat_notice=True)

    await tool.execute(query="q", count=3)
    # A wider request is a superset, not a repeat.
    assert "[repeat]" not in await tool.execute(query="q", count=8)

    tool.start_turn()
    # The same query on a later turn is a re-check, not a loop.
    assert "[repeat]" not in await tool.execute(query="q", count=3)
    assert len(transport.requests) == 3


class _DeadSearchTransport(httpx.AsyncBaseTransport):
    """Retrieval service that fails every search."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(503, json={"error": "unavailable"})


@pytest.mark.asyncio
async def test_repeat_notice_never_caches_a_retrieval_failure(monkeypatch):
    transport = _DeadSearchTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765", repeat_notice=True)

    # A transient failure must stay retryable: caching it would make one bad
    # response permanent for the rest of the turn.
    assert "[repeat]" not in await tool.execute(query="q")
    assert "[repeat]" not in await tool.execute(query="q")
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_snippet_dedup_is_off_by_default(monkeypatch):
    """The default must repeat the preview: the flow-off anchor shares this path.

    Deduplicating unconditionally would move the anchor, because the anchor renders
    snippets on the same corpus path.
    """
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765")

    assert "cultural" in await tool.execute(query="first query")
    assert "cultural" in await tool.execute(query="second query")


@pytest.mark.asyncio
async def test_snippet_dedup_marks_repeats_and_is_scoped_to_the_turn(monkeypatch):
    """A document previewed once is not previewed again, and says so.

    A blank where neighbouring results carry text would read as "this result has no
    content", which is a different claim than "you have already seen this one".
    """
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765", snippet_dedup_by_docid=True)

    first = await tool.execute(query="first query")
    second = await tool.execute(query="second query")

    assert "cultural" in first and _SNIPPET_REPEAT_NOTE not in first
    assert _SNIPPET_REPEAT_NOTE in second and "cultural" not in second
    # The URL still identifies the result; only the preview is suppressed.
    assert "https://x/1" in second

    tool.start_turn()
    assert "cultural" in await tool.execute(query="first query")


@pytest.mark.asyncio
async def test_web_ledger_is_off_unless_configured(monkeypatch, tmp_path):
    """No env var, no ledger, no failure - every already-measured arm is unchanged."""
    monkeypatch.delenv("RAVEN_WEB_LEDGER", raising=False)
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765")

    assert "Results for:" in await tool.execute(query="q")
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_web_ledger_records_ordered_urls_and_marks_replays(monkeypatch, tmp_path):
    """Rank order and the replay bit are both first-class, written when they happen.

    The replay bit is the reason this ledger exists: a cache hit never reaches the
    retrieval service, so a service-side log cannot see it at all.
    """
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebSearchTool(corpus_endpoint="http://local:8765", repeat_notice=True)

    await tool.execute(query="same query")
    await tool.execute(query="same query")

    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["op"] for r in rows] == ["search", "search"]
    # The corpus stub serves one result; the URL list is a list, in rank order.
    assert rows[0]["urls"] == ["https://x/1"] and rows[0]["n"] == 1
    assert rows[0]["replay"] is False
    # Second call never reached the service, and the ledger says so.
    assert rows[1]["replay"] is True and rows[1]["urls"] == ["https://x/1"]
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_web_ledger_records_fetch_url_and_chars(monkeypatch, tmp_path):
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    tool = WebFetchTool(corpus_endpoint="http://local:8765")

    await tool.execute(url="https://x/1")

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["op"] == "fetch" and row["url"] == "https://x/1"
    assert row["ok"] is True and row["chars"] == len("body text")


# ---------------------------------------------------------------------------
# Reader-path resilience: bounded retry, and a resolver failure is not a refusal
# ---------------------------------------------------------------------------


class _FlakyReaderClient:
    """Reader client that raises a scripted sequence before answering."""

    def __init__(self, failures, response):
        self._failures = list(failures)
        self._response = response
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return self._response


class _StatusResponse:
    """A response whose raise_for_status raises a real httpx.HTTPStatusError."""

    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self):
        request = httpx.Request("GET", "https://r.jina.ai/https://x.example/")
        raise httpx.HTTPStatusError(
            f"status {self.status_code}",
            request=request,
            response=httpx.Response(self.status_code, request=request),
        )


def _patch_reader(monkeypatch, client):
    monkeypatch.setattr(
        "raven.agent.tools.web.httpx.AsyncClient", lambda **kw: client
    )
    async def _ok_target(u, **_):
        return (True, "")

    monkeypatch.setattr(
        "raven.agent.tools.web.validate_url_target_async", _ok_target
    )
    slept = []

    async def _record_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("raven.agent.tools.web.asyncio.sleep", _record_sleep)
    return slept


@pytest.mark.asyncio
async def test_fetch_retries_a_disconnect_then_succeeds(monkeypatch):
    disconnect = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    client = _FlakyReaderClient([disconnect, disconnect], _FakeResponse(text="page body"))
    slept = _patch_reader(monkeypatch, client)

    out = json.loads(await WebFetchTool().execute(url="https://x.example/"))

    assert out["text"] == "page body"
    assert client.calls == 3
    assert slept == [1.0, 4.0]


@pytest.mark.asyncio
async def test_fetch_gives_up_after_the_last_backoff(monkeypatch):
    disconnect = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    client = _FlakyReaderClient([disconnect] * 5, _FakeResponse(text="never reached"))
    _patch_reader(monkeypatch, client)

    out = json.loads(await WebFetchTool().execute(url="https://x.example/"))

    assert "Server disconnected" in out["error"]
    assert client.calls == 3


@pytest.mark.asyncio
async def test_fetch_does_not_retry_a_client_error(monkeypatch):
    """The batch that lost its read surface lost it to a 402; retrying buys nothing."""
    client = _FlakyReaderClient([], _StatusResponse(402))
    slept = _patch_reader(monkeypatch, client)

    out = json.loads(await WebFetchTool().execute(url="https://x.example/"))

    assert "402" in out["error"]
    assert client.calls == 1
    assert slept == []


@pytest.mark.asyncio
async def test_fetch_retries_a_throttling_status(monkeypatch):
    client = _FlakyReaderClient([], _StatusResponse(503))
    slept = _patch_reader(monkeypatch, client)

    await WebFetchTool().execute(url="https://x.example/")

    assert client.calls == 3
    assert slept == [1.0, 4.0]


@pytest.mark.asyncio
async def test_retries_are_a_separate_ledger_op(monkeypatch, tmp_path):
    """One logical fetch stays one ``fetch`` line, or per-arm fetch counts inflate
    by however many transient errors that arm happened to meet."""
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    disconnect = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    client = _FlakyReaderClient([disconnect, disconnect], _FakeResponse(text="page body"))
    _patch_reader(monkeypatch, client)

    await WebFetchTool().execute(url="https://x.example/")

    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["op"] for r in rows] == ["fetch_retry", "fetch_retry", "fetch"]
    assert rows[0]["key"] == "https://x.example/"
    assert [r["attempt"] for r in rows[:2]] == [1, 2]
    assert rows[-1]["ok"] is True


@pytest.mark.asyncio
async def test_a_resolver_failure_no_longer_refuses_the_fetch(monkeypatch):
    """The reader opens the connection, so a local resolver failure is not evidence."""
    import socket

    def fake_getaddrinfo(*_args, **_kwargs):
        raise socket.gaierror("temporary failure in name resolution")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    client = _FlakyReaderClient([], _FakeResponse(text="page body"))
    monkeypatch.setattr("raven.agent.tools.web.httpx.AsyncClient", lambda **kw: client)

    out = json.loads(await WebFetchTool().execute(url="https://transient.example/"))

    assert "error" not in out
    assert out["text"] == "page body"
    assert client.calls == 1


class _FlakySearchClient(_FlakyReaderClient):
    async def post(self, *args, **kwargs):
        return await self.get(*args, **kwargs)


@pytest.mark.asyncio
async def test_search_retries_on_the_same_policy_as_fetch(monkeypatch, tmp_path):
    """Search took the larger share of the damage on the batch that motivated this."""
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    disconnect = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    client = _FlakySearchClient([disconnect], _FakeResponse(json_data=SERPER_DATA))
    slept = _patch_reader(monkeypatch, client)

    out = await WebSearchTool(api_key="k").execute(query="who")

    assert "Result One" in out
    assert client.calls == 2 and slept == [1.0]
    ops = [json.loads(line)["op"] for line in ledger.read_text().splitlines()]
    assert ops == ["search_retry", "search"]


@pytest.mark.asyncio
async def test_the_retry_budget_is_per_turn_and_bounded(monkeypatch):
    """Unbounded retries cost the arm that browses most, which is the arm under test."""
    from raven.agent.tools import web as web_mod

    disconnect = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    client = _FlakyReaderClient([disconnect] * 50, _FakeResponse(text="never"))
    _patch_reader(monkeypatch, client)
    tool = WebFetchTool()
    tool._retry_budget = web_mod._RetryBudget(1)

    await tool.execute(url="https://x.example/")
    assert client.calls == 2  # one retry granted, then the budget refuses

    tool.start_turn()
    client.calls = 0
    await tool.execute(url="https://x.example/")
    # Refilled to the default, so the per-call cap binds again instead of the budget.
    assert client.calls == 3


@pytest.mark.asyncio
async def test_a_rate_limiter_is_waited_out_on_its_own_terms(monkeypatch):
    """429 is not a transport error: honour Retry-After, and spread the rest."""
    from raven.agent.tools import web as web_mod

    request = httpx.Request("GET", "https://r.jina.ai/x")
    limited = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(429, request=request, headers={"Retry-After": "7"}),
    )
    client = _FlakyReaderClient([limited], _FakeResponse(text="page body"))
    slept = _patch_reader(monkeypatch, client)

    await WebFetchTool().execute(url="https://x.example/")
    assert slept == [7.0]

    # Without the header the wait is spread by request key, not drawn at random:
    # two workers separate, and a rerun of one arm replays identically.
    spread = [web_mod._retry_delay(1.0, 429, None, k) for k in ("a", "b", "c")]
    assert len(set(spread)) == 3
    assert all(1.0 <= s < 2.0 for s in spread)
    assert spread == [web_mod._retry_delay(1.0, 429, None, k) for k in ("a", "b", "c")]
    assert web_mod._retry_delay(1.0, None, None, "a") == 1.0


# ---------------------------------------------------------------------------
# Ledger: three outcomes stop sharing one boolean, and SERP shaping gets a size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_zero_hit_query_is_not_reported_as_a_tool_error(monkeypatch, tmp_path):
    """Measured: 18.9% of the anchor's searches returned nothing, and every one of
    them set ``failed``. A gate reading that as a search-error rate calls a healthy
    run broken."""
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    with _patch_client(_FakeResponse(json_data={"organic": []})):
        await WebSearchTool(api_key="k").execute(query="nothing matches this")

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["n"] == 0
    assert row["zero_hit"] is True
    assert row["transport_err"] is False
    # Unchanged, and still meaning "not cached for replay".
    assert row["failed"] is True


@pytest.mark.asyncio
async def test_a_transport_error_is_reported_as_one(monkeypatch, tmp_path):
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    client = _FlakySearchClient([httpx.ConnectError("no route")] * 5, _FakeResponse())
    _patch_reader(monkeypatch, client)

    await WebSearchTool(api_key="k").execute(query="anything")

    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    final = rows[-1]
    assert final["op"] == "search"
    assert final["transport_err"] is True
    assert final["zero_hit"] is False


@pytest.mark.asyncio
async def test_the_ledger_sizes_the_channels_a_dr_profile_switches_off(monkeypatch, tmp_path):
    """The anchor's direct-answer channel had no measured magnitude anywhere on disk."""
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    with _patch_client(_FakeResponse(json_data=SERPER_DATA)):
        await WebSearchTool(api_key="k").execute(query="who")
    on = json.loads(ledger.read_text().splitlines()[0])
    assert on["answer_box_chars"] == len(SERPER_DATA["answerBox"]["answer"])
    assert on["knowledge_chars"] == len("Entity") + len("entity description")
    assert on["snippet_lines"] == 2 and on["snippet_chars"] > 0

    # Same payload through a DR profile: every channel off, so every size is 0.
    ledger.unlink()
    with _patch_client(_FakeResponse(json_data=SERPER_DATA)):
        await WebSearchTool(api_key="k", include_answer_box=False,
                            include_knowledge_graph=False,
                            include_snippets=False).execute(query="who")
    off = json.loads(ledger.read_text().splitlines()[0])
    assert off["answer_box_chars"] == 0
    assert off["knowledge_chars"] == 0
    assert off["snippet_lines"] == 0 and off["snippet_chars"] == 0


@pytest.mark.asyncio
async def test_the_corpus_renderer_has_no_answer_box_to_switch_off(monkeypatch, tmp_path):
    """Load-bearing for caliber: those two knobs are no-ops on the corpus axis, so a
    corpus-axis flow-on-minus-flow-off and a web-axis one are not the same contrast."""
    ledger = tmp_path / "batch_arm__question.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    transport = _CorpusTransport()
    _patch_corpus_client(monkeypatch, transport)
    await WebSearchTool(corpus_endpoint="http://local:8765",
                        include_answer_box=True,
                        include_knowledge_graph=True).execute(query="q")

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["source"] == "corpus"
    assert row["answer_box_chars"] == 0 and row["knowledge_chars"] == 0
