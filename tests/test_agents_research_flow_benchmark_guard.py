"""The benchmark guard refuses published question-and-answer copies, and stays off.

Measured against 21 leaked items of one 400-item evaluation -- items answered
correctly whose first gold-bearing tool result was a published benchmark copy. Two
of those are the reason the rule is not a single substring on ``"<host>/datasets"``:
one was served by ``datasets-server.huggingface.co`` (a subdomain), one by a
``huggingface.co`` path outside ``/datasets`` (a model repository). The redirect
unwrapping is here for a third measured reason: the search provider returns
``google.com/goto?url=<target>`` rows, whose host is the wrapper's.

Default-off is part of the contract, not an implementation detail: declining to read
a dataset page is right for an evaluation and wrong for someone who asked about the
dataset.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.support.benchmark_guard import (  # noqa: E402
    blocks_query,
    blocks_url,
    query_refusal,
    unwrap_url,
    url_refusal,
)
from research_flow.tools import web  # noqa: E402
from research_flow.tools.web import WebFetchTool, WebSearchTool, set_current_session  # noqa: E402

HF_DATASET = "https://huggingface.co/datasets/org/browsecomp-copy"


class _RefusingTransport(httpx.AsyncBaseTransport):
    """Fails the test if anything reaches the network."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return httpx.Response(200, request=request, json={})


class _OrganicTransport(httpx.AsyncBaseTransport):
    """Serves one fixed SERP: a benchmark copy ranked above two ordinary pages."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return httpx.Response(
            200,
            request=request,
            json={
                "organic": [
                    {"title": "the answer key", "link": HF_DATASET + "/viewer/default/train"},
                    {"title": "Abbey Square", "link": "https://en.wikipedia.org/wiki/Abbey_Square"},
                    {"title": "Enniscorthy", "link": "https://www.isfdb.org/x"},
                ]
            },
        )


@pytest.fixture
def transport(monkeypatch):
    """Install a transport and keep DNS deterministic beside the stubbed HTTP."""

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


class TestBlocksUrl:
    def test_dataset_and_space_pages(self):
        assert blocks_url("https://huggingface.co/datasets/org/browsecomp-copy/viewer/default/train")
        assert blocks_url("https://huggingface.co/spaces/org/leaderboard")

    def test_subdomain_is_reached_by_the_host_test(self):
        # The substring "huggingface.co/datasets" never appears in this URL.
        url = "https://datasets-server.huggingface.co/search?query=10+MJ&dataset=org%2FHLE_text_200"
        assert "huggingface.co/datasets" not in url
        assert blocks_url(url)

    def test_model_repository_path_is_reached_because_no_test_reads_the_path(self):
        assert blocks_url("https://huggingface.co/someone/deepresearch/blob/main/trajectories.jsonl")

    def test_short_host_form(self):
        assert blocks_url("https://hf.co/datasets/org/copy")
        assert blocks_url("hf.co/datasets/org/copy")

    def test_redirect_wrapper_is_unwrapped_before_matching(self):
        assert blocks_url("https://www.google.com/goto?url=https://huggingface.co/datasets/org/copy")
        assert blocks_url("https://r.jina.ai/https://huggingface.co/datasets/org/copy")
        assert blocks_url("https://www.google.com/goto?url=https%3A%2F%2Fhuggingface.co%2Fdatasets%2Forg%2Fcopy")

    def test_ordinary_sources_are_untouched(self):
        for url in (
            "https://en.wikipedia.org/wiki/Vessel_(structure)",
            "https://arxiv.org/html/2508.00271v1",
            "https://www.isfdb.org/cgi-bin/pl.cgi?59674+f",
            "",
        ):
            assert not blocks_url(url)

    def test_a_lookalike_host_is_not_blocked(self):
        # BigBang-v1's bare ``in`` test blocks both of these: ``huggingface.co`` is a
        # prefix of ``huggingface.com``, and their substring has no host boundary. A
        # wrongly blocked source is invisible to the model -- it cannot be told from a
        # page that does not exist -- so the boundary is the one place this guard is
        # deliberately stricter than the rule it aligns with.
        assert not blocks_url("https://nothuggingface.com/datasets/org/copy")
        assert not blocks_url("https://huggingface.community/datasets/org/copy")


class TestBlocksQuery:
    def test_query_naming_the_host(self):
        assert blocks_query("browsecomp huggingface dataset answer key")
        assert blocks_query("Hugging Face bc-en-879 gold")
        assert blocks_query("site:hf.co browsecomp")

    def test_ordinary_query(self):
        assert not blocks_query('"Seamus Rafter" sculpture Enniscorthy Abbey Square')
        assert not blocks_query("")


class TestUnwrap:
    def test_plain_url_unchanged(self):
        assert unwrap_url("https://example.org/a?b=c") == "https://example.org/a?b=c"

    def test_bounded_depth_terminates(self):
        wrapped = "https://r.jina.ai/https://www.google.com/goto?url=https://example.org/x"
        assert unwrap_url(wrapped).startswith("https://")


class TestRefusalText:
    def test_refusals_say_why_rather_than_reading_as_a_fault(self):
        # Both BigBang-v1 and MiroFlow return a sentence here: a transport-shaped
        # failure is retried, and there is nothing to retry.
        for text in (url_refusal(), query_refusal()):
            assert "Blocked:" in text
            assert "error" not in text.lower()


class TestOffByDefault:
    def test_flow_config_default(self):
        cfg = FlowConfig()
        assert cfg.benchmark_guard.enabled is False
        assert cfg.benchmark_guard.block_queries is True

    def test_tools_default_to_inert(self):
        assert WebSearchTool(api_key="k").benchmark_guard is False
        assert WebFetchTool(api_key="k").benchmark_guard is False

    def test_tools_accept_the_flag(self):
        assert WebSearchTool(api_key="k", benchmark_guard=True).benchmark_guard is True
        assert WebFetchTool(api_key="k", benchmark_guard=True).benchmark_guard is True


class TestTheLivePath:
    """Driven through ``execute``, because the policy functions passing says nothing
    about the three call sites that use them -- and ``_log_search``'s parameters are
    keyword-only, so a drift there is silent until a run writes a row."""

    @pytest.mark.asyncio
    async def test_a_guarded_query_is_refused_before_any_request(self, monkeypatch, transport):
        t = transport(_RefusingTransport())
        set_current_session("guard-q")
        tool = WebSearchTool(api_key="k", benchmark_guard=True)
        rows: list[dict] = []
        monkeypatch.setattr(
            type(tool),
            "_log_search",
            lambda self, state, query, n, urls, rendered, shaping, **kw: rows.append(kw),
        )

        rendered = await tool.execute(query="browsecomp huggingface answer key", count=3)

        assert rendered == query_refusal()
        assert t.calls == [], "the point of the rule is that the call does not happen"
        # The row separates this from the saturation ladder's refusals, whose flag it
        # would otherwise share.
        assert len(rows) == 1
        assert rows[0]["guard_refused"] is True and rows[0]["suppressed"] is True

    @pytest.mark.asyncio
    async def test_the_same_query_is_served_with_the_guard_off(self, transport):
        """Not vacuous: without the flag the query reaches the vendor."""
        t = transport(_OrganicTransport())
        set_current_session("guard-off-q")
        tool = WebSearchTool(api_key="k")

        rendered = await tool.execute(query="browsecomp huggingface answer key", count=3)

        assert t.calls, "the control case must actually search"
        assert rendered != query_refusal()

    @pytest.mark.asyncio
    async def test_a_benchmark_row_is_dropped_and_the_next_result_takes_its_slot(self, transport):
        t = transport(_OrganicTransport())
        set_current_session("guard-serp")
        tool = WebSearchTool(api_key="k", benchmark_guard=True, benchmark_guard_queries=False)

        rendered = await tool.execute(query="Abbey Square sculpture", count=2)

        assert t.calls, "the SERP was fetched; only the row is dropped"
        assert "huggingface.co" not in rendered
        # Positive half: the slot the dropped row would have held is filled, rather
        # than the model being shown a gap where the dataset page was.
        assert "en.wikipedia.org/wiki/Abbey_Square" in rendered
        assert "isfdb.org" in rendered

    @pytest.mark.asyncio
    async def test_the_benchmark_row_is_served_with_the_guard_off(self, transport):
        transport(_OrganicTransport())
        set_current_session("guard-off-serp")
        tool = WebSearchTool(api_key="k")

        rendered = await tool.execute(query="Abbey Square sculpture", count=2)

        assert "huggingface.co" in rendered, "the control case must show what the guard removes"

    @pytest.mark.asyncio
    async def test_a_guarded_fetch_is_refused_before_the_reader_is_called(self, transport):
        t = transport(_RefusingTransport())
        set_current_session("guard-fetch")
        tool = WebFetchTool(api_key="k", benchmark_guard=True)

        rendered = await tool.execute(url=HF_DATASET)

        assert rendered == url_refusal()
        assert t.calls == []

    @pytest.mark.asyncio
    async def test_the_same_fetch_is_attempted_with_the_guard_off(self, transport):
        t = transport(_RefusingTransport())
        set_current_session("guard-off-fetch")
        tool = WebFetchTool(api_key="k")

        rendered = await tool.execute(url=HF_DATASET)

        assert t.calls, "the control case must reach the reader"
        assert rendered != url_refusal()
