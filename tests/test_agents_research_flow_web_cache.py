"""The host-wide web cache: a second run reads the first run's search and reader answers."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.support.benchmark_guard import page_refusal, url_refusal  # noqa: E402
from research_flow.support.web_cache import WebCache  # noqa: E402
from research_flow.tools import web  # noqa: E402
from research_flow.tools.web import (  # noqa: E402
    WebFetchTool,
    WebSearchTool,
    set_current_session,
    set_current_task,
)

PAGE = "Title: Abbey Square\n\nMarkdown Content:\n" + "Enniscorthy bronze statue. " * 40
SERP = {"organic": [{"title": "Abbey Square", "link": "https://example.org/abbey", "snippet": "statue"}]}


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, page: str = PAGE, serp: dict | None = None) -> None:
        self.page = page
        self.serp = SERP if serp is None else serp
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.host == "google.serper.dev":
            return httpx.Response(200, request=request, json=self.serp)
        return httpx.Response(200, request=request, text=self.page)


@pytest.fixture
def transport(monkeypatch):
    real = httpx.AsyncClient

    def install(t):
        def factory(**kwargs):
            kwargs.pop("proxy", None)
            kwargs.pop("transport", None)
            return real(transport=t, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        monkeypatch.setattr(web, "_RETRY_BACKOFF_S", tuple(0.001 for _ in web._RETRY_BACKOFF_S))
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port or 0))
            ],
        )
        return t

    return install


def _events(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


async def _fetch_twice(tmp_path, transport, page=PAGE):
    t = transport(_Transport(page=page))
    cache = WebCache(tmp_path, 3600)
    set_current_session("a")
    first = await WebFetchTool(api_key="k", web_cache=cache).execute("https://example.org/abbey")
    set_current_session("b")
    second = await WebFetchTool(api_key="k", web_cache=cache).execute("https://example.org/abbey")
    return t, first, second


async def test_a_second_tool_reads_the_page_from_the_cache(tmp_path, transport):
    t, first, second = await _fetch_twice(tmp_path, transport)
    assert len(t.calls) == 1
    assert json.loads(first)["text"] == json.loads(second)["text"] == PAGE
    assert [e["hit"] for e in _events(tmp_path)] == [False, True]


@pytest.mark.parametrize(
    "page",
    [
        "Title: x\n\nMarkdown Content:\nshort stub",
        "Title: x\nWarning: Target URL returned error 503: Service Unavailable\n\nMarkdown Content:\n" + "x " * 400,
        "Title: x\nWarning: This page maybe requiring CAPTCHA\n\nMarkdown Content:\n" + "x " * 400,
        "Title: x\n\nMarkdown Content:\n" + "�" * 600,
    ],
    ids=["stub", "target_error", "captcha", "encoding_lost"],
)
async def test_reads_likely_to_be_transient_are_not_stored(tmp_path, transport, page):
    t, _first, _second = await _fetch_twice(tmp_path, transport, page=page)
    reader_calls = [c for c in t.calls if "r.jina.ai" in c]
    # Every read went to the reader; the garbled page is also re-read with x-no-cache.
    assert len(reader_calls) >= 2
    assert not list(tmp_path.rglob("*.z"))


async def test_the_guard_still_refuses_a_host_on_a_hit(tmp_path, transport):
    transport(_Transport())
    cache = WebCache(tmp_path, 3600)
    url = "https://huggingface.co/datasets/x/y"
    cache.put("fetch", f"jina\n{url}", PAGE)
    out = await WebFetchTool(api_key="k", web_cache=cache, benchmark_guard=True).execute(url)
    assert out == url_refusal()


async def test_the_question_rule_still_refuses_a_cached_copy(tmp_path, transport):
    # The cache is shared across tasks; the page rule is judged per task, after the read.
    question = "Which sculptor created the bronze statue of Seamus Rafter on Abbey Square in Enniscorthy?"
    transport(_Transport())
    cache = WebCache(tmp_path, 3600)
    url = "https://example.org/copy"
    cache.put("fetch", f"jina\n{url}", f"Markdown Content:\nprompt: {question}\nanswer: X\n" + "pad " * 200)
    set_current_task(question)
    out = await WebFetchTool(api_key="k", web_cache=cache, benchmark_guard=True).execute(url)
    assert out == page_refusal()


def test_put_never_raises_on_an_unencodable_key(tmp_path):
    cache = WebCache(tmp_path, 3600)
    cache.put("search", "abc \ud800", "{}")
    cache.put("search", "ok", "x\ud800")
    assert cache.get("search", "abc \ud800") is None


async def test_an_expired_entry_is_fetched_again(tmp_path, transport):
    t = transport(_Transport())
    cache = WebCache(tmp_path, 0)
    tool = WebFetchTool(api_key="k", web_cache=cache)
    await tool.execute("https://example.org/abbey")
    await tool.execute("https://example.org/abbey")
    assert len(t.calls) == 2


async def test_search_is_served_from_the_cache_across_tools(tmp_path, transport):
    t = transport(_Transport())
    cache = WebCache(tmp_path, 3600)
    outs = []
    for session in ("a", "b"):
        set_current_session(session)
        outs.append(await WebSearchTool(api_key="k", web_cache=cache).execute("abbey square statue"))
    assert len([c for c in t.calls if "serper" in c]) == 1
    assert "https://example.org/abbey" in outs[0] and outs[0] == outs[1]


async def test_a_zero_hit_search_is_not_stored(tmp_path, transport):
    t = transport(_Transport(serp={"organic": []}))
    cache = WebCache(tmp_path, 3600)
    for session in ("a", "b"):
        set_current_session(session)
        await WebSearchTool(api_key="k", web_cache=cache).execute("nothing here")
    assert len(t.calls) == 2


async def test_the_key_is_not_part_of_the_cache_key(tmp_path, transport):
    t = transport(_Transport())
    cache = WebCache(tmp_path, 3600)
    set_current_session("a")
    await WebFetchTool(api_key="secret-one", web_cache=cache).execute("https://example.org/abbey")
    set_current_session("b")
    await WebFetchTool(api_key="secret-two", web_cache=cache).execute("https://example.org/abbey")
    assert len(t.calls) == 1
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert b"secret" not in f.read_bytes()


async def test_without_a_cache_nothing_changes(tmp_path, transport):
    t = transport(_Transport())
    tool = WebFetchTool(api_key="k")
    await tool.execute("https://example.org/abbey")
    await tool.execute("https://example.org/abbey")
    assert len(t.calls) == 2


def test_config_is_off_by_default_and_reads_camel_case():
    assert FlowConfig().web_cache.enabled is False
    cfg = FlowConfig.model_validate({"webCache": {"enabled": True, "dir": "/x", "ttlSeconds": 60, "search": False}})
    assert (cfg.web_cache.enabled, cfg.web_cache.dir, cfg.web_cache.ttl_seconds, cfg.web_cache.search) == (
        True,
        "/x",
        60,
        False,
    )
